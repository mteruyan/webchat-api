"""
totp.py — verificación en dos pasos (2FA) con aplicaciones autenticadoras (TOTP, RFC 6238).

Compatible con Google Authenticator, Microsoft Authenticator, Authy, 1Password, etc.
(SHA-1, 6 dígitos, 30 s). No requiere librerías extra para el cálculo del código.

- El secreto se guarda CIFRADO (Fernet, clave derivada de SECRET_KEY) en asesores.totp_secreto.
- Anti-reutilización: se guarda el último paso de tiempo aceptado (totp_ultimo_paso); un
  mismo código no sirve dos veces.
- 10 códigos de respaldo de un solo uso, guardados solo como HMAC (no se pueden leer).
- Si la migración (migracion_2fa.sql) no se ha ejecutado, todo se comporta como "2FA apagado".
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from db import get_connection, release_connection

logger = logging.getLogger("totp")

EMISOR = "Sanitas Webchat"
PASO_SEGUNDOS = 30
DIGITOS = 6
VENTANA = 1            # tolera ±30 s de desfase de reloj
CANTIDAD_RESPALDO = 10


# ---------------------------------------------------------------- criptografía
def _clave_maestra() -> bytes:
    return hashlib.sha256(b"2fa-totp|" + os.environ["SECRET_KEY"].encode()).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_clave_maestra()))


def cifrar(secreto: str) -> str:
    return _fernet().encrypt(secreto.encode()).decode()


def descifrar(cifrado: str) -> str | None:
    try:
        return _fernet().decrypt(cifrado.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _hash_respaldo(codigo: str) -> str:
    limpio = codigo.replace("-", "").replace(" ", "").lower()
    return hmac.new(_clave_maestra(), limpio.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- TOTP puro
def generar_secreto() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _codigo_en_paso(secreto: str, paso: int) -> str:
    clave = base64.b32decode(secreto + "=" * (-len(secreto) % 8), casefold=True)
    mac = hmac.new(clave, struct.pack(">Q", paso), hashlib.sha1).digest()
    desp = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[desp:desp + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITOS)
    return str(num).zfill(DIGITOS)


def paso_actual(ahora: float | None = None) -> int:
    return int((time.time() if ahora is None else ahora) // PASO_SEGUNDOS)


def verificar_codigo(secreto: str, codigo: str, ultimo_paso: int | None = None,
                     ahora: float | None = None) -> int | None:
    """Devuelve el paso de tiempo aceptado, o None si el código no vale
    (incorrecto, o ya usado: paso <= ultimo_paso)."""
    codigo = "".join(c for c in (codigo or "") if c.isdigit())
    if len(codigo) != DIGITOS:
        return None
    base = paso_actual(ahora)
    for paso in range(base - VENTANA, base + VENTANA + 1):
        if ultimo_paso is not None and paso <= ultimo_paso:
            continue
        if hmac.compare_digest(_codigo_en_paso(secreto, paso), codigo):
            return paso
    return None


def uri_otpauth(secreto: str, usuario: str) -> str:
    etiqueta = quote(f"{EMISOR}:{usuario}")
    return (f"otpauth://totp/{etiqueta}?secret={secreto}&issuer={quote(EMISOR)}"
            f"&algorithm=SHA1&digits={DIGITOS}&period={PASO_SEGUNDOS}")


def qr_data_uri(texto: str) -> str:
    import segno
    return segno.make(texto, error="m").png_data_uri(scale=6, border=2, dark="#0A2540")


def generar_respaldo() -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(3)}" for _ in range(CANTIDAD_RESPALDO)]


# ------------------------------------------------------------------ base de datos
def obtener_estado(asesor_id: int) -> dict:
    """{'disponible': bool, 'activo': bool, 'secreto': str|None, 'ultimo_paso': int|None}.
    disponible=False si las columnas aún no existen (migración sin ejecutar)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT totp_activo, totp_secreto, totp_ultimo_paso FROM asesores WHERE id = %s",
                        (asesor_id,))
            fila = cur.fetchone()
        if not fila:
            return {"disponible": True, "activo": False, "secreto": None, "ultimo_paso": None}
        secreto = descifrar(fila[1]) if fila[1] else None
        return {"disponible": True, "activo": bool(fila[0]) and secreto is not None,
                "secreto": secreto, "ultimo_paso": fila[2]}
    except Exception:
        conn.rollback()
        return {"disponible": False, "activo": False, "secreto": None, "ultimo_paso": None}
    finally:
        release_connection(conn)


def guardar_pendiente(asesor_id: int, secreto: str) -> None:
    """Guarda un secreto nuevo SIN activarlo (se activa al confirmar un código)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET totp_secreto = %s, totp_activo = FALSE, "
                        "totp_ultimo_paso = NULL, totp_respaldo = NULL WHERE id = %s",
                        (cifrar(secreto), asesor_id))
        conn.commit()
    finally:
        release_connection(conn)


def activar(asesor_id: int, paso: int) -> list[str]:
    """Activa el 2FA y devuelve los códigos de respaldo (se muestran UNA sola vez)."""
    codigos = generar_respaldo()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET totp_activo = TRUE, totp_ultimo_paso = %s, totp_respaldo = %s "
                        "WHERE id = %s",
                        (paso, json.dumps([_hash_respaldo(c) for c in codigos]), asesor_id))
        conn.commit()
    finally:
        release_connection(conn)
    return codigos


def marcar_paso(asesor_id: int, paso: int) -> bool:
    """Registra el paso usado; False si otra petición ya lo consumió (carrera/reutilización)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET totp_ultimo_paso = %s "
                        "WHERE id = %s AND (totp_ultimo_paso IS NULL OR totp_ultimo_paso < %s)",
                        (paso, asesor_id, paso))
            ok = cur.rowcount == 1
        conn.commit()
        return ok
    finally:
        release_connection(conn)


def consumir_respaldo(asesor_id: int, codigo: str) -> bool:
    """Un código de respaldo sirve una sola vez."""
    limpio = (codigo or "").replace("-", "").replace(" ", "").lower()
    if len(limpio) != 10:
        return False
    h = _hash_respaldo(limpio)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT totp_respaldo FROM asesores WHERE id = %s FOR UPDATE", (asesor_id,))
            fila = cur.fetchone()
            lista = fila[0] if fila and fila[0] else []
            if isinstance(lista, str):
                lista = json.loads(lista)
            restantes = [x for x in lista if not hmac.compare_digest(x, h)]
            if len(restantes) == len(lista):
                conn.rollback()
                return False
            cur.execute("UPDATE asesores SET totp_respaldo = %s WHERE id = %s",
                        (json.dumps(restantes), asesor_id))
        conn.commit()
        return True
    finally:
        release_connection(conn)


def desactivar(asesor_id: int) -> None:
    """Quita el 2FA de una cuenta (lo usa el supervisor si la persona perdió su celular)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET totp_activo = FALSE, totp_secreto = NULL, "
                        "totp_ultimo_paso = NULL, totp_respaldo = NULL WHERE id = %s", (asesor_id,))
        conn.commit()
    finally:
        release_connection(conn)


def estado_por_ids() -> set[int]:
    """Ids de asesores con 2FA activo (para mostrarlo en la administración)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM asesores WHERE totp_activo")
            return {r[0] for r in cur.fetchall()}
    except Exception:
        conn.rollback()
        return set()
    finally:
        release_connection(conn)
