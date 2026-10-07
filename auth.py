"""
auth.py — autenticación de asesores para webchat-api.

Flujo:
1. POST /login {usuario, password} -> valida contra asesores.password_hash (bcrypt)
   -> devuelve un JWT firmado con SECRET_KEY, válido por TOKEN_TTL_HORAS.
2. El asesor se conecta a /ws/asesor?token=<jwt> en vez de mandar asesor_id
   directo — el token ya lleva el asesor_id adentro, firmado, así que no se
   puede falsificar sin conocer SECRET_KEY.

SECRET_KEY viene de la variable de entorno del mismo nombre (Secret Manager,
igual que DB_PASSWORD) — nunca hardcodear acá.
"""

import os
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta

TOKEN_TTL_HORAS = 12

# Hash bcrypt válido de una contraseña que nadie usa — se compara contra este
# cuando el usuario de /login NO existe, para que la respuesta tarde
# aproximadamente lo mismo (el costo de bcrypt) que cuando SÍ existe pero la
# contraseña es incorrecta. Sin esto, alguien podría medir el tiempo de
# respuesta para adivinar qué nombres de usuario son válidos.
HASH_DUMMY = bcrypt.hashpw(b"contrasena-que-nadie-usa", bcrypt.gensalt()).decode()

# ---------------------------------------------------------------------------
# Bloqueo temporal tras varios intentos fallidos de /login (fuerza bruta).
# En memoria del proceso: es una primera barrera simple; para un control más
# robusto (por IP, entre instancias) se recomienda Cloud Armor por delante de
# Cloud Run — ver informe de seguridad.
# ---------------------------------------------------------------------------
MAX_INTENTOS_LOGIN = 5
BLOQUEO_MINUTOS = 15
_intentos_login: dict[str, dict] = {}  # usuario (lowercase) -> {"fallos": int, "bloqueado_hasta": datetime | None}


def segundos_bloqueo_restante(usuario: str) -> int:
    """0 si el usuario puede intentar iniciar sesión; si no, los segundos que
    le quedan de bloqueo."""
    entrada = _intentos_login.get(usuario.lower())
    if not entrada or not entrada.get("bloqueado_hasta"):
        return 0
    restante = (entrada["bloqueado_hasta"] - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(restante))


def registrar_intento_fallido(usuario: str) -> None:
    clave = usuario.lower()
    entrada = _intentos_login.setdefault(clave, {"fallos": 0, "bloqueado_hasta": None})
    entrada["fallos"] += 1
    if entrada["fallos"] >= MAX_INTENTOS_LOGIN:
        entrada["bloqueado_hasta"] = datetime.now(timezone.utc) + timedelta(minutes=BLOQUEO_MINUTOS)
        entrada["fallos"] = 0


def registrar_login_exitoso(usuario: str) -> None:
    _intentos_login.pop(usuario.lower(), None)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        # password_hash todavía es el placeholder 'PENDIENTE' u otro valor no-bcrypt
        return False


def crear_token(asesor_id: int) -> str:
    payload = {
        "asesor_id": asesor_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HORAS),
    }
    return jwt.encode(payload, os.environ["SECRET_KEY"], algorithm="HS256")


# Vida corta, para los links de /archivo-receta y /conversacion-pdf. Esos
# links van en una URL (query string) para que funcionen como <a href> o
# <img src> directos — y una URL queda escrita en los logs de acceso de
# Cloud Run. Antes llevaban el mismo JWT de 12 horas de toda la sesión; ahora
# llevan uno de solo unos minutos, así que si alguien lee esa URL en un log,
# la ventana en la que el token todavía sirve es mínima.
TOKEN_ARCHIVO_TTL_MINUTOS = 5


def crear_token_archivo(asesor_id: int) -> str:
    payload = {
        "asesor_id": asesor_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=TOKEN_ARCHIVO_TTL_MINUTOS),
    }
    return jwt.encode(payload, os.environ["SECRET_KEY"], algorithm="HS256")


def decodificar_token(token: str) -> int | None:
    """Devuelve el asesor_id si el token es válido y no expiró; None si no."""
    try:
        payload = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return payload["asesor_id"]
    except jwt.PyJWTError:
        return None
