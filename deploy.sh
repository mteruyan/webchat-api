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
# ALLOWED_ORIGINS: solo los dominios de Sanitas (marketeru.pe, que se usó para pruebas, ya se retiró).

# UNA SOLA INSTANCIA (--max-instances=1, --concurrency=1000: el máximo de Cloud Run). OJO: el 30/10 el "Rate exceeded."
# NO fue por falta de cupo (el pico real de la semana fue ~85 conexiones): el servidor se congeló, las peticiones se
# acumularon sin terminar y llenaron los 400. Ver el vigía en main.py ("Vigía" en Cloud Logging).
# --memory=1Gi: con el valor por defecto (512Mi) la memoria llegó al 88 % la semana del 23/10.
# El estado de los WebSockets (quién esta conectado,
# quién escucha cada conversación) vive en la memoria del proceso. Con 2+ instancias, el cliente puede quedar en
# una y el asesor en otra y los mensajes no se entregan. No subir max-instances hasta migrar ese estado a Redis.
# --no-cpu-throttling: la CPU queda siempre asignada (el aviso de inactividad corre en segundo plano).
# Las conexiones de Cloud SQL (db-g1-small) se comparten con sofi-apis y otros: ver los pools en db.py (webchat 20 + sofi 3).

# Nota sobre --set-env-vars: usamos el delimitador alterno "^;^" (en vez del
# "," por defecto) precisamente para que ALLOWED_ORIGINS pueda llevar varias
# URLs separadas por comas SIN tener que escapar comas (\,) — esa forma es
# frágil (basta que un editor o un copy/paste se coma un backslash para que
# gcloud reciba mal el argumento, como pasó). Con "^;^", las comas dentro de
# cada valor son texto normal y ";" separa las variables entre sí.
gcloud run deploy webchat-api --source . \
  --project=sanitas-recetas-prod --region=us-central1 \
  --add-cloudsql-instances=sanitas-recetas-prod:us-central1:sanitas-recetas-db \
  --set-env-vars="^;^INSTANCE_CONNECTION_NAME=sanitas-recetas-prod:us-central1:sanitas-recetas-db;DB_NAME=webchat;DB_USER=recetas_user;SOFI_DB_NAME=sofi_whatsapp;GCS_BUCKET=sanitas-webchat-contenido;ALLOWED_ORIGINS=https://www.sanitasperu.com,https://sanitasperu.com;IA_PALABRA_CLAVE=#sofi-lab;IA_TESTERS=51975448483,51997855645;MAD_PALABRA_CLAVE=#sofi-mad;MAD_TESTERS=51975448483" \
  --set-secrets=DB_PASSWORD=webchat-db-password:latest,SECRET_KEY=webchat-jwt-secret:latest,SOFI_APIS_KEY=API_KEY:latest,DANACONNECT_AUTH=webchat-danaconnect-auth:latest,VAPID_PRIVATE_KEY=webchat-vapid-private:latest \
  --min-instances=1 --max-instances=1 --concurrency=1000 --memory=1Gi --cpu=1 --no-cpu-throttling --timeout=3600 --allow-unauthenticated
