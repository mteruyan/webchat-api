#!/bin/bash
# Deploy de webchat-api — usar SIEMPRE este script completo, nunca gcloud run
# services update por separado para secretos: cada deploy resetea las variables
# a lo que traiga este comando, así que los 3 secretos deben ir siempre juntos.

gcloud run deploy webchat-api --source . \
  --project=sanitas-recetas-prod --region=us-central1 \
  --add-cloudsql-instances=sanitas-recetas-prod:us-central1:sanitas-recetas-db \
  --set-env-vars=INSTANCE_CONNECTION_NAME=sanitas-recetas-prod:us-central1:sanitas-recetas-db,DB_NAME=webchat,DB_USER=recetas_user,GCS_BUCKET=sanitas-webchat-recetas,GCS_CONTENIDO_BUCKET=sanitas-webchat-contenido \
  --set-secrets=DB_PASSWORD=webchat-db-password:latest,SECRET_KEY=webchat-jwt-secret:latest,SOFI_APIS_KEY=API_KEY:latest \
  --min-instances=1 --max-instances=3 --allow-unauthenticated
