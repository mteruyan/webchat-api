"""monitoreo.py — rendimiento del servidor del webchat para el dashboard (vista «Rendimiento»).

El vigía de main.py llama a este módulo:
  - muestrear(...) cada 5 segundos: memoria, CPU y conexiones (se guarda el máximo de cada ventana);
  - guardar_resumen(...) cada 5 minutos: una fila en monitoreo_servidor con lo medido en esos 5 minutos;
  - registrar_evento(...) al arrancar el servidor y antes de que el vigía lo reinicie por un cuelgue.
MedidorPeticiones (middleware ASGI) cuenta las peticiones HTTP, sus errores 5xx y su demora (los WebSockets no
pasan por aquí). Todo es barato: unas sumas en memoria y una fila cada 5 minutos.
Si falta la migración (migracion_monitoreo.sql), no se guarda nada y el servidor sigue igual.
"""
import logging
import os
import resource
import threading
import time
import uuid

from db import get_connection, release_connection

logger = logging.getLogger(__name__)

INSTANCIA = uuid.uuid4().hex[:8]                       # distingue procesos (un reinicio = instancia nueva)
REVISION = os.environ.get("K_REVISION", "local")      # Cloud Run: revisión desplegada (cambia con cada deploy)
DIAS_GUARDADOS = 90
_INICIO = time.time()

_lock = threading.Lock()
_ventana = {"peticiones": 0, "errores_5xx": 0, "latencias": [], "memoria_max": 0.0, "cpu_max": 0.0,
            "cpu_suma": 0.0, "cpu_muestras": 0, "bd_max": 0, "ia_saturado": 0}
_MAX_LATENCIAS = 20000
_ultimo = {"memoria_mb": None, "cpu_pct": None, "cpu_t": None, "cpu_reloj": None, "fuente_memoria": None}
_aviso_migracion = False


