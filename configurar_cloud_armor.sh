#!/bin/bash
# configurar_cloud_armor.sh — pone un Load Balancer HTTPS + Cloud Armor
# delante de webchat-api, en el dominio api.sanitasperu.com.
#
# CORRE ESTO DESDE Cloud Shell (o tu terminal con gcloud autenticado al
# proyecto sanitas-recetas-prod), UNA SECCIÓN A LA VEZ — no todo de un
# tirón, porque el PASO 6 (DNS) requiere que alguien más (el equipo que
# administra el dominio sanitasperu.com) cree un registro, y el PASO 8
# (bloquear la URL *.run.app) solo debe correr una vez que ya confirmaste
# que todo funciona por el dominio nuevo.
#
# Por qué esto hace falta: Cloud Armor (rate limiting, reglas WAF) solo
# protege el tráfico que pasa por un Load Balancer — no la URL directa
# *.run.app que usa hoy el servicio. Sin este Load Balancer, Cloud Armor
# no tiene nada que proteger.

set -e

PROYECTO="sanitas-recetas-prod"
REGION="us-central1"
SERVICIO_RUN="webchat-api"
DOMINIO="api.sanitasperu.com"   # <- cambialo aquí si al final usan otro subdominio

gcloud config set project "$PROYECTO"


# ---------------------------------------------------------------------------
# PASO 1 — Política de Cloud Armor: rate limiting + reglas WAF preconfiguradas
# ---------------------------------------------------------------------------
gcloud compute security-policies create webchat-armor-policy \
  --description="Rate limiting y WAF para webchat-api"

# Regla general: cualquier IP que pase de 100 peticiones/minuto queda
# bloqueada 10 minutos (protege todo el servicio, no solo /login).
gcloud compute security-policies rules create 1000 \
  --security-policy=webchat-armor-policy \
  --expression="true" \
  --action=rate-based-ban \
  --rate-limit-threshold-count=100 \
  --rate-limit-threshold-interval-sec=60 \
  --ban-duration-sec=600 \
  --conform-action=allow \
  --exceed-action=deny-429 \
  --enforce-on-key=IP

# Regla más estricta específica para /login: máx. 5 peticiones/minuto por
# IP — refuerza (a nivel de red) el bloqueo por usuario que ya quedó en el
# código (auth.py). Si alguien reparte los intentos entre varios usuarios
# desde la misma IP, esta regla los frena igual.
gcloud compute security-policies rules create 900 \
  --security-policy=webchat-armor-policy \
  --expression="request.path.matches('/login')" \
  --action=rate-based-ban \
  --rate-limit-threshold-count=5 \
  --rate-limit-threshold-interval-sec=60 \
  --ban-duration-sec=900 \
  --conform-action=allow \
  --exceed-action=deny-429 \
  --enforce-on-key=IP

# Reglas preconfiguradas de OWASP — bloquean patrones conocidos de XSS e
# inyección SQL en la petición (defensa adicional a lo que ya hace el código).
gcloud compute security-policies rules create 800 \
  --security-policy=webchat-armor-policy \
  --expression="evaluatePreconfiguredExpr('xss-stable')" \
  --action=deny-403

gcloud compute security-policies rules create 810 \
  --security-policy=webchat-armor-policy \
  --expression="evaluatePreconfiguredExpr('sqli-stable')" \
  --action=deny-403


# ---------------------------------------------------------------------------
# PASO 2 — IP fija para el Load Balancer
# ---------------------------------------------------------------------------
gcloud compute addresses create webchat-api-ip --global


# ---------------------------------------------------------------------------
# PASO 3 — Conectar el Load Balancer con el servicio de Cloud Run
# ---------------------------------------------------------------------------
gcloud compute network-endpoint-groups create webchat-api-neg \
  --region="$REGION" \
  --network-endpoint-type=serverless \
  --cloud-run-service="$SERVICIO_RUN"

gcloud compute backend-services create webchat-api-backend \
  --global \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --security-policy=webchat-armor-policy

gcloud compute backend-services add-backend webchat-api-backend \
  --global \
  --network-endpoint-group=webchat-api-neg \
  --network-endpoint-group-region="$REGION"


# ---------------------------------------------------------------------------
# PASO 4 — Certificado HTTPS administrado por Google, para $DOMINIO
# (Google emite el certificado solo, pero necesita que el DNS del PASO 6 ya
# esté apuntando a la IP del PASO 2 para poder validarlo — puede tardar
# hasta un par de horas después de que el DNS propague.)
# ---------------------------------------------------------------------------
gcloud compute ssl-certificates create webchat-api-cert \
  --domains="$DOMINIO"


# ---------------------------------------------------------------------------
# PASO 5 — Enrutamiento HTTPS: url-map + proxy + regla de forwarding
# ---------------------------------------------------------------------------
gcloud compute url-maps create webchat-api-urlmap \
  --default-service=webchat-api-backend

gcloud compute target-https-proxies create webchat-api-https-proxy \
  --url-map=webchat-api-urlmap \
  --ssl-certificates=webchat-api-cert

gcloud compute forwarding-rules create webchat-api-https-rule \
  --global \
  --target-https-proxy=webchat-api-https-proxy \
  --address=webchat-api-ip \
  --ports=443

# La IP que hay que darle al equipo de DNS es esta:
echo ""
echo "=== IP para el registro DNS de $DOMINIO ==="
gcloud compute addresses describe webchat-api-ip --global --format="get(address)"
echo "============================================"


# ---------------------------------------------------------------------------
# PASO 6 — (Lo hace el equipo de dominios de Sanitas, no gcloud)
# Crear un registro DNS tipo A: $DOMINIO -> la IP que imprimió el paso 5.
# Esperar a que propague (puede ser desde minutos hasta un par de horas).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PASO 7 — Verificar que todo funciona ANTES de bloquear la URL *.run.app
# ---------------------------------------------------------------------------
# 1) Confirmar que el certificado ya quedó ACTIVE (puede tardar tras el DNS):
#      gcloud compute ssl-certificates describe webchat-api-cert --format="get(managed.status)"
# 2) Probar la API por el dominio nuevo:
#      curl https://api.sanitasperu.com/health
# 3) Actualizar el <script data-api-base="..."> del widget en el sitio de
#    Sanitas (y cualquier lugar que use la URL *.run.app a mano) para que
#    apunte a https://api.sanitasperu.com — el widget seguirá funcionando
#    con las DOS URLs mientras tanto, así que no hay corte de servicio.
# 4) Probar login varias veces mal para confirmar que a la 5ª te bloquea
#    (ya debería pasar por el código; con Cloud Armor además debería
#    devolver 429 directamente del Load Balancer si repites muy rápido).


# ---------------------------------------------------------------------------
# PASO 8 — SOLO cuando el paso 7 esté 100% confirmado: cerrar la puerta de
# atrás. Sin esto, alguien puede seguir llamando directo a *.run.app y
# saltarse Cloud Armor por completo — el Load Balancer de nada sirve si la
# URL original sigue abierta al público.
# ---------------------------------------------------------------------------
# gcloud run services update webchat-api \
#   --region="$REGION" \
#   --ingress=internal-and-cloud-load-balancing
#
# (Lo dejé comentado a propósito — descomenta y corre esta línea recién
# cuando confirmes el paso 7. Revertirlo, si algo sale mal, es:
#   gcloud run services update webchat-api --region="$REGION" --ingress=all
# )
