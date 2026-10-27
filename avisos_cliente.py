"""avisos_cliente.py — avisarle al cliente que el asesor le respondió, aunque haya salido del chat.

Reglas (para una conversación con asesor, sobre las respuestas del asesor o supervisor que el cliente no leyó):
  - Push inmediato si el chat está cerrado u oculto (lo dispara main.py al entregar el mensaje).
  - Push a los 30 s sin leer, si el inmediato no salió (p. ej. el celular dejó la conexión abierta en segundo
    plano y el servidor creía que el cliente estaba mirando).
  - SMS a los 3 minutos sin leer, solo si no le llegó un push por esas respuestas. Máximo 1 SMS cada 30 minutos
    por conversación, y solo si hay respuestas nuevas desde el último SMS.

"Leído" = el widget abierto y visible en pantalla (mensajes.leido_en). Si el cliente escribió después de la
respuesta, también se da por leída.

Todo queda en la tabla avisos_cliente (migracion_avisos_cliente.sql). Si la migración no se corrió, no avisa
nada y el chat sigue igual.
"""

import logging
from datetime import datetime, timedelta, timezone

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

PUSH_ESPERA_SEG = 30
SMS_ESPERA_MIN = 3
SMS_TOPE_MIN = 30
SMS_REINTENTO_MIN = 5          # si Danaconnect falló, se reintenta recién a los 5 minutos
VENTANA_HORAS = 12             # no se avisa por respuestas más antiguas que esto

_aviso_migracion = False


def _falta_migracion(e: Exception) -> bool:
    global _aviso_migracion
    if "avisos_cliente" in str(e):
        if not _aviso_migracion:
            logger.warning("avisos: falta ejecutar migracion_avisos_cliente.sql (no se envían avisos automáticos)")
            _aviso_migracion = True
        return True
    return False


def registrar(conversacion_id: int, canal: str, motivo: str, ok: bool = True):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO avisos_cliente (conversacion_id, canal, motivo, ok, enviado_en) VALUES (%s, %s, %s, %s, %s)",
                        (conversacion_id, canal, motivo, ok, datetime.now(LIMA_TZ)))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if not _falta_migracion(e):
            logger.warning("avisos: no se pudo registrar el aviso", exc_info=True)
    finally:
        release_connection(conn)


