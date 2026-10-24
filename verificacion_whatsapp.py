"""Verificación del celular "al revés" por WhatsApp (respaldo del SMS).

Flujo: el widget pide un código (VERIFICAR-123456) amarrado a la conversación y
al celular que la persona escribió. La persona lo envía por WhatsApp desde ESE
celular al número de Sanitas; el bot de Chattigo llama a
POST /verificacion/whatsapp con el número real del remitente (USER_IDENTIFIER) y
el texto. Aquí solo se comprueba y se marca "verificado" en la base; el widget
(que consulta su propio estado) es quien hace avanzar el flujo, así funciona
aunque Chattigo y el widget caigan en instancias distintas de Cloud Run.
"""
import re
import secrets
import logging
from datetime import datetime, timezone, timedelta

from db import get_connection, release_connection

logger = logging.getLogger("verificacion_whatsapp")
LIMA_TZ = timezone(timedelta(hours=-5))

VIGENCIA_SEG = 300            # el código dura 5 minutos
MAX_GENERADOS_30MIN = 5       # códigos que se pueden pedir por conversación cada 30 min
MAX_INTENTOS_OTRO_NUMERO = 3  # envíos desde un número distinto antes de invalidar el código
MAX_FALLOS_15MIN = 5          # códigos inválidos por número remitente cada 15 min

_PATRON = re.compile(r"VERIFICAR\s*[-–—_:\s]*\s*(\d{6})", re.IGNORECASE)


def ultimos9(celular) -> str:
    digitos = "".join(c for c in str(celular or "") if c.isdigit())
    return digitos[-9:]


def extraer_codigo(mensaje) -> str | None:
    m = _PATRON.search(str(mensaje or ""))
    return m.group(1) if m else None


def iniciar(conversacion_id: int, celular: str) -> dict:
    """Crea un código nuevo para esta conversación (invalida los anteriores sin usar)."""
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM verificaciones_whatsapp "
                "WHERE conversacion_id = %s AND creado_en > %s",
                (conversacion_id, ahora - timedelta(minutes=30)),
            )
            if cur.fetchone()[0] >= MAX_GENERADOS_30MIN:
                return {"ok": False, "motivo": "demasiados"}
            cur.execute(
                "UPDATE verificaciones_whatsapp SET expira_en = %s "
                "WHERE conversacion_id = %s AND verificado_en IS NULL AND expira_en > %s",
                (ahora, conversacion_id, ahora),
            )
            codigo = None
            for _ in range(20):
                candidato = f"{secrets.randbelow(1_000_000):06d}"
                cur.execute(
                    "SELECT 1 FROM verificaciones_whatsapp "
                    "WHERE codigo = %s AND consumido_en IS NULL AND expira_en > %s",
                    (candidato, ahora),
                )
                if not cur.fetchone():
                    codigo = candidato
                    break
            if codigo is None:
                return {"ok": False, "motivo": "error"}
            cur.execute(
                "INSERT INTO verificaciones_whatsapp (codigo, conversacion_id, celular, creado_en, expira_en) "
                "VALUES (%s, %s, %s, %s, %s)",
                (codigo, conversacion_id, celular, ahora, ahora + timedelta(seconds=VIGENCIA_SEG)),
            )
        conn.commit()
        return {"ok": True, "codigo": codigo, "expira_seg": VIGENCIA_SEG}
    except Exception:
        conn.rollback()
        logger.exception("No se pudo crear la verificación por WhatsApp (¿falta migracion_verificacion_whatsapp.sql?)")
        return {"ok": False, "motivo": "error"}
    finally:
        release_connection(conn)


