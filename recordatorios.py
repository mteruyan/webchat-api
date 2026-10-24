"""
recordatorios.py — recordatorios SMS de entrega para recetas extendidas.

Un asesor programa, desde la conversación con el paciente, los SMS que saldrán en las
fechas en que le toca recoger la siguiente entrega (por protección al paciente solo se
entrega medicación para un máximo de 30 días por vez; la receta puede ser de hasta
6 meses). El envío lo dispara un Cloud Scheduler que llama a
POST /internal/recordatorios/procesar (ver main.py); aquí solo está la lógica y los datos.

Varias instancias de Cloud Run pueden recibir la llamada a la vez: los pendientes se
"reclaman" con FOR UPDATE SKIP LOCKED, así que un recordatorio nunca se envía dos veces.
"""

import logging
from datetime import datetime, timezone, timedelta, date

from db import get_connection, release_connection

logger = logging.getLogger("recordatorios")
LIMA_TZ = timezone(timedelta(hours=-5))

MAX_RECORDATORIOS = 5          # receta de 6 meses = 6 entregas = 5 recordatorios
MAX_PENDIENTES_POR_PACIENTE = 10
MAX_DIAS_ADELANTE = 200
MAX_CARACTERES_SMS = 300
MAX_INTENTOS = 3
DIAS_MAX_ATRASO = 7            # un recordatorio que no salió en 7 días ya no se manda (sería confuso)
TIEMPO_RECLAMO_MIN = 15        # un "enviando" colgado más de esto se reintenta

MENSAJE_DEFECTO = (
    "Sanitas: Hola {{NOMBRE}}, ya puedes coordinar la entrega {{ENTREGA}} de {{TOTAL}} de tus "
    "medicamentos. Escribenos aqui: https://www.sanitasperu.com/?ac=1"
)


class TablaNoExiste(Exception):
    """La migración migracion_recordatorios_receta.sql aún no se ejecutó."""


def hoy_lima() -> date:
    return datetime.now(LIMA_TZ).date()


def primer_nombre(nombre: str | None) -> str:
    p = (nombre or "").strip().split()
    return p[0].capitalize() if p else "paciente"


def armar_mensaje(plantilla: str, nombre: str | None, entrega_n: int, total: int) -> str:
    return (plantilla.replace("{{NOMBRE}}", primer_nombre(nombre))
                     .replace("{{ENTREGA}}", str(entrega_n))
                     .replace("{{TOTAL}}", str(total)))


def _ejecutar_con_tabla(fn):
    """Convierte 'la tabla no existe' en TablaNoExiste (para responder 503 claro)."""
    import psycopg2.errors
    try:
        return fn()
    except psycopg2.errors.UndefinedTable as e:
        raise TablaNoExiste() from e


def contexto_conversacion(conversacion_id: int) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.usuario_id, u.nombre, u.telefono, u.dni
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s""", (conversacion_id,))
            fila = cur.fetchone()
    finally:
        release_connection(conn)
    if not fila:
        return None
    return {"usuario_id": fila[0], "nombre": fila[1], "telefono": fila[2], "dni": fila[3]}


def listar(usuario_id: int) -> list[dict]:
    def _q():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT r.id, r.fecha_envio, r.estado, r.entrega_n, r.entregas_total, r.mensaje,
                              r.celular, r.enviado_en, r.ultimo_error, a.nombre_display
                       FROM recordatorios_receta r LEFT JOIN asesores a ON a.id = r.programado_por
                       WHERE r.usuario_id = %s AND r.estado <> 'cancelado'
                       ORDER BY r.fecha_envio, r.id""", (usuario_id,))
                filas = cur.fetchall()
            conn.rollback()
        finally:
            release_connection(conn)
        return [{"id": f[0], "fecha_envio": f[1].isoformat(), "estado": f[2], "entrega_n": f[3],
                 "entregas_total": f[4], "mensaje": f[5], "celular": f[6][-9:],
                 "enviado_en": f[7].astimezone(LIMA_TZ).strftime("%Y-%m-%d %H:%M") if f[7] else None,
                 "error": f[8], "programado_por": f[9]} for f in filas]
    return _ejecutar_con_tabla(_q)


