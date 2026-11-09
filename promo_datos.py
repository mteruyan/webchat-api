"""promo_datos.py — invitación a actualizar celular y correo en la landing de Sanitas (actualizacion_datos_url).

El webchat no está integrado con la base de clientes de Sanitas: solo invita a actualizar los datos en la landing
oficial, en tres momentos en que no estorba:
  - "espera":   mientras espera al asesor (en la tarjeta de espera);
  - "cierre":   al terminar la conversación con el asesor;
  - "celular":  justo después de validar su celular con el código SMS.

Para no cansar al cliente (por usuario del webchat):
  - como máximo una vez por conversación;
  - si hizo clic o eligió "Ahora no", no se le vuelve a mostrar en DIAS_SILENCIO días;
  - como máximo MAX_VISTAS_30D veces en 30 días aunque no responda.

Eventos en actualizacion_datos_eventos (migracion_actualizacion_datos.sql): mostrado / clic / cerrado. Sin la
tabla no se muestra nada (no hay cómo respetar la frecuencia).
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

URL_DEFECTO = "https://actualizaciondatos.sanitasperu.com/"
LUGARES = ("espera", "cierre", "celular")
EVENTOS = ("clic", "cerrado")
DIAS_SILENCIO_DEFECTO = 90
MAX_VISTAS_30D = 3

TEXTOS = {
    "espera": ("Mientras esperas: ¿tu celular y correo están al día?",
               "Actualízalos en 1 minuto en la página oficial de Sanitas. Se abre en otra pestaña y no pierdes tu lugar."),
    "cierre": ("¿Tu celular y correo están al día?",
               "Así te llegan bien los avisos de Sanitas. Actualízalos en 1 minuto en la página oficial."),
    "celular": ("¿Cambiaste de número?",
                "Asegúrate de que Sanitas también tenga tu celular y correo actuales. Toma 1 minuto en la página oficial."),
}

_falta_tabla_avisada = False


def _avisar_falta_tabla():
    global _falta_tabla_avisada
    if not _falta_tabla_avisada:
        logger.warning("promo_datos: falta ejecutar migracion_actualizacion_datos.sql (no se invita a actualizar datos)")
        _falta_tabla_avisada = True


def url_con_origen(url: str, lugar: str) -> str:
    """Agrega utm_* para que el área dueña de la landing pueda medir cuántos llegan desde el webchat."""
    try:
        p = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
        q += [("utm_source", "webchat"), ("utm_medium", "chat"), ("utm_campaign", "actualizacion_datos"),
              ("utm_content", lugar)]
        return urlunsplit((p.scheme, p.netloc, p.path or "/", urlencode(q), p.fragment))
    except Exception:  # noqa: BLE001
        return url


def decidir(usuario_id: int, conversacion_id: int, lugar: str, url: str, dias_silencio: int) -> dict | None:
    """Si corresponde mostrar la invitación, la registra como mostrada y devuelve lo que ve el cliente."""
    if lugar not in LUGARES or not usuario_id or not url:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT
                         count(*) FILTER (WHERE conversacion_id = %s AND evento = 'mostrado'),
                         count(*) FILTER (WHERE evento IN ('clic', 'cerrado') AND creado_en >= now() - make_interval(days => %s)),
                         count(*) FILTER (WHERE evento = 'mostrado' AND creado_en >= now() - interval '30 days')
                       FROM actualizacion_datos_eventos WHERE usuario_id = %s""",
                    (conversacion_id, dias_silencio, usuario_id))
            except Exception:  # noqa: BLE001 — falta la migración
                conn.rollback()
                _avisar_falta_tabla()
                return None
            en_conv, respondio, vistas = cur.fetchone()
            if en_conv or respondio or vistas >= MAX_VISTAS_30D:
                conn.rollback()
                return None
            cur.execute("""INSERT INTO actualizacion_datos_eventos (usuario_id, conversacion_id, lugar, evento, creado_en)
                           VALUES (%s, %s, %s, 'mostrado', %s)""",
                        (usuario_id, conversacion_id, lugar, datetime.now(LIMA_TZ)))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("promo_datos: no se pudo decidir para %s", conversacion_id, exc_info=True)
        return None
    finally:
        release_connection(conn)
    titulo, texto = TEXTOS[lugar]
    return {"lugar": lugar, "url": url_con_origen(url, lugar), "titulo": titulo, "texto": texto}


def registrar(usuario_id: int, conversacion_id: int, lugar: str, evento: str):
    if evento not in EVENTOS or lugar not in LUGARES or not usuario_id:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO actualizacion_datos_eventos (usuario_id, conversacion_id, lugar, evento, creado_en)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (usuario_id, conversacion_id, lugar, evento, datetime.now(LIMA_TZ)))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("promo_datos: no se pudo registrar %s/%s", lugar, evento, exc_info=True)
    finally:
        release_connection(conn)


def estadisticas() -> dict | None:
    """Últimos 30 días por lugar: mostradas, clics y "Ahora no". None si falta la tabla."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""SELECT lugar, evento, count(*), count(DISTINCT usuario_id)
                               FROM actualizacion_datos_eventos WHERE creado_en >= now() - interval '30 days'
                               GROUP BY lugar, evento""")
            except Exception:  # noqa: BLE001
                conn.rollback()
                return None
            filas = cur.fetchall()
        conn.rollback()
    finally:
        release_connection(conn)
    r = {lugar: {"mostrado": 0, "clic": 0, "cerrado": 0, "clientes_clic": 0} for lugar in LUGARES}
    for lugar, evento, n, clientes in filas:
        if lugar in r and evento in r[lugar]:
            r[lugar][evento] = n
            if evento == "clic":
                r[lugar]["clientes_clic"] = clientes
    return r
