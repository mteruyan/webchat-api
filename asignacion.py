"""
asignacion.py — motor de asignación de conversaciones a asesores.

Reglas:
- Un asesor "conectado" es el que tiene una fila abierta en asesor_sesiones
  (desconectado_en IS NULL) — la marca main.py al abrir/cerrar el WebSocket de /ws/asesor.
- Un asesor solo recibe conversaciones de campañas en su perfil (asesor_campanas).
- Se respeta asesores.max_conversaciones_simultaneas (parametrizable, default 5).
- Entre los candidatos válidos, se asigna al que tenga MENOS conversaciones activas
  ahora mismo; en caso de empate, al que lleve menos asignadas en el día (Lima); si
  persiste, al que lleve más tiempo sin recibir una.
- La cola (conversaciones en estado='en_cola') se atiende en orden FIFO por
  entrada_cola dentro de cada campaña.

`manager` es la instancia de ConnectionManager de main.py — se pasa como parámetro
para evitar import circular (asignacion.py no importa main.py).
"""

import json
from datetime import datetime, timezone, timedelta
from db import get_connection, release_connection
import supervisor

LIMA_TZ = timezone(timedelta(hours=-5))


def _reacciones_de(metadata) -> dict:
    """{emoji: cantidad} desde la metadata del mensaje (dict o JSON en texto)."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    resumen = {}
    for emoji in ((metadata or {}).get("reacciones") or {}).values():
        resumen[emoji] = resumen.get(emoji, 0) + 1
    return resumen


def _historial(cur, conversacion_id: int) -> list:
    cur.execute(
        """SELECT id, emisor_tipo, tipo_contenido, contenido, enviado_en, leido_en, metadata FROM mensajes
           WHERE conversacion_id = %s ORDER BY enviado_en ASC""",
        (conversacion_id,),
    )
    return [
        {"id": mid, "emisor_tipo": e, "tipo_contenido": tc, "texto": c, "enviado_en": t.isoformat(),
         "leido": leido is not None, "reacciones": _reacciones_de(meta)}
        for mid, e, tc, c, t, leido, meta in cur.fetchall()
    ]


def _buscar_mejor_asesor(cur, campana_id: int):
    """Devuelve (asesor_id, nombre_display) del mejor candidato conectado, con
    cupo y que NO esté en pausa, o None."""
    cur.execute(
        """
        SELECT a.id, a.nombre_display, a.max_conversaciones_simultaneas,
               COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas,
               COUNT(c.id) FILTER (WHERE c.asignado_en >= date_trunc('day', NOW() AT TIME ZONE 'America/Lima')
                                                          AT TIME ZONE 'America/Lima') AS asignadas_hoy,
               MAX(c.asignado_en) AS ultima_asignacion
        FROM asesores a
        JOIN asesor_campanas ac ON ac.asesor_id = a.id AND ac.campana_id = %s
        JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
        LEFT JOIN conversaciones c ON c.asesor_id = a.id
        WHERE a.activo = TRUE AND a.en_pausa = FALSE
        GROUP BY a.id, a.nombre_display, a.max_conversaciones_simultaneas
        HAVING COUNT(c.id) FILTER (WHERE c.estado = 'asignada') < a.max_conversaciones_simultaneas
        -- 1) menos activas ahora; 2) empate: menos asignadas hoy; 3) empate: quien lleva
        -- más tiempo sin recibir una (NULLS FIRST = nunca recibió); 4) id, para que sea estable.
        ORDER BY activas ASC, asignadas_hoy ASC, ultima_asignacion ASC NULLS FIRST, a.id ASC
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