def crear(conversacion_id: int, usuario_id: int, documento: str | None, nombre: str | None,
          celular: str, dias_receta: int, fechas: list[date], plantilla: str,
          asesor_id: int) -> list[int]:
    """Inserta un recordatorio por fecha (la entrega 1 ya se hizo en la atención)."""
    total = len(fechas) + 1
    def _q():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM recordatorios_receta WHERE usuario_id = %s "
                            "AND estado IN ('pendiente', 'enviando')", (usuario_id,))
                if cur.fetchone()[0] + len(fechas) > MAX_PENDIENTES_POR_PACIENTE:
                    raise ValueError("Este paciente ya tiene demasiados recordatorios pendientes. "
                                     "Cancela alguno antes de programar más.")
                cur.execute("SELECT fecha_envio FROM recordatorios_receta WHERE usuario_id = %s AND celular = %s "
                            "AND estado IN ('pendiente', 'enviando') AND fecha_envio = ANY(%s)",
                            (usuario_id, celular, fechas))
                repetidas = [f[0].strftime("%d/%m/%Y") for f in cur.fetchall()]
                if repetidas:
                    raise ValueError("Ya hay un recordatorio pendiente para este paciente el " + ", ".join(repetidas) + ".")
                ids = []
                for i, f in enumerate(fechas):
                    n = i + 2
                    cur.execute(
                        """INSERT INTO recordatorios_receta
                             (conversacion_id, usuario_id, documento, nombre, celular, mensaje, fecha_envio,
                              entrega_n, entregas_total, dias_receta, programado_por)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (conversacion_id, usuario_id, documento, nombre, celular,
                         armar_mensaje(plantilla, nombre, n, total), f, n, total, dias_receta, asesor_id))
                    ids.append(cur.fetchone()[0])
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)
    return _ejecutar_con_tabla(_q)


def cancelar(recordatorio_id: int, asesor_id: int) -> bool:
    def _q():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE recordatorios_receta SET estado = 'cancelado', cancelado_en = NOW(), cancelado_por = %s "
                            "WHERE id = %s AND estado IN ('pendiente', 'error')", (asesor_id, recordatorio_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        finally:
            release_connection(conn)
    return _ejecutar_con_tabla(_q)


def procesar_pendientes(enviar_sms, avisar_push=None) -> dict:
    """Envía los recordatorios que vencen hoy (o que quedaron atrasados hasta 7 días).
    `enviar_sms(celular, texto) -> bool`. `avisar_push(usuario_id, info)` (opcional) manda además un aviso push. Devuelve un resumen."""
    hoy = hoy_lima()
    resumen = {"enviados": 0, "errores": 0, "vencidos": 0}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Los que ya pasaron el plazo máximo de atraso no se mandan.
            cur.execute("UPDATE recordatorios_receta SET estado = 'error', ultimo_error = 'No se envió a tiempo (más de 7 días de atraso)' "
                        "WHERE estado IN ('pendiente', 'error', 'enviando') AND fecha_envio < %s", (hoy - timedelta(days=DIAS_MAX_ATRASO),))
            resumen["vencidos"] = cur.rowcount
            cur.execute(
                """UPDATE recordatorios_receta SET estado = 'enviando', intentos = intentos + 1, reclamado_en = NOW()
                   WHERE id IN (
                     SELECT id FROM recordatorios_receta
                     WHERE fecha_envio <= %s
                       AND ( estado = 'pendiente'
                          OR (estado = 'error' AND intentos < %s)
                          OR (estado = 'enviando' AND reclamado_en < NOW() - make_interval(mins => %s)) )
                     ORDER BY fecha_envio, id LIMIT 200
                     FOR UPDATE SKIP LOCKED)
                   RETURNING id, celular, mensaje, usuario_id, nombre, entrega_n, entregas_total""",
                (hoy, MAX_INTENTOS, TIEMPO_RECLAMO_MIN))
            lote = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)

    for rid, celular, mensaje, usuario_id, nombre_pac, entrega_n, entregas_total in lote:
        try:
            ok = bool(enviar_sms(celular, mensaje))
            error = None if ok else "El proveedor de SMS no aceptó el envío"
        except Exception as e:  # noqa: BLE001
            ok, error = False, f"Excepción al enviar: {e}"[:200]
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if ok:
                    cur.execute("UPDATE recordatorios_receta SET estado = 'enviado', enviado_en = NOW(), ultimo_error = NULL WHERE id = %s", (rid,))
                else:
                    cur.execute("UPDATE recordatorios_receta SET estado = 'error', ultimo_error = %s WHERE id = %s", (error, rid))
            conn.commit()
        finally:
            release_connection(conn)
        resumen["enviados" if ok else "errores"] += 1
        if ok and avisar_push and usuario_id:
            try:
                avisar_push(usuario_id, {"nombre": nombre_pac, "entrega_n": entrega_n, "entregas_total": entregas_total})
            except Exception:  # noqa: BLE001  (el push es un extra: nunca afecta al SMS)
                logger.exception("Aviso push del recordatorio %s falló", rid)
        if not ok:
            logger.error("Recordatorio %s no enviado: %s", rid, error)
    return resumen
