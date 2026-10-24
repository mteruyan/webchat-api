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
import logging
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
from db import get_connection, release_connection

TOKEN_TTL_HORAS = 12

# Hash bcrypt válido de una contraseña que nadie usa — se compara contra este
# cuando el usuario de /login NO existe, para que la respuesta tarde
# aproximadamente lo mismo (el costo de bcrypt) que cuando SÍ existe pero la
# contraseña es incorrecta. Sin esto, alguien podría medir el tiempo de
# respuesta para adivinar qué nombres de usuario son válidos.
HASH_DUMMY = bcrypt.hashpw(b"contrasena-que-nadie-usa", bcrypt.gensalt()).decode()

# ---------------------------------------------------------------------------
# Bloqueo temporal tras varios intentos fallidos de /login (fuerza bruta).
# El contador vive en la base (tabla login_intentos, ver migracion_login_intentos.sql)
# para que lo compartan TODAS las instancias de Cloud Run y sobreviva a los
# despliegues. Plan B: si la tabla no existe o la base falla, se usa un contador
# en memoria del proceso (como antes) para no dejar a nadie sin poder entrar.
# Además hay un límite por IP en Cloud Armor (regla 900).
# ---------------------------------------------------------------------------
MAX_INTENTOS_LOGIN = 5
BLOQUEO_MINUTOS = 15
OLVIDAR_FALLOS_HORAS = 24   # los fallos sueltos se olvidan tras este tiempo sin nuevos intentos
_intentos_login: dict[str, dict] = {}  # plan B: usuario (lowercase) -> {"fallos": int, "bloqueado_hasta": datetime | None}
logger = logging.getLogger("auth")


def _clave(usuario: str) -> str:
    return (usuario or "").strip().lower()[:100]


def _restante_memoria(clave: str) -> int:
    entrada = _intentos_login.get(clave)
    if not entrada or not entrada.get("bloqueado_hasta"):
        return 0
    restante = (entrada["bloqueado_hasta"] - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(restante))


def segundos_bloqueo_restante(usuario: str) -> int:
    """0 si el usuario puede intentar iniciar sesión; si no, los segundos que
    le quedan de bloqueo."""
    clave = _clave(usuario)
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT GREATEST(0, CEIL(EXTRACT(EPOCH FROM (bloqueado_hasta - NOW())))) "
                "FROM login_intentos WHERE usuario = %s AND bloqueado_hasta > NOW()",
                (clave,),
            )
            fila = cur.fetchone()
        conn.rollback()
        return int(fila[0]) if fila else 0
    except Exception:
        logger.warning("login_intentos no disponible: se usa el contador en memoria", exc_info=True)
        return _restante_memoria(clave)
    finally:
        if conn:
            release_connection(conn)


def registrar_intento_fallido(usuario: str) -> None:
    clave = _clave(usuario)
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            # El UPSERT bloquea la fila: dos instancias que fallen a la vez no se pisan.
            cur.execute(
                """INSERT INTO login_intentos (usuario, fallos, ultimo_fallo) VALUES (%s, 1, NOW())
                   ON CONFLICT (usuario) DO UPDATE SET
                     fallos = CASE WHEN login_intentos.ultimo_fallo < NOW() - make_interval(hours => %s)
                                   THEN 1 ELSE login_intentos.fallos + 1 END,
                     ultimo_fallo = NOW()
                   RETURNING fallos""",
                (clave, OLVIDAR_FALLOS_HORAS),
            )
            fallos = cur.fetchone()[0]
            if fallos >= MAX_INTENTOS_LOGIN:
                cur.execute(
                    "UPDATE login_intentos SET bloqueado_hasta = NOW() + make_interval(mins => %s), fallos = 0 "
                    "WHERE usuario = %s",
                    (BLOQUEO_MINUTOS, clave),
                )
            # Limpieza: filas viejas (p. ej. nombres de usuario inventados) no se acumulan.
            cur.execute(
                "DELETE FROM login_intentos WHERE ultimo_fallo < NOW() - make_interval(hours => %s) "
                "AND (bloqueado_hasta IS NULL OR bloqueado_hasta < NOW())",
                (OLVIDAR_FALLOS_HORAS,),
            )
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.warning("login_intentos no disponible: se usa el contador en memoria", exc_info=True)
        entrada = _intentos_login.setdefault(clave, {"fallos": 0, "bloqueado_hasta": None})
        entrada["fallos"] += 1
        if entrada["fallos"] >= MAX_INTENTOS_LOGIN:
            entrada["bloqueado_hasta"] = datetime.now(timezone.utc) + timedelta(minutes=BLOQUEO_MINUTOS)
            entrada["fallos"] = 0
    finally:
        if conn:
            release_connection(conn)


def registrar_login_exitoso(usuario: str) -> None:
    clave = _clave(usuario)
    _intentos_login.pop(clave, None)
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM login_intentos WHERE usuario = %s", (clave,))
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.warning("No se pudo limpiar login_intentos", exc_info=True)
    finally:
        if conn:
            release_connection(conn)


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
    except (jwt.PyJWTError, KeyError):
        return None


# Token intermedio del 2FA: se entrega tras acertar la contraseña y SOLO sirve para
# los endpoints /auth/2fa/*. No lleva "asesor_id", así que decodificar_token() lo rechaza
# y no abre ninguna consola ni WebSocket.
PRE2FA_TTL_MINUTOS = 5


def crear_token_pre2fa(asesor_id: int, fase: str) -> str:
    """fase: 'verificar' (ya tiene 2FA) | 'enrolar' (debe configurarlo ahora)."""
    payload = {
        "pre2fa": asesor_id,
        "fase": fase,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=PRE2FA_TTL_MINUTOS),
    }
    return jwt.encode(payload, os.environ["SECRET_KEY"], algorithm="HS256")


def decodificar_pre2fa(token: str) -> tuple[int, str] | None:
    try:
        payload = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return payload["pre2fa"], payload["fase"]
    except (jwt.PyJWTError, KeyError):
        return None
