#!/bin/bash
# Deploy de webchat-api — usar SIEMPRE este script completo, nunca gcloud run
# services update por separado para secretos: cada deploy resetea las variables
# a lo que traiga este comando, así que los 5 secretos deben ir siempre juntos.
#
# VAPID_PRIVATE_KEY (avisos push): el secreto "webchat-vapid-private" debe existir ANTES del
# deploy (ver generar_vapid.py). Si no existe, este comando falla.
#
# DANACONNECT_AUTH usa el secreto "webchat-danaconnect-auth" (Secret Manager).
# Si necesitas corregir su valor más adelante, NO uses "gcloud secrets create"
# (falla porque ya existe) — usa:
#   gcloud secrets versions add webchat-danaconnect-auth --data-file=-
# Como este script referencia ":latest", el próximo deploy toma la versión
# nueva automáticamente.
#
# AGENTE DE IA OCULTO (modo experimental): IA_PALABRA_CLAVE es la palabra secreta que se escribe en el
# chat y IA_TESTERS la lista blanca de celulares de prueba (51XXXXXXXXX, separados por comas; deben
# haber pasado antes la verificación por código SMS). CAMBIA los valores de ejemplo por los tuyos.
# Para apagar el modo IA sin desplegar de nuevo: UPDATE bot_menus SET activo = FALSE WHERE codigo = 'ia_conversacion';
# Para apagarlo desplegando: deja IA_PALABRA_CLAVE vacía (IA_PALABRA_CLAVE=) o quita IA_TESTERS.
#
# MEDICO A DOMICILIO / AMBULANCIA OCULTO (prueba): MAD_PALABRA_CLAVE es su palabra secreta y MAD_TESTERS su lista
# blanca PROPIA (a proposito separada de IA_TESTERS: este flujo despacha profesionales de verdad y manda SMS reales).
# Mismo formato (51XXXXXXXXX separados por comas, celulares ya verificados por SMS en el webchat).
# Para apagarlo sin desplegar: UPDATE bot_menus SET activo = FALSE WHERE codigo = 'mad_inicio';
#
# TARIFAS 2027 (cartas_tarifas.py): GCS_CARTAS_BUCKET es el bucket PRIVADO de las cartas (sanitas-webchat-contenido
# tiene lectura pública). La cuenta de servicio de Cloud Run necesita roles/storage.objectViewer en él.
# SINIESTRALIDAD (siniestralidad.py): DANACONNECT_SINIESTRALIDAD_CONV es el ID de la conversation de EMAIL en Danaconnect
# que manda el enlace para firmar los formatos (variables EMAIL, NOMBRE, URL, FORMATO). Vacio = el envio por correo
# queda deshabilitado en la consola (el enlace por chat funciona igual). Configurado: 620852.
#
# ALLOWED_ORIGINS: solo los dominios de Sanitas (marketeru.pe, que se usó para pruebas, ya se retiró).

# PRUEBA DE VIDA (--liveness-probe, 13/11): sin ella Cloud Run solo reinicia el contenedor si el proceso TERMINA;
# si queda vivo pero colgado (sin responder), sigue así indefinidamente. Consulta /vivo cada 20 s y, tras 3 fallas
# seguidas (~1 min), lo reinicia. /vivo no toca la base (ver main.py). La caída del 13/11 fue OTRA cosa (Cloud Run
# retiró la instancia sana y, con max-instances=1, no pudo levantar el reemplazo mientras duraban los WebSockets):
# eso lo resuelve _vigilar_drenaje en main.py.
# UNA SOLA INSTANCIA (--max-instances=1, --concurrency=1000: el máximo de Cloud Run). OJO: el 30/10 el "Rate exceeded."
# NO fue por falta de cupo (el pico real de la semana fue ~85 conexiones): el servidor se congeló, las peticiones se
# acumularon sin terminar y llenaron los 400. Ver el vigía en main.py ("Vigía" en Cloud Logging).
# --memory=1Gi: con el valor por defecto (512Mi) la memoria llegó al 88 % la semana del 23/10.
# VARIAS INSTANCIAS (20/11): los WebSockets viven en la memoria de cada instancia; bus.py las comunica por Postgres
# LISTEN/NOTIFY (presencia en ws_presencia, eventos por pg_notify) y solo la LÍDER corre las tareas de fondo.
# Requiere migracion_dos_instancias.sql. Despliegue por fases:
#   Fase 1:  ./deploy.sh                    -> 1 instancia con el bus activo (validar 1-2 días)
#   Fase 2:  MAX_INSTANCIAS=2 ./deploy.sh   -> mínimo 1, máximo 2, con afinidad de sesión
# Emergencia: BUS_ACTIVO=no apaga el bus (solo válido con 1 instancia).
MAX_INSTANCIAS="${MAX_INSTANCIAS:-1}"
if [ "$MAX_INSTANCIAS" -gt 1 ]; then DB_POOL_MAX=10; else DB_POOL_MAX=20; fi
# --no-cpu-throttling: la CPU queda siempre asignada (el aviso de inactividad corre en segundo plano).
# Las conexiones de Cloud SQL (db-g1-small) se comparten con sofi-apis y otros: por instancia = DB_POOL_MAX + sofi 3 +
# recetas 2 + bus 2. Con 2 instancias: 2 x 17 = 34 (antes 1 x 27). max_connections = 50 (20/11): quedan ~9 para sofi-apis y otros.

# Nota sobre --set-env-vars: usamos el delimitador alterno "^;^" (en vez del
# "," por defecto) precisamente para que ALLOWED_ORIGINS pueda llevar varias
# URLs separadas por comas SIN tener que escapar comas (\,) — esa forma es
# frágil (basta que un editor o un copy/paste se coma un backslash para que
# gcloud reciba mal el argumento, como pasó). Con "^;^", las comas dentro de
# cada valor son texto normal y ";" separa las variables entre sí.
gcloud run deploy webchat-api --source . \
  --project=sanitas-recetas-prod --region=us-central1 \
  --add-cloudsql-instances=sanitas-recetas-prod:us-central1:sanitas-recetas-db \
  --set-env-vars="^;^INSTANCE_CONNECTION_NAME=sanitas-recetas-prod:us-central1:sanitas-recetas-db;DB_NAME=webchat;DB_USER=recetas_user;SOFI_DB_NAME=sofi_whatsapp;GCS_BUCKET=sanitas-webchat-contenido;GCS_CARTAS_BUCKET=sanitas-webchat-cartas;ALLOWED_ORIGINS=https://www.sanitasperu.com,https://sanitasperu.com;IA_PALABRA_CLAVE=#sofi-lab;IA_TESTERS=51975448483,51997855645;MAD_PALABRA_CLAVE=#sofi-mad;MAD_TESTERS=51975448483;DANACONNECT_SINIESTRALIDAD_CONV=620852;DB_POOL_MAX=${DB_POOL_MAX};BUS_ACTIVO=si" \
  --set-secrets=DB_PASSWORD=webchat-db-password:latest,SECRET_KEY=webchat-jwt-secret:latest,SOFI_APIS_KEY=API_KEY:latest,DANACONNECT_AUTH=webchat-danaconnect-auth:latest,VAPID_PRIVATE_KEY=webchat-vapid-private:latest \
  --liveness-probe=httpGet.path=/vivo,periodSeconds=20,timeoutSeconds=5,failureThreshold=3 \
  --min-instances=1 --max-instances="${MAX_INSTANCIAS}" --session-affinity --concurrency=1000 --memory=1Gi --cpu=1 --no-cpu-throttling --timeout=3600 --allow-unauthenticated
