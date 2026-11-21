"""bus.py — Varias instancias de Cloud Run (nov-2026): se comunican por Postgres LISTEN/NOTIFY.

Problema que resuelve: con una sola instancia (--max-instances=1), cuando Cloud Run la retira (mantenimiento de su
infraestructura) no puede levantar otra mientras duren los WebSockets y todo lo nuevo recibe 429 "Rate exceeded"
(13/11 y 20/11). Con máximo 2 instancias levanta la segunda al instante, pero entonces un cliente puede estar
conectado a una instancia y su asesor a la otra. Este módulo une las instancias:

  1) Mensajes entre instancias: manager.enviar_a_* entrega a las conexiones de ESTA instancia y, si el destino no
     está aquí (o es para todos), lo publica con pg_notify; las demás lo entregan a sus conexiones.
     Un hilo publicador (en orden, sin frenar al servidor) y un hilo oyente (LISTEN), cada uno con su conexión.
     Eventos de más de ~7 KB (límite de NOTIFY) van por la tabla bus_eventos y se manda solo su id.
  2) Presencia compartida (tabla ws_presencia): quién está conectado en las otras instancias (widgets, asesores,
     supervisores, escuchas) y si el cliente está mirando el chat. Se lee cada PRESENCIA_CADA_SEG (caché).
  3) Líder único (pg_try_advisory_lock en la conexión del oyente): solo una instancia corre las tareas de fondo
     (asignar la cola, inactividad, encuestas, avisos...). Si se cae, otra toma el relevo en segundos.
  4) Latido de cada instancia (tabla instancias_webchat): el líder borra la presencia y cierra las sesiones de
     asesor de las instancias muertas (si no, quedarían asesores "fantasma" a los que se asigna).

BUS_ACTIVO=no (variable de entorno) lo apaga: todo funciona como con una sola instancia (esta es la líder).
Tablas: migracion_dos_instancias.sql.
"""

import json
import logging
import os
import queue
import select
import threading
import time
import asyncio

logger = logging.getLogger(__name__)

CANAL = "webchat_bus"
MAX_NOTIFY = 7000                 # el payload de NOTIFY no puede pasar de 8000 bytes
LLAVE_LIDER = 7720261120          # pg_advisory_lock de las tareas de fondo
LATIDO_CADA_SEG = 10
INSTANCIA_MUERTA_SEG = 45         # sin latido por más de esto = instancia caída
PRESENCIA_CADA_SEG = 5
LIDER_REVISAR_CADA_SEG = 5

ACTIVO = os.environ.get("BUS_ACTIVO", "si").strip().lower() not in ("no", "0", "false")

_instancia = None                 # monitoreo.INSTANCIA (se fija en iniciar)
_loop: asyncio.AbstractEventLoop | None = None
_cola_entrada: asyncio.Queue | None = None
_cola_salida: "queue.Queue" = queue.Queue(maxsize=20000)
_manejador = None                 # coroutine(mensaje: dict) que entrega un mensaje recibido
es_lider = not ACTIVO             # sin bus, esta instancia hace todo
_remotos = {"widget": {}, "asesor": {}, "supervisor": {}, "escucha_sup": {}, "escucha_ase": {}}
_listo = threading.Event()
estado = {"publicados": 0, "recibidos": 0, "errores": 0, "ultimo_error": "", "conectado": False}


