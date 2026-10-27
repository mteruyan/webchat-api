"""
supervisor.py — snapshot de estado global de asesores, estadísticas del día,
etiquetas de cierre, y notificaciones para la consola de supervisor.
"""

from datetime import datetime, timezone, timedelta
from db import get_connection, release_connection
import auth

LIMA_TZ = timezone(timedelta(hours=-5))

_COLUMNAS = ["asesor_id", "nombre_display", "conectado", "en_pausa",
             "conversaciones_activas", "cupo_maximo", "campanas"]


def _fila_a_dict(row):
    return dict(zip(_COLUMNAS, row))


def _inicio_de_hoy() -> datetime:
    ahora = datetime.now(LIMA_TZ)
    return ahora.replace(hour=0, minute=0, second=0, microsecond=0)


def _conversaciones_activas_por_asesor(cur, asesor_ids=None):
    """Devuelve {asesor_id: [{"conversacion_id", "usuario_nombre", "telefono",
    "entrada_cola", "asignado_en", "ultimo_mensaje_texto", "ultimo_mensaje_tipo",
    "ultimo_mensaje_en"}, ...]} — para que el tablero de supervisor pueda ofrecer
    un botón de escuchar por conversación, sin que nadie tenga que escribir un
    conversacion_id a mano, mostrar tiempos de espera/atención, y adelantar
    (como en el inbox del asesor) la hora y el texto del último mensaje."""
    if asesor_ids is not None and not asesor_ids:
        return {}
    query = """
        SELECT c.asesor_id, c.id, COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en,
               c.pausada_en, ult.contenido, ult.tipo_contenido, ult.enviado_en, c.bandera, c.campana_id
        FROM conversaciones c
        JOIN usuarios_webchat u ON u.id = c.usuario_id
        LEFT JOIN LATERAL (
            SELECT contenido, tipo_contenido, enviado_en
            FROM mensajes m
            WHERE m.conversacion_id = c.id
            ORDER BY m.enviado_en DESC
            LIMIT 1
        ) ult ON TRUE
        WHERE c.estado = 'asignada'
    """
    params = ()
    if asesor_ids is not None:
        query += " AND c.asesor_id = ANY(%s)"
        params = (asesor_ids,)
    cur.execute(query, params)
    resultado = {}
    for (asesor_id, conversacion_id, nombre, telefono, dni, entrada_cola, asignado_en,
         pausada_en, ultimo_contenido, ultimo_tipo, ultimo_en, bandera, campana_id) in cur.fetchall():
        resultado.setdefault(asesor_id, []).append({
            "conversacion_id": conversacion_id,
            "usuario_nombre": nombre or f"Usuario {conversacion_id}",
            "telefono": telefono,
            "dni": dni,
            "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
            "asignado_en": asignado_en.isoformat() if asignado_en else None,
            "pausada": pausada_en is not None,
            "bandera": bandera,
            "campana_id": campana_id,
            "ultimo_mensaje_texto": ultimo_contenido,
            "ultimo_mensaje_tipo": ultimo_tipo,
            "ultimo_mensaje_en": ultimo_en.isoformat() if ultimo_en else None,
        })
    return resultado


def _estadisticas_dia_por_asesor(cur, asesor_ids=None):
    """Devuelve {asesor_id: {"segundos_conectado", "segundos_pausado", "atendidas_hoy"}}
    — todo acotado a lo transcurrido HOY (hora de Lima), incluso si una sesión
    empezó ayer (se cuenta solo la porción de hoy)."""
    if asesor_ids is not None and not asesor_ids:
        return {}
    inicio = _inicio_de_hoy()

    filtro_asesor = "AND asesor_id = ANY(%(ids)s)" if asesor_ids is not None else ""
    params = {"inicio": inicio}
    if asesor_ids is not None:
        params["ids"] = asesor_ids

    resultado = {}

    cur.execute(
        f"""
        SELECT asesor_id, COALESCE(SUM(EXTRACT(EPOCH FROM
                 (COALESCE(desconectado_en, now()) - GREATEST(conectado_en, %(inicio)s)))), 0)
        FROM asesor_sesiones
        WHERE COALESCE(desconectado_en, now()) >= %(inicio)s {filtro_asesor}
        GROUP BY asesor_id
        """,
        params,
    )
    for asesor_id, segundos in cur.fetchall():
        resultado.setdefault(asesor_id, {})["segundos_conectado"] = int(segundos)

    cur.execute(
        f"""
        SELECT asesor_id, COALESCE(SUM(EXTRACT(EPOCH FROM
                 (COALESCE(fin, now()) - GREATEST(inicio, %(inicio)s)))), 0)
        FROM asesor_pausas
        WHERE COALESCE(fin, now()) >= %(inicio)s {filtro_asesor}
        GROUP BY asesor_id
        """,
        params,
    )
    for asesor_id, segundos in cur.fetchall():
        resultado.setdefault(asesor_id, {})["segundos_pausado"] = int(segundos)

    cur.execute(
        f"""
        SELECT asesor_id, COUNT(*) FROM conversaciones
        WHERE estado = 'cerrada' AND cerrado_en >= %(inicio)s {filtro_asesor}
        GROUP BY asesor_id
        """,
        params,
    )
    for asesor_id, cantidad in cur.fetchall():
        resultado.setdefault(asesor_id, {})["atendidas_hoy"] = cantidad

    return resultado


def _motivos_pausa_abiertos(cur, asesor_ids):
    """{asesor_id: (motivo, detalle)} de la pausa en curso de cada asesor.
    Si la migración del motivo aún no se corrió, devuelve {} sin romper el tablero."""
    if not asesor_ids:
        return {}
    try:
        cur.execute("SAVEPOINT sp_motivos")
        cur.execute(
            """SELECT asesor_id, motivo, motivo_detalle FROM asesor_pausas
               WHERE fin IS NULL AND asesor_id = ANY(%s)""",
            (asesor_ids,),
        )
        return {a: (m, d) for a, m, d in cur.fetchall()}
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT sp_motivos")
        return {}


def _completar_motivo_pausa(d: dict, motivos: dict):
    m, det = motivos.get(d["asesor_id"], (None, None))
    d["motivo_pausa"] = m if d.get("en_pausa") else None
    d["motivo_pausa_detalle"] = det if d.get("en_pausa") else None


def _completar_stats(d: dict, stats: dict):
    e = stats.get(d["asesor_id"], {})
    d["segundos_conectado_hoy"] = e.get("segundos_conectado", 0)
    d["segundos_pausado_hoy"] = e.get("segundos_pausado", 0)
    d["atendidas_hoy"] = e.get("atendidas_hoy", 0)


def _conversaciones_en_cola(cur):
    """Conversaciones derivadas a un asesor que TODAVÍA no se les asignó nadie
    (estado='en_cola') — para el panel de supervisor "Sin asignar", que suele
    pasar cuando ningún asesor de esa campaña está conectado en ese momento."""
    cur.execute(
        """
        SELECT c.id, COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO'), COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR'), COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI'), camp.nombre, camp.codigo, c.entrada_cola
        FROM conversaciones c
        JOIN usuarios_webchat u ON u.id = c.usuario_id
        LEFT JOIN campanas camp ON camp.id = c.campana_id
        WHERE c.estado = 'en_cola'
        ORDER BY c.entrada_cola ASC
        """
    )
    resultado = []
    for conv_id, nombre, telefono, dni, campana_nombre, campana_codigo, entrada_cola in cur.fetchall():
        resultado.append({
            "conversacion_id": conv_id,
            "usuario_nombre": nombre or f"Usuario {conv_id}",
            "telefono": telefono,
            "dni": dni,
            "campana_nombre": campana_nombre,
            "campana_codigo": campana_codigo,
            "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
        })
    return resultado


