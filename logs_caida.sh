#!/bin/bash
# Extrae los logs de la caida del 13/11 (13:00 a 15:10 hora Lima) para analizarlos. Correr en Cloud Shell:
#   bash ~/webchat-api/logs_caida.sh
# Genera 3 archivos en ~/: mandamelos (o pega su contenido).
DESDE="2026-11-13T18:00:00Z"   # 13:00 Lima
HASTA="2026-11-13T20:10:00Z"   # 15:10 Lima
P="--project=sanitas-recetas-prod"
BASE="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"webchat-api\" AND timestamp>=\"$DESDE\" AND timestamp<=\"$HASTA\""

# 1) Advertencias y errores de la aplicacion (vigia, pool de BD, excepciones).
gcloud logging read "$BASE AND severity>=WARNING" $P --limit=500 --order=asc \
  --format='value(timestamp,severity,textPayload,jsonPayload.message)' > ~/caida_1_errores.txt

# 2) Eventos de la plataforma: reinicios, memoria excedida, contenedor terminado, probes.
gcloud logging read "$BASE AND (logName:\"varlog%2Fsystem\" OR textPayload:\"Memory limit\" OR textPayload:\"terminated\" OR textPayload:\"probe\" OR textPayload:\"Starting\" OR textPayload:\"Vig\")" $P \
  --limit=300 --order=asc --format='value(timestamp,severity,logName,textPayload)' > ~/caida_2_plataforma.txt

# 3) Peticiones con error 5xx o lentas (mas de 10 s), minuto a minuto.
gcloud logging read "$BASE AND httpRequest.requestUrl:* AND (httpRequest.status>=500 OR httpRequest.latency>\"10s\")" $P \
  --limit=1000 --order=asc --format='value(timestamp,httpRequest.status,httpRequest.latency,httpRequest.requestUrl)' > ~/caida_3_peticiones.txt

wc -l ~/caida_1_errores.txt ~/caida_2_plataforma.txt ~/caida_3_peticiones.txt