def _conectar():
    """Conexión directa (fuera del pool), en modo autocommit."""
    import psycopg2
    from db import _OPCIONES
    opciones = dict(_OPCIONES, application_name="webchat-api-bus")
    conn = psycopg2.connect(host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
                            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASSWORD"], **opciones)
    conn.autocommit = True
    return conn


def _error(texto: str):
    estado["errores"] += 1
    estado["ultimo_error"] = texto[:300]


# ---------------------------------------------------------------------------
# Publicar (hilo único, en orden)
# ---------------------------------------------------------------------------
def publicar(tipo: str, destino=None, evento: dict | None = None, **extra):
    """Encola un mensaje para las OTRAS instancias. No bloquea: lo manda el hilo publicador."""
    if not ACTIVO:
        return
    msg = {"o": _instancia, "t": tipo, "d": destino, "e": evento}
    if extra:
        msg["x"] = extra
    try:
        _cola_salida.put_nowait(("notify", msg))
    except queue.Full:
        _error("cola de salida llena: se descartó un mensaje")


def sql(consulta: str, params: tuple = ()):
    """Encola un cambio de presencia (en orden con los mensajes; no bloquea)."""
    if not ACTIVO:
        return
    try:
        _cola_salida.put_nowait(("sql", (consulta, params)))
    except queue.Full:
        _error("cola de salida llena: se descartó un cambio de presencia")


def _hilo_publicador():
    conn = None
    proximo_latido = 0.0
    while True:
        try:
            if conn is None or conn.closed:
                conn = _conectar()
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM ws_presencia WHERE instancia = %s", (_instancia,))
                proximo_latido = 0.0
            ahora = time.monotonic()
            if ahora >= proximo_latido:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO instancias_webchat (instancia, iniciada_en, latido, lider)
                                   VALUES (%s, now(), now(), %s)
                                   ON CONFLICT (instancia) DO UPDATE SET latido = now(), lider = EXCLUDED.lider""",
                                (_instancia, es_lider))
                proximo_latido = ahora + LATIDO_CADA_SEG
            try:
                tipo, dato = _cola_salida.get(timeout=1.0)
            except queue.Empty:
                continue
            with conn.cursor() as cur:
                if tipo == "sql":
                    cur.execute(*dato)
                    continue
                texto = json.dumps(dato, ensure_ascii=False, default=str, separators=(",", ":"))
                if len(texto.encode("utf-8")) > MAX_NOTIFY:
                    cur.execute("INSERT INTO bus_eventos (payload) VALUES (%s) RETURNING id", (texto,))
                    texto = json.dumps({"o": _instancia, "ref": cur.fetchone()[0]})
                cur.execute("SELECT pg_notify(%s, %s)", (CANAL, texto))
                estado["publicados"] += 1
        except Exception as e:  # noqa: BLE001
            _error(f"publicador: {e}")
            logger.warning("Bus: error al publicar (%s); se reconecta", e)
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(2)


# ---------------------------------------------------------------------------
# Escuchar (hilo único) + liderazgo
# ---------------------------------------------------------------------------
def _hilo_oyente():
    global es_lider
    conn = None
    proxima_lider = 0.0
    while True:
        try:
            if conn is None or conn.closed:
                es_lider = False
                conn = _conectar()
                with conn.cursor() as cur:
                    cur.execute(f"LISTEN {CANAL}")
                estado["conectado"] = True
                _listo.set()
                logger.info("Bus: escuchando como instancia %s", _instancia)
                proxima_lider = 0.0
            ahora = time.monotonic()
            if ahora >= proxima_lider:
                with conn.cursor() as cur:
                    if not es_lider:
                        cur.execute("SELECT pg_try_advisory_lock(%s)", (LLAVE_LIDER,))
                        if cur.fetchone()[0]:
                            es_lider = True
                            logger.warning("Bus: la instancia %s es la LÍDER (tareas de fondo)", _instancia)
                    else:
                        cur.execute("SELECT 1")   # mantiene viva la conexión que sostiene el candado
                proxima_lider = ahora + LIDER_REVISAR_CADA_SEG
            if select.select([conn], [], [], 1.0) == ([], [], []):
                continue
            conn.poll()
            while conn.notifies:
                aviso = conn.notifies.pop(0)
                try:
                    msg = json.loads(aviso.payload)
                except ValueError:
                    continue
                if msg.get("o") == _instancia:
                    continue
                if "ref" in msg:
                    with conn.cursor() as cur:
                        cur.execute("SELECT payload FROM bus_eventos WHERE id = %s", (msg["ref"],))
                        fila = cur.fetchone()
                    if not fila:
                        continue
                    msg = json.loads(fila[0])
                estado["recibidos"] += 1
                if _loop is not None and _cola_entrada is not None:
                    _loop.call_soon_threadsafe(_cola_entrada.put_nowait, msg)
        except Exception as e:  # noqa: BLE001
            es_lider = False
            estado["conectado"] = False
            _error(f"oyente: {e}")
            logger.warning("Bus: se perdió la conexión del oyente (%s); se reconecta", e)
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(2)


async def _consumir():
    """Entrega en orden, en el event loop, lo que llega de las otras instancias."""
    while True:
        msg = await _cola_entrada.get()
        try:
            await _manejador(msg)
        except Exception:  # noqa: BLE001
            logger.warning("Bus: no se pudo entregar un mensaje (%s)", msg.get("t"), exc_info=True)


# ---------------------------------------------------------------------------
# Presencia de las otras instancias (caché)
# ---------------------------------------------------------------------------
def _hilo_presencia():
    from db import get_connection, release_connection
    while True:
        time.sleep(PRESENCIA_CADA_SEG)
        conn = None
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute("""SELECT p.tipo, p.clave, p.visible FROM ws_presencia p
                               JOIN instancias_webchat i ON i.instancia = p.instancia
                               WHERE p.instancia <> %s AND i.latido > now() - make_interval(secs => %s)""",
                            (_instancia, INSTANCIA_MUERTA_SEG))
                filas = cur.fetchall()
            conn.rollback()
            nuevo = {k: {} for k in _remotos}
            for tipo, clave, visible in filas:
                if tipo in nuevo:
                    nuevo[tipo][clave] = bool(visible)
            _remotos.update(nuevo)
        except Exception as e:  # noqa: BLE001
            _error(f"presencia: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if conn is not None:
                release_connection(conn)


def remotos(tipo: str) -> dict:
    """{clave: visible} de las otras instancias vivas (claves en texto)."""
    return _remotos.get(tipo, {})


def presencia(tipo: str, clave, conectado: bool, visible: bool = True):
    clave = str(clave)
    if conectado:
        sql("""INSERT INTO ws_presencia (instancia, tipo, clave, visible, actualizado_en)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (instancia, tipo, clave) DO UPDATE SET visible = EXCLUDED.visible, actualizado_en = now()""",
            (_instancia, tipo, clave, visible))
    else:
        sql("DELETE FROM ws_presencia WHERE instancia = %s AND tipo = %s AND clave = %s", (_instancia, tipo, clave))


# ---------------------------------------------------------------------------
# Limpieza (la corre la líder)
# ---------------------------------------------------------------------------
def limpiar_instancias_muertas() -> list:
    """Borra la presencia de las instancias caídas y cierra las sesiones de asesor que quedaron abiertas en ellas.
    Devuelve los asesor_id cuya sesión se cerró (para avisar a los supervisores)."""
    if not ACTIVO:
        return []
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from db import get_connection, release_connection
    ahora = datetime.now(ZoneInfo("America/Lima"))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT instancia FROM instancias_webchat
                           WHERE latido < now() - make_interval(secs => %s)""", (INSTANCIA_MUERTA_SEG,))
            muertas = [r[0] for r in cur.fetchall()]
            cerrados = []
            if muertas:
                cur.execute("DELETE FROM ws_presencia WHERE instancia = ANY(%s)", (muertas,))
                cur.execute("""UPDATE asesor_sesiones SET desconectado_en = %s
                               WHERE desconectado_en IS NULL AND instancia = ANY(%s) RETURNING asesor_id""",
                            (ahora, muertas))
                cerrados = sorted({r[0] for r in cur.fetchall()})
                for asesor_id in cerrados:
                    # Sin otra sesión abierta: se cierra también su pausa (como al desconectarse).
                    cur.execute("SELECT 1 FROM asesor_sesiones WHERE asesor_id = %s AND desconectado_en IS NULL",
                                (asesor_id,))
                    if cur.fetchone() is None:
                        cur.execute("UPDATE asesor_pausas SET fin = %s WHERE asesor_id = %s AND fin IS NULL",
                                    (ahora, asesor_id))
                        cur.execute("UPDATE asesores SET en_pausa = FALSE WHERE id = %s", (asesor_id,))
                cur.execute("DELETE FROM instancias_webchat WHERE latido < now() - interval '1 day'")
                logger.warning("Bus: instancias caídas %s; sesiones de asesor cerradas: %s", muertas, cerrados)
            cur.execute("DELETE FROM bus_eventos WHERE creado_en < now() - interval '10 minutes'")
            cur.execute("DELETE FROM widget_cids WHERE creado_en < now() - interval '2 days'")
        conn.commit()
        return cerrados
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("Bus: no se pudo limpiar las instancias caídas", exc_info=True)
        return []
    finally:
        release_connection(conn)


