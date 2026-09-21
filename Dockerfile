FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run inyecta PORT; uvicorn debe escuchar en 0.0.0.0 en ese puerto.
# --workers 1 porque el estado de conexiones WebSocket vive en memoria del proceso;
# escalar horizontalmente se hace con más instancias de Cloud Run (min-instances),
# no con más workers dentro de una misma instancia.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
