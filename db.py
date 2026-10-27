"""db.py — conexión compartida a Cloud SQL para todo el servicio webchat-api."""

import os
import time
import psycopg2
from psycopg2 import pool

# Pools chicos a propósito: la instancia de Cloud SQL se comparte con sofi-apis y otros sistemas. Con UNA
# instancia de Cloud Run, el webchat usa como máximo 8 + 2. El 2026-10-23 se pasó de db-f1-micro (25 conexiones)
# a db-g1-small: estos números se pueden subir después de revisar las métricas de conexiones de esa semana.
_MAX_WEBCHAT = 8
_MAX_SOFI = 2
_ESPERA_MAX_SEG = 3.0   # si el pool está lleno, se espera un momento a que se libere una conexión
# Una conexión que estuvo quieta más de esto se prueba (SELECT 1) antes de entregarla: si la base se
# reinició (cambio de tipo de máquina, mantenimiento), la conexión vieja está muerta y fallaría al usarla.
_VALIDAR_SI_QUIETA_SEG = 10.0
_ultimo_uso: dict = {}   # id(conexión) -> time.monotonic() de cuando se devolvió al pool

# Inicializamos un pool de conexiones global
_pool = None

def get_connection():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            1, _MAX_WEBCHAT,  # Mínimo 1
            host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
    return _tomar(_pool)


def _tomar(p):
    """psycopg2 lanza PoolError al instante si el pool está lleno; aquí se espera un poco a que se libere una
    conexión antes de rendirse (un pico corto no debe verse como error para el cliente).
    Además nunca entrega una conexión muerta: si lo está, la cierra y pide otra (el pool abre una nueva)."""
    limite = time.monotonic() + _ESPERA_MAX_SEG
    while True:
        try:
            conn = p.getconn()
        except pool.PoolError:
            if time.monotonic() >= limite:
                raise
            time.sleep(0.05)
            continue
        if _esta_viva(conn):
            return conn
        _ultimo_uso.pop(id(conn), None)
        try:
            p.putconn(conn, close=True)
        except Exception:  # noqa: BLE001
            pass


def _esta_viva(conn) -> bool:
    if conn.closed:
        return False
    devuelta = _ultimo_uso.get(id(conn))
    if devuelta is None or time.monotonic() - devuelta < _VALIDAR_SI_QUIETA_SEG:
        return True   # recién abierta o usada hace poco: no hace falta probarla
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.rollback()   # que quede igual que antes de la prueba (sin transacción abierta)
        return True
    except Exception:  # noqa: BLE001
        return False


def _devolver(p, conn):
    if conn.closed:
        _ultimo_uso.pop(id(conn), None)
    else:
        _ultimo_uso[id(conn)] = time.monotonic()
    p.putconn(conn)


def release_connection(conn):
    if _pool and conn:
        _devolver(_pool, conn)


# Segundo pool, hacia la base "sofi_whatsapp" -- MISMA instancia Cloud SQL
# (sanitas-recetas-prod) y mismas credenciales que la base "webchat" de
# arriba, pero otra base de datos (ahí vive sofi_usuarios, con los datos del
# paciente SIN enmascarar). Se usa solo para el correo interno a las
# asesoras -- ver api_handlers._datos_sin_enmascarar_por_doi.
_pool_sofi = None

def get_connection_sofi():
    global _pool_sofi
    if _pool_sofi is None:
        _pool_sofi = pool.ThreadedConnectionPool(
            1, _MAX_SOFI,
            host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            # Default "sofi_whatsapp": deploy.sh nunca definía SOFI_DB_NAME, así que
            # os.environ["SOFI_DB_NAME"] lanzaba KeyError, el except de
            # _datos_sin_enmascarar_por_doi lo tragaba en silencio y el correo a las
            # asesoras salía siempre con los datos enmascarados.
            dbname=os.environ.get("SOFI_DB_NAME", "sofi_whatsapp"),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
    return _tomar(_pool_sofi)

def release_connection_sofi(conn):
    if _pool_sofi and conn:
        _devolver(_pool_sofi, conn)
