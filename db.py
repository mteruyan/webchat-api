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