async def transferir_conversacion(conversacion_id: int, asesor_destino_id: int, manager,
                                   asesor_solicitante_id: int = None) -> dict:
    """Pasa una conversación YA asignada de un asesor a otro que esté conectado
    y con cupo libre. Se usa tanto desde la consola de asesor (transfiere una
    de las suyas — asesor_solicitante_id valida que sea dueño) como desde la
    de supervisor (puede transferir cualquiera, sin esa validación).
    Devuelve {"ok": True, ...} o {"ok": False, "motivo": "..."}."""
    if not conversacion_id or not asesor_destino_id:
        return {"ok": False, "motivo": "Faltan datos para transferir."}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asesor_id, estado, campana_id FROM conversaciones WHERE id = %s",
                (conversacion_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"ok": False, "motivo": "La conversación no existe."}
            asesor_origen_id, estado, campana_id = row
            if estado != "asignada":
                return {"ok": False, "motivo": "La conversación ya no está asignada a un asesor."}
            if asesor_solicitante_id is not None and asesor_origen_id != asesor_solicitante_id:
                return {"ok": False, "motivo": "Esta conversación ya no está a tu cargo."}
            if asesor_destino_id == asesor_origen_id:
                return {"ok": False, "motivo": "Ya está asignada a ese asesor."}

            cur.execute(
                """SELECT a.nombre_display, a.max_conversaciones_simultaneas, a.en_pausa,
                          (s.asesor_id IS NOT NULL) AS conectado,
                          COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas
                   FROM asesores a
                   LEFT JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
                   LEFT JOIN conversaciones c ON c.asesor_id = a.id
                   WHERE a.id = %s AND a.activo = TRUE
                   GROUP BY a.id, a.nombre_display, a.max_conversaciones_simultaneas, a.en_pausa, s.asesor_id""",
                (asesor_destino_id,),
            )
            destino = cur.fetchone()
            if destino is None:
                return {"ok": False, "motivo": "El asesor destino no existe o está inactivo."}
            nombre_destino, cupo_max, en_pausa_destino, conectado_destino, activas_destino = destino
            if not conectado_destino:
                return {"ok": False, "motivo": f"{nombre_destino} no está conectado en este momento."}
            if en_pausa_destino:
                return {"ok": False, "motivo": f"{nombre_destino} está en pausa."}
            if activas_destino >= cupo_max:
                return {"ok": False, "motivo": f"{nombre_destino} no tiene cupo disponible."}

            nombre_origen = None
            if asesor_origen_id:
                cur.execute("SELECT nombre_display FROM asesores WHERE id = %s", (asesor_origen_id,))
                r = cur.fetchone()
                nombre_origen = r[0] if r else None

            _asignar(cur, conversacion_id, asesor_destino_id)

            texto_sistema = (
                f"🔀 Conversación transferida de {nombre_origen} a {nombre_destino}."
                if nombre_origen else f"🔀 Conversación transferida a {nombre_destino}."
            )
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'bot', 'texto', %s, %s)""",
                (conversacion_id, texto_sistema, datetime.now(LIMA_TZ)),
            )

            cur.execute(
                """SELECT COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en, c.bandera, c.aviso_inactividad_activo FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                (conversacion_id,),
            )
            usuario_nombre, usuario_telefono, usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo = cur.fetchone()
            historial = _historial(cur, conversacion_id)
        conn.commit()
    finally:
        release_connection(conn)

    if asesor_origen_id:
        await manager.enviar_a_asesor(asesor_origen_id, {
            "type": "conversacion_removida",
            "payload": {"conversacion_id": conversacion_id, "motivo": "transferida", "nombre_destino": nombre_destino},
        })
    await manager.enviar_a_asesor(asesor_destino_id, {
        "type": "nueva_asignacion",
        "payload": {"conversacion_id": conversacion_id, "resumen_usuario": usuario_nombre,
                    "telefono": usuario_telefono, "dni": usuario_dni, "campana": campana_id, "historial": historial,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "bandera": bandera, "aviso_inactividad_activo": aviso_inactividad_activo},
    })
    await manager.enviar_a_widget(conversacion_id, {
        "type": "asignado", "payload": {"asesor_nombre": nombre_destino},
    })
    await manager.notificar_escuchas(conversacion_id, {
        "type": "mensaje_conversacion",
        "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "bot", "texto": texto_sistema},
    })
    if asesor_origen_id:
        await supervisor.notificar_cambio_asesor(asesor_origen_id, manager)
    await supervisor.notificar_cambio_asesor(asesor_destino_id, manager)

    return {"ok": True, "asesor_origen_id": asesor_origen_id, "asesor_destino_id": asesor_destino_id,
            "nombre_destino": nombre_destino}


