#!/bin/bash
# probar_voz_sanitas.sh — genera varias muestras de voz "Sánitas" usando
# Google Cloud Text-to-Speech (voces neuronales, mucho más naturales que
# espeak). Córrelo en Cloud Shell.
#
# Uso: ./probar_voz_sanitas.sh
# Te va a crear varios archivos .mp3 en esta misma carpeta — descárgalos
# (botón de "..." en Cloud Shell > Download, o `cloudshell download-files`)
# y escúchalos. Cuando elijas uno, me dices cuál (por nombre de voz) y lo
# integro al widget.

set -e

PROYECTO="sanitas-recetas-prod"
gcloud config set project "$PROYECTO" >/dev/null

# Habilita la API (si ya está habilitada, no hace nada)
gcloud services enable texttospeech.googleapis.com

TOKEN=$(gcloud auth print-access-token)

generar() {
  local voz="$1"
  local rate="$2"
  local pitch="$3"
  local salida="sanitas_${voz}.mp3"
  # El languageCode debe coincidir con el prefijo del nombre de la voz
  # (es-US-... -> es-US, es-ES-... -> es-ES), si no la API lo rechaza.
  local idioma=$(echo "$voz" | cut -d'-' -f1,2)

  echo "Generando $salida (voz=$voz, idioma=$idioma, rate=$rate, pitch=$pitch)..."

  curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "x-goog-user-project: $PROYECTO" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data "{
      \"input\": {\"text\": \"Sánitas\"},
      \"voice\": {\"languageCode\": \"$idioma\", \"name\": \"$voz\"},
      \"audioConfig\": {\"audioEncoding\": \"MP3\", \"speakingRate\": $rate, \"pitch\": $pitch}
    }" \
    "https://texttospeech.googleapis.com/v1/text:synthesize" > "_resp_${voz}.json"

  python3 -c "
import json, base64
d = json.load(open('_resp_${voz}.json'))
if 'audioContent' not in d:
    print('ERROR:', d)
else:
    open('${salida}', 'wb').write(base64.b64decode(d['audioContent']))
    print('OK -> ${salida}')
"
  rm -f "_resp_${voz}.json"
}

# Voces femeninas jóvenes/enérgicas en español para probar (Neural2 y
# Wavenet son las de mejor calidad). speakingRate > 1 = más rápido/animado;
# pitch > 0 = un poco más aguda/juvenil.
generar "es-US-Neural2-A"  1.15 2.0
generar "es-US-Wavenet-A"  1.15 2.0
generar "es-ES-Neural2-A"  1.15 2.0
generar "es-ES-Wavenet-C"  1.1  1.5

echo ""
echo "Listo. Archivos generados: sanitas_es-US-Neural2-A.mp3, sanitas_es-US-Wavenet-A.mp3,"
echo "sanitas_es-ES-Neural2-A.mp3, sanitas_es-ES-Wavenet-C.mp3"
echo "Descárgalos y escúchalos — dime cuál prefieres (o si quieres que ajuste rate/pitch de alguno)."
