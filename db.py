"""db.py — conexión compartida a Cloud SQL para todo el servicio webchat-api."""

import os
import psycopg2
from psycopg2 import pool

# Inicializamos un pool de conexiones global
_pool = None

def get_connection():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            1, 20,  # Mínimo 1, máximo 20 conexiones
            host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
    return _pool.getconn()

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
            1, 5,
            host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            # Default "sofi_whatsapp": deploy.sh nunca definía SOFI_DB_NAME, así que
            # os.environ["SOFI_DB_NAME"] lanzaba KeyError, el except de
            # _datos_sin_enmascarar_por_doi lo tragaba en silencio y el correo a las
            # asesoras salía siempre con los datos enmascarados.
            dbname=os.environ.get("SOFI_DB_NAME", "sofi_whatsapp"),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
    return _pool_sofi.getconn()

def release_connection_sofi(conn):
    if _pool_sofi and conn:
        _pool_sofi.putconn(conn)