def confirmar(celular_remitente: str, mensaje: str) -> dict:
    """Lo llama Chattigo. Devuelve {verificado, motivo, conversacion_id?}."""
    ahora = datetime.now(LIMA_TZ)
    remitente = ultimos9(celular_remitente)
    codigo = extraer_codigo(mensaje)
    if not remitente or not codigo:
        return {"verificado": False, "motivo": "formato"}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM verificaciones_whatsapp_fallos WHERE celular = %s AND creado_en > %s",
                (remitente, ahora - timedelta(minutes=15)),
            )
            if cur.fetchone()[0] >= MAX_FALLOS_15MIN:
                return {"verificado": False, "motivo": "bloqueado"}

            cur.execute(
                "SELECT id, conversacion_id, celular, expira_en, verificado_en, celular_remitente, intentos "
                "FROM verificaciones_whatsapp WHERE codigo = %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (codigo,),
            )
            fila = cur.fetchone()
            if not fila:
                cur.execute("INSERT INTO verificaciones_whatsapp_fallos (celular, creado_en) VALUES (%s, %s)",
                            (remitente, ahora))
                conn.commit()
                return {"verificado": False, "motivo": "no_existe"}

            vid, conv_id, celular_esperado, expira_en, verificado_en, rem_previo, intentos = fila
            if verificado_en is not None:
                # Enviado dos veces: si es la misma persona, se confirma de nuevo sin error.
                conn.commit()
                if ultimos9(rem_previo) == remitente:
                    return {"verificado": True, "motivo": "ya_verificado", "conversacion_id": conv_id}
                return {"verificado": False, "motivo": "ya_usado"}
            if expira_en <= ahora:
                conn.commit()
                return {"verificado": False, "motivo": "vencido"}
            if ultimos9(celular_esperado) != remitente:
                intentos += 1
                cur.execute(
                    "UPDATE verificaciones_whatsapp SET intentos = %s, "
                    "expira_en = CASE WHEN %s >= %s THEN %s ELSE expira_en END WHERE id = %s",
                    (intentos, intentos, MAX_INTENTOS_OTRO_NUMERO, ahora, vid),
                )
                cur.execute("INSERT INTO verificaciones_whatsapp_fallos (celular, creado_en) VALUES (%s, %s)",
                            (remitente, ahora))
                conn.commit()
                return {"verificado": False, "motivo": "celular_distinto"}

            cur.execute(
                "UPDATE verificaciones_whatsapp SET verificado_en = %s, celular_remitente = %s WHERE id = %s",
                (ahora, remitente, vid),
            )
        conn.commit()
        return {"verificado": True, "motivo": "ok", "conversacion_id": conv_id}
    except Exception:
        conn.rollback()
        logger.exception("Error al confirmar la verificación por WhatsApp")
        return {"verificado": False, "motivo": "error"}
    finally:
        release_connection(conn)


def estado(conversacion_id: int, codigo: str) -> str:
    """'pendiente' | 'verificado' | 'vencido' | 'consumido' | 'desconocido' (solo del dueño del código)."""
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expira_en, verificado_en, consumido_en FROM verificaciones_whatsapp "
                "WHERE codigo = %s AND conversacion_id = %s ORDER BY id DESC LIMIT 1",
                (codigo, conversacion_id),
            )
            fila = cur.fetchone()
    finally:
        release_connection(conn)
    if not fila:
        return "desconocido"
    expira_en, verificado_en, consumido_en = fila
    if consumido_en:
        return "consumido"
    if verificado_en:
        return "verificado"
    return "vencido" if expira_en <= ahora else "pendiente"


def consumir(conversacion_id: int, codigo: str) -> bool:
    """Marca el código como usado (un solo uso). True solo la primera vez y solo si ya estaba verificado."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE verificaciones_whatsapp SET consumido_en = %s "
                "WHERE codigo = %s AND conversacion_id = %s AND verificado_en IS NOT NULL "
                "AND consumido_en IS NULL RETURNING id",
                (datetime.now(LIMA_TZ), codigo, conversacion_id),
            )
            ok = cur.fetchone() is not None
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        logger.exception("No se pudo consumir la verificación por WhatsApp")
        return False
    finally:
        release_connection(conn)