# ---------------------------------------------------------------------------
# Peticiones HTTP
# ---------------------------------------------------------------------------
class MedidorPeticiones:
    """Middleware ASGI: cuenta cada petición HTTP, sus respuestas 5xx y cuánto tardó (hasta enviar los encabezados
    de la respuesta). No toca el contenido ni los WebSockets."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        inicio = time.perf_counter()
        estado = {"codigo": 500}

        async def enviar(mensaje):
            if mensaje.get("type") == "http.response.start":
                estado["codigo"] = mensaje.get("status", 500)
                estado["ms"] = (time.perf_counter() - inicio) * 1000
            await send(mensaje)

        try:
            await self.app(scope, receive, enviar)
        finally:
            ms = estado.get("ms", (time.perf_counter() - inicio) * 1000)
            with _lock:
                _ventana["peticiones"] += 1
                if estado["codigo"] >= 500:
                    _ventana["errores_5xx"] += 1
                if len(_ventana["latencias"]) < _MAX_LATENCIAS:
                    _ventana["latencias"].append(ms)


def contar_saturacion_ia():
    """planes.py avisa cada vez que Google responde «saturado» (429/503)."""
    with _lock:
        _ventana["ia_saturado"] += 1


# ---------------------------------------------------------------------------
# Memoria y CPU del contenedor
# ---------------------------------------------------------------------------
def _leer_numero(ruta: str):
    try:
        with open(ruta) as f:
            texto = f.read().strip()
        return None if texto in ("", "max") else int(texto)
    except (OSError, ValueError):
        return None


def memoria_mb() -> tuple:
    """(MB en uso, de dónde se leyó). Prioriza la memoria del contenedor (cgroup: lo mismo que mide Cloud Run, incluye
    los procesos que leen PDF); si no está disponible, la del proceso principal."""
    for ruta in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        valor = _leer_numero(ruta)
        if valor:
            return valor / 1048576, "contenedor"
    try:
        with open("/proc/self/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return int(linea.split()[1]) / 1024, "proceso"
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, "proceso (pico)"


def memoria_limite_mb():
    for ruta in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        valor = _leer_numero(ruta)
        if valor and valor < 1 << 40:   # sin límite real = número gigante
            return valor / 1048576
    return None


def _cpu_segundos() -> float:
    yo = resource.getrusage(resource.RUSAGE_SELF)
    hijos = resource.getrusage(resource.RUSAGE_CHILDREN)   # procesos que leen PDF (pypdf)
    return yo.ru_utime + yo.ru_stime + hijos.ru_utime + hijos.ru_stime


def muestrear(bd_en_uso: int):
    """Cada 5 s (desde el hilo del vigía): memoria, CPU de los últimos 5 s y conexiones de BD en uso."""
    mem, fuente = memoria_mb()
    ahora, cpu = time.monotonic(), _cpu_segundos()
    pct = None
    if _ultimo["cpu_t"] is not None and ahora > _ultimo["cpu_reloj"]:
        pct = max(0.0, (cpu - _ultimo["cpu_t"]) / (ahora - _ultimo["cpu_reloj"]) * 100)
    _ultimo.update(memoria_mb=mem, cpu_pct=pct, cpu_t=cpu, cpu_reloj=ahora, fuente_memoria=fuente)
    with _lock:
        _ventana["memoria_max"] = max(_ventana["memoria_max"], mem)
        _ventana["bd_max"] = max(_ventana["bd_max"], bd_en_uso or 0)
        if pct is not None:
            _ventana["cpu_max"] = max(_ventana["cpu_max"], pct)
            _ventana["cpu_suma"] += pct
            _ventana["cpu_muestras"] += 1


def _percentil(valores: list, p: float):
    if not valores:
        return None
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


# ---------------------------------------------------------------------------
# Guardado
# ---------------------------------------------------------------------------
def _falta_tabla(e: Exception) -> bool:
    global _aviso_migracion
    if "monitoreo_servidor" in str(e) or "servidor_eventos" in str(e):
        if not _aviso_migracion:
            logger.warning("monitoreo: falta ejecutar migracion_monitoreo.sql (no se guarda el rendimiento)")
            _aviso_migracion = True
        return True
    return False


def guardar_resumen(bloqueos: int, bloqueo_max_s: float, sin_hilos: int, ws_widget: int, ws_asesores: int,
                    ws_supervisores: int):
    """Cada 5 minutos (en un hilo aparte, para no frenar al vigía): una fila con lo medido en la ventana."""
    with _lock:
        v = dict(_ventana, latencias=_ventana["latencias"])
        _ventana.update(peticiones=0, errores_5xx=0, latencias=[], memoria_max=0.0, cpu_max=0.0, cpu_suma=0.0,
                        cpu_muestras=0, bd_max=0, ia_saturado=0)
    fila = (INSTANCIA, REVISION, bloqueos, round(bloqueo_max_s, 1), sin_hilos, v["bd_max"], ws_widget, ws_asesores,
            ws_supervisores, round(v["memoria_max"], 1) or None,
            round(v["cpu_suma"] / v["cpu_muestras"], 1) if v["cpu_muestras"] else None,
            round(v["cpu_max"], 1) if v["cpu_muestras"] else None, v["peticiones"], v["errores_5xx"],
            _percentil(v["latencias"], 50), _percentil(v["latencias"], 95), v["ia_saturado"])
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO monitoreo_servidor (instancia, revision, bloqueos, bloqueo_max_s, sin_hilos, bd_max,
                               ws_widget, ws_asesores, ws_supervisores, memoria_mb, cpu_prom, cpu_max, peticiones,
                               errores_5xx, lat_p50_ms, lat_p95_ms, ia_saturado)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", fila)
            if time.localtime().tm_hour == 3 and time.localtime().tm_min < 5:   # limpieza diaria
                cur.execute("DELETE FROM monitoreo_servidor WHERE registrado_en < now() - make_interval(days => %s)",
                            (DIAS_GUARDADOS,))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        if conn:
            conn.rollback()
        if not _falta_tabla(e):
            logger.warning("monitoreo: no se pudo guardar el resumen: %s", str(e)[:200])
    finally:
        if conn:
            release_connection(conn)


def registrar_evento(tipo: str, detalle: str = None):
    """tipo: arranque | reinicio_vigia."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO servidor_eventos (instancia, revision, tipo, detalle) VALUES (%s, %s, %s, %s)""",
                        (INSTANCIA, REVISION, tipo, (detalle or "")[:500] or None))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        if conn:
            conn.rollback()
        if not _falta_tabla(e):
            logger.warning("monitoreo: no se pudo registrar el evento %s: %s", tipo, str(e)[:200])
    finally:
        if conn:
            release_connection(conn)


def registrar_evento_rapido(tipo: str, detalle: str = None, espera_max: float = 3.0):
    """Para el aviso justo antes de un reinicio: no espera más de espera_max (la base podría ser la que cuelga)."""
    h = threading.Thread(target=registrar_evento, args=(tipo, detalle), daemon=True)
    h.start()
    h.join(espera_max)


