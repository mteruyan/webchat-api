"""espera_cola.py — cuánto le falta al cliente que espera un asesor y el aviso "Avísame cuando me toque".

El cliente espera en uno de dos lugares:
  - "cola": aún sin asesor (estado en_cola). FIFO por campaña.
      personas_antes = clientes de su campaña que entraron a la cola antes que él.
      ritmo = conversaciones de esa campaña asignadas en los últimos VENTANA_MIN minutos.
  - "bandeja": ya asignado, pero la asesora todavía no le escribe (con cupos altos, p. ej. 40, casi nadie pasa
    por la cola: la espera real está aquí).
      personas_antes = clientes de esa misma asesora que esperan su primera respuesta y llegaron antes.
      ritmo = conversaciones a las que esa asesora dio su primera respuesta en los últimos VENTANA_MIN minutos.
  minutos estimados = (personas_antes + 1) / ritmo, mostrado como rango ("entre 10 y 15 minutos"). Sin ritmo
  suficiente no se inventa un número: solo la posición. Las conversaciones pausadas no cuentan.

Aviso: si la espera estimada supera el umbral (cola_aviso_minutos, 15 por defecto) se le ofrece "Avísame cuando me
toque". Si lo pide, cuando quede 1 persona antes que él se le avisa por push y SMS con el enlace a su conversación;
si la asesora le responde antes, el SMS sale de inmediato. Columnas en conversaciones: migracion_aviso_turno.sql
(sin ellas, se muestra la estimación pero no se ofrece el aviso).
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

VENTANA_MIN = 90               # asignaciones recientes con las que se mide el ritmo de la campaña
MIN_ASIGNADAS = 3              # con menos que esto en la ventana no se estima (solo la posición)
UMBRAL_DEFECTO = 15            # minutos estimados desde los que se ofrece el aviso
SIN_ESTIMADO_OFRECER_DESDE = 3   # sin estimación: se ofrece el aviso si tiene al menos tantas personas antes
AVISAR_CON_PERSONAS_ANTES = 1  # se avisa cuando le queda esta cantidad de personas antes (o menos)
EN_BREVE_MIN = 3               # estimado menor a esto: "en breve"

_falta_migracion_avisada = False


def _redondear_rango(est: float) -> tuple[int, int]:
    """Rango honesto alrededor del estimado: ±25 %, de a 5 minutos cuando pasa de 10."""
    lo = max(1, math.floor(est * 0.75))
    hi = max(lo + 2, math.ceil(est * 1.25))
    if hi > 10:
        lo = max(5, 5 * math.floor(lo / 5))
        hi = max(lo + 5, 5 * math.ceil(hi / 5))
    return lo, hi


def _columnas_aviso(cur) -> bool:
    global _falta_migracion_avisada
    cur.execute("""SELECT count(*) FROM information_schema.columns
                   WHERE table_name = 'conversaciones' AND column_name = 'aviso_turno_pedido_en'""")
    hay = cur.fetchone()[0] > 0
    if not hay and not _falta_migracion_avisada:
        logger.warning("espera_cola: falta ejecutar migracion_aviso_turno.sql (no se ofrece el aviso de turno)")
        _falta_migracion_avisada = True
    return hay


def _con_push(cur, usuario_ids: list) -> set:
    if not usuario_ids:
        return set()
    try:
        cur.execute("SAVEPOINT espera_push")
        cur.execute("""SELECT DISTINCT usuario_id FROM app_dispositivos
                       WHERE usuario_id = ANY(%s) AND push_activo AND push_endpoint IS NOT NULL""", (usuario_ids,))
        r = {f[0] for f in cur.fetchall()}
        cur.execute("RELEASE SAVEPOINT espera_push")
        return r
    except Exception:  # noqa: BLE001 — sin la tabla de push, solo SMS
        cur.execute("ROLLBACK TO SAVEPOINT espera_push")
        return set()


def _celular_valido(tel) -> str | None:
    dig = "".join(ch for ch in str(tel or "") if ch.isdigit())[-9:]
    return dig if len(dig) == 9 and dig.startswith("9") else None


_SIN_RESPUESTA = """NOT EXISTS (SELECT 1 FROM mensajes m WHERE m.conversacion_id = c.id
                       AND m.emisor_tipo IN ('asesor', 'supervisor') AND m.enviado_en >= c.asignado_en)"""


def estado_colas(umbral: int = UMBRAL_DEFECTO, conversacion_id: int | None = None) -> list[dict]:
    """Una entrada por cliente que espera (en cola o en la bandeja de su asesora sin primera respuesta), o solo
    `conversacion_id`, con su posición, estimación y aviso."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            con_aviso = _columnas_aviso(cur)
            extra = ("c.aviso_turno_pedido_en, c.aviso_turno_enviado_en" if con_aviso else "NULL::timestamptz, NULL::timestamptz")
            cur.execute(
                f"""SELECT c.id, c.estado, c.campana_id, c.asesor_id, a.nombre_display, c.usuario_id, {extra},
                           COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR'), u.sesion_token
                    FROM conversaciones c
                    JOIN usuarios_webchat u ON u.id = c.usuario_id
                    LEFT JOIN asesores a ON a.id = c.asesor_id
                    WHERE c.estado = 'en_cola'
                       OR (c.estado = 'asignada' AND c.asignado_en IS NOT NULL AND c.pausada_en IS NULL
                           AND c.asesor_id IS NOT NULL AND {_SIN_RESPUESTA})
                    ORDER BY c.estado, c.campana_id, c.asesor_id, COALESCE(c.entrada_cola, c.asignado_en) NULLS LAST, c.id""")
            filas = cur.fetchall()
            if not filas:
                conn.rollback()
                return []
            ritmo_campana, ritmo_asesor = {}, {}
            if any(f[1] == "en_cola" for f in filas):
                cur.execute(
                    """SELECT campana_id, count(*) FROM conversaciones
                       WHERE asignado_en >= now() - make_interval(mins => %s) AND entrada_cola IS NOT NULL
                         AND asignado_en >= entrada_cola AND asignado_en - entrada_cola < interval '12 hours'
                       GROUP BY campana_id""", (VENTANA_MIN,))
                ritmo_campana = dict(cur.fetchall())
            asesores = list({f[3] for f in filas if f[1] == "asignada"})
            if asesores:
                # Primeras respuestas de cada asesora en la ventana (conversaciones asignadas en las últimas 12 h).
                cur.execute(
                    """SELECT c.asesor_id, count(*) FROM conversaciones c
                       WHERE c.asesor_id = ANY(%s) AND c.asignado_en >= now() - interval '12 hours'
                         AND (SELECT min(m.enviado_en) FROM mensajes m
                              WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('asesor', 'supervisor')
                                AND m.enviado_en >= c.asignado_en) >= now() - make_interval(mins => %s)
                       GROUP BY c.asesor_id""", (asesores, VENTANA_MIN))
                ritmo_asesor = dict(cur.fetchall())
            con_push = _con_push(cur, list({f[5] for f in filas if f[5]}))
        conn.rollback()
    finally:
        release_connection(conn)

    resultado = []
    antes_por_grupo: dict = {}
    for cid, estado, campana, asesor_id, asesor_nombre, usuario_id, pedido, enviado, telefono, sesion in filas:
        en_bandeja = estado == "asignada"
        grupo = ("asesor", asesor_id) if en_bandeja else ("campana", campana)
        antes = antes_por_grupo.get(grupo, 0)
        antes_por_grupo[grupo] = antes + 1
        if conversacion_id is not None and cid != conversacion_id:
            continue
        n = (ritmo_asesor.get(asesor_id, 0) if en_bandeja else ritmo_campana.get(campana, 0))
        est = None
        if n >= MIN_ASIGNADAS:
            est = min(240.0, (antes + 1) * VENTANA_MIN / n)
        celular = _celular_valido(telefono)
        canales = {"push": usuario_id in con_push, "sms": bool(celular and sesion)}
        tiene_canal = canales["push"] or canales["sms"]
        if est is not None:
            ofrecer = est > umbral
        else:
            ofrecer = antes >= SIN_ESTIMADO_OFRECER_DESDE
        e = {"conversacion_id": cid, "modo": "bandeja" if en_bandeja else "cola",
             "campana_id": campana, "asesor_id": asesor_id if en_bandeja else None,
             "asesor": (asesor_nombre or "").split()[0] if en_bandeja and asesor_nombre else None,
             "usuario_id": usuario_id,
             "personas_antes": antes, "estimado_min": round(est, 1) if est is not None else None,
             "en_breve": est is not None and est < EN_BREVE_MIN,
             "rango": list(_redondear_rango(est)) if est is not None and est >= EN_BREVE_MIN else None,
             "ofrecer_aviso": bool(con_aviso and tiene_canal and ofrecer and antes > AVISAR_CON_PERSONAS_ANTES),
             "aviso_pedido": bool(pedido), "aviso_enviado": bool(enviado),
             "canales": canales, "celular": celular, "sesion_token": sesion}
        # Si ya lo pidió, la tarjeta sigue mostrando el aviso aunque la espera baje del umbral.
        if pedido:
            e["ofrecer_aviso"] = True
        resultado.append(e)
    return resultado


