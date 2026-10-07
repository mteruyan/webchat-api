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
        SELECT c.asesor_id, c.id, u.nombre, u.telefono, u.dni, c.entrada_cola, c.asignado_en,
               c.pausada_en, ult.contenido, ult.tipo_contenido, ult.enviado_en, c.bandera
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
         pausada_en, ultimo_contenido, ultimo_tipo, ultimo_en, bandera) in cur.fetchall():
        resultado.setdefault(asesor_id, []).append({
            "conversacion_id": conversacion_id,
            "usuario_nombre": nombre or f"Usuario {conversacion_id}",
            "telefono": telefono,
            "dni": dni,
            "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
            "asignado_en": asignado_en.isoformat() if asignado_en else None,
            "pausada": pausada_en is not None,
            "bandera": bandera,
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
        SELECT c.id, u.nombre, u.telefono, u.dni, camp.nombre, camp.codigo, c.entrada_cola
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
            en_cola = _conversaciones_en_cola(cur)
    finally:
        release_connection(conn)
    await manager.enviar_a_todos_supervisores({"type": "estado_global", "payload": {"asesores": data, "en_cola": en_cola}})
