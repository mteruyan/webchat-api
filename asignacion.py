"""
asignacion.py — motor de asignación de conversaciones a asesores.

Reglas:
- Un asesor "conectado" es el que tiene una fila abierta en asesor_sesiones
  (desconectado_en IS NULL) — la marca main.py al abrir/cerrar el WebSocket de /ws/asesor.
- Un asesor solo recibe conversaciones de campañas en su perfil (asesor_campanas).
- Se respeta asesores.max_conversaciones_simultaneas (parametrizable, default 5).
- Entre los candidatos válidos, se asigna al que tenga MENOS conversaciones activas
  ahora mismo (balanceo simple por carga, no round-robin estricto).
- La cola (conversaciones en estado='en_cola') se atiende en orden FIFO por
  entrada_cola dentro de cada campaña.

`manager` es la instancia de ConnectionManager de main.py — se pasa como parámetro
para evitar import circular (asignacion.py no importa main.py).
"""

from datetime import datetime, timezone, timedelta
from db import get_connection, release_connection
import supervisor

LIMA_TZ = timezone(timedelta(hours=-5))


def _historial(cur, conversacion_id: int) -> list:
    cur.execute(
        """SELECT emisor_tipo, tipo_contenido, contenido, enviado_en, leido_en FROM mensajes
           WHERE conversacion_id = %s ORDER BY enviado_en ASC""",
        (conversacion_id,),
    )
    return [
        {"emisor_tipo": e, "tipo_contenido": tc, "texto": c, "enviado_en": t.isoformat(), "leido": leido is not None}
        for e, tc, c, t, leido in cur.fetchall()
    ]


