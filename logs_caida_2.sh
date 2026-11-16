#!/bin/bash
# Segunda extraccion de la caida del 13/11 (solo lectura). Correr en Cloud Shell:
#   bash ~/webchat-api/logs_caida_2.sh
# Genera 4 archivos en ~/ : mandamelos.
P="--project=sanitas-recetas-prod"
SVC='resource.type="cloud_run_revision" AND resource.labels.service_name="webchat-api"'

# 1) Auditoria: deploys, cambios de configuracion o de trafico del servicio entre 12:00 y 15:30 Lima.
#    (Confirma si hubo algun intento de deploy cerca de las 13:16.)
gcloud logging read "logName:\"cloudaudit.googleapis.com\" AND protoPayload.serviceName=\"run.googleapis.com\" AND timestamp>=\"2026-11-13T17:00:00Z\" AND timestamp<=\"2026-11-13T20:30:00Z\"" $P \
  --limit=100 --order=asc \
  --format='value(timestamp,protoPayload.methodName,protoPayload.authenticationInfo.principalEmail,protoPayload.resourceName,protoPayload.status.message)' > ~/caida2_1_auditoria.txt

# 2) TODOS los logs (cualquier nivel) entre 13:10 y 13:25 Lima, con la REVISION e INSTANCIA de cada linea.
gcloud logging read "$SVC AND timestamp>=\"2026-11-13T18:10:00Z\" AND timestamp<=\"2026-11-13T18:25:00Z\"" $P \
  --limit=3000 --order=asc \
  --format='value(timestamp,severity,resource.labels.revision_name,labels.instanceId,httpRequest.status,httpRequest.latency,httpRequest.requestUrl,textPayload)' > ~/caida2_2_todo_1310_1325.txt

# 3) Eventos de plataforma (varlog/system) de TODO el dia, con revision e instancia.
gcloud logging read "$SVC AND logName:\"varlog%2Fsystem\" AND timestamp>=\"2026-11-13T11:00:00Z\"" $P \
  --limit=500 --order=asc \
  --format='value(timestamp,severity,resource.labels.revision_name,labels.instanceId,textPayload)' > ~/caida2_3_plataforma_dia.txt

# 4) Peticiones por minuto (cantidad y latencia maxima) de 12:30 a 13:20 Lima: ver si hubo un pico antes del corte.
gcloud logging read "$SVC AND httpRequest.requestUrl:* AND timestamp>=\"2026-11-13T17:30:00Z\" AND timestamp<=\"2026-11-13T18:20:00Z\"" $P \
  --limit=20000 --order=asc --format='value(timestamp,httpRequest.status,httpRequest.latency,httpRequest.requestUrl)' \
  | awk -F'\t' '{m=substr($1,1,16); n[m]++; l=$3; sub("s","",l); if (l+0>mx[m]) mx[m]=l+0; if ($2>=500) e[m]++}
               END {for (k in n) printf "%s\tpeticiones=%d\terrores5xx=%d\tlatencia_max=%.1fs\n", k, n[k], e[k], mx[k]}' \
  | sort > ~/caida2_4_por_minuto.txt

wc -l ~/caida2_*.txt
