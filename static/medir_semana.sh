#!/usr/bin/env bash
# Medición de la última semana del webchat (logs de Cloud Run + Cloud Armor) y, al final, las métricas de
# memoria / CPU / base de datos (medir_metricas.py).
# Uso en Cloud Shell, dentro de ~/webchat-api:   bash medir_semana.sh          (o: bash medir_semana.sh 3d)
set -u
P="sanitas-recetas-prod"
DIAS="${1:-7d}"
LF='resource.type="cloud_run_revision" AND resource.labels.service_name="webchat-api"'

leer() {   # leer "<filtro>" <limite> "<formato>"
  gcloud logging read "$1" --project="$P" --freshness="$DIAS" --limit="$2" --format="$3" 2>/dev/null
}
contar() { leer "$LF AND ($1)" 20000 'value(timestamp)' | grep -c . ; }
titulo() { printf '\n==================== %s ====================\n' "$1"; }

echo "Webchat: medición de los últimos $DIAS (hora del servidor = UTC; Lima = UTC-5)"

titulo "1. Arranques del servidor (deploys + reinicios)"
echo "Arranques: $(contar 'textPayload:"Application startup complete"')"
echo "(Cada deploy cuenta 1. Si hay más arranques que deploys, Cloud Run reinició el contenedor.)"
leer "$LF AND textPayload:\"Application startup complete\"" 30 'value(timestamp,resource.labels.revision_name)'

titulo "2. Caídas del contenedor (memoria agotada, salidas inesperadas)"
echo "Eventos: $(contar 'textPayload:"Memory limit" OR textPayload:"memory limit" OR textPayload:"Container called exit" OR textPayload:"Uncaught signal" OR textPayload:"terminated"')"
leer "$LF AND (textPayload:\"Memory limit\" OR textPayload:\"memory limit\" OR textPayload:\"Container called exit\" OR textPayload:\"Uncaught signal\" OR textPayload:\"terminated\")" 15 'value(timestamp,textPayload)' | cut -c1-220

titulo "3. Vigía: bloqueos y reinicios (reinicia el servidor si queda colgado más de 60 s)"
echo "REINICIOS hechos por el vigía: $(contar 'textPayload:"Vigía: servidor colgado"')"
leer "$LF AND textPayload:\"Vigía: servidor colgado\"" 10 'value(timestamp,textPayload)' | cut -c1-220
echo "Hilo principal bloqueado: $(contar 'textPayload:"Vigía: hilo principal bloqueado"')"
echo "Sin hilos de trabajo libres: $(contar 'textPayload:"Vigía: no hay hilos"')"
echo "Pool de base de datos lleno: $(contar 'textPayload:"Pool de BD"')"
leer "$LF AND (textPayload:\"Vigía: hilo principal bloqueado\" OR textPayload:\"Vigía: no hay hilos\" OR textPayload:\"Pool de BD\")" 20 'value(timestamp,resource.labels.revision_name,textPayload)' | cut -c1-200

titulo "4. Vigía: resumen de carga (cada 5 minutos)"
leer "$LF AND textPayload:\"Vigía resumen\"" 5000 'value(timestamp,textPayload)' | python3 -c '
import re, sys
n = bloq = 0; maxs = 0.0; bd = ws = ase = sup = 0; pico = {}
for linea in sys.stdin:
    m = re.search(r"principal (\d+) \(máx ([\d.]+) s\).*?webchat (\d+)/\d+.*?widget (\d+), asesores (\d+), supervisores (\d+)", linea)
    if not m:
        continue
    n += 1
    b, s, d, w, a, su = int(m[1]), float(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6])
    bloq += b; maxs = max(maxs, s); bd = max(bd, d); sup = max(sup, su)
    if w > ws: ws, pico["widget"] = w, linea[:16]
    if a > ase: ase, pico["asesores"] = a, linea[:16]
print(f"Resúmenes leídos: {n} (unos {n * 5 // 60} h de registro)")
print(f"Bloqueos del hilo principal en total: {bloq}  | bloqueo más largo: {maxs:.1f} s")
print(f"Pico de conexiones a la BD del webchat: {bd} de 20")
pw, pa = pico.get("widget", "-"), pico.get("asesores", "-")
print(f"Pico de clientes con el widget abierto: {ws}  ({pw} UTC)")
print(f"Pico de asesores conectados: {ase}  ({pa} UTC) | supervisores: {sup}")
'

titulo "5. Respuestas con error del servidor (HTTP 5xx)"
echo "Total 5xx: $(contar 'httpRequest.status>=500')"
leer "$LF AND httpRequest.status>=500" 2000 'value(httpRequest.status,httpRequest.requestUrl)' \
  | sed -E 's#https?://[^/]+##; s#\?.*##' | sort | uniq -c | sort -rn | head -10

titulo "6. Errores registrados por la aplicación (top 10)"
echo "Total ERROR: $(contar 'severity>=ERROR')"
leer "$LF AND severity>=ERROR" 3000 'value(textPayload)' | grep . | cut -c1-90 \
  | sed -E 's/[0-9]{3,}/N/g' | sort | uniq -c | sort -rn | head -10

titulo "7. Plan de salud (IA)"
echo "Planes digitalizados: $(contar 'textPayload:"digitalizado como plan"')"
echo "PDF reutilizados (sin IA): $(contar 'textPayload:"ya estaba cargado como plan"')"
echo "Google saturado (429/503), reintentos: $(contar 'textPayload:"Google saturado"')"
echo "...que igual respondieron tras reintentar: $(contar 'textPayload:"respondió tras"')"
echo "Se pasó a un modelo de respaldo: $(contar 'textPayload:"se prueba el siguiente modelo"')"

titulo "8. Cloud Armor: peticiones bloqueadas"
LB='resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"'
echo "Bloqueadas: $(leer "$LB" 20000 'value(timestamp)' | grep -c .)"
leer "$LB" 3000 'value(jsonPayload.enforcedSecurityPolicy.name,jsonPayload.enforcedSecurityPolicy.priority,httpRequest.requestUrl)' \
  | sed -E 's#https?://[^/]+##; s#\?.*##' | sort | uniq -c | sort -rn | head -10

titulo "9. Memoria, CPU, solicitudes y base de datos (Cloud Monitoring)"
python3 "$(dirname "$0")/medir_metricas.py" "${DIAS%d}"