# ---------------------------------------------------------------------------
# Datos para el dashboard
# ---------------------------------------------------------------------------
def _consultar(cur, sql: str, params=()):
    """Una consulta que puede fallar (falta de permisos o de tabla) sin arruinar las demás."""
    cur.execute("SAVEPOINT sp_mon")
    try:
        cur.execute(sql, params)
        filas = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT sp_mon")
        return filas
    except Exception as e:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT sp_mon")
        _falta_tabla(e)
        return None


def _num(x):
    return float(x) if x is not None else None


def datos(desde, hasta, ahora: dict) -> dict:
    """Todo lo de la vista «Rendimiento»: ahora (en vivo), totales del período, por día, por hora, eventos de
    arranque/reinicio y estado de la base de datos. desde/hasta: datetimes con zona (hasta excluido)."""
    mem, fuente = memoria_mb()
    resultado = {
        "ahora": {**ahora, "instancia": INSTANCIA, "revision": REVISION, "minutos_encendido": int((time.time() - _INICIO) / 60),
                  "memoria_mb": round(mem, 1), "memoria_limite_mb": memoria_limite_mb(), "fuente_memoria": fuente,
                  "cpu_pct": round(_ultimo["cpu_pct"], 1) if _ultimo["cpu_pct"] is not None else None},
        "disponible": True,
    }
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            tz = "America/Lima"
            totales = _consultar(cur, """
                SELECT count(*), COALESCE(sum(bloqueos), 0), max(bloqueo_max_s), COALESCE(sum(sin_hilos), 0),
                       COALESCE(sum(peticiones), 0), COALESCE(sum(errores_5xx), 0), max(memoria_mb), avg(memoria_mb),
                       max(cpu_max), avg(cpu_prom), max(bd_max), max(ws_widget), max(ws_asesores), max(lat_p95_ms),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lat_p50_ms), COALESCE(sum(ia_saturado), 0)
                FROM monitoreo_servidor WHERE registrado_en >= %s AND registrado_en < %s""", (desde, hasta))
            if totales is None:
                resultado["disponible"] = False
            else:
                t = totales[0]
                resultado["periodo"] = {
                    "muestras": t[0], "bloqueos": int(t[1]), "bloqueo_max_s": _num(t[2]), "sin_hilos": int(t[3]),
                    "peticiones": int(t[4]), "errores_5xx": int(t[5]), "memoria_max_mb": _num(t[6]),
                    "memoria_prom_mb": _num(t[7]), "cpu_max": _num(t[8]), "cpu_prom": _num(t[9]), "bd_max": t[10],
                    "ws_widget_max": t[11], "ws_asesores_max": t[12], "lat_p95_max_ms": _num(t[13]),
                    "lat_p50_ms": _num(t[14]), "ia_saturado": int(t[15])}
                filas = _consultar(cur, f"""
                    SELECT to_char(date_trunc('day', registrado_en AT TIME ZONE '{tz}'), 'YYYY-MM-DD'),
                           max(memoria_mb), avg(memoria_mb), max(cpu_max), avg(cpu_prom), sum(peticiones),
                           sum(errores_5xx), max(lat_p95_ms), percentile_cont(0.5) WITHIN GROUP (ORDER BY lat_p50_ms),
                           sum(bloqueos), max(bloqueo_max_s), sum(sin_hilos), max(bd_max), max(ws_widget),
                           max(ws_asesores), sum(ia_saturado), count(*)
                    FROM monitoreo_servidor WHERE registrado_en >= %s AND registrado_en < %s
                    GROUP BY 1 ORDER BY 1""", (desde, hasta)) or []
                resultado["por_dia"] = [{
                    "dia": f[0], "memoria_max_mb": _num(f[1]), "memoria_prom_mb": _num(f[2]), "cpu_max": _num(f[3]),
                    "cpu_prom": _num(f[4]), "peticiones": int(f[5] or 0), "errores_5xx": int(f[6] or 0),
                    "lat_p95_max_ms": _num(f[7]), "lat_p50_ms": _num(f[8]), "bloqueos": int(f[9] or 0),
                    "bloqueo_max_s": _num(f[10]), "sin_hilos": int(f[11] or 0), "bd_max": f[12], "ws_widget_max": f[13],
                    "ws_asesores_max": f[14], "ia_saturado": int(f[15] or 0), "muestras": f[16]} for f in filas]
                filas = _consultar(cur, f"""
                    SELECT to_char(date_trunc('hour', registrado_en AT TIME ZONE '{tz}'), 'YYYY-MM-DD"T"HH24'),
                           max(memoria_mb), max(cpu_max), max(ws_widget), max(ws_asesores), sum(peticiones),
                           sum(errores_5xx), sum(bloqueos), max(bd_max)
                    FROM monitoreo_servidor WHERE registrado_en >= %s AND registrado_en < %s
                    GROUP BY 1 ORDER BY 1""", (desde, hasta)) or []
                resultado["por_hora"] = [{
                    "hora": f[0], "memoria_max_mb": _num(f[1]), "cpu_max": _num(f[2]), "ws_widget": f[3],
                    "ws_asesores": f[4], "peticiones": int(f[5] or 0), "errores_5xx": int(f[6] or 0),
                    "bloqueos": int(f[7] or 0), "bd_max": f[8]} for f in filas]
                # Arranques: el primero de cada revisión es un deploy; los siguientes de la misma revisión, reinicios.
                filas = _consultar(cur, f"""
                    SELECT to_char(ocurrido_en AT TIME ZONE '{tz}', 'YYYY-MM-DD HH24:MI'), tipo, revision, instancia,
                           detalle, ocurrido_en >= %s AND ocurrido_en < %s,
                           CASE WHEN tipo = 'arranque' AND row_number() OVER (PARTITION BY revision, tipo
                                                                              ORDER BY ocurrido_en) = 1
                                THEN 'deploy' WHEN tipo = 'arranque' THEN 'reinicio' ELSE tipo END
                    FROM servidor_eventos WHERE ocurrido_en < %s ORDER BY ocurrido_en""", (desde, hasta, hasta)) or []
                eventos = [{"cuando": f[0], "tipo": f[6], "revision": f[2], "instancia": f[3], "detalle": f[4]}
                           for f in filas if f[5]]
                resultado["eventos"] = eventos[-60:]
                resultado["periodo"].update(
                    deploys=sum(e["tipo"] == "deploy" for e in eventos),
                    reinicios=sum(e["tipo"] == "reinicio" for e in eventos),
                    reinicios_vigia=sum(e["tipo"] == "reinicio_vigia" for e in eventos))
            resultado["bd"] = _datos_bd(cur)
            ia = _consultar(cur, f"""
                SELECT to_char(date_trunc('day', creado_en AT TIME ZONE '{tz}'), 'YYYY-MM-DD'), count(*),
                       avg(segundos), max(segundos),
                       count(*) FILTER (WHERE respuesta IS NULL OR respuesta = '')
                FROM plan_preguntas WHERE creado_en >= %s AND creado_en < %s GROUP BY 1 ORDER BY 1""", (desde, hasta))
            resultado["ia"] = [{"dia": f[0], "preguntas": f[1], "seg_prom": _num(f[2]), "seg_max": _num(f[3]),
                                "sin_respuesta": f[4]} for f in ia] if ia is not None else None
        conn.rollback()
    finally:
        release_connection(conn)
    return resultado