def _buscar_mejor_asesor(cur, campana_id: int):
    """Devuelve (asesor_id, nombre_display) del mejor candidato conectado, con
    cupo y que NO esté en pausa, o None."""
    cur.execute(
        """
        SELECT a.id, a.nombre_display, a.max_conversaciones_simultaneas,
               COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas
        FROM asesores a
        JOIN asesor_campanas ac ON ac.asesor_id = a.id AND ac.campana_id = %s
        JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
        LEFT JOIN conversaciones c ON c.asesor_id = a.id
        WHERE a.activo = TRUE AND a.en_pausa = FALSE
        GROUP BY a.id, a.nombre_display, a.max_conversaciones_simultaneas
        HAVING COUNT(c.id) FILTER (WHERE c.estado = 'asignada') < a.max_conversaciones_simultaneas
        ORDER BY activas ASC
        LIMIT 1
        """,
        (campana_id,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _asignar(cur, conversacion_id: int, asesor_id: int):
    cur.execute(
        "UPDATE conversaciones SET estado = 'asignada', asesor_id = %s, asignado_en = %s WHERE id = %s",
        (asesor_id, datetime.now(LIMA_TZ), conversacion_id),
    )


async def intentar_asignar_conversacion(conversacion_id: int, manager) -> dict | None:
    """Se llama justo después de que el bot deriva una conversación (evento 'en_cola').
    Si hay asesor disponible, la asigna de inmediato y notifica por WS a ambos lados.
    Devuelve {"asesor_id":..., "nombre_display":...} si asignó, o None si quedó en cola."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            campana_id = cur.fetchone()[0]

            asesor = _buscar_mejor_asesor(cur, campana_id)
            if asesor is None:
                return None

            asesor_id, nombre_display = asesor
            _asignar(cur, conversacion_id, asesor_id)

            texto_bienvenida = supervisor.obtener_configuracion("mensaje_bienvenida_asesor")
            if texto_bienvenida:
                texto_bienvenida = texto_bienvenida.replace("{{ASESOR_NOMBRE}}", nombre_display)
                cur.execute(
                    """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                       VALUES (%s, 'bot', 'texto', %s, %s)""",
                    (conversacion_id, texto_bienvenida, datetime.now(LIMA_TZ)),
                )

            cur.execute(
                """SELECT u.nombre, u.telefono, c.entrada_cola, c.asignado_en FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                (conversacion_id,),
            )
            usuario_nombre, usuario_telefono, entrada_cola, asignado_en = cur.fetchone()
            historial = _historial(cur, conversacion_id)
        conn.commit()
    finally:
        release_connection(conn)

    await manager.enviar_a_widget(conversacion_id, {
        "type": "asignado", "payload": {"asesor_nombre": nombre_display},
    })
    if texto_bienvenida:
        await manager.enviar_a_widget(conversacion_id, {
            "type": "mensaje_bot", "payload": {"texto": texto_bienvenida},
        })
    await manager.enviar_a_asesor(asesor_id, {
        "type": "nueva_asignacion",
        "payload": {"conversacion_id": conversacion_id, "resumen_usuario": usuario_nombre,
                    "telefono": usuario_telefono, "campana": campana_id, "historial": historial,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None},
    })
    await supervisor.notificar_cambio_asesor(asesor_id, manager)
    return {"asesor_id": asesor_id, "nombre_display": nombre_display}


async def asignar_desde_cola_para_asesor(asesor_id: int, manager):
    """Se llama cuando un asesor se conecta o libera cupo (cierra una conversación).
    Va tomando de la cola, en FIFO por campaña, hasta llenar su cupo o vaciar la cola.
    No toma nada si el asesor está en pausa."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_conversaciones_simultaneas, nombre_display, en_pausa FROM asesores WHERE id = %s",
                (asesor_id,),
            )
            cupo_max, nombre_display, en_pausa = cur.fetchone()
            if en_pausa:
                return

            cur.execute(
                "SELECT COUNT(*) FROM conversaciones WHERE asesor_id = %s AND estado = 'asignada'",
                (asesor_id,),
            )
            activas = cur.fetchone()[0]
            cupo_libre = cupo_max - activas
            if cupo_libre <= 0:
                return

            cur.execute(
                """SELECT c.id, c.campana_id FROM conversaciones c
                   JOIN asesor_campanas ac ON ac.campana_id = c.campana_id AND ac.asesor_id = %s
                   WHERE c.estado = 'en_cola'
                   ORDER BY c.entrada_cola ASC
                   LIMIT %s""",
                (asesor_id, cupo_libre),
            )
            candidatas = cur.fetchall()

            texto_bienvenida = supervisor.obtener_configuracion("mensaje_bienvenida_asesor")
            if texto_bienvenida:
                texto_bienvenida = texto_bienvenida.replace("{{ASESOR_NOMBRE}}", nombre_display)

            asignadas = []
            for conversacion_id, campana_id in candidatas:
                _asignar(cur, conversacion_id, asesor_id)
                if texto_bienvenida:
                    cur.execute(
                        """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                           VALUES (%s, 'bot', 'texto', %s, %s)""",
                        (conversacion_id, texto_bienvenida, datetime.now(LIMA_TZ)),
                    )
                cur.execute(
                    """SELECT u.nombre, u.telefono, c.entrada_cola, c.asignado_en FROM conversaciones c
                       JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                    (conversacion_id,),
                )
                usuario_nombre, usuario_telefono, entrada_cola, asignado_en = cur.fetchone()
                historial = _historial(cur, conversacion_id)
                asignadas.append((conversacion_id, campana_id, usuario_nombre, usuario_telefono,
                                   entrada_cola, asignado_en, historial))
        conn.commit()
    finally:
        release_connection(conn)

    for (conversacion_id, campana_id, usuario_nombre, usuario_telefono,
         entrada_cola, asignado_en, historial) in asignadas:
        await manager.enviar_a_widget(conversacion_id, {
            "type": "asignado", "payload": {"asesor_nombre": nombre_display},
        })
        if texto_bienvenida:
            await manager.enviar_a_widget(conversacion_id, {
                "type": "mensaje_bot", "payload": {"texto": texto_bienvenida},
            })
        await manager.enviar_a_asesor(asesor_id, {
            "type": "nueva_asignacion",
            "payload": {"conversacion_id": conversacion_id, "resumen_usuario": usuario_nombre,
                        "telefono": usuario_telefono, "campana": campana_id, "historial": historial,
                        "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                        "asignado_en": asignado_en.isoformat() if asignado_en else None},
        })
    if asignadas:
        await supervisor.notificar_cambio_asesor(asesor_id, manager)
