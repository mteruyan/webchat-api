"""
enlaces_directos.py -- SMS de invitacion con enlace DIRECTO a una campana y, opcionalmente, a un asesor.

El que envia (asesor o supervisor) elige la campana (solo entre las suyas) y, si quiere, un asesor de esa
campana. El SMS lleva un enlace con token (?ad=...). Al abrirlo el cliente cae en esa campana:
  1) fuera de horario / feriado de la campana -> se le muestra el mensaje de fuera de horario;
  2) asesor elegido disponible (conectado, sin pausa, con cupo)      -> conversa con el;
  3) si no -> otro asesor disponible de la MISMA campana;
  4) si nadie esta conectado dentro del horario -> queda en cola de esa campana.
No importa main.py (la logica de WebSocket vive en main.py).
"""
import logging
import secrets
from datetime import datetime, timezone, timedelta

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))
VIGENCIA_DIAS = 3


def campanas_del_asesor(asesor_id: int) -> list[int]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM asesor_campanas WHERE asesor_id = %s", (asesor_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        release_connection(conn)


def destinos(campana_ids_permitidas) -> list[dict]:
    """Campanas activas (solo las permitidas; None = todas) con sus asesores.
    [{"id","nombre","asesores":[{"id","nombre","conectado"}]}]"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre FROM campanas WHERE activo = TRUE ORDER BY nombre")
            campanas = [(i, n) for i, n in cur.fetchall()
                        if campana_ids_permitidas is None or i in campana_ids_permitidas]
            cur.execute(
                """SELECT ac.campana_id, a.id, COALESCE(a.nombre_display, a.nombre) AS nombre,
                          EXISTS (SELECT 1 FROM asesor_sesiones s WHERE s.asesor_id = a.id AND s.desconectado_en IS NULL)
                   FROM asesor_campanas ac JOIN asesores a ON a.id = ac.asesor_id
                   WHERE a.activo = TRUE AND a.rol <> 'editor'
                   ORDER BY nombre""")
            por_campana: dict[int, list] = {}
            for campana_id, aid, nombre, conectado in cur.fetchall():
                por_campana.setdefault(campana_id, []).append({"id": aid, "nombre": nombre, "conectado": bool(conectado)})
    finally:
        release_connection(conn)
    return [{"id": i, "nombre": n, "asesores": por_campana.get(i, [])} for i, n in campanas]


def crear_enlace(celular: str, campana_id, asesor_id, remitente_id: int, rol: str, permitidas):
    """Valida el destino y registra el enlace. Devuelve (token, nombre_destino) o (None, motivo_de_error)."""
    try:
        campana_id = int(campana_id)
        asesor_id = int(asesor_id) if asesor_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None, "Destino no válido."
    if permitidas is not None and campana_id not in permitidas:
        return None, "Solo puedes elegir una campaña a la que perteneces."
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre FROM campanas WHERE id = %s AND activo = TRUE", (campana_id,))
            c = cur.fetchone()
            if not c:
                return None, "La campaña ya no está activa."
            nombre_destino = c[0]
            if asesor_id is not None:
                cur.execute(
                    """SELECT COALESCE(a.nombre_display, a.nombre) FROM asesores a
                       JOIN asesor_campanas ac ON ac.asesor_id = a.id AND ac.campana_id = %s
                       WHERE a.id = %s AND a.activo = TRUE AND a.rol <> 'editor'""", (campana_id, asesor_id))
                a = cur.fetchone()
                if not a:
                    return None, "Ese asesor no pertenece a la campaña elegida."
                nombre_destino = a[0]
            token = secrets.token_urlsafe(6)
            cur.execute(
                """INSERT INTO enlaces_directos (token, celular, campana_id, asesor_id, enviado_por, rol_emisor, expira_en)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (token, celular, campana_id, asesor_id, remitente_id,
                 "supervisor" if rol == "supervisor" else "asesor",
                 datetime.now(LIMA_TZ) + timedelta(days=VIGENCIA_DIAS)))
        conn.commit()
        return token, nombre_destino
    except Exception:
        conn.rollback()
        logger.exception("No se pudo registrar el enlace directo (¿falta migracion_enlace_directo.sql?)")
        return None, "No se pudo preparar el enlace. Avisa al administrador (falta la migración de enlace directo)."
    finally:
        release_connection(conn)


def borrar_enlace(token: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM enlaces_directos WHERE token = %s", (token,))
        conn.commit()
    finally:
        release_connection(conn)


def tomar_pendiente(conversacion_id: int):
    """Enlace directo ya canjeado y aún no atendido del cliente de esta conversacion (lo marca como entregado).
    Devuelve {"id","campana_id","campana_codigo","campana_nombre","asesor_id"} o None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.id, e.campana_id, cam.codigo, cam.nombre, e.asesor_id
                   FROM enlaces_directos e
                   JOIN conversaciones c ON c.usuario_id = e.usuario_id
                   JOIN campanas cam ON cam.id = e.campana_id
                   WHERE c.id = %s AND e.canjeado_en IS NOT NULL AND e.entregado_en IS NULL
                   ORDER BY e.id DESC FOR UPDATE OF e SKIP LOCKED""", (conversacion_id,))
            filas = cur.fetchall()
            if not filas:
                conn.commit()
                return None
            cur.execute("UPDATE enlaces_directos SET entregado_en = %s, conversacion_id = %s WHERE id = ANY(%s)",
                        (datetime.now(LIMA_TZ), conversacion_id, [f[0] for f in filas]))
        conn.commit()
        i, campana_id, codigo, nombre, asesor_id = filas[0]
        return {"id": i, "campana_id": campana_id, "campana_codigo": codigo, "campana_nombre": nombre, "asesor_id": asesor_id}
    except Exception:
        conn.rollback()
        logger.warning("No se pudo consultar el enlace directo pendiente", exc_info=True)
        return None
    finally:
        release_connection(conn)


def registrar_resultado(enlace_id: int, resultado: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE enlaces_directos SET resultado = %s WHERE id = %s", (resultado[:30], enlace_id))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        release_connection(conn)