def _datos_bd(cur) -> dict:
    bd = {}
    f = _consultar(cur, """SELECT current_setting('max_connections')::int, pg_database_size(current_database()),
                                  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
                                  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
                                     AND state <> 'idle' AND query_start < now() - interval '5 seconds'
                                     AND pid <> pg_backend_pid()),
                                  (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'),
                                  (SELECT round(100.0 * sum(blks_hit) / NULLIF(sum(blks_hit) + sum(blks_read), 0), 2)
                                     FROM pg_stat_database WHERE datname = current_database())""")
    if f:
        mc, tam, conex, lentas, abiertas, cache = f[0]
        bd.update(max_conexiones=mc, tamano_mb=round(tam / 1048576, 1), conexiones=conex, consultas_lentas=lentas,
                  transacciones_abiertas=abiertas, cache_hit_pct=_num(cache))
    filas = _consultar(cur, """
        SELECT COALESCE(NULLIF(application_name, ''), '(sin nombre)'), COALESCE(datname, ''),
               count(*), count(*) FILTER (WHERE state = 'active')
        FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1, 2 ORDER BY 3 DESC""")
    bd["conexiones_por_sistema"] = [{"sistema": a, "base": b, "total": c, "activas": d} for a, b, c, d in filas or []]
    filas = _consultar(cur, """
        SELECT relname, pg_total_relation_size(relid), n_live_tup, n_dead_tup,
               to_char(GREATEST(last_autovacuum, last_vacuum) AT TIME ZONE 'America/Lima', 'DD/MM HH24:MI')
        FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10""")
    bd["tablas"] = [{"tabla": a, "mb": round(b / 1048576, 1), "filas": c, "muertas": d, "ultimo_vacuum": e}
                    for a, b, c, d, e in filas or []]
    return bd
