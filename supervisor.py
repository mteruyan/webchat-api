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
    "entrada_cola", "asignado_en"}, ...]} — para que el tablero de supervisor
    pueda ofrecer un botón de escuchar por conversación, sin que nadie tenga
    que escribir un conversacion_id a mano, y mostrar tiempos de espera/atención."""
    if asesor_ids is not None and not asesor_ids:
        return {}
    query = """
        SELECT c.asesor_id, c.id, u.nombre, u.telefono, c.entrada_cola, c.asignado_en
        FROM conversaciones c
        JOIN usuarios_webchat u ON u.id = c.usuario_id
        WHERE c.estado = 'asignada'
    """
    params = ()
    if asesor_ids is not None:
        query += " AND c.asesor_id = ANY(%s)"
        params = (asesor_ids,)
    cur.execute(query, params)
    resultado = {}
    for asesor_id, conversacion_id, nombre, telefono, entrada_cola, asignado_en in cur.fetchall():
        resultado.setdefault(asesor_id, []).append({
            "conversacion_id": conversacion_id,
            "usuario_nombre": nombre or f"Usuario {conversacion_id}",
            "telefono": telefono,
            "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
            "asignado_en": asignado_en.isoformat() if asignado_en else None,
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


def _completar_stats(d: dict, stats: dict):
    e = stats.get(d["asesor_id"], {})
    d["segundos_conectado_hoy"] = e.get("segundos_conectado", 0)
    d["segundos_pausado_hoy"] = e.get("segundos_pausado", 0)
    d["atendidas_hoy"] = e.get("atendidas_hoy", 0)


def _snapshot(cur):
    cur.execute(
        """
        SELECT a.id, a.nombre_display,
               (s.asesor_id IS NOT NULL) AS conectado,
               a.en_pausa,
               COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas,
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
    for f in filas:
        f["conversaciones"] = conv_map.get(f["asesor_id"], [])
        _completar_stats(f, stats_map)
    return filas


def _snapshot_uno(cur, asesor_id: int):
    cur.execute(
        """
        SELECT a.id, a.nombre_display,
               (s.asesor_id IS NOT NULL) AS conectado,
               a.en_pausa,
               COUNT(c.id) FILTER (WHERE c.estado = 'asignada') AS activas,
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
    return data


async def enviar_estado_global(supervisor_id: int, manager):
    """Snapshot completo — se manda una vez, al conectar la consola de supervisor."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            data = _snapshot(cur)
    finally:
        release_connection(conn)
    await manager.enviar_a_supervisor(supervisor_id, {"type": "estado_global", "payload": {"asesores": data}})


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


# ---------------------------------------------------------------------
# Etiquetas de cierre — mantenimiento desde la vista de supervisor
# ---------------------------------------------------------------------

def _obtener_etiquetas(cur, solo_activas: bool = False):
    query = "SELECT id, nombre, activo FROM etiquetas_cierre"
    if solo_activas:
        query += " WHERE activo = TRUE"
    query += " ORDER BY nombre"
    cur.execute(query)
    return [{"id": i, "nombre": n, "activo": a} for i, n, a in cur.fetchall()]


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
    await manager.enviar_a_todos_supervisores({"type": "etiquetas_disponibles", "payload": {"etiquetas": todas}})
    await manager.enviar_a_todos_asesores({"type": "etiquetas_disponibles", "payload": {"etiquetas": activas}})


def crear_etiqueta(nombre: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO etiquetas_cierre (nombre) VALUES (%s)", (nombre,))
        conn.commit()
    finally:
        release_connection(conn)


def editar_etiqueta(etiqueta_id: int, nombre: str = None, activo: bool = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if nombre is not None:
                cur.execute("UPDATE etiquetas_cierre SET nombre = %s WHERE id = %s", (nombre, etiqueta_id))
            if activo is not None:
                cur.execute("UPDATE etiquetas_cierre SET activo = %s WHERE id = %s", (activo, etiqueta_id))
        conn.commit()
    finally:
        release_connection(conn)


def eliminar_etiqueta(etiqueta_id: int):
    """Desactiva en vez de borrar de verdad — así no se pierde la referencia
    en conversaciones ya cerradas que usaron esta etiqueta."""
    editar_etiqueta(etiqueta_id, activo=False)


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
    await manager.enviar_a_supervisor(supervisor_id, {
        "type": "configuracion",
        "payload": {"mensaje_cierre_conversacion": cierre, "mensaje_bienvenida_asesor": bienvenida},
    })


# ---------------------------------------------------------------------
# Mantenimiento de asesores — crear, editar, resetear clave
# ---------------------------------------------------------------------

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
            return [dict(zip(columnas, row)) for row in cur.fetchall()]
    finally:
        release_connection(conn)


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
            for campana_id in (campana_ids or []):
                cur.execute(
                    "INSERT INTO asesor_campanas (asesor_id, campana_id) VALUES (%s, %s)",
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
            if nombre_display is not None:
                cur.execute("UPDATE asesores SET nombre_display = %s WHERE id = %s", (nombre_display, asesor_id))
            if rol is not None:
                cur.execute("UPDATE asesores SET rol = %s WHERE id = %s", (rol, asesor_id))
            if activo is not None:
                cur.execute("UPDATE asesores SET activo = %s WHERE id = %s", (activo, asesor_id))
            if cupo is not None:
                cur.execute("UPDATE asesores SET max_conversaciones_simultaneas = %s WHERE id = %s",
                            (cupo, asesor_id))
            if campana_ids is not None:
                cur.execute("DELETE FROM asesor_campanas WHERE asesor_id = %s", (asesor_id,))
                for campana_id in campana_ids:
                    cur.execute(
                        "INSERT INTO asesor_campanas (asesor_id, campana_id) VALUES (%s, %s)",
                        (asesor_id, campana_id),
                    )
        conn.commit()
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
    finally:
        release_connection(conn)
    await manager.enviar_a_todos_supervisores({"type": "estado_global", "payload": {"asesores": data}})