def _snapshot(cur):
    cur.execute(
        """
        SELECT a.id, a.nombre_display,
               (s.asesor_id IS NOT NULL) AS conectado,
               a.en_pausa,
               COUNT(DISTINCT c.id) FILTER (WHERE c.estado = 'asignada') AS activas,
               a.max_conversaciones_simultaneas,
               COALESCE(array_agg(DISTINCT camp.codigo) FILTER (WHERE camp.codigo IS NOT NULL), '{}') AS campanas
        FROM asesores a
        LEFT JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
        LEFT JOIN conversaciones c ON c.asesor_id = a.id
        LEFT JOIN asesor_campanas ac ON ac.asesor_id = a.id
        LEFT JOIN campanas camp ON camp.id = ac.campana_id
        WHERE a.rol = 'asesor' AND a.activo = TRUE
        GROUP BY a.id, a.nombre_display, s.asesor_id, a.en_pausa, a.max_conversaciones_simultaneas
        ORDER BY a.nombre_display
        """
    )
    filas = [_fila_a_dict(row) for row in cur.fetchall()]
    ids = [f["asesor_id"] for f in filas]
    conv_map = _conversaciones_activas_por_asesor(cur, ids)
    stats_map = _estadisticas_dia_por_asesor(cur, ids)
    motivos = _motivos_pausa_abiertos(cur, ids)
    for f in filas:
        f["conversaciones"] = conv_map.get(f["asesor_id"], [])
        _completar_stats(f, stats_map)
        _completar_motivo_pausa(f, motivos)
    return filas