def para_widget(e: dict | None) -> dict:
    """Lo que ve el cliente (sin token ni celular completo)."""
    if not e:
        return {}
    cel = e.get("celular")
    return {"modo": e["modo"], "asesor": e.get("asesor"),
            "personas_antes": e["personas_antes"], "en_breve": e["en_breve"], "rango": e["rango"],
            "ofrecer_aviso": e["ofrecer_aviso"], "aviso_pedido": e["aviso_pedido"],
            "canales": e["canales"], "celular_oculto": f"*** *** {cel[-3:]}" if cel else None,
            "avisar_con": AVISAR_CON_PERSONAS_ANTES}


def pedir_aviso(conversacion_id: int, pedir: bool = True) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if pedir:
                cur.execute("""UPDATE conversaciones SET aviso_turno_pedido_en = %s, aviso_turno_enviado_en = NULL,
                                                         aviso_turno_canales = NULL
                               WHERE id = %s AND estado IN ('en_cola', 'asignada')""", (datetime.now(LIMA_TZ), conversacion_id))
            else:
                cur.execute("""UPDATE conversaciones SET aviso_turno_pedido_en = NULL
                               WHERE id = %s AND aviso_turno_enviado_en IS NULL""", (conversacion_id,))
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("espera_cola: no se pudo guardar el pedido de aviso de %s", conversacion_id, exc_info=True)
        return False
    finally:
        release_connection(conn)