def pendientes(ahora: datetime | None = None) -> list[dict]:
    """Avisos que corresponde mandar ahora: [{"conversacion_id", "canal", "motivo", "telefono", "sesion_token", "asesor"}]."""
    ahora = ahora or datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    WITH sin_leer AS (
                        -- Respuestas del asesor/supervisor que el cliente no leyó y a las que no contestó después
                        SELECT c.id AS conversacion_id, m.enviado_en
                        FROM conversaciones c
                        JOIN mensajes m ON m.conversacion_id = c.id
                        WHERE c.estado = 'asignada'
                          AND m.emisor_tipo IN ('asesor', 'supervisor') AND m.leido_en IS NULL
                          AND m.enviado_en > %(ventana)s
                          AND NOT EXISTS (SELECT 1 FROM mensajes mu WHERE mu.conversacion_id = c.id
                                            AND mu.emisor_tipo = 'usuario' AND mu.enviado_en > m.enviado_en)
                    ), ultimos AS (
                        SELECT s.conversacion_id,
                               MAX(a.enviado_en) FILTER (WHERE a.canal = 'push' AND a.ok) AS ultimo_push,
                               MAX(a.enviado_en) FILTER (WHERE a.canal = 'sms' AND a.ok) AS ultimo_sms,
                               MAX(a.enviado_en) FILTER (WHERE a.canal = 'sms' AND NOT a.ok) AS ultimo_sms_fallido
                        FROM (SELECT DISTINCT conversacion_id FROM sin_leer) s
                        LEFT JOIN avisos_cliente a ON a.conversacion_id = s.conversacion_id
                        GROUP BY s.conversacion_id
                    )
                    SELECT s.conversacion_id,
                           -- primera respuesta sin leer que todavía no tuvo push / SMS
                           MIN(s.enviado_en) FILTER (WHERE s.enviado_en > COALESCE(u.ultimo_push, '-infinity')) AS sin_push_desde,
                           MIN(s.enviado_en) FILTER (WHERE s.enviado_en > COALESCE(u.ultimo_sms, '-infinity')) AS sin_sms_desde,
                           BOOL_OR(u.ultimo_push IS NOT NULL AND u.ultimo_push >= s.enviado_en) AS hubo_push,
                           MAX(u.ultimo_sms) AS ultimo_sms, MAX(u.ultimo_sms_fallido) AS ultimo_sms_fallido
                    FROM sin_leer s JOIN ultimos u ON u.conversacion_id = s.conversacion_id
                    GROUP BY s.conversacion_id
                    """,
                    {"ventana": ahora - timedelta(hours=VENTANA_HORAS)},
                )
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if not _falta_migracion(e):
                    logger.exception("avisos: no se pudo revisar las respuestas sin leer")
                return []
            filas = cur.fetchall()
            acciones = []
            for cid, sin_push_desde, sin_sms_desde, hubo_push, ultimo_sms, ultimo_sms_fallido in filas:
                if sin_push_desde and sin_push_desde <= ahora - timedelta(seconds=PUSH_ESPERA_SEG):
                    acciones.append({"conversacion_id": cid, "canal": "push", "motivo": "sin_leer_30s",
                                     "sin_push_desde": sin_push_desde})
                if (sin_sms_desde and sin_sms_desde <= ahora - timedelta(minutes=SMS_ESPERA_MIN)
                        and not hubo_push
                        and (ultimo_sms is None or ultimo_sms <= ahora - timedelta(minutes=SMS_TOPE_MIN))
                        and (ultimo_sms_fallido is None or ultimo_sms_fallido <= ahora - timedelta(minutes=SMS_REINTENTO_MIN))):
                    acciones.append({"conversacion_id": cid, "canal": "sms", "motivo": "sin_leer_3min"})
            if acciones:
                ids = list({a["conversacion_id"] for a in acciones})
                cur.execute(
                    """SELECT c.id, COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR'), u.sesion_token,
                              COALESCE(a.nombre_display, '')
                       FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                       LEFT JOIN asesores a ON a.id = c.asesor_id WHERE c.id = ANY(%s)""", (ids,))
                datos = {i: (t, s, n) for i, t, s, n in cur.fetchall()}
                for a in acciones:
                    t, s, n = datos.get(a["conversacion_id"], (None, None, ""))
                    a.update({"telefono": t, "sesion_token": s, "asesor": n})
        conn.rollback()
        return acciones
    finally:
        release_connection(conn)


def estadisticas(f_desde: datetime, f_hasta: datetime, campana_ids=None) -> dict:
    """Para el dashboard: qué tan rápido leen los clientes las respuestas del asesor y qué avisos se enviaron."""
    filtro, extra = "", []
    if campana_ids is not None:
        filtro, extra = " AND c.campana_id = ANY(%s)", [list(int(i) for i in campana_ids)]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Momento en que el cliente "vio" cada respuesta: la marca de lectura, o su siguiente mensaje si
            # contestó antes (le respondió al asesor, así que la leyó).
            base = f"""WITH r AS (
                    SELECT m.enviado_en,
                           LEAST(m.leido_en, (SELECT MIN(mu.enviado_en) FROM mensajes mu
                                              WHERE mu.conversacion_id = m.conversacion_id AND mu.emisor_tipo = 'usuario'
                                                AND mu.enviado_en > m.enviado_en)) AS visto_en
                    FROM mensajes m JOIN conversaciones c ON c.id = m.conversacion_id
                    WHERE m.emisor_tipo IN ('asesor', 'supervisor') AND m.enviado_en >= %s AND m.enviado_en < %s{filtro})"""
            cur.execute(
                base + """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE visto_en - enviado_en <= interval '5 minutes'),
                       COUNT(*) FILTER (WHERE visto_en IS NULL),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM visto_en - enviado_en))
                           FILTER (WHERE visto_en IS NOT NULL)
                FROM r""", [f_desde, f_hasta] + extra)
            total, en_5, nunca, mediana = cur.fetchone()
            cur.execute(
                base + """
                SELECT (enviado_en AT TIME ZONE 'America/Lima')::date, COUNT(*),
                       COUNT(*) FILTER (WHERE visto_en - enviado_en <= interval '5 minutes')
                FROM r GROUP BY 1 ORDER BY 1""", [f_desde, f_hasta] + extra)
            por_dia = [{"dia": d.isoformat(), "respuestas": n, "pct_5min": round(r * 100.0 / n, 1) if n else None}
                       for d, n, r in cur.fetchall()]
            avisos = {"disponible": True}
            try:
                cur.execute("SAVEPOINT sp_avisos")
                cur.execute(
                    f"""SELECT a.canal, COUNT(*) FILTER (WHERE a.ok), COUNT(*) FILTER (WHERE NOT a.ok),
                               COUNT(DISTINCT a.conversacion_id) FILTER (WHERE a.ok),
                               -- el cliente leyó alguna respuesta dentro de los 30 min siguientes al aviso
                               COUNT(*) FILTER (WHERE a.ok AND EXISTS (
                                   SELECT 1 FROM mensajes m WHERE m.conversacion_id = a.conversacion_id
                                     AND m.emisor_tipo IN ('asesor', 'supervisor')
                                     AND m.leido_en >= a.enviado_en AND m.leido_en < a.enviado_en + interval '30 minutes'))
                        FROM avisos_cliente a JOIN conversaciones c ON c.id = a.conversacion_id
                        WHERE a.enviado_en >= %s AND a.enviado_en < %s{filtro}
                        GROUP BY a.canal""", [f_desde, f_hasta] + extra)
                for canal, ok, fallidos, convs, leidos in cur.fetchall():
                    avisos[canal] = {"enviados": ok, "fallidos": fallidos, "conversaciones": convs,
                                     "leidos_30min": leidos, "pct_leidos_30min": round(leidos * 100.0 / ok, 1) if ok else None}
                cur.execute("RELEASE SAVEPOINT sp_avisos")
            except Exception:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_avisos")
                avisos = {"disponible": False}
        conn.rollback()
    finally:
        release_connection(conn)
    return {
        "respuestas": total,
        "leidas_5min": en_5, "pct_leidas_5min": round(en_5 * 100.0 / total, 1) if total else None,
        "nunca_leidas": nunca, "pct_nunca_leidas": round(nunca * 100.0 / total, 1) if total else None,
        "mediana_min": round(float(mediana) / 60, 1) if mediana is not None else None,
        "por_dia": por_dia,
        "avisos": avisos,
    }
