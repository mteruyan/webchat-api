#!/bin/bash
# Deploy de webchat-api — usar SIEMPRE este script completo, nunca gcloud run
# services update por separado para secretos: cada deploy resetea las variables
# a lo que traiga este comando, así que los 4 secretos deben ir siempre juntos.
#
# DANACONNECT_AUTH usa el secreto "webchat-danaconnect-auth" (Secret Manager).
# Si necesitas corregir su valor más adelante, NO uses "gcloud secrets create"
# (falla porque ya existe) — usa:
#   gcloud secrets versions add webchat-danaconnect-auth --data-file=-
# Como este script referencia ":latest", el próximo deploy toma la versión
# nueva automáticamente.
#
# ALLOWED_ORIGINS incluye "https://marketeru.pe" SOLO mientras se prueba ahí
# (galería de dentista) — cuando el widget quede embebido en www.sanitasperu.com
# hay que QUITAR marketeru.pe de esta lista y volver a desplegar.

# Nota sobre --set-env-vars: usamos el delimitador alterno "^;^" (en vez del
# "," por defecto) precisamente para que ALLOWED_ORIGINS pueda llevar varias
# URLs separadas por comas SIN tener que escapar comas (\,) — esa forma es
# frágil (basta que un editor o un copy/paste se coma un backslash para que
# gcloud reciba mal el argumento, como pasó). Con "^;^", las comas dentro de
# cada valor son texto normal y ";" separa las variables entre sí.
gcloud run deploy webchat-api --source . \
  --project=sanitas-recetas-prod --region=us-central1 \
  --add-cloudsql-instances=sanitas-recetas-prod:us-central1:sanitas-recetas-db \
  --set-env-vars="^;^INSTANCE_CONNECTION_NAME=sanitas-recetas-prod:us-central1:sanitas-recetas-db;DB_NAME=webchat;DB_USER=recetas_user;SOFI_DB_NAME=sofi_whatsapp;GCS_BUCKET=sanitas-webchat-contenido;ALLOWED_ORIGINS=https://www.sanitasperu.com,https://sanitasperu.com,https://marketeru.pe" \
  --set-secrets=DB_PASSWORD=webchat-db-password:latest,SECRET_KEY=webchat-jwt-secret:latest,SOFI_APIS_KEY=API_KEY:latest,DANACONNECT_AUTH=webchat-danaconnect-auth:latest \
  --min-instances=1 --max-instances=3 --allow-unauthenticated