def cid_nuevo(clave: str, cid: str) -> bool | None:
    """True si el cid no se había visto en NINGUNA instancia; None si no se pudo comprobar."""
    if not ACTIVO:
        return None
    from db import get_connection, release_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO widget_cids (clave, cid) VALUES (%s, %s)
                           ON CONFLICT DO NOTHING RETURNING 1""", (clave[:120], cid[:64]))
            nuevo = cur.fetchone() is not None
        conn.commit()
        return nuevo
    except Exception:  # noqa: BLE001
        conn.rollback()
        return None
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
def iniciar(loop: asyncio.AbstractEventLoop, instancia: str, manejador):
    """Se llama al arrancar (startup de FastAPI). manejador: coroutine(msg) que entrega lo recibido."""
    global _loop, _cola_entrada, _manejador, _instancia
    _instancia = instancia
    if not ACTIVO:
        logger.info("Bus apagado (BUS_ACTIVO=no): una sola instancia, esta hace todo")
        return
    _loop = loop
    _cola_entrada = asyncio.Queue()
    _manejador = manejador
    loop.create_task(_consumir())
    for objetivo, nombre in ((_hilo_publicador, "bus-publica"), (_hilo_oyente, "bus-escucha"),
                             (_hilo_presencia, "bus-presencia")):
        threading.Thread(target=objetivo, name=nombre, daemon=True).start()
