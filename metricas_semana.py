#!/usr/bin/env python3
"""metricas_semana.py — exporta las métricas de rendimiento del webchat de los últimos 7 días a un CSV.

Uso (en Cloud Shell, con el proyecto de producción):
    python3 metricas_semana.py
Genera metricas_webchat.csv (una fila por métrica, serie y bloque de 5 minutos). Solo lee métricas: no cambia nada.
"""
import csv
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROYECTO = "sanitas-recetas-prod"
SERVICIO = "webchat-api"
INSTANCIA_SQL = "sanitas-recetas-prod:sanitas-recetas-db"
DIAS = 7
PASO = "300s"   # bloques de 5 minutos

RUN = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICIO}"'
SQL = f'resource.type="cloudsql_database" AND resource.labels.database_id="{INSTANCIA_SQL}"'

# (nombre, filtro, alineador, reductor, agrupar_por)
METRICAS = [
    ("concurrencia_p99", f'metric.type="run.googleapis.com/container/max_request_concurrencies" AND {RUN}',
     "ALIGN_PERCENTILE_99", "REDUCE_MAX", []),
    ("concurrencia_p50", f'metric.type="run.googleapis.com/container/max_request_concurrencies" AND {RUN}',
     "ALIGN_PERCENTILE_50", "REDUCE_MAX", []),
    ("peticiones_por_seg", f'metric.type="run.googleapis.com/request_count" AND {RUN}',
     "ALIGN_RATE", "REDUCE_SUM", ["metric.labels.response_code_class"]),
    ("latencia_p50_ms", f'metric.type="run.googleapis.com/request_latencies" AND {RUN}',
     "ALIGN_PERCENTILE_50", "REDUCE_MAX", []),
    ("latencia_p95_ms", f'metric.type="run.googleapis.com/request_latencies" AND {RUN}',
     "ALIGN_PERCENTILE_95", "REDUCE_MAX", []),
    ("cpu_p99", f'metric.type="run.googleapis.com/container/cpu/utilizations" AND {RUN}',
     "ALIGN_PERCENTILE_99", "REDUCE_MAX", []),
    ("memoria_p99", f'metric.type="run.googleapis.com/container/memory/utilizations" AND {RUN}',
     "ALIGN_PERCENTILE_99", "REDUCE_MAX", []),
    ("instancias", f'metric.type="run.googleapis.com/container/instance_count" AND {RUN}',
     "ALIGN_MAX", "REDUCE_SUM", ["metric.labels.state"]),
    ("sql_cpu", f'metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND {SQL}',
     "ALIGN_MAX", None, []),
    ("sql_memoria", f'metric.type="cloudsql.googleapis.com/database/memory/utilization" AND {SQL}',
     "ALIGN_MAX", None, []),
    ("sql_conexiones", f'metric.type="cloudsql.googleapis.com/database/postgresql/num_backends" AND {SQL}',
     "ALIGN_MAX", "REDUCE_SUM", ["metric.labels.database"]),
]


def token() -> str:
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()


def leer(tk, filtro, alineador, reductor, agrupar, desde, hasta):
    params = {
        "filter": filtro,
        "interval.startTime": desde, "interval.endTime": hasta,
        "aggregation.alignmentPeriod": PASO, "aggregation.perSeriesAligner": alineador,
        "pageSize": "500",
    }
    if reductor:
        params["aggregation.crossSeriesReducer"] = reductor
        for g in agrupar:
            params.setdefault("aggregation.groupByFields", [])
            params["aggregation.groupByFields"].append(g)
    series, pagina = [], None
    while True:
        if pagina:
            params["pageToken"] = pagina
        url = (f"https://monitoring.googleapis.com/v3/projects/{PROYECTO}/timeSeries?"
               + urllib.parse.urlencode(params, doseq=True))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tk}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        series += d.get("timeSeries", [])
        pagina = d.get("nextPageToken")
        if not pagina:
            return series


def main():
    hasta = datetime.now(timezone.utc)
    desde = hasta - timedelta(days=DIAS)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    print("Leyendo métricas de los últimos 7 días (1 a 2 minutos)…", flush=True)
    tk = token()
    filas = 0
    with open("metricas_webchat.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metrica", "serie", "inicio_utc", "valor"])
        for i, (nombre, filtro, alin, red, grupo) in enumerate(METRICAS, 1):
            print(f"  [{i}/{len(METRICAS)}] {nombre}…", flush=True)
            try:
                series = leer(tk, filtro, alin, red, grupo, iso(desde), iso(hasta))
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ {nombre}: no se pudo leer ({e})", file=sys.stderr)
                continue
            n = 0
            for s in series:
                etiquetas = {**s.get("metric", {}).get("labels", {}), **s.get("resource", {}).get("labels", {})}
                serie = ",".join(f"{k}={v}" for k, v in sorted(etiquetas.items())
                                 if k in ("response_code_class", "state", "database", "revision_name"))
                for p in s.get("points", []):
                    v = p["value"]
                    valor = v.get("doubleValue", v.get("int64Value", v.get("boolValue")))
                    w.writerow([nombre, serie, p["interval"]["startTime"], valor])
                    n += 1
            filas += n
            print(f"        {n} puntos", flush=True)
    print(f"Listo: metricas_webchat.csv ({filas} filas)")


if __name__ == "__main__":
    main()
