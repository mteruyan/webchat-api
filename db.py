"""db.py — conexión compartida a Cloud SQL para todo el servicio webchat-api."""

import asyncio
import logging
import os
import threading
import time
from psycopg2 import pool

logger = logging.getLogger(__name__)

# La instancia de Cloud SQL (db-g1-small) se comparte con sofi-apis y otros sistemas. Semana del 23 al 30/10:
# pico de 17 conexiones en total (webchat 7 de 8, sofi_whatsapp 11). Con 8 el pool del webchat se llenaba en
# los picos; 20 + 3 deja margen y sigue lejos del máximo de la instancia (revisar con SHOW max_connections;).
_MAX_WEBCHAT = 20
_MAX_SOFI = 3
# Si el pool está lleno se espera a que se libere una conexión. En un hilo de trabajo se puede esperar
# (no frena a nadie más); en el hilo principal del servidor (el que atiende los WebSockets y todas las
# peticiones async) esperar congela TODO el servicio, así que ahí se espera muy poco y se falla rápido.
_ESPERA_MAX_SEG = 3.0
_ESPERA_MAX_HILO_PRINCIPAL_SEG = 0.1
# Una conexión que estuvo quieta más de esto se prueba (SELECT 1) antes de entregarla: si la base se
# reinició (cambio de tipo de máquina, mantenimiento), la conexión vieja está muerta y fallaría al usarla.
_VALIDAR_SI_QUIETA_SEG = 10.0
_ultimo_uso: dict = {}   # id(conexión) -> time.monotonic() de cuando se devolvió al pool

# Límites de tiempo (el 30/10 el servicio quedó 27 minutos colgado sin usar CPU: nada cortaba una espera).
#   connect_timeout: abrir una conexión nueva no puede tardar más de 5 s.
#   statement_timeout: ninguna consulta puede pasar de 30 s (las del dashboard son las más pesadas).
#   idle_in_transaction_session_timeout: una conexión que quedó con una transacción abierta y sin uso
#   por 5 minutos la cierra la propia base (libera bloqueos de filas y la conexión).
# Las conexiones van por el socket local de Cloud SQL (/cloudsql/...), no por TCP: los keepalives de TCP
# no aplican ahí.
_OPCIONES = {
    "connect_timeout": 5,
    "options": "-c statement_timeout=30000 -c idle_in_transaction_session_timeout=300000",
    "application_name": "webchat-api",
}

_ultimo_aviso_lleno = 0.0
_aviso_lock = threading.Lock()

# Inicializamos un pool de conexiones global
_pool = None
_pool_lock = threading.Lock()


def get_connection():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pool.ThreadedConnectionPool(
                    1, _MAX_WEBCHAT,  # Mínimo 1
                    host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
                    dbname=os.environ["DB_NAME"],
                    user=os.environ["DB_USER"],
                    password=os.environ["DB_PASSWORD"],
                    **_OPCIONES,
                )
    return _tomar(_pool, "webchat")


def _en_hilo_principal() -> bool:
    """True si se está corriendo dentro del event loop (hilo principal del servidor)."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _avisar_pool_lleno(nombre: str, en_principal: bool):
    """Deja constancia en el log (como máximo una vez por minuto) de que el pool se llenó."""
    global _ultimo_aviso_lleno
    ahora = time.monotonic()
    with _aviso_lock:
        if ahora - _ultimo_aviso_lleno < 60:
            return
        _ultimo_aviso_lleno = ahora
    logger.warning("Pool de BD '%s' lleno (%s): %s", nombre,
                   "hilo principal" if en_principal else "hilo de trabajo", estado_pools())


def _tomar(p, nombre: str = "webchat"):
    """psycopg2 lanza PoolError al instante si el pool está lleno; aquí se espera un poco a que se libere una
    conexión antes de rendirse (un pico corto no debe verse como error para el cliente).
    Además nunca entrega una conexión muerta: si lo está, la cierra y pide otra (el pool abre una nueva)."""
    en_principal = _en_hilo_principal()
    limite = time.monotonic() + (_ESPERA_MAX_HILO_PRINCIPAL_SEG if en_principal else _ESPERA_MAX_SEG)
    avisado = False
    while True:
        try:
            conn = p.getconn()
        except pool.PoolError:
            if not avisado:
                _avisar_pool_lleno(nombre, en_principal)
                avisado = True
            if time.monotonic() >= limite:
                raise
            time.sleep(0.02 if en_principal else 0.05)
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


def estado_pools() -> str:
    """Conexiones en uso / máximo de cada pool (para los logs de diagnóstico)."""
    partes = []
    for nombre, p, maximo in (("webchat", _pool, _MAX_WEBCHAT), ("sofi", _pool_sofi, _MAX_SOFI)):
        if p is not None:
            partes.append(f"{nombre} {len(getattr(p, '_used', {}))}/{maximo}")
    return ", ".join(partes) or "sin pools abiertos"


# Segundo pool, hacia la base "sofi_whatsapp" -- MISMA instancia Cloud SQL
# (sanitas-recetas-prod) y mismas credenciales que la base "webchat" de
# arriba, pero otra base de datos (ahí vive sofi_usuarios, con los datos del
# paciente SIN enmascarar). Se usa solo para el correo interno a las
# asesoras -- ver api_handlers._datos_sin_enmascarar_por_doi.
_pool_sofi = None


def get_connection_sofi():
    global _pool_sofi
    if _pool_sofi is None:
        with _pool_lock:
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
                    password=os.environ["DB_PASSWORD"],
                    **_OPCIONES,
                )
    return _tomar(_pool_sofi, "sofi")


def release_connection_sofi(conn):
    if _pool_sofi and conn:
        _devolver(_pool_sofi, conn)