def _snapshot_uno(cur, asesor_id: int):
    cur.execute(
        """
        SELECT a.id, a.nombre_display,
               (s.asesor_id IS NOT NULL) AS conectado,
               a.en_pausa,
               COUNT(DISTINCT c.id) FILTER (WHERE c.estado = 'asignada') AS activas,
               a.max_conversaciones_simultaneas,
               COALESCE(array_agg(DISTINCT camp.codigo) FILTER (WHERE camp.codigo IS NOT NULL), '{}') AS campanas
        FROM asesores a
        LEFT JOIN asesor_sesiones s ON s.asesor_id = a.id AND s.desconectado_en IS NULL
        LEFT JOIN conversaciones c ON c.asesor_id = a.id
        LEFT JOIN asesor_campanas ac ON ac.asesor_id = a.id
        LEFT JOIN campanas camp ON camp.id = ac.campana_id
        WHERE a.id = %s
        GROUP BY a.id, a.nombre_display, s.asesor_id, a.en_pausa, a.max_conversaciones_simultaneas
        """,
        (asesor_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    data = _fila_a_dict(row)
    conv_map = _conversaciones_activas_por_asesor(cur, [asesor_id])
    stats_map = _estadisticas_dia_por_asesor(cur, [asesor_id])
    data["conversaciones"] = conv_map.get(asesor_id, [])
    _completar_stats(data, stats_map)
    _completar_motivo_pausa(data, _motivos_pausa_abiertos(cur, [asesor_id]))
    return data


async def enviar_estado_global(supervisor_id: int, manager):
    """Snapshot completo — se manda una vez, al conectar la consola de supervisor."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            data = _snapshot(cur)
            en_cola = _conversaciones_en_cola(cur)
    finally:
        release_connection(conn)
    await manager.enviar_a_supervisor(supervisor_id, {"type": "estado_global", "payload": {"asesores": data, "en_cola": en_cola}})


async def notificar_cambio_asesor(asesor_id: int, manager):
    """Se llama cada vez que cambia algo de un asesor puntual (conecta, desconecta,
    se pausa/reanuda, recibe o cierra una conversación) — actualiza a TODOS los
    supervisores conectados, no solo a uno."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            data = _snapshot_uno(cur, asesor_id)
    finally:
        release_connection(conn)
    if data:
        await manager.enviar_a_todos_supervisores({"type": "asesor_estado_cambio", "payload": data})


async def enviar_mis_estadisticas(asesor_id: int, manager):
    """Tiempo conectado/en pausa de HOY del propio asesor -- para que su
    barra superior muestre lo mismo que el supervisor ya ve de él (ver
    _estadisticas_dia_por_asesor). Se manda al conectar y cada vez que
    cambia su disponibilidad (pausar/reanudar) o cierra una conversación.
    Incluye las "atendidas hoy" (conversaciones cerradas hoy, igual que en el
    tablero de supervisor) y el estado real de pausa guardado en la base, para
    que la pantalla nunca quede mostrando algo distinto a lo que el servidor
    usa al asignar conversaciones."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            stats = _estadisticas_dia_por_asesor(cur, [asesor_id]).get(asesor_id, {})
            cur.execute("SELECT en_pausa FROM asesores WHERE id = %s", (asesor_id,))
            fila = cur.fetchone()
            en_pausa = bool(fila[0]) if fila else False
            motivo, detalle = _motivos_pausa_abiertos(cur, [asesor_id]).get(asesor_id, (None, None))
    finally:
        release_connection(conn)
    await manager.enviar_a_asesor(asesor_id, {
        "type": "mis_estadisticas",
        "payload": {
            "segundos_conectado_hoy": stats.get("segundos_conectado", 0),
            "segundos_pausado_hoy": stats.get("segundos_pausado", 0),
            "atendidas_hoy": stats.get("atendidas_hoy", 0),
            "en_pausa": en_pausa,
            "motivo_pausa": motivo if en_pausa else None,
            "motivo_pausa_detalle": detalle if en_pausa else None,
        },
    })


async def enviar_lista_asesores(destinatario_id: int, manager, a_asesor: bool = False, excluir_id: int = None):
    """Lista liviana de asesores (id, nombre, conectado, en_pausa, carga,
    campañas) para el selector de "Transferir a..." — tanto desde la consola
    de asesor como desde la de supervisor. No incluye el detalle de
    conversaciones de cada uno (eso ya lo tiene el tablero de supervisor vía
    estado_global)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            filas = _snapshot(cur)
    finally:
        release_connection(conn)
    asesores = [
        {
            "asesor_id": f["asesor_id"],
            "nombre_display": f["nombre_display"],
            "conectado": f["conectado"],
            "en_pausa": f["en_pausa"],
            "conversaciones_activas": f["conversaciones_activas"],
            "cupo_maximo": f["cupo_maximo"],
            "campanas": f["campanas"],
        }
        for f in filas
        if excluir_id is None or f["asesor_id"] != excluir_id
    ]
    evento = {"type": "lista_asesores", "payload": {"asesores": asesores}}
    if a_asesor:
        await manager.enviar_a_asesor(destinatario_id, evento)
    else:
        await manager.enviar_a_supervisor(destinatario_id, evento)


# ---------------------------------------------------------------------
# Etiquetas de cierre — mantenimiento desde la vista de supervisor
# ---------------------------------------------------------------------

def _obtener_etiquetas(cur, solo_activas: bool = False):
    """campana_ids vacío = la etiqueta vale para TODAS las campañas."""
    query = """SELECT e.id, e.nombre, e.activo,
                      COALESCE(array_agg(ec.campana_id ORDER BY ec.campana_id)
                               FILTER (WHERE ec.campana_id IS NOT NULL), '{}') AS campana_ids
               FROM etiquetas_cierre e
               LEFT JOIN etiqueta_campanas ec ON ec.etiqueta_id = e.id"""
    if solo_activas:
        query += " WHERE e.activo = TRUE"
    query += " GROUP BY e.id, e.nombre, e.activo ORDER BY e.nombre"
    try:
        cur.execute(query)
        return [{"id": i, "nombre": n, "activo": a, "campana_ids": list(c)} for i, n, a, c in cur.fetchall()]
    except Exception:
        # Aún no existe la tabla etiqueta_campanas (migración sin correr): todas valen para todas.
        cur.connection.rollback()
        q2 = "SELECT id, nombre, activo FROM etiquetas_cierre"
        if solo_activas:
            q2 += " WHERE activo = TRUE"
        q2 += " ORDER BY nombre"
        cur.execute(q2)
        return [{"id": i, "nombre": n, "activo": a, "campana_ids": []} for i, n, a in cur.fetchall()]


async def enviar_etiquetas(destinatario_id: int, manager, a_asesor: bool = False):
    """a_asesor=True manda solo las activas (para elegir al cerrar una conversación);
    a_asesor=False (supervisor) manda todas, incluidas las desactivadas, para poder
    reactivarlas."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            etiquetas = _obtener_etiquetas(cur, solo_activas=a_asesor)
    finally:
        release_connection(conn)
    evento = {"type": "etiquetas_disponibles", "payload": {"etiquetas": etiquetas}}
    if not a_asesor:
        evento["payload"]["campanas"] = obtener_campanas()
    if a_asesor:
        await manager.enviar_a_asesor(destinatario_id, evento)
    else:
        await manager.enviar_a_supervisor(destinatario_id, evento)


async def notificar_etiquetas_a_todos(manager):
    """Tras crear/editar/eliminar una etiqueta, actualiza a todos los supervisores
    Y a todos los asesores conectados (para que su selector de cierre quede al día)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            todas = _obtener_etiquetas(cur, solo_activas=False)
            activas = _obtener_etiquetas(cur, solo_activas=True)
    finally:
        release_connection(conn)
    await manager.enviar_a_todos_supervisores({"type": "etiquetas_disponibles",
                                               "payload": {"etiquetas": todas, "campanas": obtener_campanas()}})
    await manager.enviar_a_todos_asesores({"type": "etiquetas_disponibles", "payload": {"etiquetas": activas}})


def _guardar_campanas_etiqueta(cur, etiqueta_id: int, campana_ids):
    """Reemplaza las campañas de la etiqueta (lista vacía = todas las campañas)."""
    ids = sorted({int(c) for c in (campana_ids or [])})
    cur.execute("DELETE FROM etiqueta_campanas WHERE etiqueta_id = %s", (etiqueta_id,))
    for campana_id in ids:
        cur.execute(
            "INSERT INTO etiqueta_campanas (etiqueta_id, campana_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (etiqueta_id, campana_id),
        )


def crear_etiqueta(nombre: str, campana_ids=None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO etiquetas_cierre (nombre) VALUES (%s) RETURNING id", (nombre,))
            etiqueta_id = cur.fetchone()[0]
            if campana_ids:
                _guardar_campanas_etiqueta(cur, etiqueta_id, campana_ids)
        conn.commit()
    finally:
        release_connection(conn)


def editar_etiqueta(etiqueta_id: int, nombre: str = None, activo: bool = None, campana_ids=None):
    """campana_ids=None no toca las campañas; una lista (incluso vacía = todas) las reemplaza."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if nombre is not None:
                cur.execute("UPDATE etiquetas_cierre SET nombre = %s WHERE id = %s", (nombre, etiqueta_id))
            if activo is not None:
                cur.execute("UPDATE etiquetas_cierre SET activo = %s WHERE id = %s", (activo, etiqueta_id))
            if campana_ids is not None:
                _guardar_campanas_etiqueta(cur, etiqueta_id, campana_ids)
        conn.commit()
    finally:
        release_connection(conn)


def eliminar_etiqueta(etiqueta_id: int):
    """Desactiva en vez de borrar de verdad — así no se pierde la referencia
    en conversaciones ya cerradas que usaron esta etiqueta."""
    editar_etiqueta(etiqueta_id, activo=False)


# ---------------------------------------------------------------------
# Respuestas rápidas — mantenimiento SOLO desde supervisor (título, cuerpo,
# campaña a la que pertenece); los asesores las usan para responder preguntas
# frecuentes, pero solo ven las de las campañas que tienen asignadas.
# ---------------------------------------------------------------------

def _obtener_respuestas_rapidas(cur, solo_activas: bool = False, campana_ids: list | None = None):
    """campana_ids=None trae de todas las campañas (vista de supervisor);
    una lista (incluso vacía) filtra a solo esas campañas (vista de asesor,
    según lo que tenga asignado en asesor_campanas)."""
    if campana_ids is not None and not campana_ids:
        return []
    query = """
        SELECT r.id, r.titulo, r.cuerpo, r.campana_id, c.nombre, r.activo
        FROM respuestas_rapidas r
        JOIN campanas c ON c.id = r.campana_id
        WHERE TRUE
    """
    params = []
    if solo_activas:
        query += " AND r.activo = TRUE"
    if campana_ids is not None:
        query += " AND r.campana_id = ANY(%s)"
        params.append(campana_ids)
    query += " ORDER BY c.nombre, r.titulo"
    cur.execute(query, params)
    return [{"id": i, "titulo": t, "cuerpo": cu, "campana_id": cid, "campana_nombre": cn, "activo": a}
            for i, t, cu, cid, cn, a in cur.fetchall()]


def _campanas_de_asesor(cur, asesor_id: int) -> list:
    cur.execute("SELECT campana_id FROM asesor_campanas WHERE asesor_id = %s", (asesor_id,))
    return [row[0] for row in cur.fetchall()]


async def enviar_respuestas_rapidas(destinatario_id: int, manager, a_asesor: bool = False):
    """a_asesor=True: solo las activas de las campañas de ESE asesor (para
    usar al responder). a_asesor=False (supervisor): todas, de todas las
    campañas, incluidas las desactivadas, para poder mantenerlas."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if a_asesor:
                campana_ids = _campanas_de_asesor(cur, destinatario_id)
                respuestas = _obtener_respuestas_rapidas(cur, solo_activas=True, campana_ids=campana_ids)
                payload = {"respuestas": respuestas}
            else:
                respuestas = _obtener_respuestas_rapidas(cur, solo_activas=False)
                # El supervisor necesita el catálogo de campañas para el
                # selector del formulario "Nueva respuesta rápida" -- se lo
                # mandamos junto para no depender de que antes haya abierto
                # el panel de "Asesores" (que es lo único que hoy lo carga).
                payload = {"respuestas": respuestas, "campanas": obtener_campanas()}
    finally:
        release_connection(conn)
    evento = {"type": "respuestas_rapidas_disponibles", "payload": payload}
    if a_asesor:
        await manager.enviar_a_asesor(destinatario_id, evento)
    else:
        await manager.enviar_a_supervisor(destinatario_id, evento)


async def notificar_respuestas_rapidas_a_todos(manager):
    """Tras crear/editar/eliminar una respuesta rápida: refresca a todos los
    supervisores (todas, de todas las campañas) y a cada asesor CONECTADO
    (solo las activas de sus propias campañas)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            todas = _obtener_respuestas_rapidas(cur, solo_activas=False)
            campanas_por_asesor = {
                asesor_id: _campanas_de_asesor(cur, asesor_id)
                for asesor_id in list(manager.asesor_connections.keys())
            }
    finally:
        release_connection(conn)
    await manager.enviar_a_todos_supervisores(
        {"type": "respuestas_rapidas_disponibles", "payload": {"respuestas": todas}}
    )
    activas = [r for r in todas if r["activo"]]
    for asesor_id, campana_ids in campanas_por_asesor.items():
        filtradas = [r for r in activas if r["campana_id"] in campana_ids]
        await manager.enviar_a_asesor(
            asesor_id, {"type": "respuestas_rapidas_disponibles", "payload": {"respuestas": filtradas}}
        )


def crear_respuesta_rapida(titulo: str, cuerpo: str, campana_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO respuestas_rapidas (titulo, cuerpo, campana_id) VALUES (%s, %s, %s)",
                (titulo, cuerpo, campana_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def editar_respuesta_rapida(respuesta_id: int, titulo: str = None, cuerpo: str = None,
                             campana_id: int = None, activo: bool = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if titulo is not None:
                cur.execute("UPDATE respuestas_rapidas SET titulo = %s WHERE id = %s", (titulo, respuesta_id))
            if cuerpo is not None:
                cur.execute("UPDATE respuestas_rapidas SET cuerpo = %s WHERE id = %s", (cuerpo, respuesta_id))
            if campana_id is not None:
                cur.execute("UPDATE respuestas_rapidas SET campana_id = %s WHERE id = %s", (campana_id, respuesta_id))
            if activo is not None:
                cur.execute("UPDATE respuestas_rapidas SET activo = %s WHERE id = %s", (activo, respuesta_id))
        conn.commit()
    finally:
        release_connection(conn)


def eliminar_respuesta_rapida(respuesta_id: int):
    """Borra de verdad -- a diferencia de las etiquetas de cierre, nada queda
    referenciando el id de una respuesta rápida (el texto ya enviado a un
    cliente queda guardado como un mensaje normal, independiente), así que no
    hace falta solo desactivarla."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM respuestas_rapidas WHERE id = %s", (respuesta_id,))
        conn.commit()
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------
# Configuración global (clave/valor) — hoy solo el mensaje de cierre,
# pero sirve para cualquier otro ajuste simple que se agregue después.
# ---------------------------------------------------------------------

def obtener_configuracion(clave: str) -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT valor FROM configuracion_global WHERE clave = %s", (clave,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_connection(conn)


def guardar_configuracion(clave: str, valor: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO configuracion_global (clave, valor) VALUES (%s, %s)
                   ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor""",
                (clave, valor),
            )
        conn.commit()
    finally:
        release_connection(conn)


async def enviar_configuracion(supervisor_id: int, manager):
    cierre = obtener_configuracion("mensaje_cierre_conversacion")
    bienvenida = obtener_configuracion("mensaje_bienvenida_asesor")
    inactividad_minutos = obtener_configuracion("inactividad_minutos") or "5"
    await manager.enviar_a_supervisor(supervisor_id, {
        "type": "configuracion",
        "payload": {
            "mensaje_cierre_conversacion": cierre,
            "mensaje_bienvenida_asesor": bienvenida,
            "inactividad_minutos": inactividad_minutos,
        },
    })


# ---------------------------------------------------------------------
# Mantenimiento de asesores — crear, editar, resetear clave
# ---------------------------------------------------------------------

def listar_horarios():
    """Horario de atención de cada campaña (usado por validar_horario_consultas),
    editable desde el panel de supervisor en vez de por SQL a mano. Siempre
    devuelve los 7 días de la semana por campaña (0=lunes...6=domingo, igual
    que Python) — un día sin fila en horarios_atencion se muestra como
    "no atiende ese día"."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, codigo, nombre, mensaje_fuera_horario FROM campanas WHERE activo = TRUE ORDER BY nombre"
            )
            campanas = cur.fetchall()
            cur.execute("SELECT campana_id, dia_semana, hora_inicio, hora_fin, activo FROM horarios_atencion")
            por_campana = {}
            for campana_id, dia, hora_inicio, hora_fin, activo in cur.fetchall():
                por_campana.setdefault(campana_id, {})[dia] = {
                    "hora_inicio": hora_inicio.strftime("%H:%M") if hora_inicio else None,
                    "hora_fin": hora_fin.strftime("%H:%M") if hora_fin else None,
                    "activo": activo,
                }
    finally:
        release_connection(conn)

    resultado = []
    for campana_id, codigo, nombre, mensaje_fuera_horario in campanas:
        dias = []
        for dia_semana in range(7):
            fila = por_campana.get(campana_id, {}).get(dia_semana)
            dias.append({
                "dia_semana": dia_semana,
                "hora_inicio": fila["hora_inicio"] if fila else None,
                "hora_fin": fila["hora_fin"] if fila else None,
                "activo": bool(fila and fila["activo"]),
            })
        resultado.append({
            "campana_id": campana_id,
            "codigo": codigo,
            "nombre": nombre,
            "mensaje_fuera_horario": mensaje_fuera_horario or "",
            "dias": dias,
        })
    return resultado


def guardar_horarios(campana_codigo: str, mensaje_fuera_horario: str, dias: list) -> dict:
    """Reemplaza por completo el horario de una campaña. `dias` es una lista
    de hasta 7 {"dia_semana", "hora_inicio", "hora_fin", "activo"} — los días
    con activo=False (o ausentes) simplemente no atienden ese día."""
    if not campana_codigo:
        return {"ok": False, "motivo": "Falta indicar la campaña."}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM campanas WHERE codigo = %s", (campana_codigo,))
            row = cur.fetchone()
            if row is None:
                return {"ok": False, "motivo": "La campaña no existe."}
            campana_id = row[0]

            cur.execute(
                "UPDATE campanas SET mensaje_fuera_horario = %s WHERE id = %s",
                (mensaje_fuera_horario or None, campana_id),
            )
            cur.execute("DELETE FROM horarios_atencion WHERE campana_id = %s", (campana_id,))
            for dia in (dias or []):
                if not dia.get("activo") or not dia.get("hora_inicio") or not dia.get("hora_fin"):
                    continue
                cur.execute(
                    """INSERT INTO horarios_atencion (campana_id, dia_semana, hora_inicio, hora_fin, activo)
                       VALUES (%s, %s, %s, %s, TRUE)""",
                    (campana_id, dia["dia_semana"], dia["hora_inicio"], dia["hora_fin"]),
                )
        conn.commit()
        return {"ok": True}
    finally:
        release_connection(conn)


def listar_feriados():
    """Feriados vigentes (de hoy en adelante, hora de Lima), agrupados por fecha +
    descripción + mensaje: cada grupo lista las campañas a las que aplica."""
    hoy = datetime.now(timezone(timedelta(hours=-5))).date()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT f.id, f.fecha, f.descripcion, f.mensaje, c.id, c.codigo, c.nombre
                   FROM feriados_campana f JOIN campanas c ON c.id = f.campana_id
                   WHERE f.fecha >= %s ORDER BY f.fecha, c.nombre""",
                (hoy,),
            )
            filas = cur.fetchall()
    except Exception:
        conn.rollback()
        return []  # tabla aún sin crear
    finally:
        release_connection(conn)
    grupos = {}
    for fid, fecha, descripcion, mensaje, cid, codigo, nombre in filas:
        clave = (fecha, descripcion or "", mensaje or "")
        g = grupos.setdefault(clave, {"fecha": fecha.isoformat(), "descripcion": descripcion or "",
                                      "mensaje": mensaje or "", "ids": [], "campanas": []})
        g["ids"].append(fid)
        g["campanas"].append({"id": cid, "codigo": codigo, "nombre": nombre})
    return list(grupos.values())


def crear_feriado(fecha: str, descripcion: str, mensaje: str, campana_ids: list, creado_por: int) -> dict:
    """Crea (o actualiza) el feriado para cada campaña elegida."""
    try:
        fecha_dt = datetime.strptime((fecha or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "motivo": "La fecha no es válida."}
    ids = [int(i) for i in (campana_ids or []) if str(i).isdigit()]
    if not ids:
        return {"ok": False, "motivo": "Elige al menos una campaña."}
    descripcion = (descripcion or "").strip()[:120] or None
    mensaje = (mensaje or "").strip()[:1000] or None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for cid in ids:
                cur.execute(
                    """INSERT INTO feriados_campana (campana_id, fecha, descripcion, mensaje, creado_por)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (campana_id, fecha) DO UPDATE
                       SET descripcion = EXCLUDED.descripcion, mensaje = EXCLUDED.mensaje,
                           creado_por = EXCLUDED.creado_por""",
                    (cid, fecha_dt, descripcion, mensaje, creado_por),
                )
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        return {"ok": False, "motivo": "No se pudo guardar el feriado (¿se ejecutó migracion_feriados.sql?)."}
    finally:
        release_connection(conn)


def eliminar_feriados(ids: list) -> dict:
    ids = [int(i) for i in (ids or []) if str(i).isdigit()]
    if not ids:
        return {"ok": False, "motivo": "Falta indicar qué feriado quitar."}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM feriados_campana WHERE id = ANY(%s)", (ids,))
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        return {"ok": False, "motivo": "No se pudo quitar el feriado."}
    finally:
        release_connection(conn)


def obtener_campanas():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, codigo, nombre FROM campanas WHERE activo = TRUE ORDER BY nombre")
            return [{"id": i, "codigo": c, "nombre": n} for i, c, n in cur.fetchall()]
    finally:
        release_connection(conn)


def listar_asesores_admin():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.usuario, a.nombre, a.nombre_display, a.rol, a.activo,
                       a.max_conversaciones_simultaneas,
                       COALESCE(array_agg(ac.campana_id) FILTER (WHERE ac.campana_id IS NOT NULL), '{}')
                FROM asesores a
                LEFT JOIN asesor_campanas ac ON ac.asesor_id = a.id
                GROUP BY a.id
                ORDER BY a.nombre_display
                """
            )
            columnas = ["asesor_id", "usuario", "nombre", "nombre_display", "rol", "activo",
                        "cupo_maximo", "campana_ids"]
            filas = [dict(zip(columnas, row)) for row in cur.fetchall()]
            for f in filas:
                f["es_maximo"] = False
            try:
                cur.execute("SAVEPOINT sp_niveles")
                cur.execute("SELECT id, es_maximo FROM asesores")
                maximos = {i for i, m in cur.fetchall() if m}
                cur.execute("SELECT asesor_id, campana_id FROM supervisor_campanas ORDER BY campana_id")
                por_sup = {}
                for a_id, c_id in cur.fetchall():
                    por_sup.setdefault(a_id, []).append(c_id)
                cur.execute("RELEASE SAVEPOINT sp_niveles")
                for f in filas:
                    f["es_maximo"] = f["asesor_id"] in maximos
                    if f["rol"] == "supervisor":
                        f["campana_ids"] = por_sup.get(f["asesor_id"], [])
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_niveles")  # migración de niveles sin correr
    finally:
        release_connection(conn)
    try:
        import totp
        con_2fa = totp.estado_por_ids()
    except Exception:
        con_2fa = set()
    for f in filas:
        f["dosfa"] = f["asesor_id"] in con_2fa
    return filas


def _tabla_campanas(rol: str) -> str:
    """Las campañas de un asesor son las que ATIENDE (asesor_campanas: el motor de
    asignación le manda conversaciones). Las de un supervisor son el alcance de lo
    que puede VER y gestionar (supervisor_campanas): nunca deben mezclarse, o un
    supervisor empezaría a recibir conversaciones."""
    return "supervisor_campanas" if rol == "supervisor" else "asesor_campanas"


def crear_asesor(usuario: str, nombre: str, nombre_display: str, password: str,
                  rol: str, campana_ids: list) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO asesores (usuario, nombre, nombre_display, password_hash, rol)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (usuario, nombre, nombre_display, auth.hash_password(password), rol),
            )
            asesor_id = cur.fetchone()[0]
            tabla = _tabla_campanas(rol)
            for campana_id in (campana_ids or []):
                cur.execute(
                    f"INSERT INTO {tabla} (asesor_id, campana_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (asesor_id, campana_id),
                )
        conn.commit()
        return asesor_id
    finally:
        release_connection(conn)


