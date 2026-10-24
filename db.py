"""db.py — conexión compartida a Cloud SQL para todo el servicio webchat-api."""

import os
import time
import psycopg2
from psycopg2 import pool

# Pools chicos a propósito: la instancia de Cloud SQL (db-f1-micro) solo admite 25 conexiones EN TOTAL y la
# comparten sofi-apis y otros sistemas. Con UNA instancia de Cloud Run, el webchat usa como máximo 8 + 2.
# Si se sube el tipo de máquina de Cloud SQL, estos números se pueden volver a subir.
_MAX_WEBCHAT = 8
_MAX_SOFI = 2
_ESPERA_MAX_SEG = 3.0   # si el pool está lleno, se espera un momento a que se libere una conexión

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
    conexión antes de rendirse (un pico corto no debe verse como error para el cliente)."""
    limite = time.monotonic() + _ESPERA_MAX_SEG
    while True:
        try:
            return p.getconn()
        except pool.PoolError:
            if time.monotonic() >= limite:
                raise
            time.sleep(0.05)


def release_connection(conn):
    if _pool and conn:
        _pool.putconn(conn)


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
        _pool_sofi.putconn(conn)