def respondidas_con_aviso_pendiente() -> list[dict]:
    """Pidió el aviso pero la asesora le respondió antes de que le tocara avisar (atendió en otro orden): se le
    avisa ya mismo, sin esperar los 3 minutos del SMS normal de "te respondió"."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if not _columnas_aviso(cur):
                conn.rollback()
                return []
            cur.execute(
                f"""SELECT c.id, c.usuario_id, COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR'),
                           u.sesion_token, COALESCE(a.nombre_display, '')
                    FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                    LEFT JOIN asesores a ON a.id = c.asesor_id
                    WHERE c.estado = 'asignada' AND c.aviso_turno_pedido_en IS NOT NULL
                      AND c.aviso_turno_enviado_en IS NULL AND c.aviso_turno_pedido_en >= now() - interval '12 hours'
                      AND c.asignado_en IS NOT NULL AND NOT {_SIN_RESPUESTA}""")
            filas = cur.fetchall()
        conn.rollback()
    finally:
        release_connection(conn)
    return [{"conversacion_id": f[0], "usuario_id": f[1], "celular": _celular_valido(f[2]), "sesion_token": f[3],
             "asesor": f[4], "respondio": True} for f in filas]


def marcar_enviado(conversacion_id: int, canales: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE conversaciones SET aviso_turno_enviado_en = %s, aviso_turno_canales = %s
                           WHERE id = %s""", (datetime.now(LIMA_TZ), canales[:40], conversacion_id))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("espera_cola: no se pudo marcar el aviso de %s", conversacion_id, exc_info=True)
    finally:
        release_connection(conn)