def editar_asesor(asesor_id: int, nombre_display: str = None, rol: str = None,
                   activo: bool = None, cupo: int = None, campana_ids: list = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # El supervisor máximo no se puede degradar, desactivar ni cambiar de rol desde
            # la pantalla (así nadie se queda sin acceso total por un clic).
            es_max = _es_maximo_cur(cur, asesor_id)
            if nombre_display is not None:
                cur.execute("UPDATE asesores SET nombre_display = %s WHERE id = %s", (nombre_display, asesor_id))
            if rol is not None and not es_max:
                cur.execute("UPDATE asesores SET rol = %s WHERE id = %s", (rol, asesor_id))
            if activo is not None and not (es_max and not activo):
                cur.execute("UPDATE asesores SET activo = %s WHERE id = %s", (activo, asesor_id))
            if cupo is not None:
                cur.execute("UPDATE asesores SET max_conversaciones_simultaneas = %s WHERE id = %s",
                            (cupo, asesor_id))
            cur.execute("SELECT rol FROM asesores WHERE id = %s", (asesor_id,))
            fila_rol = cur.fetchone()
            rol_final = fila_rol[0] if fila_rol else rol
            if rol is not None and not es_max:
                # Al cambiar de asesor <-> supervisor, las campañas del rol anterior no valen.
                otra = "asesor_campanas" if rol_final == "supervisor" else "supervisor_campanas"
                try:
                    cur.execute("SAVEPOINT sp_otra")
                    cur.execute(f"DELETE FROM {otra} WHERE asesor_id = %s", (asesor_id,))
                    cur.execute("RELEASE SAVEPOINT sp_otra")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_otra")
            if campana_ids is not None and not es_max:
                tabla = _tabla_campanas(rol_final)
                cur.execute(f"DELETE FROM {tabla} WHERE asesor_id = %s", (asesor_id,))
                for campana_id in campana_ids:
                    cur.execute(
                        f"INSERT INTO {tabla} (asesor_id, campana_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (asesor_id, campana_id),
                    )
        conn.commit()
    finally:
        release_connection(conn)


def editar_campanas_asesor(asesor_id, campana_ids: list, alcance) -> dict:
    """Agrega o quita campañas a un ASESOR (no a supervisores, editores ni al máximo).
    Con alcance (supervisor de campaña): solo toca las campañas de su alcance y deja intactas las demás
    que tenga el asesor. Sin alcance (máximo): las reemplaza todas.
    Devuelve {"ok": True, "nombre": ...} o {"ok": False, "motivo": ...}."""
    if not str(asesor_id or "").isdigit():
        return {"ok": False, "motivo": "Falta el asesor."}
    pedidas = set(_ids_validos(campana_ids))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rol, activo, nombre_display FROM asesores WHERE id = %s", (int(asesor_id),))
            fila = cur.fetchone()
            if fila is None:
                conn.rollback()
                return {"ok": False, "motivo": "Ese asesor no existe."}
            rol, activo, nombre = fila
            if rol != "asesor" or _es_maximo_cur(cur, int(asesor_id)):
                conn.rollback()
                return {"ok": False, "motivo": "Solo se pueden cambiar las campañas de un asesor."}
            if alcance is not None and not activo:
                conn.rollback()
                return {"ok": False, "motivo": f"{nombre} está desactivado."}
            if pedidas:
                cur.execute("SELECT id FROM campanas WHERE id = ANY(%s)", (list(pedidas),))
                if len({r[0] for r in cur.fetchall()}) != len(pedidas):
                    conn.rollback()
                    return {"ok": False, "motivo": "Alguna de las campañas elegidas no existe."}
            if alcance is None:
                cur.execute("DELETE FROM asesor_campanas WHERE asesor_id = %s", (int(asesor_id),))
            else:
                cur.execute("DELETE FROM asesor_campanas WHERE asesor_id = %s AND campana_id = ANY(%s)",
                            (int(asesor_id), list(alcance["ids"])))
                pedidas &= set(alcance["ids"])
            for campana_id in sorted(pedidas):
                cur.execute(
                    "INSERT INTO asesor_campanas (asesor_id, campana_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (int(asesor_id), campana_id),
                )
        conn.commit()
        return {"ok": True, "nombre": nombre}
    finally:
        release_connection(conn)


def resetear_password_asesor(asesor_id: int, password: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE asesores SET password_hash = %s WHERE id = %s",
                (auth.hash_password(password), asesor_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


async def notificar_asesores_admin_a_todos(manager):
    payload = {"asesores": listar_asesores_admin(), "campanas": obtener_campanas()}
    await manager.enviar_a_todos_supervisores({"type": "asesores_admin", "payload": payload})


async def notificar_estado_global_a_todos(manager):
    """Tras crear/editar un asesor (cambia de rol, se activa/desactiva, etc.),
    el tablero de todos los supervisores debe reflejarlo de inmediato."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            data = _snapshot(cur)
            en_cola = _conversaciones_en_cola(cur)
    finally:
        release_connection(conn)
    await manager.enviar_a_todos_supervisores({"type": "estado_global", "payload": {"asesores": data, "en_cola": en_cola}})


# ---------------------------------------------------------------------
# Niveles de supervisor: máximo (ve y gestiona todo) y de campaña (solo lo
# de las campañas que se le asignaron en supervisor_campanas).
# Si la migración (migracion_supervisor_niveles.sql) aún no se corrió, todos
# los supervisores se comportan como máximos (igual que antes).
# ---------------------------------------------------------------------

def _es_maximo_cur(cur, asesor_id: int) -> bool:
    try:
        cur.execute("SAVEPOINT sp_maximo")
        cur.execute("SELECT es_maximo FROM asesores WHERE id = %s", (asesor_id,))
        fila = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT sp_maximo")
        return bool(fila and fila[0])
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT sp_maximo")
        return False


# "Ver como": el supervisor máximo puede simular el alcance de un supervisor de campaña
# (solo restringe, nunca amplía). Vive en memoria: se borra al desconectar o al redeployar.
_VER_COMO: dict = {}


def fijar_ver_como(asesor_id: int, campana_ids):
    """campana_ids vacío/None = volver a la vista completa."""
    if campana_ids:
        _VER_COMO[asesor_id] = [int(i) for i in campana_ids]
    else:
        _VER_COMO.pop(asesor_id, None)


def ver_como_activo(asesor_id: int):
    return _VER_COMO.get(asesor_id)


def _alcance_de_campanas(ids):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, codigo, nombre FROM campanas WHERE id = ANY(%s) AND activo = TRUE ORDER BY nombre",
                        (list(ids),))
            filas = cur.fetchall()
        conn.rollback()
    finally:
        release_connection(conn)
    return {"ids": [f[0] for f in filas], "codigos": [f[1] for f in filas], "nombres": [f[2] for f in filas]}


def obtener_alcance(asesor_id: int, real: bool = False):
    """None = sin restricción (supervisor máximo, editor, asesor, o niveles sin migrar).
    real=True ignora "Ver como" (para saber si de verdad es máximo).
    Si no, {"ids": [campana_id...], "codigos": [codigo...], "nombres": [...]} con las
    campañas que ese supervisor puede ver (lista vacía = no ve nada)."""
    simulado = None
    filas = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rol FROM asesores WHERE id = %s", (asesor_id,))
            fila = cur.fetchone()
            if fila is None or fila[0] != "supervisor":
                return None
            try:
                cur.execute("SAVEPOINT sp_alcance")
                cur.execute("SELECT es_maximo FROM asesores WHERE id = %s", (asesor_id,))
                if cur.fetchone()[0]:
                    cur.execute("RELEASE SAVEPOINT sp_alcance")
                    simulado = None if real else _VER_COMO.get(asesor_id)
                    if not simulado:
                        return None
                    filas = None  # se resuelve abajo, ya sin esta conexión
                if simulado is None:
                    cur.execute(
                        """SELECT c.id, c.codigo, c.nombre FROM supervisor_campanas sc
                           JOIN campanas c ON c.id = sc.campana_id
                           WHERE sc.asesor_id = %s AND c.activo = TRUE ORDER BY c.nombre""",
                        (asesor_id,),
                    )
                    filas = cur.fetchall()
                    cur.execute("RELEASE SAVEPOINT sp_alcance")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_alcance")
                return None  # migración sin correr: comportamiento de siempre
        conn.rollback()
    finally:
        release_connection(conn)
    if simulado:
        return _alcance_de_campanas(simulado)
    return {"ids": [f[0] for f in filas], "codigos": [f[1] for f in filas], "nombres": [f[2] for f in filas]}


def campanas_efectivas(alcance, campana_id=None):
    """Campañas sobre las que se calcula un dashboard/reporte. None = todas.
    [] = ninguna (el filtro devuelve cero filas)."""
    if alcance is None:
        return None if campana_id is None else [int(campana_id)]
    if campana_id is None:
        return list(alcance["ids"])
    return [int(campana_id)] if int(campana_id) in alcance["ids"] else []


def conversacion_en_alcance(conversacion_id, alcance) -> bool:
    if alcance is None:
        return True
    try:
        conv = int(conversacion_id)
    except (TypeError, ValueError):
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM conversaciones WHERE id = %s", (conv,))
            fila = cur.fetchone()
        conn.rollback()
    finally:
        release_connection(conn)
    return bool(fila and fila[0] is not None and fila[0] in alcance["ids"])


def asesor_en_alcance(asesor_id, alcance) -> bool:
    """El asesor atiende al menos una de las campañas del alcance."""
    if alcance is None:
        return True
    try:
        aid = int(asesor_id)
    except (TypeError, ValueError):
        return False
    if not alcance["ids"]:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM asesor_campanas WHERE asesor_id = %s AND campana_id = ANY(%s) LIMIT 1",
                        (aid, alcance["ids"]))
            ok = cur.fetchone() is not None
        conn.rollback()
    finally:
        release_connection(conn)
    return ok


def _asesor_de_alcance(d: dict, alcance) -> bool:
    return bool(set(d.get("campanas") or []) & set(alcance["codigos"]))


def _recortar_asesor(d: dict, alcance) -> dict:
    """Copia del asesor solo con las conversaciones de las campañas del alcance."""
    c = dict(d)
    convs = [x for x in (d.get("conversaciones") or []) if x.get("campana_id") in alcance["ids"]]
    c["conversaciones"] = convs
    c["conversaciones_activas"] = len(convs)
    c["campanas"] = [k for k in (d.get("campanas") or []) if k in alcance["codigos"]]
    return c


def filtrar_evento(evento: dict, alcance):
    """Deja pasar a un supervisor de campaña solo lo que le corresponde. Devuelve el
    evento (posiblemente recortado) o None si no debe recibirlo."""
    if alcance is None:
        return evento
    tipo = evento.get("type")
    p = evento.get("payload") or {}
    if tipo == "estado_global":
        return {**evento, "payload": {
            **p,
            "asesores": [_recortar_asesor(a, alcance) for a in (p.get("asesores") or []) if _asesor_de_alcance(a, alcance)],
            "en_cola": [q for q in (p.get("en_cola") or []) if q.get("campana_codigo") in alcance["codigos"]],
        }}
    if tipo == "asesor_estado_cambio":
        return {**evento, "payload": _recortar_asesor(p, alcance)} if _asesor_de_alcance(p, alcance) else None
    if tipo == "lista_asesores":
        return {**evento, "payload": {**p, "asesores": [a for a in (p.get("asesores") or []) if _asesor_de_alcance(a, alcance)]}}
    if tipo == "asesores_admin":
        # El supervisor de campaña puede agregar o quitar SUS campañas a los asesores (no crear usuarios ni
        # cambiar nada más): ve solo los asesores activos, y de cada uno solo las campañas de su alcance.
        ids = set(alcance["ids"])
        asesores = [{"asesor_id": a["asesor_id"], "usuario": a.get("usuario"), "nombre_display": a.get("nombre_display"),
                     "rol": "asesor", "activo": True, "cupo_maximo": a.get("cupo_maximo"), "es_maximo": False,
                     "dosfa": False, "campana_ids": [c for c in (a.get("campana_ids") or []) if c in ids]}
                    for a in (p.get("asesores") or [])
                    if a.get("rol") == "asesor" and a.get("activo") and not a.get("es_maximo")]
        return {**evento, "payload": {"asesores": asesores, "limitado": True,
                                      "campanas": [c for c in (p.get("campanas") or []) if c.get("id") in ids]}}
    if tipo == "campanas_admin":
        return None  # solo el supervisor máximo administra campañas
    if tipo == "respuestas_rapidas_disponibles":
        out = {**p, "respuestas": [r for r in (p.get("respuestas") or []) if r.get("campana_id") in alcance["ids"]]}
        if "campanas" in p:
            out["campanas"] = [c for c in p["campanas"] if c.get("id") in alcance["ids"]]
        return {**evento, "payload": out}
    if tipo == "etiquetas_disponibles":
        # Las generales (sin campañas) y las de sus campañas; las de otras campañas no.
        etqs = [e for e in (p.get("etiquetas") or [])
                if not e.get("campana_ids") or set(e["campana_ids"]) & set(alcance["ids"])]
        out = {**p, "etiquetas": etqs}
        if "campanas" in p:
            out["campanas"] = [c for c in p["campanas"] if c.get("id") in alcance["ids"]]
        return {**evento, "payload": out}
    if tipo == "horarios_actuales":
        return {**evento, "payload": {**p, "campanas": [c for c in (p.get("campanas") or []) if c.get("codigo") in alcance["codigos"]]}}
    if tipo == "feriados_actuales":
        feriados = []
        for f in (p.get("feriados") or []):
            pares = [(i, c) for i, c in zip(f.get("ids") or [], f.get("campanas") or []) if c.get("id") in alcance["ids"]]
            if pares:
                feriados.append({**f, "ids": [i for i, _ in pares], "campanas": [c for _, c in pares]})
        return {**evento, "payload": {**p, "feriados": feriados,
                                      "campanas": [c for c in (p.get("campanas") or []) if c.get("id") in alcance["ids"]]}}
    return evento


_SOLO_MAXIMO = {
    # "obtener_asesores_admin" ya no: el supervisor de campaña ve la lista recortada (ver filtrar_evento) para
    # agregar o quitar sus campañas con "editar_campanas_asesor". Crear y editar usuarios sigue siendo del máximo.
    "crear_asesor", "editar_asesor", "quitar_2fa_asesor", "resetear_password_asesor",
    "listar_campanas_admin", "crear_campana", "editar_campana",
    # Ajustes que valen para todo el chat, no para una campaña:
    "guardar_mensaje_cierre", "guardar_mensaje_bienvenida", "guardar_inactividad_minutos",
}


def _campanas_de_etiqueta(etiqueta_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM etiqueta_campanas WHERE etiqueta_id = %s", (etiqueta_id,))
            r = [x[0] for x in cur.fetchall()]
        conn.rollback()
        return r
    except Exception:
        conn.rollback()
        return []
    finally:
        release_connection(conn)


def _campana_de_respuesta_rapida(rr_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM respuestas_rapidas WHERE id = %s", (rr_id,))
            f = cur.fetchone()
        conn.rollback()
        return f[0] if f else None
    except Exception:
        conn.rollback()
        return None
    finally:
        release_connection(conn)


def _campanas_de_feriados(ids):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM feriados_campana WHERE id = ANY(%s)", ([int(i) for i in ids],))
            r = [x[0] for x in cur.fetchall()]
        conn.rollback()
        return r
    except Exception:
        conn.rollback()
        return []
    finally:
        release_connection(conn)


def _ids_validos(lista):
    return [int(i) for i in (lista or []) if str(i).isdigit()]


def motivo_denegado(tipo: str, payload: dict, alcance):
    """Texto del motivo si un supervisor de campaña NO puede hacer esta acción; None si puede."""
    if alcance is None:
        return None
    if tipo in _SOLO_MAXIMO:
        return "Esta opción solo está disponible para el supervisor máximo."
    otra = "Esa conversación es de una campaña que no tienes asignada."
    if payload.get("conversacion_id") is not None and not conversacion_en_alcance(payload.get("conversacion_id"), alcance):
        return otra
    ids = alcance["ids"]
    if tipo == "editar_campanas_asesor":
        pedidas = payload.get("campana_ids")
        if not isinstance(pedidas, list) or len(_ids_validos(pedidas)) != len(pedidas) \
                or not set(_ids_validos(pedidas)) <= set(ids):
            return "Solo puedes agregar o quitar campañas que tengas asignadas."
    if tipo in ("cambiar_cupo",) and not asesor_en_alcance(payload.get("asesor_id"), alcance):
        return "Ese asesor no atiende ninguna de tus campañas."
    if tipo in ("transferir_conversacion", "asignar_conversacion_manual") \
            and not asesor_en_alcance(payload.get("asesor_destino_id"), alcance):
        return "Ese asesor no atiende ninguna de tus campañas."
    if tipo == "guardar_horarios" and payload.get("campana_codigo") not in alcance["codigos"]:
        return "No tienes asignada esa campaña."
    if tipo == "crear_feriado":
        pedidas = _ids_validos(payload.get("campana_ids"))
        if not pedidas or not set(pedidas) <= set(ids):
            return "Elige solo campañas que tengas asignadas."
    if tipo == "eliminar_feriados":
        cs = _campanas_de_feriados(_ids_validos(payload.get("ids")))
        if not cs or not set(cs) <= set(ids):
            return "Solo puedes quitar feriados de tus campañas."
    if tipo in ("crear_respuesta_rapida", "editar_respuesta_rapida"):
        c = payload.get("campana_id")
        if tipo == "editar_respuesta_rapida" and c is None:
            c = _campana_de_respuesta_rapida(payload.get("id"))
        if c is None or int(c) not in ids or (
                tipo == "editar_respuesta_rapida" and _campana_de_respuesta_rapida(payload.get("id")) not in ids):
            return "Solo puedes gestionar respuestas rápidas de tus campañas."
    if tipo == "eliminar_respuesta_rapida" and _campana_de_respuesta_rapida(payload.get("id")) not in ids:
        return "Solo puedes gestionar respuestas rápidas de tus campañas."
    if tipo in ("crear_etiqueta", "editar_etiqueta", "eliminar_etiqueta"):
        nuevas = _ids_validos(payload.get("campana_ids"))
        if tipo == "crear_etiqueta" or (tipo == "editar_etiqueta" and payload.get("campana_ids") is not None):
            if not nuevas or not set(nuevas) <= set(ids):
                return "Las etiquetas que crees o edites deben ser solo de tus campañas."
        if tipo in ("editar_etiqueta", "eliminar_etiqueta"):
            actuales = _campanas_de_etiqueta(payload.get("id"))
            if not actuales or not set(actuales) <= set(ids):
                return "Esa etiqueta es general o de otras campañas; solo la modifica el supervisor máximo."
    return None


# ---------------------------------------------------------------------
# Campañas: alta y mantenimiento desde la pantalla (antes era solo por SQL)
# ---------------------------------------------------------------------

def _generar_codigo(nombre: str) -> str:
    import re
    import unicodedata
    base = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:40]
    return base or "campana"


def listar_campanas_admin():
    """Todas las campañas (activas o no) para la pantalla de mantenimiento."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.codigo, c.nombre, c.activo, COALESCE(c.mensaje_fuera_horario, ''),
                          (SELECT COUNT(*) FROM asesor_campanas ac JOIN asesores a ON a.id = ac.asesor_id
                            WHERE ac.campana_id = c.id AND a.activo = TRUE AND a.rol = 'asesor')
                   FROM campanas c ORDER BY c.activo DESC, c.nombre"""
            )
            filas = [{"id": i, "codigo": cod, "nombre": n, "activo": a, "mensaje_fuera_horario": m, "asesores": q}
                     for i, cod, n, a, m, q in cur.fetchall()]
            # Encuesta de satisfacción por campaña (columna de migracion_encuesta_satisfaccion.sql).
            try:
                cur.execute("SAVEPOINT sp_encuesta")
                cur.execute("SELECT id, encuesta_satisfaccion FROM campanas")
                con_encuesta = {i for i, e in cur.fetchall() if e}
                cur.execute("RELEASE SAVEPOINT sp_encuesta")
                for f in filas:
                    f["encuesta"] = f["id"] in con_encuesta
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_encuesta")   # migración sin correr: no se muestra
        conn.rollback()
        return filas
    finally:
        release_connection(conn)


def crear_campana(nombre: str, mensaje_fuera_horario: str = "", dias: list = None) -> dict:
    nombre = (nombre or "").strip()
    if len(nombre) < 3:
        return {"ok": False, "motivo": "Escribe el nombre de la campaña (mínimo 3 letras)."}
    if len(nombre) > 80:
        return {"ok": False, "motivo": "El nombre es demasiado largo (máximo 80)."}
    mensaje = (mensaje_fuera_horario or "").strip()[:1000] or (
        f"En este momento estamos fuera de nuestro horario de atención para {nombre}. "
        "Por favor, vuelve a escribirnos dentro de nuestro horario de atención.")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM campanas WHERE lower(nombre) = lower(%s)", (nombre,))
            if cur.fetchone():
                return {"ok": False, "motivo": "Ya existe una campaña con ese nombre."}
            base = _generar_codigo(nombre)
            codigo, n = base, 2
            while True:
                cur.execute("SELECT 1 FROM campanas WHERE codigo = %s", (codigo,))
                if cur.fetchone() is None:
                    break
                codigo = f"{base}_{n}"
                n += 1
            cur.execute(
                """INSERT INTO campanas (codigo, nombre, activo, mensaje_fuera_horario)
                   VALUES (%s, %s, TRUE, %s) RETURNING id""",
                (codigo, nombre, mensaje),
            )
            campana_id = cur.fetchone()[0]
            for dia in (dias or []):
                try:
                    d = int(dia.get("dia_semana"))
                except (TypeError, ValueError):
                    continue
                if not (0 <= d <= 6) or not dia.get("activo") or not dia.get("hora_inicio") or not dia.get("hora_fin"):
                    continue
                if str(dia["hora_fin"]) <= str(dia["hora_inicio"]):
                    conn.rollback()
                    return {"ok": False, "motivo": "En el horario, la hora de fin debe ser mayor que la de inicio."}
                cur.execute(
                    """INSERT INTO horarios_atencion (campana_id, dia_semana, hora_inicio, hora_fin, activo)
                       VALUES (%s, %s, %s, %s, TRUE)""",
                    (campana_id, d, dia["hora_inicio"], dia["hora_fin"]),
                )
        conn.commit()
        return {"ok": True, "id": campana_id, "codigo": codigo}
    except Exception:
        conn.rollback()
        return {"ok": False, "motivo": "No se pudo crear la campaña."}
    finally:
        release_connection(conn)


def editar_campana(campana_id, nombre: str = None, activo: bool = None, encuesta: bool = None) -> dict:
    try:
        campana_id = int(campana_id)
    except (TypeError, ValueError):
        return {"ok": False, "motivo": "Campaña inválida."}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if nombre is not None:
                nombre = nombre.strip()
                if len(nombre) < 3 or len(nombre) > 80:
                    return {"ok": False, "motivo": "El nombre debe tener entre 3 y 80 letras."}
                cur.execute("SELECT 1 FROM campanas WHERE lower(nombre) = lower(%s) AND id <> %s", (nombre, campana_id))
                if cur.fetchone():
                    return {"ok": False, "motivo": "Ya existe una campaña con ese nombre."}
                cur.execute("UPDATE campanas SET nombre = %s WHERE id = %s", (nombre, campana_id))
            if activo is not None:
                cur.execute("UPDATE campanas SET activo = %s WHERE id = %s", (bool(activo), campana_id))
            if encuesta is not None:
                cur.execute("UPDATE campanas SET encuesta_satisfaccion = %s WHERE id = %s", (bool(encuesta), campana_id))
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        return {"ok": False, "motivo": "No se pudo guardar la campaña."}
    finally:
        release_connection(conn)