async def intentar_asignar_conversacion(conversacion_id: int, manager) -> dict | None:
    """Se llama justo después de que el bot deriva una conversación (evento 'en_cola').
    Si hay asesor disponible, la asigna de inmediato y notifica por WS a ambos lados.
    Devuelve {"asesor_id":..., "nombre_display":...} si asignó, o None si quedó en cola."""
    # La conexión se devuelve ANTES de cualquier await: retenerla mientras se notifica a los supervisores
    # (que a su vez piden conexiones) podía agotar el pool y frenar todo el servidor.
    sin_asesor = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            campana_id = cur.fetchone()[0]

            asesor = _buscar_mejor_asesor(cur, campana_id)
            if asesor is None:
                sin_asesor = True
            else:
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
                    """SELECT COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en, c.bandera, c.aviso_inactividad_activo FROM conversaciones c
                       JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                    (conversacion_id,),
                )
                usuario_nombre, usuario_telefono, usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo = cur.fetchone()
                historial = _historial(cur, conversacion_id)
        conn.commit()
    finally:
        release_connection(conn)

    if sin_asesor:
        await supervisor.notificar_estado_global_a_todos(manager)  # entró a la cola, sin nadie disponible
        return None

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
                    "telefono": usuario_telefono, "dni": usuario_dni, "campana": campana_id, "historial": historial,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "bandera": bandera, "aviso_inactividad_activo": aviso_inactividad_activo},
    })
    await supervisor.notificar_cambio_asesor(asesor_id, manager)
    await supervisor.notificar_estado_global_a_todos(manager)  # salió de la cola
    return {"asesor_id": asesor_id, "nombre_display": nombre_display}


async def asignar_manual(conversacion_id: int, asesor_id: int, manager) -> dict:
    """El supervisor elige a mano un asesor para una conversación que está
    'en_cola' (nadie de esa campaña estaba conectado cuando el bot la derivó).
    Reutiliza las mismas validaciones de cupo/pausa/conexión que transferir_conversacion.
    Devuelve {"ok": True} o {"ok": False, "motivo": "..."}."""
    if not conversacion_id or not asesor_id:
        return {"ok": False, "motivo": "Faltan datos para asignar."}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT estado, campana_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            row = cur.fetchone()
            if row is None:
                return {"ok": False, "motivo": "La conversación no existe."}
            estado, campana_id = row
            if estado != "en_cola":
                return {"ok": False, "motivo": "Esta conversación ya no está esperando en cola."}

            cur.execute(
                """SELECT a.nombre_display, a.max_conversaciones_simultaneas, a.en_pausa,
                          (s.asesor_id IS NOT NULL) AS conectado,
                          COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas
                   FROM asesores a
                   LEFT JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
                   LEFT JOIN conversaciones c ON c.asesor_id = a.id
                   WHERE a.id = %s AND a.activo = TRUE
                   GROUP BY a.id, a.nombre_display, a.max_conversaciones_simultaneas, a.en_pausa, s.asesor_id""",
                (asesor_id,),
            )
            destino = cur.fetchone()
            if destino is None:
                return {"ok": False, "motivo": "El asesor no existe o está inactivo."}
            nombre_display, cupo_max, en_pausa, conectado, activas = destino
            if not conectado:
                return {"ok": False, "motivo": f"{nombre_display} no está conectado en este momento."}
            if en_pausa:
                return {"ok": False, "motivo": f"{nombre_display} está en pausa."}
            if activas >= cupo_max:
                return {"ok": False, "motivo": f"{nombre_display} no tiene cupo disponible."}

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
                """SELECT COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en, c.bandera, c.aviso_inactividad_activo FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                (conversacion_id,),
            )
            usuario_nombre, usuario_telefono, usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo = cur.fetchone()
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
                    "telefono": usuario_telefono, "dni": usuario_dni, "campana": campana_id, "historial": historial,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "bandera": bandera, "aviso_inactividad_activo": aviso_inactividad_activo},
    })
    await supervisor.notificar_cambio_asesor(asesor_id, manager)
    await supervisor.notificar_estado_global_a_todos(manager)  # salió de la cola
    return {"ok": True}


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
                    """SELECT COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en, c.bandera, c.aviso_inactividad_activo FROM conversaciones c
                       JOIN usuarios_webchat u ON u.id = c.usuario_id WHERE c.id = %s""",
                    (conversacion_id,),
                )
                usuario_nombre, usuario_telefono, usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo = cur.fetchone()
                historial = _historial(cur, conversacion_id)
                asignadas.append((conversacion_id, campana_id, usuario_nombre, usuario_telefono,
                                   usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo, historial))
        conn.commit()
    finally:
        release_connection(conn)

    for (conversacion_id, campana_id, usuario_nombre, usuario_telefono,
         usuario_dni, entrada_cola, asignado_en, bandera, aviso_inactividad_activo, historial) in asignadas:
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
                        "telefono": usuario_telefono, "dni": usuario_dni, "campana": campana_id, "historial": historial,
                        "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                        "asignado_en": asignado_en.isoformat() if asignado_en else None,
                        "bandera": bandera, "aviso_inactividad_activo": aviso_inactividad_activo},
        })
    if asignadas:
        await supervisor.notificar_cambio_asesor(asesor_id, manager)
        await supervisor.notificar_estado_global_a_todos(manager)  # se vació total o parcialmente la cola
