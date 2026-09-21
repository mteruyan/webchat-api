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


def decodificar_token(token: str) -> int | None:
    """Devuelve el asesor_id si el token es válido y no expiró; None si no."""
    try:
        payload = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return payload["asesor_id"]
    except jwt.PyJWTError:
        return None
