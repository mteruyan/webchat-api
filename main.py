"""
webchat-api — servicio Cloud Run del webchat de Sanitas.

Endpoints:
  GET  /health         — healthcheck
  POST /login           — autentica asesor/supervisor (usuario/password) y devuelve un JWT
  WS   /ws/widget       — usuario final (widget embebible)
  WS   /ws/asesor       — consola de asesor (requiere ?token=<jwt> de /login)
  WS   /ws/supervisor   — consola de supervisor (requiere ?token=<jwt> de un usuario con rol='supervisor')
"""

import os
import io
import re
import uuid
import secrets
import json
import asyncio
import contextlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Header, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
import google.auth
import google.auth.transport.requests
from google.cloud import storage
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

from db import get_connection, release_connection
from bot_engine import iniciar_conversacion, recibir_entrada_usuario, conversacion_en_ia, entrar_a_nodo
from api_handlers import enviar_sms_danaconnect, enviar_llamada_otp, _generar_codigo_otp, validar_horario_consultas
import asignacion
import enlaces_directos
import auth
import reportes
import supervisor
import usuarios_sofi
import totp
import recordatorios
import push
import temas_consulta
import verificacion_whatsapp
import encuesta

LIMA_TZ = timezone(timedelta(hours=-5))

app = FastAPI(title="webchat-api")

# Solo el sitio real de Sanitas puede llamar a esta API desde un navegador.
# Las consolas propias (/asesor, /supervisor, /editor, /dashboard) no necesitan
# estar en esta lista: las sirve este mismo servicio, así que sus llamadas a
# la API son same-origin y el navegador no les aplica CORS.
# ALLOWED_ORIGINS permite agregar dominios sin tocar código (coma-separados);
# por defecto solo el dominio confirmado por Sanitas para el widget.
_origenes_default = "https://www.sanitasperu.com,https://sanitasperu.com"
allowed_origins = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _origenes_default).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Usado por Cloud Run para healthchecks; también sirve para probar la conexión a BD a mano.
    Es pública: solo dice "ok"/"error" (el detalle del error queda en los logs, no se le muestra a nadie).
    Responde 200 aunque la base falle a propósito: reiniciar la instancia no arregla la base y cortaría
    todos los WebSockets."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"status": "ok", "db": "ok"}
    except Exception:
        logger.warning("Healthcheck: la base de datos no respondió", exc_info=True)
        return {"status": "ok", "db": "error"}
    finally:
        if conn is not None:
            release_connection(conn)


@app.get("/widget.js")
def widget_js():
    """
    Sirve el script embebible. En el sitio de Sanitas:
      <script src="https://.../widget.js" data-api-base="https://.../" defer></script>
    """
    return FileResponse(
        "static/widget.js", media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# Agente de IA OCULTO (modo experimental). Nadie lo ve en ningún menú: se entra solo escribiendo
# una palabra clave secreta (IA_PALABRA_CLAVE) Y desde un celular de la lista blanca (IA_TESTERS,
# "51XXXXXXXXX" separados por comas) que ya pasó la verificación por código. Si falta cualquiera
# de las dos variables, o el celular no está en la lista, el mensaje se trata como texto normal,
# sin ninguna pista de que existe algo. Nodo del bot: "ia_conversacion" (tipo conversacion_ia).
# ---------------------------------------------------------------------------
NODO_IA = "ia_conversacion"


def _normalizar_celular_ia(valor) -> str:
    d = "".join(ch for ch in str(valor or "") if ch.isdigit())
    return "51" + d if len(d) == 9 else d


def _ia_testers(variable: str = "IA_TESTERS") -> set:
    return {n for n in (_normalizar_celular_ia(x) for x in (os.environ.get(variable) or "").split(",")) if n}


def _es_palabra_clave_ia(texto, variable: str = "IA_PALABRA_CLAVE") -> bool:
    clave = (os.environ.get(variable) or "").strip().lower()
    return bool(clave) and (texto or "").strip().lower() == clave


# Médico a domicilio / Ambulancia OCULTO (prueba): mismo doble candado que el agente de IA pero con su
# propia palabra clave (MAD_PALABRA_CLAVE) y su propia lista blanca (MAD_TESTERS), a propósito
# separada de IA_TESTERS: este flujo despacha profesionales de verdad y manda SMS reales.
# Nodo del bot: "mad_inicio" (no está enlazado desde ningún menú).
NODO_MAD = "mad_inicio"


def _celular_tester_de_sesion(sesion_token: str, variable: str = "IA_TESTERS"):
    """Celular (51XXXXXXXXX) si esta sesión tiene un celular YA VERIFICADO con código y está en
    la lista blanca; si no, None. Se valida contra el celular guardado en el servidor, nunca
    contra lo que mande el navegador."""
    testers = _ia_testers(variable)
    if not testers:
        return None
    try:
        datos = _obtener_datos_usuario(sesion_token)
    except Exception:  # noqa: BLE001
        return None
    celular = _normalizar_celular_ia(datos.get("telefono")) if datos else ""
    if datos and datos.get("verificado") and celular in testers:
        return celular
    return None


# ---------------------------------------------------------------------------
# Avisos push (PWA y widget del sitio). Ver push.py y migracion_push_avisos.sql.
# ---------------------------------------------------------------------------
_tareas_push: set = set()


def _lanzar_aviso_push(conversacion_id: int, tipo: str = "respuesta", ctx: dict | None = None):
    """Dispara el aviso sin frenar al asesor (la red del servicio push puede tardar)."""
    try:
        t = asyncio.get_running_loop().create_task(asyncio.to_thread(push.avisar_conversacion, conversacion_id, tipo, ctx))
    except RuntimeError:
        return
    _tareas_push.add(t)
    t.add_done_callback(_tareas_push.discard)


_golpes_push: dict = {}


def _limitar_push(request: Request, ruta: str, maximo: int = 30, ventana: int = 60):
    """Freno simple por IP: estos endpoints son públicos (el cliente no tiene login)."""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    clave = f"{ip}|{ruta}"          # un contador por IP y por endpoint
    ahora = time.time()
    marcas = [t for t in _golpes_push.get(clave, []) if ahora - t < ventana]
    if len(marcas) >= maximo:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes")
    marcas.append(ahora)
    _golpes_push[clave] = marcas
    if len(_golpes_push) > 5000:
        for k in [k for k, v in _golpes_push.items() if not v or ahora - v[-1] > ventana]:
            _golpes_push.pop(k, None)


_UUID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")


def _url_abrir_segura(url: str | None) -> str | None:
    """Solo rutas del propio servicio o páginas de los orígenes permitidos (el aviso abre esa dirección)."""
    if not url:
        return None
    if url.startswith("/") and not url.startswith("//"):
        return url[:300]
    for o in allowed_origins:
        if url == o or url.startswith(o.rstrip("/") + "/") or url.startswith(o.rstrip("/") + "?"):
            return url[:300]
    return None


class PushVisita(BaseModel):
    dispositivo_id: str
    sesion_token: str | None = None
    modo: str = "navegador"
    permiso: str | None = None
    url_abrir: str | None = None


class PushSuscripcion(BaseModel):
    dispositivo_id: str
    sesion_token: str | None = None
    modo: str = "navegador"
    suscripcion: dict
    url_abrir: str | None = None


class PushCancelar(BaseModel):
    dispositivo_id: str


def _validar_disp(dispositivo_id: str):
    if not _UUID_RE.match(dispositivo_id or ""):
        raise HTTPException(status_code=400, detail="dispositivo_id inválido")


def _push_no_disponible():
    return HTTPException(status_code=503, detail="Los avisos todavía no están habilitados en el servidor")


@app.get("/push/clave")
def push_clave():
    try:
        return {"clave": push.clave_publica_b64u()}
    except push.PushNoConfigurado:
        raise _push_no_disponible()


@app.post("/push/visita")
def push_visita(datos: PushVisita, request: Request):
    """El app/widget avisa que se abrió (modo 'app' = instalada) y en qué estado está el permiso."""
    _limitar_push(request, "visita")
    _validar_disp(datos.dispositivo_id)
    try:
        push.registrar_visita(datos.dispositivo_id, datos.sesion_token, datos.modo, datos.permiso,
                              request.headers.get("user-agent", ""), _url_abrir_segura(datos.url_abrir))
    except Exception as e:  # noqa: BLE001
        if "app_dispositivos" in str(e):
            raise _push_no_disponible()
        raise
    return {"ok": True}


@app.post("/push/suscribir")
def push_suscribir(datos: PushSuscripcion, request: Request):
    _limitar_push(request, "suscribir", maximo=10)
    _validar_disp(datos.dispositivo_id)
    try:
        push.clave_publica_b64u()
        push.guardar_suscripcion(datos.dispositivo_id, datos.sesion_token, datos.suscripcion, datos.modo,
                                 request.headers.get("user-agent", ""), _url_abrir_segura(datos.url_abrir))
    except push.PushNoConfigurado:
        raise _push_no_disponible()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        if "app_dispositivos" in str(e):
            raise _push_no_disponible()
        raise
    return {"ok": True}


@app.post("/push/cancelar")
def push_cancelar(datos: PushCancelar, request: Request):
    _limitar_push(request, "cancelar")
    _validar_disp(datos.dispositivo_id)
    push.cancelar_suscripcion(datos.dispositivo_id)
    return {"ok": True}


@app.post("/push/prueba")
async def push_prueba(datos: PushCancelar, request: Request):
    """Manda un aviso de prueba SOLO al dispositivo que lo pide (botón 'Probar' del app)."""
    _limitar_push(request, "prueba", maximo=5)
    _validar_disp(datos.dispositivo_id)
    r = await asyncio.to_thread(push.avisar_dispositivo, datos.dispositivo_id, "prueba")
    return r


# ---------------------------------------------------------------------------
# PWA experimental ("app" de Sofi): misma conversación del widget, instalable en
# el celular. La pantalla vive en /app; el service worker se sirve desde la raíz
# (/app-sw.js) para poder controlar el alcance /app.
# ---------------------------------------------------------------------------
_ARCHIVOS_APP = {
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png", "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png", "apple-touch-icon.png": "image/png",
    "icon-notif-192.png": "image/png", "badge-96.png": "image/png",
}


@app.get("/app")
def pwa_pantalla():
    return FileResponse("static/app.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/app-sw.js")
def pwa_service_worker():
    return FileResponse("static/app/sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/app"})


@app.get("/app/{archivo}")
def pwa_archivo(archivo: str):
    tipo = _ARCHIVOS_APP.get(archivo)
    if not tipo:
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(os.path.join("static/app", archivo), media_type=tipo,
                        headers={"Cache-Control": "public, max-age=3600"})


# Segunda capa de defensa contra XSS (además del escapeHtml() del lado del
# cliente) para las 4 consolas internas: aunque algo se cuele sin escapar,
# el navegador se niega a ejecutar código venido de otro origen, a incrustar
# la página dentro de un iframe ajeno (anti-clickjacking), o a cargar objetos
# insertados dinámicamente (plugins, <object>). 'unsafe-inline' en script/
# style sigue siendo necesario porque estas consolas usan <script> y estilos
# inline — no reemplaza el escapado de datos, es una capa adicional.
_CSP_CONSOLA = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://storage.googleapis.com; "
    "media-src 'self' https://storage.googleapis.com; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_HEADERS_CONSOLA = {
    # Siempre revalidar: así, tras un deploy, las consolas cargan la versión nueva sin Ctrl+F5.
    "Cache-Control": "no-cache",
    "Content-Security-Policy": _CSP_CONSOLA,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@app.get("/asesor")
def consola_asesor():
    """Consola de asesor — URL fija que cada asesor guarda en favoritos."""
    return FileResponse("static/asesor.html", media_type="text/html", headers=_HEADERS_CONSOLA)


@app.get("/static/supervisor_resumen.js")
def supervisor_resumen_js():
    """Tabla resumen de asesores de la consola de supervisor (se carga desde supervisor.html)."""
    return FileResponse("static/supervisor_resumen.js", media_type="application/javascript", headers=_HEADERS_CONSOLA)


@app.get("/supervisor")
def consola_supervisor():
    """Consola de supervisor — URL fija."""
    return FileResponse("static/supervisor.html", media_type="text/html", headers=_HEADERS_CONSOLA)


@app.get("/editor")
def consola_editor():
    """Editor de textos y orden de botones del bot — URL fija."""
    return FileResponse("static/editor.html", media_type="text/html", headers=_HEADERS_CONSOLA)


@app.get("/dashboard")
def consola_dashboard():
    """Dashboard de estadísticas de conversaciones — URL fija."""
    return FileResponse("static/dashboard.html", media_type="text/html", headers=_HEADERS_CONSOLA)


@app.post("/editor/subir-imagen")
async def subir_imagen_editor(file: UploadFile = File(...), authorization: str | None = Header(None)):
    """
    Sube contenido del bot (imágenes de menús/bienvenida, o un PDF adjunto —
    p.ej. los formatos de Reembolsos o la carta de tarifas) a un bucket
    DISTINTO al de las recetas — este es de lectura pública y permanente,
    porque es contenido de marca/formularios, no datos de salud de un paciente.
    Necesita la variable de entorno GCS_CONTENIDO_BUCKET.

    El mismo campo "imagen_url" del nodo se usa para ambos casos — el widget
    ya sabe mostrar un <img> si es imagen, o un botón de descarga si es PDF.
    """
    _verificar_editor(authorization)

    content_type = file.content_type or ""
    es_pdf = content_type == "application/pdf"
    if not content_type.startswith("image/") and not es_pdf:
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes o PDF")

    contenido = await file.read()
    if len(contenido) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Archivo muy grande (máximo 10 MB)")

    extension = (file.filename or "").split(".")[-1] if "." in (file.filename or "") else ("pdf" if es_pdf else "jpg")
    nombre_archivo = f"bot-content/{uuid.uuid4()}.{extension}"

    client = storage.Client()
    bucket_nombre = os.environ.get("GCS_CONTENIDO_BUCKET", "sanitas-webchat-contenido")
    bucket = client.bucket(bucket_nombre)
    blob = bucket.blob(nombre_archivo)
    blob.upload_from_string(contenido, content_type=file.content_type)

    return {"url": f"https://storage.googleapis.com/{bucket_nombre}/{nombre_archivo}"}


def _generar_url_firmada(nombre_archivo: str, minutos: int = 60, descargar_como: str | None = None) -> str:
    """Firma una URL de corta duración para un objeto del bucket privado de
    recetas. Se llama al vuelo cada vez que alguien necesita VER el archivo —
    nunca se persiste una URL firmada en la base de datos, porque expira
    (acá está el bug que reportó Miguel: una firmada guardada permanentemente
    se vuelve inaccesible en cuanto pasa su expiración)."""
    client = storage.Client()
    bucket = client.bucket(os.environ["GCS_BUCKET"])
    blob = bucket.blob(nombre_archivo)

    # Cloud Run no tiene una llave privada local para firmar — hay que pasarle
    # explícitamente el email + access_token para que use la IAM Credentials API
    # (requiere el rol roles/iam.serviceAccountTokenCreator sobre sí mismo).
    credentials, _ = google.auth.default()
    credentials.refresh(google.auth.transport.requests.Request())
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=minutos),
        method="GET",
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
        # Con esto el navegador DESCARGA el archivo en vez de mostrarlo (sin
        # necesitar CORS en el bucket). Solo se usa cuando el asesor/supervisor
        # pulsa "Descargar" en el visor de imágenes.
        **({"response_disposition": f'attachment; filename="{descargar_como}"'} if descargar_como else {}),
    )


# Documentos de oficina permitidos en /upload: extensión -> content-type
# canónico. No se incluyen formatos con macros (.xlsm, .docm).
_DOCUMENTOS_OFIMATICA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
}


def _firma_documento_valida(extension: str, contenido: bytes) -> bool:
    """xlsx/docx son ZIP ('PK\\x03\\x04'); xls/doc son OLE2 (D0 CF 11 E0); csv es
    texto plano (sin bytes nulos). Evita que un ejecutable renombrado a .xlsx
    quede guardado y se sirva como si fuera un documento."""
    if extension in ("xlsx", "docx"):
        return contenido.startswith(b"PK\x03\x04")
    if extension in ("xls", "doc"):
        return contenido.startswith(b"\xd0\xcf\x11\xe0")
    if extension == "csv":
        return b"\x00" not in contenido[:4096]
    return False


_EXT_IMAGEN_POR_TIPO = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/pjpeg": "jpg", "image/png": "png",
                        "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}
_EXT_IMAGEN_NOMBRE = {"jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "bmp", "heic", "heif", "heics", "avif", "tif", "tiff"}


def _tipo_imagen_real(contenido: bytes) -> str | None:
    """Deduce el formato por la FIRMA del archivo (no por el nombre ni el content-type, que los celulares mandan
    mal): devuelve jpg|png|gif|webp|bmp|heic|avif|tiff, o None si no parece una imagen."""
    c = contenido[:16]
    if c[:3] == b"\xff\xd8\xff":
        return "jpg"
    if c[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if c[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if c[:4] == b"RIFF" and c[8:12] == b"WEBP":
        return "webp"
    if c[:2] == b"BM":
        return "bmp"
    if c[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if c[4:8] == b"ftyp":
        marca = c[8:12]
        if marca in (b"avif", b"avis"):
            return "avif"
        if marca in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"heif"):
            return "heic"
    return None


# Conversión de HEIC/AVIF/TIFF a JPG: tope de tamaño y de conversiones simultáneas, para que una o varias
# imágenes enormes no agoten la memoria de la única instancia. 50 MP deja pasar las fotos de 48 MP de los
# iPhone recientes (~150 MB al decodificar); 2 a la vez como máximo.
_MAX_PIXELES_CONVERSION = 50_000_000
_conversiones_imagen = threading.BoundedSemaphore(2)


def _normalizar_imagen(contenido: bytes, content_type: str, nombre: str) -> tuple[bytes, str, str] | None:
    """Para imágenes: (contenido, extension, content_type) ya normalizados, o None si NO es una imagen.
    - La extensión sale del formato real, no del nombre (un 'Captura 10.44.12' ya no queda como archivo '.12').
    - HEIC/HEIF (iPhone), AVIF y TIFF se convierten a JPEG para que se vean en Windows y en el navegador.
    - Se aplica la orientación EXIF para que la foto no salga girada."""
    real = _tipo_imagen_real(contenido)
    ext_nombre = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    if real is None:
        # Firma desconocida: si dice ser imagen se deja pasar como estaba (no se rompe lo que ya funcionaba).
        if (content_type or "").startswith("image/") and (content_type or "") in _EXT_IMAGEN_POR_TIPO:
            return contenido, _EXT_IMAGEN_POR_TIPO[content_type], content_type
        return None
    if real in ("jpg", "png", "gif", "webp", "bmp"):
        tipo = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}[real]
        return contenido, real, tipo
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = _MAX_PIXELES_CONVERSION
        if real in ("heic", "avif"):
            import pillow_heif
            pillow_heif.register_heif_opener()
        with _conversiones_imagen:
            img = Image.open(io.BytesIO(contenido))   # solo lee la cabecera: aquí todavía no se decodifica
            ancho, alto = img.size
            if ancho * alto > _MAX_PIXELES_CONVERSION:
                # Una imagen chica en bytes puede pedir GB de memoria al decodificarla (y hay UNA sola
                # instancia: si se queda sin memoria se caen todas las conversaciones). Se guarda tal cual.
                logging.getLogger(__name__).warning(
                    "Imagen %s de %sx%s px supera el límite de conversión; se guarda sin convertir", real, ancho, alto)
                return contenido, real if real != "heic" else (ext_nombre or "heic"), content_type or "application/octet-stream"
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            salida = io.BytesIO()
            img.save(salida, format="JPEG", quality=92, optimize=True)
            return salida.getvalue(), "jpg", "image/jpeg"
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("No se pudo convertir la imagen %s a JPEG; se guarda tal cual", real)
        return contenido, real if real != "heic" else (ext_nombre or "heic"), content_type or "application/octet-stream"


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    conversacion_id: int | None = Form(None),
    sesion_token: str | None = Form(None),
    authorization: str | None = Header(None),
):
    """
    Recibe un archivo (imagen o PDF de receta) y lo sube a Cloud Storage.
    Necesita la variable de entorno GCS_BUCKET con el nombre del bucket
    (privado — sin acceso público, ver notas de deploy).

    Antes estaba completamente abierto (sin login, sin tamaño ni tipo de
    archivo validados) — cualquiera en internet podía subir lo que quisiera
    al bucket privado. Ahora lo usan dos tipos de llamante, cada uno con su
    propia forma de probar que tiene derecho a subir:
      - El WIDGET (paciente, sin login) manda conversacion_id + sesion_token,
        igual que /widget/archivo — solo puede si esa sesión es dueña de esa
        conversación.
      - Las consolas de asesor/supervisor (pegar una imagen con Ctrl+V para
        mandarla al paciente) mandan su propio JWT de staff en Authorization.
    Si no viene ninguna de las dos formas, o no valida, se rechaza. Además
    se aplican los mismos límites de tipo/tamaño que /editor/subir-imagen.

    Devuelve DOS cosas:
    - "url": una URL FIRMADA de 1 hora, para que el flujo del bot (p.ej.
      procesar-receta, que la descarga casi de inmediato) la use ya mismo.
    - "ruta": el nombre del objeto dentro del bucket (sin firmar). Es lo que
      se debe GUARDAR en la base de datos para referencia futura — una URL
      firmada nunca se persiste, porque expira; en su lugar, cuando un asesor
      o supervisor necesite VER el archivo más tarde, se pide una firmada
      NUEVA a /archivo-receta (autenticado) con esta ruta.
    """
    autorizado = False
    if authorization and authorization.startswith("Bearer "):
        staff_id = auth.decodificar_token(authorization[len("Bearer "):])
        if staff_id is not None and _obtener_rol(staff_id) is not None:
            autorizado = True

    if not autorizado:
        if not conversacion_id or not sesion_token:
            raise HTTPException(status_code=401, detail="No autorizado")
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM conversaciones c
                       JOIN usuarios_webchat u ON u.id = c.usuario_id
                       WHERE c.id = %s AND u.sesion_token = %s""",
                    (conversacion_id, sesion_token),
                )
                if cur.fetchone() is None:
                    raise HTTPException(status_code=403, detail="No autorizado")
        finally:
            release_connection(conn)

    content_type = file.content_type or ""
    es_pdf = content_type == "application/pdf"
    es_audio = content_type.startswith("audio/")

    # Excel/Word/CSV: se valida por EXTENSIÓN del nombre (no por content-type)
    # porque los navegadores/Windows mandan cualquier cosa para estos
    # (p.ej. un .csv llega como application/vnd.ms-excel, o un .xlsx como
    # application/octet-stream). Por eso se deduce el content-type canónico de
    # la extensión y se verifica la firma real del archivo más abajo.
    extension_original = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    tipo_documento = _DOCUMENTOS_OFIMATICA.get(extension_original)
    es_documento = tipo_documento is not None

    nombre_original = file.filename or ""
    # Algunos celulares/navegadores mandan la foto como application/octet-stream o con otro tipo: se deja pasar
    # si el NOMBRE parece de imagen y luego se verifica la firma real del archivo.
    parece_imagen = content_type.startswith("image/") or extension_original in _EXT_IMAGEN_NOMBRE
    if not parece_imagen and not es_pdf and not es_audio and not es_documento:
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes, PDF, audios, Excel, CSV o Word")

    contenido = await file.read()
    limite = 5 * 1024 * 1024 if es_audio else 10 * 1024 * 1024
    if len(contenido) > limite:
        raise HTTPException(status_code=400, detail="Archivo muy grande")
    if es_documento and not _firma_documento_valida(extension_original, contenido):
        raise HTTPException(status_code=400, detail="El archivo no es un documento válido")

    # Los navegadores graban audio como webm/opus (Chrome/Edge/Firefox) u
    # ogg/mp4 (Safari) — el nombre real del archivo que manda MediaRecorder
    # casi nunca trae extensión útil, así que se deduce del content-type.
    _EXTENSIONES_AUDIO = {
        "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
        "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    }
    if es_audio:
        extension = _EXTENSIONES_AUDIO.get(content_type.split(";")[0].strip(), "webm")
    elif es_documento:
        extension = extension_original
    else:
        extension = (file.filename or "").split(".")[-1] if "." in (file.filename or "") else ("pdf" if es_pdf else "jpg")
    tipo_subida = tipo_documento if es_documento else file.content_type
    if parece_imagen and not es_pdf and not es_audio and not es_documento:
        norm = await asyncio.to_thread(_normalizar_imagen, contenido, content_type, nombre_original)
        if norm is None:
            raise HTTPException(status_code=400, detail="El archivo no es una imagen válida")
        contenido, extension, tipo_subida = norm
    nombre_archivo = f"webchat/{uuid.uuid4()}.{extension}"

    client = storage.Client()
    bucket = client.bucket(os.environ["GCS_BUCKET"])
    blob = bucket.blob(nombre_archivo)
    blob.upload_from_string(contenido, content_type=tipo_subida)

    url = _generar_url_firmada(nombre_archivo, minutos=60)
    return {"url": url, "ruta": nombre_archivo}


def _verificar_staff_por_query(token: str | None) -> int:
    """Para endpoints que se abren desde un <a href> o <img src> directo
    (no se le puede poner un header Authorization a un click), así que el
    token viaja como query param — mismo patrón que ya usan /ws/asesor y
    /ws/supervisor. Cualquier asesor/supervisor/editor activo con sesión
    vigente puede pedir la URL firmada; no hace falta un rol específico,
    ya es información que ese archivo ya le llegó a alguno de ellos por chat."""
    if not token:
        raise HTTPException(status_code=401, detail="Falta el token")
    asesor_id = auth.decodificar_token(token)
    if asesor_id is None:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    rol = _obtener_rol(asesor_id)
    if rol is None:
        raise HTTPException(status_code=403, detail="Cuenta inactiva")
    return asesor_id


@app.get("/archivo-receta")
async def ver_archivo_receta(ruta: str, token: str | None = None, descargar: bool = False):
    """
    Genera una URL firmada NUEVA (corta duración) para que un asesor o
    supervisor autenticado pueda ver un archivo que el paciente subió
    (receta, foto, etc.), sin exponer el bucket como público y sin guardar
    nunca una URL firmada permanente en la base de datos.

    Uso: <a href="{urlBase}/archivo-receta?ruta=webchat/xxx.jpg&token={jwt}">
    Redirige (302) a la URL firmada — el navegador la sigue automáticamente,
    tanto en un click de <a> como en un <img src>.
    """
    _verificar_staff_por_query(token)

    # La ruta SIEMPRE debe quedar dentro de "webchat/" (carpeta de archivos
    # subidos por pacientes) — nunca se debe poder pedir un objeto arbitrario
    # del bucket ni salir de esa carpeta con "..".
    if not ruta or ".." in ruta or not ruta.startswith("webchat/"):
        raise HTTPException(status_code=400, detail="Ruta inválida")

    nombre_descarga = None
    if descargar:
        # Nombre seguro (solo el último tramo de la ruta, sin comillas ni
        # saltos de línea que rompan el header Content-Disposition).
        base = os.path.basename(ruta)
        nombre_descarga = re.sub(r"[^A-Za-z0-9._\-]", "_", base)[:120] or "imagen"
    url = _generar_url_firmada(ruta, minutos=15, descargar_como=nombre_descarga)
    return RedirectResponse(url)


@app.get("/receta-correo")
async def ver_receta_correo(ruta: str):
    """
    Variante de /archivo-receta SIN login de staff — es el link que se manda
    por correo a las asesoras cuando llega una receta nueva (ver
    api_handlers._enviar_correo_receta_asesoras). Ese correo va a un buzón
    compartido (sofimedicamentos@sanitasperu.com), no a la sesión de un
    asesor puntual, así que no hay un JWT que pedirle antes de redirigir.

    El nombre de objeto es un UUID (no adivinable ni listable) y queda
    restringido a la carpeta "webchat/", igual que /archivo-receta — el nivel
    de exposición es menor al que tenía el bucket público que usaba este
    mismo flujo en WhatsApp (ahí todo el bucket era público; acá solo se
    puede pedir, uno por uno, un archivo puntual ya conocido).
    """
    if not ruta or ".." in ruta or not ruta.startswith("webchat/"):
        raise HTTPException(status_code=400, detail="Ruta inválida")

    url = _generar_url_firmada(ruta, minutos=15)
    return RedirectResponse(url)


# Orígenes externos permitidos para /widget/redirigir — un enlace generado
# por OTRO servicio (ej. "Generar Excel" de sofi-apis) y que el bot manda
# como texto plano dentro de un mensaje. Solo se permite redirigir a
# destinos que empiecen exactamente con uno de estos prefijos, para que
# este endpoint no se vuelva un "open redirect" que cualquiera pueda usar
# para reenviar a un sitio arbitrario con el dominio de Sanitas por delante.
_DESTINOS_REDIRIGIR_PERMITIDOS = (
    "https://storage.googleapis.com/sanitas-recetas-archivos/",
)


@app.get("/widget/redirigir")
async def redirigir_descarga(u: str):
    """
    Oculta, en el texto visible del chat, la URL real de un archivo que
    genera OTRO servicio (ej. el Excel de "Generar Excel", generado por
    sofi-apis) — en vez de mostrar esa URL tal cual en el mensaje del bot,
    el widget la reemplaza por un link corto a este mismo dominio con una
    etiqueta genérica ("⬇️ Descargar archivo"); recién al hacer clic, este
    endpoint decodifica el destino real y redirige (302).

    "u" viaje en base64 URL-safe (sin relleno "=") de la URL destino —
    codificado del lado del widget, ver base64UrlEncode() en widget.js.

    Como el destino final SÍ queda visible en la barra de direcciones del
    navegador una vez que la descarga arranca (es inevitable, ahí es donde
    vive el archivo de verdad), esto no lo oculta del todo — solo evita que
    se vea escrita en el mensaje del chat, que era el pedido concreto.
    """
    import base64
    relleno = "=" * (-len(u) % 4)
    try:
        destino = base64.urlsafe_b64decode((u + relleno).encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Link inválido")

    if not destino.startswith(_DESTINOS_REDIRIGIR_PERMITIDOS):
        raise HTTPException(status_code=400, detail="Destino no permitido")

    return RedirectResponse(destino)


@app.get("/widget/archivo")
async def ver_archivo_widget(conversacion_id: int, ruta: str, sesion_token: str):
    """
    Igual que /archivo-receta pero para el WIDGET (sin login de staff) — la
    persona nunca se autentica, así que en su lugar se valida que el
    sesion_token que trae sea justo el dueño de esa conversacion_id. Así solo
    puede pedir imágenes de SU PROPIA conversación, nunca de otra.

    Uso: <img src="{urlBase}/widget/archivo?conversacion_id=123&ruta=webchat/xxx.jpg&sesion_token=...">
    """
    if not ruta or ".." in ruta or not ruta.startswith("webchat/"):
        raise HTTPException(status_code=400, detail="Ruta inválida")
    if not sesion_token:
        raise HTTPException(status_code=401, detail="Falta la sesión")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s AND u.sesion_token = %s""",
                (conversacion_id, sesion_token),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="No autorizado")
    finally:
        release_connection(conn)

    url = _generar_url_firmada(ruta, minutos=15)
    return RedirectResponse(url)


@app.get("/conversacion-pdf")
async def descargar_conversacion_pdf(conversacion_id: int, token: str | None = None):
    """
    Genera y devuelve un PDF con el historial completo de una conversación
    (cabecera con datos del paciente + burbujas de mensajes), para que un
    asesor o supervisor pueda descargarla y archivarla fuera del sistema.

    Uso: <a href="{urlBase}/conversacion-pdf?conversacion_id=123&token={jwt}">
    """
    staff_id = _verificar_staff_por_query(token)
    # Un supervisor de campaña solo descarga conversaciones de sus campañas.
    if not supervisor.conversacion_en_alcance(conversacion_id, supervisor.obtener_alcance(staff_id)):
        raise HTTPException(status_code=403, detail="Esa conversación es de una campaña que no tienes asignada")

    info = _obtener_info_conversacion(conversacion_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    historial = _obtener_historial(conversacion_id)
    pdf_bytes = _generar_pdf_conversacion(conversacion_id, info, historial)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="conversacion_{conversacion_id}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Manejo de conexiones WebSocket activas (en memoria del proceso — con
# --workers 1 y min-instances=1 esto es seguro para el volumen actual;
# ver nota de Redis pub/sub en el documento de diseño si se necesita
# escalar a más de una instancia más adelante).
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.widget_connections: dict[int, WebSocket] = {}       # conversacion_id -> ws
        self.asesor_connections: dict[int, WebSocket] = {}       # asesor_id -> ws
        self.supervisor_connections: dict[int, WebSocket] = {}   # supervisor_id -> ws
        # Alcance de cada supervisor (None = máximo, ve todo; si no, las campañas que puede ver).
        self.supervisor_alcance: dict[int, dict | None] = {}
        self.escuchas: dict[int, set] = {}                       # conversacion_id -> {supervisor_id, ...}
        # Asesores que están "ayudando en el bot" a una conversación que todavía
        # no llega a un asesor (mismo concepto que `escuchas`, pero para asesores).
        self.escuchas_asesor: dict[int, set] = {}                # conversacion_id -> {asesor_id, ...}

    async def connect_widget(self, conversacion_id: int, ws: WebSocket):
        await ws.accept()
        anterior = self.widget_connections.get(conversacion_id)
        self.widget_connections[conversacion_id] = ws
        if anterior is not None and anterior is not ws:
            # La misma conversación se abrió en otro dispositivo o pestaña (p. ej. tras
            # escanear el QR): la conexión vieja se cierra avisándole, para que no
            # compitan por la conversación ni se reconecten una a la otra sin fin.
            try:
                await anterior.send_json({"type": "sesion_en_otro_dispositivo", "payload": {}})
                await anterior.close(code=4000)
            except Exception:
                pass

    def reasignar_widget(self, conversacion_id: int, ws: WebSocket):
        """Igual que connect_widget pero SIN aceptar de nuevo — para cuando se
        reusa la misma conexión ya aceptada bajo un conversacion_id nuevo
        (iniciar_nueva_conversacion / reiniciar_datos)."""
        self.widget_connections[conversacion_id] = ws

    def disconnect_widget(self, conversacion_id: int, ws: WebSocket = None):
        """Con `ws`, solo quita el registro si esa sigue siendo LA conexión vigente
        (una conexión desplazada por otro dispositivo no debe borrar a la nueva)."""
        if ws is not None and self.widget_connections.get(conversacion_id) is not ws:
            return
        self.widget_connections.pop(conversacion_id, None)

    async def connect_asesor(self, asesor_id: int, ws: WebSocket):
        await ws.accept()
        self.asesor_connections[asesor_id] = ws

    def es_conexion_vigente_asesor(self, asesor_id: int, ws: WebSocket) -> bool:
        """True si `ws` sigue siendo LA conexión registrada de ese asesor. Al
        reconectar, el socket viejo puede enterarse de que se cayó DESPUÉS de
        que el nuevo ya conectó; en ese caso no debe marcar al asesor como
        desconectado (cerraría la sesión y la pausa del socket nuevo)."""
        return self.asesor_connections.get(asesor_id) is ws

    def disconnect_asesor(self, asesor_id: int):
        self.asesor_connections.pop(asesor_id, None)
        for ayudando in self.escuchas_asesor.values():
            ayudando.discard(asesor_id)

    def registrar_escucha_asesor(self, asesor_id: int, conversacion_id: int):
        self.escuchas_asesor.setdefault(conversacion_id, set()).add(asesor_id)

    def quitar_escucha_asesor(self, asesor_id: int, conversacion_id: int):
        self.escuchas_asesor.get(conversacion_id, set()).discard(asesor_id)

    def quitar_todas_escuchas_asesor(self, asesor_id: int):
        for ayudando in self.escuchas_asesor.values():
            ayudando.discard(asesor_id)

    async def connect_supervisor(self, supervisor_id: int, ws: WebSocket, alcance: dict | None = None):
        await ws.accept()
        self.supervisor_connections[supervisor_id] = ws
        self.supervisor_alcance[supervisor_id] = alcance

    def es_conexion_vigente_supervisor(self, supervisor_id: int, ws: WebSocket) -> bool:
        """True si `ws` sigue siendo LA conexión registrada de ese supervisor (el socket
        viejo puede enterarse de su caída después de que el nuevo ya conectó)."""
        return self.supervisor_connections.get(supervisor_id) is ws

    def disconnect_supervisor(self, supervisor_id: int):
        self.supervisor_connections.pop(supervisor_id, None)
        self.supervisor_alcance.pop(supervisor_id, None)
        for escuchando in self.escuchas.values():
            escuchando.discard(supervisor_id)

    def registrar_escucha(self, supervisor_id: int, conversacion_id: int):
        self.escuchas.setdefault(conversacion_id, set()).add(supervisor_id)

    def quitar_escucha(self, supervisor_id: int, conversacion_id: int):
        self.escuchas.get(conversacion_id, set()).discard(supervisor_id)

    async def enviar_a_widget(self, conversacion_id: int, evento: dict):
        ws = self.widget_connections.get(conversacion_id)
        if ws:
            await ws.send_json(evento)
        elif evento.get("type") in ("mensaje_asesor", "archivo_asesor"):
            # El cliente cerró la ventana/app: le llega un aviso push (si activó avisos).
            _lanzar_aviso_push(conversacion_id,
                               "archivo" if evento.get("type") == "archivo_asesor" else "respuesta",
                               {"asesor": (evento.get("payload") or {}).get("asesor_nombre")})

    async def enviar_a_asesor(self, asesor_id: int, evento: dict):
        ws = self.asesor_connections.get(asesor_id)
        if ws:
            await ws.send_json(evento)

    async def enviar_a_supervisor(self, supervisor_id: int, evento: dict):
        ws = self.supervisor_connections.get(supervisor_id)
        if ws:
            # Un supervisor de campaña solo recibe lo de sus campañas (ver supervisor.filtrar_evento).
            evento = supervisor.filtrar_evento(evento, self.supervisor_alcance.get(supervisor_id))
            if evento is not None:
                await ws.send_json(evento)

    async def enviar_a_todos_supervisores(self, evento: dict):
        for supervisor_id, ws in list(self.supervisor_connections.items()):
            e = supervisor.filtrar_evento(evento, self.supervisor_alcance.get(supervisor_id))
            if e is not None:
                await ws.send_json(e)

    async def enviar_a_todos_asesores(self, evento: dict):
        for ws in self.asesor_connections.values():
            await ws.send_json(evento)

    async def notificar_escuchas(self, conversacion_id: int, evento: dict):
        for supervisor_id in list(self.escuchas.get(conversacion_id, set())):
            await self.enviar_a_supervisor(supervisor_id, evento)
        # Asesores que están ayudando en el bot a esta conversación: reciben el
        # mismo evento envuelto, para que la consola de asesor no lo confunda
        # con los eventos de sus propias conversaciones asignadas.
        for asesor_id in list(self.escuchas_asesor.get(conversacion_id, set())):
            try:
                await self.enviar_a_asesor(asesor_id, {
                    "type": "ayuda_bot_evento",
                    "payload": {"evento": evento.get("type"), **(evento.get("payload") or {})},
                })
            except Exception:
                pass  # un asesor con la conexión caída no debe frenar al resto


manager = ConnectionManager()


def _crear_o_reanudar_conversacion(sesion_token: str):
    """Crea (o recupera) el usuario_webchat por sesion_token. Si ya tiene una
    conversación sin cerrar (bot/en_cola/asignada), la reanuda en vez de crear
    una nueva — crítico para que la reconexión automática no pierda el
    progreso ni una asignación con asesor ya en curso.
    Devuelve (conversacion_id, estado, es_nueva)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM usuarios_webchat WHERE sesion_token = %s", (sesion_token,)
            )
            row = cur.fetchone()
            if row:
                usuario_id = row[0]
                cur.execute(
                    "UPDATE usuarios_webchat SET ultima_actividad = %s WHERE id = %s",
                    (datetime.now(LIMA_TZ), usuario_id),
                )
            else:
                cur.execute(
                    "INSERT INTO usuarios_webchat (sesion_token, creado_en, ultima_actividad) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (sesion_token, datetime.now(LIMA_TZ), datetime.now(LIMA_TZ)),
                )
                usuario_id = cur.fetchone()[0]

            cur.execute(
                "SELECT id, estado FROM conversaciones WHERE usuario_id = %s AND estado != 'cerrada' "
                "ORDER BY iniciado_en DESC LIMIT 1",
                (usuario_id,),
            )
            row = cur.fetchone()
            if row:
                conn.commit()
                return row[0], row[1], False  # reanudada, no es nueva

            cur.execute(
                "INSERT INTO conversaciones (usuario_id, estado, iniciado_en) "
                "VALUES (%s, 'bot', %s) RETURNING id",
                (usuario_id, datetime.now(LIMA_TZ)),
            )
            conversacion_id = cur.fetchone()[0]
        conn.commit()
        return conversacion_id, "bot", True  # es nueva
    finally:
        release_connection(conn)


_ORIGENES_VALIDOS = {"burbuja", "invitacion_sms", "enlace_sms", "enlace_directo", "nueva_conversacion", "traspaso_qr"}


def _registrar_origen(conversacion_id: int, origen, pagina=None):
    """Guarda de dónde abrió el chat la persona (solo si todavía no tiene
    origen: el primero que llega es el bueno). Es una métrica: si falla (por
    ejemplo, falta la migración de la columna) NO debe afectar al chat."""
    origen = origen if origen in _ORIGENES_VALIDOS else "otro"
    ruta = ""
    if isinstance(pagina, str):
        ruta = pagina.split("?")[0].split("#")[0].strip()
        ruta = "".join(ch for ch in ruta if ch.isprintable())[:120]
        if not ruta.startswith("/"):
            ruta = ""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversaciones SET origen = %s, pagina_origen = %s "
                "WHERE id = %s AND origen IS NULL",
                (origen, ruta or None, conversacion_id),
            )
        conn.commit()
    except Exception as e:
        logger.warning("No se pudo registrar el origen de la conversación %s: %s", conversacion_id, e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_connection(conn)


def _hay_escuchas(conversacion_id: int) -> bool:
    """True si algún supervisor está escuchando o algún asesor está ayudando
    en el bot a esta conversación."""
    return bool(manager.escuchas.get(conversacion_id) or manager.escuchas_asesor.get(conversacion_id))


def _ultimo_mensaje_id(conversacion_id: int) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM mensajes WHERE conversacion_id = %s", (conversacion_id,))
            return cur.fetchone()[0]
    finally:
        release_connection(conn)


async def _notificar_escuchas_mensajes_nuevos(conversacion_id: int, desde_id: int):
    """Reenvía a quienes escuchan/ayudan los mensajes guardados DESPUÉS de
    `desde_id` (lo que escribió el cliente y lo que respondió el bot en un
    turno del flujo del bot, que no pasa por ningún otro aviso en vivo)."""
    def _leer():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, emisor_tipo, tipo_contenido, contenido FROM mensajes
                       WHERE conversacion_id = %s AND id > %s ORDER BY id ASC""",
                    (conversacion_id, desde_id),
                )
                return cur.fetchall()
        finally:
            release_connection(conn)
    filas = await asyncio.to_thread(_leer)

    for mid, emisor, tipo_contenido, contenido in filas:
        payload = {"conversacion_id": conversacion_id, "emisor_tipo": emisor, "mensaje_id": mid}
        if tipo_contenido == "archivo":
            payload.update({"texto": "📎 Archivo enviado", "ruta": contenido})
        elif tipo_contenido == "opciones":
            payload.update({"texto": "", "tipo_contenido": "opciones", "contenido": contenido})
        elif tipo_contenido == "formulario":
            payload["texto"] = "📋 Formulario enviado" if emisor == "usuario" else "📋 Formulario del bot"
        else:
            payload["texto"] = contenido or ""
        await manager.notificar_escuchas(conversacion_id, {"type": "mensaje_conversacion", "payload": payload})


async def _procesar_eventos_bot(conversacion_id: int, websocket: WebSocket, eventos: list):
    """Manda los eventos del bot al widget y, si alguno es 'en_cola', dispara
    el intento de asignación inmediata."""
    for evento in eventos:
        if str(evento.get("type") or "").startswith("_"):
            continue  # evento interno del motor (p. ej. "_celular_verificado"): no es para el widget
        await websocket.send_json(evento)
        if evento.get("type") == "en_cola":
            asignado = await asignacion.intentar_asignar_conversacion(conversacion_id, manager)
            if asignado is None:
                # Nadie disponible: queda esperando. Se le tranquiliza sobre cerrar la ventana.
                texto = (await asyncio.to_thread(supervisor.obtener_configuracion, "mensaje_en_cola_guardado")
                         or MENSAJES_AUTOMATICOS["mensaje_en_cola_guardado"]["default"])
                await websocket.send_json({"type": "aviso_cola", "payload": {"texto": texto}})


def _obtener_historial(conversacion_id: int) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, emisor_tipo, emisor_id, tipo_contenido, contenido, metadata,
                          enviado_en, leido_en
                   FROM mensajes WHERE conversacion_id = %s ORDER BY enviado_en ASC, id ASC""",
                (conversacion_id,),
            )
            filas = cur.fetchall()
    finally:
        release_connection(conn)

    # Para resolver la "cita" de un "responder a" sin una consulta aparte por
    # cada mensaje: como siempre se cita un mensaje de la MISMA conversación,
    # ya viene en esta misma tanda — se arma un diccionario id -> mensaje.
    por_id = {fila[0]: fila for fila in filas}

    def _preview(mensaje_id):
        fila = por_id.get(mensaje_id)
        if not fila:
            return None
        _, e_tipo, _, t_c, c, meta, *_ = fila
        meta = _parsear_metadata(meta)
        if meta.get("eliminado_en"):
            texto = "Este mensaje se eliminó"
        elif t_c == "archivo":
            texto = "📎 Archivo"
        elif t_c == "boton_click":
            texto = c or "Opción seleccionada"
        else:
            texto = c or ""
        return {"mensaje_id": mensaje_id, "emisor_tipo": e_tipo, "texto": texto[:160]}

    resultado = []
    for mid, e, emisor_id, t, c, meta, ts, leido in filas:
        meta = _parsear_metadata(meta)
        resultado.append({
            "id": mid, "emisor_tipo": e, "emisor_id": emisor_id, "tipo_contenido": t,
            "contenido": c, "enviado_en": ts.isoformat(), "leido": leido is not None,
            "editado_en": meta.get("editado_en"),
            "eliminado_en": meta.get("eliminado_en"),
            "eliminado_por": meta.get("eliminado_por"),
            "reacciones": _resumen_reacciones(meta),
            "respondido_a": _preview(meta.get("respondido_a_id")),
        })
    return resultado


# --- Historial de conversaciones pasadas en el widget -----------------------
HISTORIAL_PREVIO_DIAS = 90
HISTORIAL_PREVIO_BLOQUE = 30
_gcs_cliente = None


def _archivo_existe(ruta: str) -> bool:
    """True si el archivo todavía está en el bucket (puede haberse borrado por antigüedad)."""
    global _gcs_cliente
    if not ruta or ".." in ruta or not ruta.startswith("webchat/"):
        return False
    try:
        if _gcs_cliente is None:
            _gcs_cliente = storage.Client()
        return _gcs_cliente.bucket(os.environ["GCS_BUCKET"]).blob(ruta).exists()
    except Exception:
        logger.warning("No se pudo comprobar si existe %s", ruta, exc_info=True)
        return False


def _historial_previo(sesion_token: str, conversacion_actual: int, antes_de_id) -> dict:
    """Mensajes de conversaciones ANTERIORES de este mismo navegador (hasta 90 días, en bloques),
    del más reciente hacia atrás. Solo si el celular está verificado; respeta la fecha de corte
    'historial_desde' (se fija al usar "No soy yo / Cambiar mis datos")."""
    vacio = {"disponible": False, "mensajes": [], "conversaciones": {}, "hay_mas": False}
    try:
        antes = int(antes_de_id) if antes_de_id not in (None, "", 0) else None
    except (TypeError, ValueError):
        antes = None
    ahora = datetime.now(LIMA_TZ)
    desde_tope = ahora - timedelta(days=HISTORIAL_PREVIO_DIAS)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, verificado_en IS NOT NULL AND telefono IS NOT NULL, historial_desde "
                "FROM usuarios_webchat WHERE sesion_token = %s", (sesion_token,))
            fila = cur.fetchone()
            if not fila or not fila[1]:
                conn.rollback()
                return vacio
            usuario_id, _, corte = fila
            desde = max(desde_tope, corte) if corte else desde_tope
            cur.execute(
                """SELECT m.id, m.conversacion_id, m.emisor_tipo, m.tipo_contenido, m.contenido,
                          m.metadata, m.enviado_en, c.iniciado_en, c.cerrado_en, cam.nombre
                   FROM mensajes m
                   JOIN conversaciones c ON c.id = m.conversacion_id
                   LEFT JOIN campanas cam ON cam.id = c.campana_id
                   WHERE c.usuario_id = %s AND c.id <> %s
                     AND (%s::bigint IS NULL OR m.id < %s::bigint)
                     AND m.enviado_en >= %s
                     AND m.tipo_contenido IN ('texto', 'archivo', 'boton_click')
                   ORDER BY m.id DESC LIMIT %s""",
                (usuario_id, conversacion_actual, antes, antes, desde, HISTORIAL_PREVIO_BLOQUE + 1),
            )
            filas = cur.fetchall()
        conn.rollback()
    except Exception:
        conn.rollback()
        logger.exception("No se pudo leer el historial previo (¿falta migracion_historial_previo.sql?)")
        return vacio
    finally:
        release_connection(conn)

    hay_mas = len(filas) > HISTORIAL_PREVIO_BLOQUE
    filas = list(reversed(filas[:HISTORIAL_PREVIO_BLOQUE]))  # de más antiguo a más reciente
    mensajes, conversaciones = [], {}
    for mid, conv_id, emisor, tipo, contenido, meta, ts, ini, cer, campana in filas:
        meta = _parsear_metadata(meta)
        eliminado = bool(meta.get("eliminado_en"))
        item = {
            "id": mid, "conversacion_id": conv_id, "emisor_tipo": emisor, "tipo_contenido": tipo,
            "contenido": "" if eliminado else contenido, "enviado_en": ts.isoformat(),
            "editado_en": meta.get("editado_en"), "eliminado_en": meta.get("eliminado_en"),
        }
        if tipo == "archivo" and not eliminado:
            item["archivo_disponible"] = _archivo_existe(contenido)
        mensajes.append(item)
        conversaciones[str(conv_id)] = {
            "iniciado_en": ini.isoformat() if ini else None,
            "cerrado_en": cer.isoformat() if cer else None, "campana": campana,
        }
    return {"disponible": True, "mensajes": mensajes, "conversaciones": conversaciones, "hay_mas": hay_mas}


def _obtener_info_conversacion(conversacion_id: int) -> dict | None:
    """Datos del paciente y de la conversación en sí (para el encabezado del
    PDF de descarga) — no confundir con _obtener_historial, que es solo los
    mensajes."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.nombre, u.telefono, u.dni, c.estado, cam.nombre,
                          c.iniciado_en, c.cerrado_en, a.nombre_display, et.nombre
                   FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id
                   LEFT JOIN campanas cam ON cam.id = c.campana_id
                   LEFT JOIN asesores a ON a.id = c.asesor_id
                   LEFT JOIN etiquetas_cierre et ON et.id = c.etiqueta_cierre_id
                   WHERE c.id = %s""",
                (conversacion_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            (nombre, telefono, dni, estado, campana, iniciado_en, cerrado_en,
             asesor_nombre, etiqueta) = row
            return {
                "nombre": nombre, "telefono": telefono, "dni": dni, "estado": estado,
                "campana": campana, "iniciado_en": iniciado_en, "cerrado_en": cerrado_en,
                "asesor_nombre": asesor_nombre, "etiqueta": etiqueta,
            }
    finally:
        release_connection(conn)


def _generar_pdf_conversacion(conversacion_id: int, info: dict, historial: list) -> bytes:
    """Arma un PDF legible de la conversación completa — cabecera con datos
    del paciente y burbujas de texto (cliente a la izquierda, bot/asesor a
    la derecha), calcado del estilo de las dos consolas."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle("Titulo", parent=estilos["Heading1"], fontSize=15, spaceAfter=4)
    estilo_meta = ParagraphStyle("Meta", parent=estilos["Normal"], fontSize=9.5, textColor=colors.HexColor("#555555"))
    estilo_cliente = ParagraphStyle(
        "Cliente", parent=estilos["Normal"], fontSize=10, alignment=TA_LEFT,
        backColor=colors.HexColor("#F0F3F6"), borderPadding=6, spaceAfter=6,
    )
    estilo_staff = ParagraphStyle(
        "Staff", parent=estilos["Normal"], fontSize=10, alignment=TA_RIGHT,
        backColor=colors.HexColor("#EAF1F9"), borderPadding=6, spaceAfter=6,
    )
    estilo_bot = ParagraphStyle(
        "Bot", parent=estilos["Normal"], fontSize=9.5, alignment=TA_LEFT,
        textColor=colors.HexColor("#777777"), spaceAfter=6,
    )

    elementos = []
    elementos.append(Paragraph(f"Conversación #{conversacion_id} — {info['nombre'] or 'Usuario'}", estilo_titulo))
    datos_paciente = [
        ("Teléfono", info.get("telefono") or "—"),
        ("DNI", info.get("dni") or "—"),
        ("Campaña", info.get("campana") or "—"),
        ("Iniciado", info["iniciado_en"].strftime("%d/%m/%Y %H:%M") if info.get("iniciado_en") else "—"),
        ("Cerrado", info["cerrado_en"].strftime("%d/%m/%Y %H:%M") if info.get("cerrado_en") else "—"),
        ("Asesor", info.get("asesor_nombre") or "—"),
        ("Etiqueta de cierre", info.get("etiqueta") or "—"),
    ]
    tabla = Table([[k, v] for k, v in datos_paciente], colWidths=[4 * cm, 11 * cm])
    tabla.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    elementos.append(tabla)
    elementos.append(Spacer(1, 14))

    for m in historial:
        hora = ""
        try:
            hora = datetime.fromisoformat(m["enviado_en"]).strftime("%d/%m %H:%M")
        except Exception:
            pass
        emisor = m["emisor_tipo"]
        tipo_contenido = m.get("tipo_contenido")
        contenido = m.get("contenido") or ""

        if tipo_contenido == "opciones":
            opciones = None
            try:
                opciones = json.loads(contenido)
            except Exception:
                pass
            if opciones:
                texto = "🤖 " + " · ".join(o.get("label") or o.get("texto") or "" for o in opciones)
            else:
                texto = contenido
        elif tipo_contenido == "archivo":
            texto = "📎 Archivo enviado"
        else:
            texto = contenido

        texto_escapado = (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        etiqueta_emisor = {"usuario": "Cliente", "asesor": "Asesor", "bot": "Bot", "supervisor": "Supervisor"}.get(emisor, emisor)
        linea = f"<b>{etiqueta_emisor}</b> · {hora}<br/>{texto_escapado}"

        if emisor == "usuario":
            elementos.append(Paragraph(linea, estilo_cliente))
        elif emisor == "bot":
            elementos.append(Paragraph(linea, estilo_bot))
        else:
            elementos.append(Paragraph(linea, estilo_staff))

    doc.build(elementos)
    return buffer.getvalue()


def _nombre_asesor_de_conversacion(conversacion_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.nombre_display FROM conversaciones c
                   JOIN asesores a ON a.id = c.asesor_id WHERE c.id = %s""",
                (conversacion_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_connection(conn)


def _obtener_datos_usuario(sesion_token: str) -> dict:
    """Datos recordados de este navegador. 'verificado' = el celular ya pasó el
    código SMS alguna vez (sin vencimiento). Si la columna verificado_en aún no
    existe (migración sin correr), se usa el criterio anterior: nombre + celular."""
    conn = get_connection()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT nombre, telefono, dni, verificado_en IS NOT NULL, ruc "
                    "FROM usuarios_webchat WHERE sesion_token = %s",
                    (sesion_token,),
                )
                row = cur.fetchone()
                return ({"nombre": row[0], "telefono": row[1], "dni": row[2], "verificado": bool(row[3]),
                         "ruc": row[4]} if row else {})
        except Exception:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT nombre, telefono, dni FROM usuarios_webchat WHERE sesion_token = %s",
                    (sesion_token,),
                )
                row = cur.fetchone()
                return ({"nombre": row[0], "telefono": row[1], "dni": row[2],
                         "verificado": bool(row[0] and row[1])} if row else {})
    finally:
        release_connection(conn)


def _nombre_reconocido(sesion_token: str):
    """Nombre a mostrar en 'No soy yo' (None si el visitante no está reconocido)."""
    try:
        datos = _obtener_datos_usuario(sesion_token)
    except Exception:
        return None
    if datos and datos.get("telefono") and datos.get("verificado") and datos.get("nombre"):
        return datos["nombre"]
    return None


def _nodo_inicio_visitante() -> str:
    """Nodo donde arranca un visitante que todavía no conocemos.
    Si existe y está activo "webchat_bienvenida" (saludo + menú, SIN pedir el
    celular todavía), se usa ese. Si no existe o se desactiva, vuelve al flujo
    anterior (pedir el celular primero) -- esto sirve de respaldo y de
    "interruptor": basta con UPDATE bot_menus SET activo = FALSE WHERE
    codigo = 'webchat_bienvenida' para revertir sin desplegar."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM bot_menus WHERE codigo = 'webchat_bienvenida' AND activo = TRUE")
                if cur.fetchone():
                    return "webchat_bienvenida"
        finally:
            release_connection(conn)
    except Exception:
        pass
    return "webchat_pide_celular"


def _campana_de_conversacion(conversacion_id):
    """campana_id de la conversación (None si todavía no eligió campaña)."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT campana_id FROM conversaciones WHERE id = %s", (conversacion_id,))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            release_connection(conn)
    except Exception:
        return None


def _limpiar_datos_usuario(sesion_token: str):
    """'No soy yo' / 'Cambiar mis datos': olvida nombre, celular, DNI, RUC y verificación.
    Si alguna columna nueva aún no existe, prueba con menos columnas."""
    intentos = (
        # historial_desde: lo anterior a este momento ya no se muestra como "conversaciones pasadas".
        "nombre = NULL, telefono = NULL, dni = NULL, verificado_en = NULL, ruc = NULL, historial_desde = NOW()",
        "nombre = NULL, telefono = NULL, dni = NULL, verificado_en = NULL, ruc = NULL",
        "nombre = NULL, telefono = NULL, dni = NULL, verificado_en = NULL, historial_desde = NOW()",
        "nombre = NULL, telefono = NULL, dni = NULL, verificado_en = NULL",
        "nombre = NULL, telefono = NULL, dni = NULL",
    )
    conn = get_connection()
    try:
        for i, sets in enumerate(intentos):
            try:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE usuarios_webchat SET {sets} WHERE sesion_token = %s", (sesion_token,))
                conn.commit()
                break
            except Exception:
                conn.rollback()
                if i == len(intentos) - 1:
                    raise
    finally:
        release_connection(conn)


def _marcar_leidos_por_usuario(conversacion_id: int):
    """El usuario vio la conversación (abrió el panel): marca como leídos los
    mensajes que no son suyos (bot/asesor/supervisor)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE mensajes SET leido_en = %s
                   WHERE conversacion_id = %s AND emisor_tipo != 'usuario' AND leido_en IS NULL""",
                (datetime.now(LIMA_TZ), conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _marcar_leidos_por_asesor(conversacion_id: int):
    """El asesor vio la conversación (la seleccionó): marca como leídos los
    mensajes del usuario."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE mensajes SET leido_en = %s
                   WHERE conversacion_id = %s AND emisor_tipo = 'usuario' AND leido_en IS NULL""",
                (datetime.now(LIMA_TZ), conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


async def _cerrar_y_notificar(conversacion_id: int, etiqueta_cierre_id, cerrado_por: str):
    """Cierra la conversación, manda el mensaje de despedida configurado (si hay)
    y el evento de cierre al widget. Compartido entre el cierre desde la
    consola de asesor y desde la de supervisor."""
    _cerrar_conversacion(conversacion_id, etiqueta_cierre_id, cerrado_por)

    mensaje_cierre = supervisor.obtener_configuracion("mensaje_cierre_conversacion")
    if mensaje_cierre:
        _guardar_mensaje_sistema(conversacion_id, mensaje_cierre)
        await manager.enviar_a_widget(conversacion_id, {
            "type": "mensaje_bot", "payload": {"texto": mensaje_cierre},
        })

    await manager.enviar_a_widget(conversacion_id, {
        "type": "conversacion_cerrada", "payload": {"motivo": cerrado_por},
    })

    # Encuesta de satisfacción (solo campañas con la encuesta activada y si hubo un asesor; ver encuesta.py).
    # Si el cliente no tiene el chat abierto, queda pendiente y se le muestra al volver (dentro de 24 h).
    try:
        datos_encuesta = await asyncio.to_thread(encuesta.crear_si_corresponde, conversacion_id)
        if datos_encuesta and conversacion_id in manager.widget_connections:
            await manager.enviar_a_widget(conversacion_id, {"type": "encuesta_satisfaccion", "payload": datos_encuesta})
            await asyncio.to_thread(encuesta.marcar_entregada, datos_encuesta["encuesta_id"])
    except Exception:
        logger.warning("No se pudo enviar la encuesta de la conversación %s", conversacion_id, exc_info=True)

    # El conteo "Atendidas hoy" de la cabecera del asesor sube con cada cierre.
    asesor_de_la_conversacion = _obtener_asesor_actual(conversacion_id)

    # Quien esté ESCUCHANDO esta conversación (supervisor, o asesor que ayuda) debe enterarse del cierre:
    # si no, la pantalla se queda quieta y parece que algo falló.
    try:
        if cerrado_por == "asesor":
            quien = "el asesor"
            if asesor_de_la_conversacion:
                quien = f"el asesor {await asyncio.to_thread(_nombre_display, asesor_de_la_conversacion)}"
        elif cerrado_por == "supervisor":
            quien = "un supervisor"
        else:
            quien = "el sistema"
        aviso = f"🔒 Esta conversación fue cerrada por {quien}. Ya no recibirá más mensajes."
        await manager.notificar_escuchas(conversacion_id, {
            "type": "mensaje_conversacion",
            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "sistema", "texto": aviso},
        })
        await manager.notificar_escuchas(conversacion_id, {
            "type": "conversacion_cerrada",
            "payload": {"conversacion_id": conversacion_id, "cerrado_por": cerrado_por, "texto": aviso},
        })
    except Exception:
        logger.warning("No se pudo avisar el cierre a quienes escuchaban la conversación %s", conversacion_id, exc_info=True)
    if asesor_de_la_conversacion:
        if cerrado_por != "asesor":
            # Cerrada por un supervisor: se la quita de la consola del asesor y del tablero.
            try:
                await manager.enviar_a_asesor(asesor_de_la_conversacion, {
                    "type": "conversacion_removida",
                    "payload": {"conversacion_id": conversacion_id, "motivo": "cerrada", "cerrado_por": cerrado_por},
                })
                await supervisor.notificar_cambio_asesor(asesor_de_la_conversacion, manager)
            except Exception:
                logger.warning("No se pudo avisar el cierre al asesor", exc_info=True)
        try:
            await supervisor.enviar_mis_estadisticas(asesor_de_la_conversacion, manager)
        except Exception:
            pass  # solo es un contador: no debe romper el cierre


def _obtener_historial_usuario(conversacion_id_actual: int) -> list:
    """Todas las conversaciones anteriores del mismo usuario (por usuario_id),
    sin incluir la actual — para el botón de historial en la consola de asesor."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usuario_id FROM conversaciones WHERE id = %s", (conversacion_id_actual,))
            row = cur.fetchone()
            if not row:
                return []
            usuario_id = row[0]
            cur.execute(
                """SELECT c.id, c.iniciado_en, c.estado, cam.nombre, et.nombre
                   FROM conversaciones c
                   LEFT JOIN campanas cam ON cam.id = c.campana_id
                   LEFT JOIN etiquetas_cierre et ON et.id = c.etiqueta_cierre_id
                   WHERE c.usuario_id = %s AND c.id != %s
                   ORDER BY c.iniciado_en DESC""",
                (usuario_id, conversacion_id_actual),
            )
            return [
                {"conversacion_id": cid, "iniciado_en": ts.isoformat(), "estado": estado,
                 "campana": campana, "etiqueta": etiqueta}
                for cid, ts, estado, campana, etiqueta in cur.fetchall()
            ]
    finally:
        release_connection(conn)


def _obtener_estado_conversacion(conversacion_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT estado, asesor_id FROM conversaciones WHERE id = %s", (conversacion_id,)
            )
            return cur.fetchone()
    finally:
        release_connection(conn)


# --- Reenvío de SMS / llamada con el token OTP, durante la verificación de
# celular (nodo bot_menus "webchat_valida_pide_codigo") ---------------------
#
# Esto NO se maneja como un nodo más de bot_menus a propósito: ese nodo es
# "captura_dato" (espera que la persona escriba el código de 6 dígitos como
# texto libre), y bot_engine.recibir_entrada_usuario() no permite que un
# mismo nodo acepte a la vez texto libre Y un click de botón/menú -- un nodo
# "menu" solo avanza con un id/label exacto, lo que rompería la captura del
# código. Por eso el botón de reenvío/llamada se maneja directo acá, al
# margen del árbol de bot_menus, leyendo y escribiendo variables_sesion a mano.

LIMITE_REENVIOS_SMS_ANTES_DE_LLAMADA = 2  # (ya no condiciona el botón de llamada, que está siempre disponible)
MAX_LLAMADAS_OTP = 3  # llamadas con el token por conversación (cada una tiene costo)


def _obtener_nodo_y_variables(conversacion_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nodo_actual, variables_sesion FROM conversaciones WHERE id = %s",
                (conversacion_id,),
            )
            nodo_actual, variables_sesion = cur.fetchone()
            return nodo_actual, (variables_sesion or {})
    finally:
        release_connection(conn)


def _guardar_variables_sesion(conversacion_id: int, variables: dict):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversaciones SET variables_sesion = %s WHERE id = %s",
                (json.dumps(variables), conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _reenviar_codigo_otp(conversacion_id: int) -> dict:
    """Regenera el código OTP y lo reenvía por SMS -- botón "Reenviar código"
    del widget. Solo funciona mientras la conversación sigue parada
    exactamente en webchat_valida_pide_codigo (si ya avanzó, o nunca llegó
    ahí, no hace nada). Lleva la cuenta de reenvíos en
    variables_sesion['OTP_REENVIOS'] para habilitar la opción de llamada
    después de LIMITE_REENVIOS_SMS_ANTES_DE_LLAMADA intentos."""
    nodo_actual, variables = _obtener_nodo_y_variables(conversacion_id)
    if nodo_actual != "webchat_valida_pide_codigo":
        return {"ok": False, "motivo": "fuera_de_flujo"}

    celular = (variables.get("CELULAR") or "").strip()
    if not celular:
        return {"ok": False, "motivo": "sin_celular"}

    reenvios = int(variables.get("OTP_REENVIOS") or 0) + 1
    codigo = _generar_codigo_otp()
    mensaje = f"Tu código de verificación Sanitas es: {codigo}"
    if not enviar_sms_danaconnect(celular, mensaje):
        return {"ok": False, "motivo": "error_envio"}

    variables["OTP_REENVIOS"] = reenvios
    # Mismo shape que deja el nodo accion_api "enviar_sms_otp" -- así
    # {{ENVIO_SMS.response.codigo}} sigue funcionando igual en
    # webchat_valida_evalua_codigo cuando la persona escriba el código nuevo.
    variables["ENVIO_SMS"] = {"status": 200, "response": {"enviado": True, "codigo": codigo}}
    _guardar_variables_sesion(conversacion_id, variables)

    return {
        "ok": True,
        "reenvios": reenvios,
        "puede_llamar": reenvios >= LIMITE_REENVIOS_SMS_ANTES_DE_LLAMADA,
    }


def _solicitar_llamada_otp(conversacion_id: int) -> dict:
    """Pide que el código YA generado (el último enviado por SMS, el mismo
    que la persona debe ingresar) se lea también por llamada de voz -- no
    genera uno nuevo, para no invalidar uno que recién le esté llegando."""
    nodo_actual, variables = _obtener_nodo_y_variables(conversacion_id)
    if nodo_actual != "webchat_valida_pide_codigo":
        return {"ok": False, "motivo": "fuera_de_flujo"}

    celular = (variables.get("CELULAR") or "").strip()
    codigo = ((variables.get("ENVIO_SMS") or {}).get("response") or {}).get("codigo")
    reenvios = int(variables.get("OTP_REENVIOS") or 0)
    if not celular or not codigo:
        return {"ok": False, "motivo": "sin_datos"}

    llamadas = int(variables.get("OTP_LLAMADAS") or 0)
    if llamadas >= MAX_LLAMADAS_OTP:
        return {"ok": False, "motivo": "limite_llamadas"}

    if not enviar_llamada_otp(celular, codigo):
        return {"ok": False, "motivo": "error_llamada"}

    variables["OTP_LLAMADAS"] = llamadas + 1
    _guardar_variables_sesion(conversacion_id, variables)
    return {"ok": True, "reenvios": reenvios}


def _guardar_mensaje_usuario(conversacion_id: int, texto: str, respondido_a_id: int | None = None) -> int:
    metadata = {"respondido_a_id": respondido_a_id} if respondido_a_id else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, metadata, enviado_en)
                   VALUES (%s, 'usuario', 'texto', %s, %s, %s) RETURNING id""",
                (conversacion_id, texto, json.dumps(metadata) if metadata else None, datetime.now(LIMA_TZ)),
            )
            mensaje_id = cur.fetchone()[0]
        conn.commit()
        return mensaje_id
    finally:
        release_connection(conn)


def _guardar_mensaje_usuario_archivo(conversacion_id: int, ruta: str, respondido_a_id: int | None = None) -> int:
    """OJO: 'ruta' debe ser el nombre del objeto en el bucket (p.ej.
    'webchat/uuid.jpg'), NUNCA una URL firmada — una firmada guardada acá
    queda permanentemente inaccesible en cuanto expira. Para ver el archivo
    más tarde, se pide una firmada nueva a GET /archivo-receta."""
    metadata = {"respondido_a_id": respondido_a_id} if respondido_a_id else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, metadata, enviado_en)
                   VALUES (%s, 'usuario', 'archivo', %s, %s, %s) RETURNING id""",
                (conversacion_id, ruta, json.dumps(metadata) if metadata else None, datetime.now(LIMA_TZ)),
            )
            mensaje_id = cur.fetchone()[0]
        conn.commit()
        return mensaje_id
    finally:
        release_connection(conn)


def _guardar_mensaje_sistema(conversacion_id: int, texto: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'sistema', 'texto', %s, %s)""",
                (conversacion_id, texto, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Editar / responder-citando / eliminar mensajes (widget, asesor y
# supervisor) — igual que WhatsApp: cada quien solo sobre SUS propios
# mensajes, "editar" con ventana de 2 minutos y solo para texto, "eliminar"
# sin límite de tiempo pero dejando una "huella" (queda el mensaje, vacío,
# con metadata.eliminado_en/eliminado_por) en vez de desaparecer de la fila.
# "Responder" es aparte: no modifica nada, solo guarda metadata.respondido_a_id
# al CREAR un mensaje nuevo (ver _guardar_mensaje_usuario y similares).
# ---------------------------------------------------------------------------
VENTANA_EDICION_MINUTOS = 2


def _parsear_metadata(valor) -> dict:
    if not valor:
        return {}
    if isinstance(valor, dict):
        return valor
    try:
        return json.loads(valor)
    except Exception:
        return {}


def _obtener_mensaje(mensaje_id: int) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, conversacion_id, emisor_tipo, emisor_id, tipo_contenido,
                          contenido, metadata, enviado_en
                   FROM mensajes WHERE id = %s""",
                (mensaje_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            mid, conv_id, emisor_tipo, emisor_id, tipo_c, contenido, metadata, enviado_en = row
            return {
                "id": mid, "conversacion_id": conv_id, "emisor_tipo": emisor_tipo,
                "emisor_id": emisor_id, "tipo_contenido": tipo_c, "contenido": contenido,
                "metadata": _parsear_metadata(metadata), "enviado_en": enviado_en,
            }
    finally:
        release_connection(conn)


def _obtener_preview_mensaje(mensaje_id: int | None) -> dict | None:
    """Resumen corto de un mensaje para mostrar como 'cita' arriba de la
    respuesta (igual que el recuadro gris de WhatsApp) — se resuelve en el
    servidor así ninguna de las 3 vistas necesita buscarlo por su cuenta."""
    if not mensaje_id:
        return None
    m = _obtener_mensaje(mensaje_id)
    if not m:
        return None
    if m["metadata"].get("eliminado_en"):
        texto = "Este mensaje se eliminó"
    elif m["tipo_contenido"] == "archivo":
        texto = "📎 Archivo"
    elif m["tipo_contenido"] == "boton_click":
        texto = m["contenido"] or "Opción seleccionada"
    else:
        texto = m["contenido"] or ""
    return {"mensaje_id": mensaje_id, "emisor_tipo": m["emisor_tipo"], "texto": texto[:160]}


def _editar_mensaje(mensaje_id: int, conversacion_id: int, emisor_tipo_esperado: str,
                     emisor_id_esperado: int | None, nuevo_texto: str) -> dict | None:
    """None si no se pudo editar (mensaje ajeno, de otra conversación, ya
    eliminado, no es de texto, o ya pasó la ventana de 2 minutos)."""
    nuevo_texto = (nuevo_texto or "").strip()
    if not nuevo_texto:
        return None
    m = _obtener_mensaje(mensaje_id)
    if not m or m["conversacion_id"] != conversacion_id or m["emisor_tipo"] != emisor_tipo_esperado:
        return None
    if emisor_id_esperado is not None and m["emisor_id"] != emisor_id_esperado:
        return None
    if m["tipo_contenido"] != "texto" or m["metadata"].get("eliminado_en"):
        return None
    if datetime.now(LIMA_TZ) - m["enviado_en"] > timedelta(minutes=VENTANA_EDICION_MINUTOS):
        return None

    ahora = datetime.now(LIMA_TZ)
    nueva_metadata = {**m["metadata"], "editado_en": ahora.isoformat()}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mensajes SET contenido = %s, metadata = %s WHERE id = %s",
                (nuevo_texto, json.dumps(nueva_metadata), mensaje_id),
            )
        conn.commit()
    finally:
        release_connection(conn)
    return {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id, "texto": nuevo_texto,
            "editado_en": ahora.isoformat()}


def _eliminar_mensaje(mensaje_id: int, conversacion_id: int, emisor_tipo_esperado: str,
                       emisor_id_esperado: int | None, eliminado_por: str) -> dict | None:
    """None si no se pudo eliminar (mensaje ajeno, de otra conversación, o ya
    estaba eliminado). No borra la fila — deja la 'huella' en metadata."""
    m = _obtener_mensaje(mensaje_id)
    if not m or m["conversacion_id"] != conversacion_id or m["emisor_tipo"] != emisor_tipo_esperado:
        return None
    if emisor_id_esperado is not None and m["emisor_id"] != emisor_id_esperado:
        return None
    if m["metadata"].get("eliminado_en"):
        return None

    ahora = datetime.now(LIMA_TZ)
    nueva_metadata = {**m["metadata"], "eliminado_en": ahora.isoformat(), "eliminado_por": eliminado_por}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mensajes SET metadata = %s WHERE id = %s",
                (json.dumps(nueva_metadata), mensaje_id),
            )
        conn.commit()
    finally:
        release_connection(conn)
    return {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id,
            "eliminado_en": ahora.isoformat(), "eliminado_por": eliminado_por}


def _resumen_reacciones(metadata) -> dict:
    """{emoji: cantidad} a partir de metadata['reacciones'] ({actor: emoji})."""
    resumen = {}
    for emoji in ((metadata or {}).get("reacciones") or {}).values():
        resumen[emoji] = resumen.get(emoji, 0) + 1
    return resumen


def _reaccionar_mensaje(mensaje_id, conversacion_id, actor_key: str, emoji) -> dict | None:
    """Pone (o quita, si es el mismo emoji) la reacción de `actor_key`
    ('usuario', 'asesor:7', 'supervisor:2') a un mensaje — una reacción por
    persona, como WhatsApp. Se guarda en metadata['reacciones'] del mensaje.
    None si no aplica (emoji inválido, mensaje ajeno a la conversación o
    eliminado)."""
    emoji = (emoji or "").strip() if isinstance(emoji, str) else ""
    if not emoji or len(emoji) > 16 or any(c.isalnum() or c.isspace() for c in emoji):
        return None
    try:
        mensaje_id = int(mensaje_id)
    except (TypeError, ValueError):
        return None
    m = _obtener_mensaje(mensaje_id)
    if not m or m["conversacion_id"] != conversacion_id or m["metadata"].get("eliminado_en"):
        return None
    if m["emisor_tipo"] == "sistema":
        return None

    reacciones = dict(m["metadata"].get("reacciones") or {})
    if reacciones.get(actor_key) == emoji:
        del reacciones[actor_key]
    else:
        reacciones[actor_key] = emoji
    nueva_metadata = {**m["metadata"], "reacciones": reacciones}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE mensajes SET metadata = %s WHERE id = %s",
                        (json.dumps(nueva_metadata), mensaje_id))
        conn.commit()
    finally:
        release_connection(conn)
    return {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id,
            "reacciones": _resumen_reacciones(nueva_metadata)}


_BANDERAS_VALIDAS = ("rojo", "azul", "amarillo", "verde")


def _cambiar_bandera(conversacion_id: int, bandera: str | None, asesor_id_esperado: int | None) -> dict | None:
    """Marca (o quita) la bandera de color de una conversación VIVA (estado
    'asignada') — es solo una ayuda visual interna para que asesor/supervisor
    identifiquen el proceso en el que va cada caso, nunca la ve el paciente.
    bandera=None equivale a la bandera blanca/transparente (sin marca).

    Si asesor_id_esperado viene dado (lo llama la consola de asesor), solo
    puede marcar SUS PROPIAS conversaciones — si viene None (lo llama la
    consola de supervisor), puede marcar cualquiera que siga asignada.
    Devuelve {"conversacion_id", "bandera", "asesor_id"} o None si no aplicó
    (conversación inexistente/cerrada, o ajena)."""
    if bandera is not None and bandera not in _BANDERAS_VALIDAS:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asesor_id, estado FROM conversaciones WHERE id = %s",
                (conversacion_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            asesor_id_dueno, estado = row
            if estado != "asignada":
                return None
            if asesor_id_esperado is not None and asesor_id_dueno != asesor_id_esperado:
                return None
            cur.execute(
                "UPDATE conversaciones SET bandera = %s WHERE id = %s",
                (bandera, conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)
    return {"conversacion_id": conversacion_id, "bandera": bandera, "asesor_id": asesor_id_dueno}


async def _difundir_bandera_cambiada(conversacion_id: int, bandera: str | None, asesor_id: int | None):
    """Avisa al asesor dueño y a los supervisores escuchando — nunca al
    widget, porque la bandera es una marca interna, no algo que vea el
    paciente. También refresca el tablero de supervisor (el punto de color
    en la mini-lista de conversaciones de cada asesor)."""
    payload = {"conversacion_id": conversacion_id, "bandera": bandera}
    if asesor_id:
        await manager.enviar_a_asesor(asesor_id, {"type": "bandera_cambiada", "payload": payload})
    await manager.notificar_escuchas(conversacion_id, {"type": "bandera_cambiada", "payload": payload})
    if asesor_id:
        await supervisor.notificar_cambio_asesor(asesor_id, manager)


async def _difundir_mensaje_modificado(evento_type: str, conversacion_id: int, payload: dict):
    """Avisa a TODOS los que puedan estar viendo esta conversación (el
    widget del paciente, el asesor asignado, y cualquier supervisor
    escuchando) de que un mensaje se editó o se eliminó, para que las 3
    vistas se actualicen al toque — incluye de vuelta al que hizo la
    acción, así ninguna de las 3 vistas necesita actualizar su propio
    pantalla de forma optimista (más simple, y siempre coincide con lo que
    de verdad quedó guardado)."""
    await manager.enviar_a_widget(conversacion_id, {"type": evento_type, "payload": payload})
    estado = await asyncio.to_thread(_obtener_estado_conversacion, conversacion_id)
    if estado:
        _, asesor_id_actual = estado
        if asesor_id_actual:
            await manager.enviar_a_asesor(asesor_id_actual, {"type": evento_type, "payload": payload})
    await manager.notificar_escuchas(conversacion_id, {"type": evento_type, "payload": payload})



# ---------------------------------------------------------------------------
# Traspaso de la conversación a otro dispositivo (laptop -> celular) por QR.
# El QR NO lleva la llave de la sesión: lleva un código de un solo uso que vence
# a los 5 minutos y que el celular canjea por la llave. Si alguien fotografía el
# QR después de usado o vencido, no sirve.
# ---------------------------------------------------------------------------
TRASPASO_VIGENCIA_SEG = 300


def _crear_traspaso(sesion_token: str, url_cliente) -> dict:
    widget_url = (supervisor.obtener_configuracion("widget_url") or "").strip()
    if not widget_url and isinstance(url_cliente, str) and url_cliente.startswith(("https://", "http://")):
        widget_url = url_cliente.strip()[:500]
    if not widget_url:
        return {"ok": False, "motivo": "No se pudo determinar la página del chat."}
    try:
        import segno
    except ImportError:
        logger.exception("Falta la librería segno para generar el QR")
        return {"ok": False, "motivo": "No se pudo generar el código QR por ahora."}

    codigo = secrets.token_urlsafe(24)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM usuarios_webchat WHERE sesion_token = %s", (sesion_token,))
            fila = cur.fetchone()
            if not fila:
                return {"ok": False, "motivo": "Sesión no encontrada."}
            # Solo el último código sirve: los anteriores sin usar se invalidan.
            cur.execute("DELETE FROM traspasos_sesion WHERE usuario_id = %s AND usado_en IS NULL", (fila[0],))
            cur.execute(
                "INSERT INTO traspasos_sesion (codigo, usuario_id, creado_en, expira_en) VALUES (%s, %s, %s, %s)",
                (codigo, fila[0], datetime.now(LIMA_TZ),
                 datetime.now(LIMA_TZ) + timedelta(seconds=TRASPASO_VIGENCIA_SEG)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("No se pudo crear el traspaso (¿falta migracion_traspaso_qr.sql?)")
        return {"ok": False, "motivo": "No se pudo generar el código QR por ahora."}
    finally:
        release_connection(conn)

    separador = "&" if "?" in widget_url else "?"
    enlace = f"{widget_url}{separador}wct={codigo}"
    qr = segno.make(enlace, error="m")
    return {"ok": True, "qr": qr.svg_data_uri(scale=6, border=2, dark="#0A2540"),
            "expira_seg": TRASPASO_VIGENCIA_SEG}


# --- Verificación inversa por WhatsApp (respaldo del SMS) -------------------
WHATSAPP_VERIFICACION_NUMERO_DEFECTO = "51955973936"


def _numero_whatsapp_verificacion() -> str:
    try:
        valor = (supervisor.obtener_configuracion("whatsapp_verificacion_numero") or "").strip()
    except Exception:
        valor = ""
    digitos = "".join(c for c in valor if c.isdigit())
    return digitos or WHATSAPP_VERIFICACION_NUMERO_DEFECTO


class VerificacionWhatsapp(BaseModel):
    celular: str
    mensaje: str


_MENSAJES_VERIFICACION = {
    "ok": "✅ Listo, tu celular quedó verificado. Vuelve a la pestaña del chat de Sanitas.",
    "ya_verificado": "✅ Tu celular ya está verificado. Vuelve a la pestaña del chat de Sanitas.",
    "formato": "No pudimos leer el código. Envía el mensaje tal como aparece en el chat de Sanitas (VERIFICAR-123456).",
    "no_existe": "Ese código no es válido. Genera uno nuevo desde el chat de Sanitas.",
    "vencido": "Ese código venció. Genera uno nuevo desde el chat de Sanitas.",
    "celular_distinto": "Debes enviar el código desde el mismo celular que registraste en el chat.",
    "ya_usado": "Ese código ya fue utilizado. Genera uno nuevo desde el chat de Sanitas.",
    "bloqueado": "Demasiados intentos. Espera unos minutos y vuelve a intentarlo desde el chat de Sanitas.",
    "error": "No pudimos verificar en este momento. Inténtalo de nuevo desde el chat de Sanitas.",
}


@app.post("/verificacion/whatsapp")
async def verificacion_whatsapp_endpoint(datos: VerificacionWhatsapp, authorization: str = Header(None)):
    """Lo llama el bot de Chattigo (nodo CONNECTOR_SERVICE) con el número REAL del remitente
    (USER_IDENTIFIER) y el texto que escribió. Siempre responde 200 con {verificado, motivo, mensaje}
    para que el bot pueda mostrar el mensaje adecuado; solo la ApiKey inválida da 401."""
    esperado = os.environ.get("SOFI_APIS_KEY") or ""
    recibido = (authorization or "").strip()
    if recibido.lower().startswith("apikey "):
        recibido = recibido[7:].strip()
    if not esperado or not secrets.compare_digest(recibido.encode(), esperado.encode()):
        raise HTTPException(status_code=401, detail="No autorizado")

    resultado = await asyncio.to_thread(
        verificacion_whatsapp.confirmar, datos.celular[:30], datos.mensaje[:200])
    if resultado.get("verificado") and resultado.get("conversacion_id"):
        try:  # Si el widget está en esta misma instancia, se entera al instante (si no, lo consulta él).
            await manager.enviar_a_widget(resultado["conversacion_id"],
                                          {"type": "verificacion_whatsapp_ok", "payload": {}})
        except Exception:
            pass
    motivo = resultado.get("motivo") or "error"
    return {"verificado": bool(resultado.get("verificado")), "motivo": motivo,
            "mensaje": _MENSAJES_VERIFICACION.get(motivo, _MENSAJES_VERIFICACION["error"])}


async def _aplicar_verificacion_whatsapp(conversacion_id: int, websocket: WebSocket, codigo: str):
    """El código de WhatsApp ya está verificado en la base: se hace avanzar el bot exactamente
    como si la persona hubiera escrito el código del SMS (un solo uso)."""
    nodo_actual, variables = await asyncio.to_thread(_obtener_nodo_y_variables, conversacion_id)
    if nodo_actual != "webchat_valida_pide_codigo":
        return
    if not await asyncio.to_thread(verificacion_whatsapp.consumir, conversacion_id, codigo):
        return
    variables["ENVIO_SMS"] = {"status": 200, "response": {"enviado": True, "codigo": codigo}}
    variables["OTP_METODO"] = "whatsapp"
    await asyncio.to_thread(_guardar_variables_sesion, conversacion_id, variables)
    desde_mensaje_id = (await asyncio.to_thread(_ultimo_mensaje_id, conversacion_id)
                        if _hay_escuchas(conversacion_id) else None)
    eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id, texto=codigo)
    await websocket.send_json({"type": "verificacion_whatsapp_completada", "payload": {}})
    celular_verificado = _celular_verificado_en(eventos)
    if celular_verificado is not None:
        union = await asyncio.to_thread(_unir_a_conversacion_abierta, conversacion_id, celular_verificado)
        if union:
            await _avisar_union(websocket, union)
            return
    await _procesar_eventos_bot(conversacion_id, websocket, eventos)
    if desde_mensaje_id is not None:
        await _notificar_escuchas_mensajes_nuevos(conversacion_id, desde_mensaje_id)


class CanjearTraspaso(BaseModel):
    codigo: str


@app.post("/widget/traspaso/canjear")
def canjear_traspaso(datos: CanjearTraspaso):
    """El celular entrega el código del QR y recibe la llave de la sesión. Un solo uso."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE traspasos_sesion SET usado_en = %s
                   WHERE codigo = %s AND usado_en IS NULL AND expira_en > %s
                   RETURNING usuario_id""",
                (datetime.now(LIMA_TZ), datos.codigo[:100], datetime.now(LIMA_TZ)),
            )
            fila = cur.fetchone()
            if not fila:
                conn.commit()
                raise HTTPException(status_code=410, detail="El código venció o ya se usó")
            cur.execute("SELECT sesion_token FROM usuarios_webchat WHERE id = %s", (fila[0],))
            token = cur.fetchone()[0]
        conn.commit()
        return {"sesion_token": token}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        logger.exception("Error canjeando el traspaso")
        raise HTTPException(status_code=500, detail="No se pudo continuar la conversación")
    finally:
        release_connection(conn)


_TIPOS_FIABLES = {"mensaje_usuario", "click_opcion", "archivo_subido", "formulario_completado"}
_TIPOS_FIABLES_STAFF = {"mensaje_asesor", "mensaje_asesor_archivo", "responder_directo", "responder_directo_archivo"}
_cids_widget: dict = {}


def _registrar_cid_widget(token: str, cid: str) -> bool:
    """True si el cid es nuevo (hay que procesarlo); False si ya se procesó (reenvío tras un corte de red)."""
    vistos = _cids_widget.setdefault(token, [])
    if cid in vistos:
        return False
    vistos.append(cid)
    if len(vistos) > 200:
        del vistos[:100]
    if len(_cids_widget) > 5000:
        for k in list(_cids_widget)[:2500]:
            _cids_widget.pop(k, None)
    return True


class _Candados:
    """Candados asyncio por clave (token o conversación), que se borran solos al quedar libres.
    Las llamadas a la base del widget corren en hilos (asyncio.to_thread) para no congelar el servidor;
    estos candados mantienen el orden que antes daba el event loop: dos pestañas con el mismo token no
    crean dos conversaciones, y los mensajes de una misma conversación se procesan de a uno."""
    def __init__(self):
        self._d: dict = {}

    @contextlib.asynccontextmanager
    async def __call__(self, clave):
        ent = self._d.get(clave)
        if ent is None:
            ent = self._d[clave] = [asyncio.Lock(), 0]
        ent[1] += 1
        try:
            async with ent[0]:
                yield
        finally:
            ent[1] -= 1
            if ent[1] == 0 and self._d.get(clave) is ent:
                del self._d[clave]


_candado_widget = _Candados()


async def _crear_o_reanudar_async(sesion_token: str):
    async with _candado_widget(("token", sesion_token)):
        return await asyncio.to_thread(_crear_o_reanudar_conversacion, sesion_token)


TEXTO_UNION_CLIENTE = "✅ Te reconocimos: seguimos en tu conversación anterior."
_TEXTO_UNION_NUEVA = "Continuó en su conversación anterior #{id} (abrió el chat desde otro navegador o enlace)."
_TEXTO_UNION_ANTERIOR = ("ℹ️ El cliente volvió a entrar desde otro navegador o enlace "
                         "y continúa en esta misma conversación.")


def _unir_a_conversacion_abierta(conversacion_nueva: int, celular) -> dict | None:
    """Evita conversaciones duplicadas de una misma persona. Si el celular (recién verificado con el código,
    o ya reconocido en este navegador) tiene OTRA conversación abierta en cola o con un asesor, creada desde
    otra llave de navegador, se cierra la nueva y se devuelve la llave de la abierta para que el widget
    continúe ahí. Pasa sobre todo al tocar dos veces el enlace del SMS de invitación: muchas apps de mensajes
    lo abren en un navegador interno con almacenamiento propio, y cada vez llega sin la llave anterior.
    Solo une si la nueva sigue con el bot o en cola (nunca si ya la tomó un asesor).
    Devuelve {"conversacion_id", "sesion_token"} o None."""
    digitos = re.sub(r"\D", "", str(celular or ""))[-9:]
    if len(digitos) != 9 or not conversacion_nueva:
        return None
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, u.sesion_token
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.estado IN ('en_cola', 'asignada') AND c.id <> %s
                     AND c.usuario_id <> (SELECT usuario_id FROM conversaciones WHERE id = %s)
                     AND c.iniciado_en > %s
                     AND right(regexp_replace(COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR', ''),
                                              '[^0-9]', '', 'g'), 9) = %s
                   ORDER BY (c.estado = 'asignada') DESC, c.iniciado_en DESC
                   LIMIT 1""",
                (conversacion_nueva, conversacion_nueva, ahora - timedelta(hours=24), digitos),
            )
            fila = cur.fetchone()
            if not fila or not fila[1]:
                conn.rollback()
                return None
            anterior_id, token_anterior = fila
            cur.execute(
                """UPDATE conversaciones SET estado = 'cerrada', cerrado_en = %s, cerrado_por = 'sistema', nodo_actual = NULL
                   WHERE id = %s AND estado IN ('bot', 'en_cola') AND asesor_id IS NULL
                   RETURNING id""",
                (ahora, conversacion_nueva),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return None
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'bot', 'texto', %s, %s), (%s, 'sistema', 'texto', %s, %s)""",
                (conversacion_nueva, _TEXTO_UNION_NUEVA.format(id=anterior_id), ahora,
                 anterior_id, _TEXTO_UNION_ANTERIOR, ahora),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning("No se pudo unir la conversación %s a una abierta", conversacion_nueva, exc_info=True)
        return None
    finally:
        release_connection(conn)
    logger.info("Conversación %s unida a la abierta %s (mismo celular ***%s, otra llave de navegador)",
                conversacion_nueva, anterior_id, digitos[-3:])
    return {"conversacion_id": anterior_id, "sesion_token": token_anterior}


async def _avisar_union(websocket: WebSocket, union: dict):
    """El widget guarda la llave de la conversación abierta y se reconecta con ella (vuelve el historial)."""
    await websocket.send_json({"type": "cambiar_sesion", "payload": {
        "sesion_token": union["sesion_token"], "aviso": TEXTO_UNION_CLIENTE}})


def _celular_verificado_en(eventos) -> str | None:
    for e in eventos or []:
        if e.get("type") == "_celular_verificado":
            return (e.get("payload") or {}).get("celular") or ""
    return None


@app.websocket("/ws/widget")
async def ws_widget(websocket: WebSocket, sesion_token: str | None = None):
    """
    El widget se conecta a wss://.../ws/widget?sesion_token=<uuid o vacío>.
    Si no manda sesion_token (primera visita), se genera uno nuevo y se le
    devuelve en el evento 'sesion_iniciada' para que el widget lo guarde.
    Si ya tiene una conversación sin cerrar, la reanuda (no crea una nueva) —
    esto es lo que permite que la reconexión automática funcione bien.
    """
    nuevo_token = sesion_token or str(uuid.uuid4())
    conversacion_id, estado_actual, es_nueva = await _crear_o_reanudar_async(nuevo_token)
    await manager.connect_widget(conversacion_id, websocket)

    async def arrancar(conv_id: int, es_nueva_flag: bool, estado: str, forzar_pedir_datos: bool = False):
        if es_nueva_flag:
            datos = None if forzar_pedir_datos else await asyncio.to_thread(_obtener_datos_usuario, nuevo_token)
            if datos and datos.get("telefono") and datos.get("verificado"):
                union = await asyncio.to_thread(_unir_a_conversacion_abierta, conv_id, datos["telefono"])
                if union:
                    await _avisar_union(websocket, union)
                    return
            nodo_visitante = await asyncio.to_thread(_nodo_inicio_visitante)
            if datos and datos.get("telefono") and datos.get("verificado"):
                # Celular ya verificado en este navegador (no vence): no se
                # vuelve a pedir ni a mandar SMS. También se recuerda el DNI.
                variables_iniciales = {"CELULAR": datos["telefono"]}
                if datos.get("dni"):
                    variables_iniciales["DNI"] = datos["dni"]
                    variables_iniciales["DOI"] = datos["dni"]
                if datos.get("ruc"):
                    variables_iniciales["RUC_RECORDADO"] = datos["ruc"]
                if datos.get("nombre"):
                    variables_iniciales["NOMBRE_USUARIO"] = datos["nombre"]
                    nodo_ini = "webchat_menu_principal"
                else:
                    # Falta solo el nombre: sigue desde ahí, sin repetir celular/SMS.
                    nodo_ini = ("webchat_bienvenida" if nodo_visitante == "webchat_bienvenida"
                                else "webchat_pide_nombre")
                eventos = await asyncio.to_thread(iniciar_conversacion, conv_id, nodo_inicial=nodo_ini,
                                                  variables_iniciales=variables_iniciales)
            else:
                eventos = await asyncio.to_thread(iniciar_conversacion, conv_id, nodo_inicial=nodo_visitante)
            await _procesar_eventos_bot(conv_id, websocket, eventos)
        else:
            # Reanudando: manda el historial para reconstruir la pantalla,
            # y el estado actual (en cola / asignado) si corresponde.
            historial = await asyncio.to_thread(_obtener_historial, conv_id)
            await websocket.send_json({"type": "historial", "payload": {"mensajes": historial}})
            if estado == "asignada":
                nombre = await asyncio.to_thread(_nombre_asesor_de_conversacion, conv_id)
                if nombre:
                    await websocket.send_json({"type": "asignado", "payload": {"asesor_nombre": nombre}})
            elif estado == "en_cola":
                await websocket.send_json({"type": "en_cola", "payload": {}})
            elif estado == "bot":
                # Si se había quedado esperando el código SMS, los botones de
                # "Reenviar código"/"Llamarme" no forman parte del historial:
                # se vuelven a mandar para que no desaparezcan al reconectar.
                nodo_actual, variables = await asyncio.to_thread(_obtener_nodo_y_variables, conv_id)
                if nodo_actual == "webchat_valida_pide_codigo":
                    reenvios = int(variables.get("OTP_REENVIOS") or 0)
                    await websocket.send_json({"type": "otp_acciones", "payload": {
                        "otp_reenvios": reenvios,
                        "otp_puede_llamar": reenvios >= LIMITE_REENVIOS_SMS_ANTES_DE_LLAMADA,
                    }})

    try:
        await websocket.send_json({
            "type": "sesion_iniciada",
            "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id,
                        "reconocido": await asyncio.to_thread(_nombre_reconocido, nuevo_token)},
        })
        try:
            try:
                modo_directo, datos_directo = await _decidir_enlace_directo(conversacion_id, estado_actual, es_nueva, websocket)
            except Exception:
                logger.exception("Error evaluando el enlace directo de la conversación %s", conversacion_id)
                modo_directo, datos_directo = None, None
            if modo_directo == "directo" and es_nueva:
                pass  # no arranca el bot: la deriva _derivar_enlace_directo
            else:
                await arrancar(conversacion_id, es_nueva, estado_actual)
            if modo_directo == "directo":
                await _derivar_enlace_directo(conversacion_id, datos_directo, es_nueva, websocket)
        except Exception:
            logger.exception("Error en el arranque inicial de la conversación %s", conversacion_id)
            await websocket.send_json({
                "type": "mensaje_bot",
                "payload": {"texto": "⚠️ Ocurrió un error inesperado."},
            })
            await websocket.send_json({"type": "conversacion_cerrada", "payload": {"motivo": "error"}})

        try:
            await _entregar_archivos_pendientes(conversacion_id, websocket)
        except Exception:
            logger.exception("Error entregando archivos pendientes de la conversación %s", conversacion_id)

        # Encuesta de una atención anterior que quedó sin responder (cerró el chat antes, o recargó la página).
        # Solo si ahora está con el bot: no se interrumpe una conversación en cola o con un asesor.
        if estado_actual == "bot":
            try:
                pendiente = await asyncio.to_thread(encuesta.pendiente_para_token, nuevo_token)
                if pendiente:
                    await websocket.send_json({"type": "encuesta_satisfaccion", "payload": pendiente})
            except Exception:
                logger.warning("No se pudo revisar la encuesta pendiente", exc_info=True)

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}

            # Envío fiable: el widget guarda lo que escribe sin red y lo reenvía al volver, con un
            # identificador (cid). Si ya lo habíamos procesado, solo se confirma (no se duplica).
            cid_actual = None
            if tipo in _TIPOS_FIABLES and payload.get("cid"):
                cid_actual = str(payload.get("cid"))[:64]
                if not _registrar_cid_widget(nuevo_token, cid_actual):
                    await websocket.send_json({"type": "mensaje_recibido_servidor",
                                               "payload": {"cid": cid_actual, "dup": True}})
                    continue

            try:
                async with _candado_widget(("conv", conversacion_id)):
                    if tipo == "iniciar_nueva_conversacion":
                        # La anterior ya está cerrada (o no existía) — crea/reanuda y arranca de nuevo
                        # sobre la MISMA conexión, sin pedir de nuevo celular/nombre si ya se conocen.
                        manager.disconnect_widget(conversacion_id)
                        conversacion_id, estado_actual, es_nueva = await _crear_o_reanudar_async(nuevo_token)
                        manager.reasignar_widget(conversacion_id, websocket)
                        if es_nueva:
                            await asyncio.to_thread(_registrar_origen, conversacion_id, "nueva_conversacion")
                        await websocket.send_json({
                            "type": "sesion_iniciada",
                            "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id,
                            "reconocido": await asyncio.to_thread(_nombre_reconocido, nuevo_token)},
                        })
                        await arrancar(conversacion_id, es_nueva, estado_actual)
                        # Si dejó sin responder la encuesta de la atención que acaba de terminar, se le vuelve a ofrecer.
                        if estado_actual == "bot":
                            try:
                                pendiente = await asyncio.to_thread(encuesta.pendiente_para_token, nuevo_token)
                                if pendiente:
                                    await websocket.send_json({"type": "encuesta_satisfaccion", "payload": pendiente})
                            except Exception:
                                logger.warning("No se pudo revisar la encuesta pendiente", exc_info=True)
                        continue

                    if tipo == "ping":
                        continue  # latido del widget: mantiene viva la conexión, no hace nada más

                    if tipo == "generar_traspaso":
                        resultado = await asyncio.to_thread(_crear_traspaso, nuevo_token, payload.get("url"))
                        if resultado.get("ok"):
                            await websocket.send_json({"type": "traspaso_listo", "payload": resultado})
                        else:
                            await websocket.send_json({"type": "traspaso_error",
                                                       "payload": {"motivo": resultado.get("motivo")}})
                        continue

                    if tipo == "cargar_historial_previo":
                        resultado = await asyncio.to_thread(
                            _historial_previo, nuevo_token, conversacion_id, payload.get("antes_de"))
                        await websocket.send_json({"type": "historial_previo", "payload": resultado})
                        continue

                    if tipo == "registrar_origen":
                        await asyncio.to_thread(
                            _registrar_origen, conversacion_id, payload.get("origen"), payload.get("pagina"))
                        continue

                    if tipo == "responder_encuesta":
                        # Solo el mismo visitante puede responder su encuesta (se valida con su sesion_token).
                        r = await asyncio.to_thread(encuesta.guardar_respuesta, payload.get("encuesta_id"),
                                                    nuevo_token, payload.get("respuestas") or {})
                        await websocket.send_json({"type": "encuesta_resultado",
                                                   "payload": {**r, "encuesta_id": payload.get("encuesta_id")}})
                        continue

                    if tipo == "omitir_encuesta":
                        await asyncio.to_thread(encuesta.omitir, payload.get("encuesta_id"), nuevo_token)
                        continue

                    if tipo == "reiniciar_datos":
                        await asyncio.to_thread(_limpiar_datos_usuario, nuevo_token)
                        manager.disconnect_widget(conversacion_id)
                        conversacion_id, estado_actual, es_nueva = await _crear_o_reanudar_async(nuevo_token)
                        manager.reasignar_widget(conversacion_id, websocket)
                        await websocket.send_json({
                            "type": "sesion_iniciada",
                            "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id,
                            "reconocido": await asyncio.to_thread(_nombre_reconocido, nuevo_token)},
                        })
                        await arrancar(conversacion_id, es_nueva, estado_actual, forzar_pedir_datos=True)
                        continue

                    estado, asesor_id = await asyncio.to_thread(_obtener_estado_conversacion, conversacion_id)

                    if estado == "bot" and tipo == "reenviar_otp":
                        resultado = await asyncio.to_thread(_reenviar_codigo_otp, conversacion_id)
                        if resultado["ok"]:
                            texto = "📲 Te reenviamos el código por SMS."
                            if resultado["puede_llamar"]:
                                texto += " Si sigue sin llegarte, puedes pedir que te llamemos para dictártelo."
                            await websocket.send_json({
                                "type": "mensaje_bot",
                                "payload": {
                                    "texto": texto,
                                    "otp_reenvios": resultado["reenvios"],
                                    "otp_puede_llamar": resultado["puede_llamar"],
                                },
                            })
                        else:
                            motivo = resultado.get("motivo")
                            texto = ("⚠️ No pudimos reenviar el código en este momento. Inténtalo de nuevo en unos segundos."
                                      if motivo == "error_envio" else
                                      "⚠️ Ocurrió un error. Escribe 'menu' para reiniciar.")
                            await websocket.send_json({"type": "mensaje_bot", "payload": {"texto": texto}})
                        continue

                    if estado == "bot" and tipo == "modificar_celular":
                        # Disponible desde el primer intento de código (no hace
                        # falta haber fallado ni haber reenviado) -- si la
                        # persona se dio cuenta de que escribió mal su celular,
                        # la manda de vuelta a webchat_pide_celular borrando todo
                        # lo que ya se había guardado del intento anterior
                        # (CELULAR, ENVIO_SMS, OTP_REENVIOS, etc.), igual que un
                        # inicio de conversación nuevo.
                        nodo_actual, _ = await asyncio.to_thread(_obtener_nodo_y_variables, conversacion_id)
                        if nodo_actual == "webchat_valida_pide_codigo":
                            eventos = await asyncio.to_thread(iniciar_conversacion, conversacion_id,
                                                              nodo_inicial="webchat_pide_celular")
                            await _procesar_eventos_bot(conversacion_id, websocket, eventos)
                        else:
                            await websocket.send_json({"type": "mensaje_bot", "payload": {
                                "texto": "⚠️ Ocurrió un error. Escribe 'menu' para reiniciar."}})
                        continue

                    if estado == "bot" and tipo == "iniciar_verificacion_whatsapp":
                        nodo_actual, variables = await asyncio.to_thread(_obtener_nodo_y_variables, conversacion_id)
                        celular = (variables.get("CELULAR") or "").strip()
                        if nodo_actual != "webchat_valida_pide_codigo" or not celular:
                            await websocket.send_json({"type": "verificacion_whatsapp_error",
                                                       "payload": {"motivo": "fuera_de_flujo"}})
                            continue
                        res = await asyncio.to_thread(verificacion_whatsapp.iniciar, conversacion_id, celular)
                        if not res.get("ok"):
                            await websocket.send_json({"type": "verificacion_whatsapp_error",
                                                       "payload": {"motivo": res.get("motivo")}})
                            continue
                        texto_wa = f"VERIFICAR-{res['codigo']}"
                        numero = await asyncio.to_thread(_numero_whatsapp_verificacion)
                        enlace_wa = f"https://wa.me/{numero}?text={texto_wa}"
                        qr_wa = None
                        if payload.get("con_qr"):  # en computador la persona necesita escanearlo con su teléfono
                            try:
                                import segno
                                qr_wa = segno.make(enlace_wa, error="m").svg_data_uri(
                                    scale=6, border=2, dark="#0A2540")
                            except Exception:
                                logger.exception("No se pudo generar el QR de WhatsApp")
                        await websocket.send_json({"type": "verificacion_whatsapp_lista", "payload": {
                            "codigo": res["codigo"], "texto": texto_wa, "numero": numero,
                            "enlace": enlace_wa, "qr": qr_wa, "expira_seg": res["expira_seg"]}})
                        continue

                    if estado == "bot" and tipo == "consultar_verificacion_whatsapp":
                        codigo_wa = "".join(c for c in str(payload.get("codigo") or "") if c.isdigit())[:6]
                        est = await asyncio.to_thread(verificacion_whatsapp.estado, conversacion_id, codigo_wa)
                        if est == "verificado":
                            await _aplicar_verificacion_whatsapp(conversacion_id, websocket, codigo_wa)
                        elif est in ("vencido", "desconocido"):
                            await websocket.send_json({"type": "verificacion_whatsapp_error",
                                                       "payload": {"motivo": "vencido"}})
                        continue

                    if estado == "bot" and tipo == "solicitar_llamada_otp":
                        resultado = await asyncio.to_thread(_solicitar_llamada_otp, conversacion_id)
                        if resultado["ok"]:
                            await websocket.send_json({
                                "type": "mensaje_bot",
                                "payload": {
                                    "texto": "📞 Te estamos llamando para dictarte el código. "
                                             "Contesta la llamada y escribe aquí el código que escuches.",
                                    "otp_reenvios": resultado["reenvios"],
                                    "otp_puede_llamar": True,
                                },
                            })
                        else:
                            texto_err = ("⚠️ Ya solicitaste varias llamadas. Usa el código del SMS o verifica con WhatsApp."
                                         if resultado.get("motivo") == "limite_llamadas" else
                                         "⚠️ No pudimos iniciar la llamada en este momento. Inténtalo de nuevo en unos segundos.")
                            _, variables_otp = await asyncio.to_thread(_obtener_nodo_y_variables, conversacion_id)
                            await websocket.send_json({"type": "mensaje_bot", "payload": {
                                "texto": texto_err, "otp_reenvios": int(variables_otp.get("OTP_REENVIOS") or 0),
                                "otp_puede_llamar": True}})
                        continue

                    # --- Agente de IA oculto: entrada (palabra clave + lista blanca) -----------------
                    en_ia = False
                    if tipo == "mensaje_usuario" and estado == "bot":
                        if _es_palabra_clave_ia(payload.get("texto"), "MAD_PALABRA_CLAVE"):
                            celular_mad = await asyncio.to_thread(_celular_tester_de_sesion, nuevo_token, "MAD_TESTERS")
                            if celular_mad:
                                eventos_mad = await asyncio.to_thread(
                                    entrar_a_nodo, conversacion_id, NODO_MAD, {"CELULAR": celular_mad})
                                if eventos_mad is not None:   # None = el nodo no existe: sigue como texto normal
                                    await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {"cid": cid_actual}})
                                    await _procesar_eventos_bot(conversacion_id, websocket, eventos_mad)
                                    continue
                        if _es_palabra_clave_ia(payload.get("texto")):
                            celular_tester = await asyncio.to_thread(_celular_tester_de_sesion, nuevo_token)
                            if celular_tester:
                                eventos_ia = await asyncio.to_thread(
                                    entrar_a_nodo, conversacion_id, NODO_IA, {"CELULAR": celular_tester})
                                if eventos_ia is not None:   # None = el nodo no existe: sigue como texto normal
                                    await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {"cid": cid_actual}})
                                    await _procesar_eventos_bot(conversacion_id, websocket, eventos_ia)
                                    continue
                        en_ia = await asyncio.to_thread(conversacion_en_ia, conversacion_id)
                    elif tipo == "click_opcion" and estado == "bot":
                        en_ia = await asyncio.to_thread(conversacion_en_ia, conversacion_id)
                        if en_ia and payload.get("opcion_codigo") == "ia_menu":
                            # Botón "Menú principal" del modo IA = escribir "menu"
                            tipo = "mensaje_usuario"
                            payload = {**payload, "texto": "menu"}

                    texto_comando = (payload.get("texto") or "").strip().lower()
                    if en_ia:
                        # Dentro del modo IA el agente le sugiere "Escribe *menu*": se aceptan los
                        # asteriscos y signos de puntuación alrededor. Fuera de IA no cambia nada.
                        texto_comando = texto_comando.strip(" *_.,;:!¡¿?\"'")

                    if (tipo == "mensaje_usuario" and estado in ("cerrada", "bot")
                            and texto_comando in ("hola", "inicio", "menu", "menú")):
                        # Mismo mecanismo que el botón "Iniciar nueva conversación" —
                        # esta es la palabra clave que en WhatsApp manejaba Chattigo.
                        # Funciona en cualquier punto del bot (no solo cerrada), pero
                        # NO interrumpe una conversación ya en cola o con un asesor real.
                        if estado == "cerrada":
                            manager.disconnect_widget(conversacion_id)
                            conversacion_id, estado_actual, es_nueva = await _crear_o_reanudar_async(nuevo_token)
                            manager.reasignar_widget(conversacion_id, websocket)
                            await websocket.send_json({
                                "type": "sesion_iniciada",
                                "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id,
                            "reconocido": await asyncio.to_thread(_nombre_reconocido, nuevo_token)},
                            })
                            await arrancar(conversacion_id, es_nueva, estado_actual)
                        else:
                            # Sigue en 'bot' (en cualquier punto del árbol) — reinicia
                            # la MISMA conversación desde el principio, sin crear una
                            # fila nueva (_crear_o_reanudar_conversacion la encontraría
                            # como "abierta" y solo la reanudaría tal cual estaba).
                            await arrancar(conversacion_id, True, estado)
                        continue

                    if tipo == "marcar_leido":
                        await asyncio.to_thread(_marcar_leidos_por_usuario, conversacion_id)
                        if asesor_id:
                            await manager.enviar_a_asesor(asesor_id, {
                                "type": "mensajes_leidos", "payload": {"conversacion_id": conversacion_id},
                            })
                        # Supervisores que están viendo la conversación: el cliente leyó (✓✓).
                        await manager.notificar_escuchas(conversacion_id, {
                            "type": "mensajes_leidos",
                            "payload": {"conversacion_id": conversacion_id, "lector": "usuario"},
                        })
                        continue

                    if estado == "bot":
                        # Todavía navegando el árbol de bot_menus
                        # Si alguien (asesor/supervisor) está "ayudando en el bot" a
                        # esta conversación, hay que reenviarle lo que pase en este
                        # turno (lo que escribe el cliente y lo que responde el bot).
                        # Se marca el último mensaje ANTES de procesar para saber
                        # cuáles son nuevos después.
                        desde_mensaje_id = (await asyncio.to_thread(_ultimo_mensaje_id, conversacion_id)
                                            if _hay_escuchas(conversacion_id) else None)
                        ya_confirmado = False
                        if en_ia and (tipo == "mensaje_usuario"
                                      or (tipo == "click_opcion"
                                          and str(payload.get("opcion_codigo") or "").startswith("ia_q:"))):
                            # Turno con el agente de IA: tarda 5-15 s (hasta 45). Se confirma la recepción,
                            # se muestra "escribiendo…" y la llamada corre en un hilo aparte para no
                            # congelar el servidor (el resto de clientes y asesores siguen normales).
                            await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {"cid": cid_actual}})
                            await websocket.send_json({"type": "escribiendo", "payload": {}})
                            ya_confirmado = True
                            eventos = await asyncio.to_thread(
                                recibir_entrada_usuario, conversacion_id, texto=payload.get("texto"),
                                opcion_id=payload.get("opcion_codigo") if tipo == "click_opcion" else None)
                        # El motor del bot consulta la base y servicios externos (SOFI, SMS, procesar receta):
                        # siempre en un hilo aparte para no congelar al resto de clientes y asesores.
                        elif tipo == "mensaje_usuario":
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id,
                                                              texto=payload.get("texto"))
                        elif tipo == "click_opcion":
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id,
                                                              opcion_id=payload.get("opcion_codigo"))
                        elif tipo == "ubicacion_compartida":
                            # Solo se pasan los 3 campos esperados (lat, lng, precisión), nada más del navegador.
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id, ubicacion={
                                "lat": payload.get("lat"), "lng": payload.get("lng"), "precision": payload.get("precision"),
                                "origen": "mapa" if payload.get("origen") == "mapa" else None})
                        elif tipo == "ubicacion_error":
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id,
                                                              ubicacion_error=str(payload.get("motivo") or "")[:40])
                        elif tipo == "archivo_subido":
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id,
                                                              archivo_url=payload.get("url"), archivo_ruta=payload.get("ruta"))
                        elif tipo == "formulario_completado":
                            eventos = await asyncio.to_thread(recibir_entrada_usuario, conversacion_id,
                                                              valores=payload.get("valores"))
                        else:
                            eventos = []
                        celular_verificado = _celular_verificado_en(eventos)
                        if celular_verificado is not None:
                            union = await asyncio.to_thread(_unir_a_conversacion_abierta, conversacion_id, celular_verificado)
                            if union:
                                if not ya_confirmado:
                                    await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {"cid": cid_actual}})
                                await _avisar_union(websocket, union)
                                continue
                        if (not ya_confirmado
                                and tipo in ("mensaje_usuario", "click_opcion", "archivo_subido", "formulario_completado",
                                     "ubicacion_compartida", "ubicacion_error")):
                            await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {"cid": cid_actual}})
                        await _procesar_eventos_bot(conversacion_id, websocket, eventos)
                        if desde_mensaje_id is not None:
                            await _notificar_escuchas_mensajes_nuevos(conversacion_id, desde_mensaje_id)

                    elif tipo == "mensaje_usuario":
                        # Ya derivado (en_cola o asignada): el mensaje va directo al asesor, no al bot
                        texto = payload.get("texto")
                        respondido_a_id = payload.get("respondido_a_id")
                        mensaje_id = await asyncio.to_thread(_guardar_mensaje_usuario, conversacion_id, texto, respondido_a_id)
                        respondido_a = await asyncio.to_thread(_obtener_preview_mensaje, respondido_a_id)
                        if asesor_id:
                            # Si la conversación estaba pausada (bandeja "Pausados" del
                            # asesor), el cliente acaba de escribir — se reactiva sola
                            # y vuelve a la bandeja principal antes de entregarle el
                            # mensaje, para que le llegue ya en su lugar normal.
                            if await asyncio.to_thread(_limpiar_pausa, conversacion_id):
                                await manager.enviar_a_asesor(asesor_id, {
                                    "type": "conversacion_reactivada",
                                    "payload": {"conversacion_id": conversacion_id},
                                })
                            await manager.enviar_a_asesor(asesor_id, {
                                "type": "mensaje_usuario",
                                "payload": {"conversacion_id": conversacion_id, "texto": texto,
                                            "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                            })
                        # "Recibido" real (✓✓ gris) solo cuando de verdad llegó a un asesor
                        # conectado — si sigue en cola, se queda en un solo ✓. En los dos
                        # casos se manda igual el mensaje_id, para que el widget pueda
                        # después editar/eliminar/responder este mismo mensaje.
                        await websocket.send_json({
                            "type": "mensaje_recibido_servidor",
                            "payload": {"mensaje_id": mensaje_id, "recibido": bool(asesor_id), "cid": cid_actual},
                        })
                        await manager.notificar_escuchas(conversacion_id, {
                            "type": "mensaje_conversacion",
                            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "usuario", "texto": texto,
                                        "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                        })

                    elif tipo == "archivo_subido":
                        # Igual que mensaje_usuario pero para un archivo: al asesor le llega
                        # como link clicable (se abre en pestaña aparte), no como texto plano.
                        # Se guarda la RUTA cruda (no la URL firmada, que expira en 1 hora y
                        # quedaría inútil para siempre) — el asesor/supervisor piden una
                        # firmada nueva al vuelo vía GET /archivo-receta cuando la abren.
                        ruta = payload.get("ruta")
                        respondido_a_id = payload.get("respondido_a_id")
                        mensaje_id = await asyncio.to_thread(_guardar_mensaje_usuario_archivo, conversacion_id, ruta, respondido_a_id)
                        respondido_a = await asyncio.to_thread(_obtener_preview_mensaje, respondido_a_id)
                        if asesor_id:
                            if await asyncio.to_thread(_limpiar_pausa, conversacion_id):
                                await manager.enviar_a_asesor(asesor_id, {
                                    "type": "conversacion_reactivada",
                                    "payload": {"conversacion_id": conversacion_id},
                                })
                            await manager.enviar_a_asesor(asesor_id, {
                                "type": "archivo_usuario",
                                "payload": {"conversacion_id": conversacion_id, "ruta": ruta,
                                            "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                            })
                        await websocket.send_json({
                            "type": "mensaje_recibido_servidor",
                            "payload": {"mensaje_id": mensaje_id, "recibido": bool(asesor_id), "cid": cid_actual},
                        })
                        await manager.notificar_escuchas(conversacion_id, {
                            "type": "mensaje_conversacion",
                            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "usuario",
                                        "texto": "📎 Archivo enviado", "ruta": ruta,
                                        "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                        })
                        # Si sigue en_cola (sin asesor_id todavía), el mensaje queda guardado
                        # en el historial y el asesor lo verá al asignarse — no se pierde,
                        # pero no hay entrega en vivo hasta que haya asignación.

                    elif tipo == "editar_mensaje":
                        resultado = await asyncio.to_thread(_editar_mensaje, payload.get("mensaje_id"), conversacion_id,
                                                            "usuario", None, payload.get("texto"))
                        if resultado:
                            await _difundir_mensaje_modificado("mensaje_editado", conversacion_id, resultado)

                    elif tipo == "eliminar_mensaje":
                        resultado = await asyncio.to_thread(_eliminar_mensaje, payload.get("mensaje_id"), conversacion_id,
                                                            "usuario", None, "Paciente")
                        if resultado:
                            await _difundir_mensaje_modificado("mensaje_eliminado", conversacion_id, resultado)

                    elif tipo == "reaccionar_mensaje":
                        resultado = await asyncio.to_thread(_reaccionar_mensaje, payload.get("mensaje_id"), conversacion_id,
                                                            "usuario", payload.get("emoji"))
                        if resultado:
                            await _difundir_mensaje_modificado("mensaje_reaccion", conversacion_id, resultado)

            except WebSocketDisconnect:
                raise
            except Exception:
                # Que un error puntual no deje al usuario con la pantalla en blanco
                # para siempre — se avisa y se le devuelven botones para reintentar.
                logger.exception("Error procesando mensaje del widget (tipo=%s, conversacion_id=%s)",
                                  tipo, conversacion_id)
                await websocket.send_json({
                    "type": "mensaje_bot",
                    "payload": {"texto": "⚠️ Ocurrió un error inesperado."},
                })
                await websocket.send_json({"type": "conversacion_cerrada", "payload": {"motivo": "error"}})

    except WebSocketDisconnect:
        manager.disconnect_widget(conversacion_id, websocket)


def _marcar_conectado(asesor_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Si quedó una sesión abierta de antes (la instancia se reinició en
            # un deploy sin avisar, o el socket viejo todavía no se enteró de
            # que se cayó), se cierra primero: así no se cuenta doble el tiempo
            # conectado ni queda una sesión "fantasma" abierta.
            cur.execute(
                """UPDATE asesor_sesiones SET desconectado_en = %s
                   WHERE asesor_id = %s AND desconectado_en IS NULL""",
                (datetime.now(LIMA_TZ), asesor_id),
            )
            cur.execute(
                "INSERT INTO asesor_sesiones (asesor_id, conectado_en) VALUES (%s, %s)",
                (asesor_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _marcar_desconectado(asesor_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE asesor_sesiones SET desconectado_en = %s
                   WHERE asesor_id = %s AND desconectado_en IS NULL""",
                (datetime.now(LIMA_TZ), asesor_id),
            )
            # Si se desconecta estando en pausa, cierra también la pausa abierta
            # (para no dejar tiempo de pausa corriendo indefinidamente).
            cur.execute(
                """UPDATE asesor_pausas SET fin = %s
                   WHERE asesor_id = %s AND fin IS NULL""",
                (datetime.now(LIMA_TZ), asesor_id),
            )
            cur.execute("UPDATE asesores SET en_pausa = FALSE WHERE id = %s", (asesor_id,))
        conn.commit()
    finally:
        release_connection(conn)


MOTIVOS_PAUSA = {"reunion", "bano", "almuerzo", "otro"}


def _cambiar_pausa_asesor(asesor_id: int, pausado: bool, motivo=None, detalle=None):
    """Pausa/reanuda al asesor. Al pausar se guarda el motivo (reunion, bano,
    almuerzo, otro + un detalle opcional para "otro"). Si no viene motivo (el
    asesor se reconectó estando en pausa) se reutiliza el de la pausa que se
    cerró hace pocos minutos por la caída de conexión."""
    motivo = motivo if motivo in MOTIVOS_PAUSA else None
    detalle = " ".join(str(detalle or "").split())[:80] or None
    if motivo != "otro":
        detalle = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET en_pausa = %s WHERE id = %s", (pausado, asesor_id))
            if pausado:
                # Idempotente: si ya hay una pausa abierta (p. ej. el socket
                # viejo no alcanzó a cerrarla) no se abre una segunda.
                # Los SAVEPOINT hacen que, si todavía no se corrió la migración
                # de las columnas del motivo, la pausa igual se registre (sin motivo).
                try:
                    cur.execute("SAVEPOINT sp_motivo")
                    if motivo is None:
                        cur.execute(
                            """SELECT motivo, motivo_detalle FROM asesor_pausas
                               WHERE asesor_id = %s AND fin IS NOT NULL AND motivo IS NOT NULL
                                 AND fin >= %s
                               ORDER BY inicio DESC LIMIT 1""",
                            (asesor_id, datetime.now(LIMA_TZ) - timedelta(minutes=10)),
                        )
                        previo = cur.fetchone()
                        if previo:
                            motivo, detalle = previo
                    cur.execute(
                        """INSERT INTO asesor_pausas (asesor_id, inicio, motivo, motivo_detalle)
                           SELECT %s, %s, %s, %s
                           WHERE NOT EXISTS (
                               SELECT 1 FROM asesor_pausas WHERE asesor_id = %s AND fin IS NULL
                           )""",
                        (asesor_id, datetime.now(LIMA_TZ), motivo, detalle, asesor_id),
                    )
                except Exception as e:
                    logger.warning("Pausa sin motivo (¿falta migracion_motivo_pausa.sql?): %s", e)
                    cur.execute("ROLLBACK TO SAVEPOINT sp_motivo")
                    cur.execute(
                        """INSERT INTO asesor_pausas (asesor_id, inicio)
                           SELECT %s, %s
                           WHERE NOT EXISTS (
                               SELECT 1 FROM asesor_pausas WHERE asesor_id = %s AND fin IS NULL
                           )""",
                        (asesor_id, datetime.now(LIMA_TZ), asesor_id),
                    )
            else:
                cur.execute(
                    """UPDATE asesor_pausas SET fin = %s
                       WHERE asesor_id = %s AND fin IS NULL""",
                    (datetime.now(LIMA_TZ), asesor_id),
                )
        conn.commit()
    finally:
        release_connection(conn)


INACTIVIDAD_MINUTOS_DEFAULT = 5


def _obtener_inactividad_minutos() -> int:
    """Minutos de silencio antes de disparar el aviso automático — editable
    desde la consola de supervisor (guardado en configuracion_global junto
    con los demás mensajes automáticos). Si no se ha configurado nunca, o el
    valor guardado no es un número válido, usa el default de 5."""
    valor = supervisor.obtener_configuracion("inactividad_minutos")
    try:
        minutos = int(valor)
        return minutos if minutos > 0 else INACTIVIDAD_MINUTOS_DEFAULT
    except (TypeError, ValueError):
        return INACTIVIDAD_MINUTOS_DEFAULT


def _revisar_inactividad_sync():
    """Parte bloqueante (DB) de la revisión de inactividad. Se ejecuta SIEMPRE
    en un hilo aparte (asyncio.to_thread) — nunca directamente en el event
    loop — porque psycopg2 es síncrono y una conexión lenta a Cloud SQL no
    debe congelar el servidor (WebSockets, health checks, etc.).

    Se repite cada N minutos MIENTRAS siga el silencio (no una sola vez por
    tramo) — si la última interacción real fue a las 4:00pm y son las
    4:30pm con N=5, ya debió sonar 6 veces (4:05, 4:10, ..., 4:30). En
    cuanto haya un mensaje real nuevo de usuario/asesor, el conteo se
    reinicia solo (se compara contra ESE mensaje, no contra el último aviso
    enviado)."""
    texto = (
        supervisor.obtener_configuracion("mensaje_inactividad_5min")
        or MENSAJES_AUTOMATICOS["mensaje_inactividad_5min"]["default"]
    )
    limite = datetime.now(LIMA_TZ) - timedelta(minutes=_obtener_inactividad_minutos())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id
                FROM conversaciones c
                LEFT JOIN LATERAL (
                    SELECT MAX(enviado_en) AS ultimo FROM mensajes m
                    WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('usuario', 'asesor')
                ) um ON TRUE
                WHERE c.estado IN ('asignada', 'en_cola')
                  AND c.aviso_inactividad_activo = TRUE
                  -- Una conversación ya asignada cuyo asesor todavía no ha
                  -- respondido tiene su propio aviso (ver _revisar_espera_asesor_sync).
                  AND NOT (c.estado = 'asignada' AND c.asignado_en IS NOT NULL AND c.pausada_en IS NULL
                           AND NOT EXISTS (
                               SELECT 1 FROM mensajes m2
                               WHERE m2.conversacion_id = c.id AND m2.emisor_tipo IN ('asesor', 'supervisor')
                                 AND m2.enviado_en >= c.asignado_en))
                  AND COALESCE(um.ultimo, c.iniciado_en) <= %s
                  AND (c.aviso_inactividad_en IS NULL OR c.aviso_inactividad_en <= %s)
                """,
                (limite, limite),
            )
            conversacion_ids = [row[0] for row in cur.fetchall()]
            for conversacion_id in conversacion_ids:
                cur.execute(
                    """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                       VALUES (%s, 'bot', 'texto', %s, %s)""",
                    (conversacion_id, texto, datetime.now(LIMA_TZ)),
                )
                cur.execute(
                    "UPDATE conversaciones SET aviso_inactividad_en = %s WHERE id = %s",
                    (datetime.now(LIMA_TZ), conversacion_id),
                )
        conn.commit()
    finally:
        release_connection(conn)

    return texto, conversacion_ids


async def _revisar_inactividad():
    texto, conversacion_ids = await asyncio.to_thread(_revisar_inactividad_sync)
    for conversacion_id in conversacion_ids:
        await manager.enviar_a_widget(conversacion_id, {
            "type": "mensaje_bot", "payload": {"texto": texto},
        })
        await manager.notificar_escuchas(conversacion_id, {
            "type": "mensaje_conversacion",
            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "bot", "texto": texto},
        })


# Aviso "Gracias por tu paciencia..." — cuando una conversación YA está asignada
# a un asesor pero este todavía no ha respondido: se manda a los 30 segundos de
# la asignación y luego cada 5 minutos, hasta que el asesor (o un supervisor)
# escriba. Texto editable en el Editor ("mensaje_espera_asesor"); {{nombre_del_asesor}}
# se reemplaza por el nombre del asesor que tiene la conversación.
ESPERA_ASESOR_PRIMER_AVISO_SEG = 30
ESPERA_ASESOR_REPETIR_MIN = 5
ESPERA_ASESOR_MAX_HORAS = 3  # no se insiste con conversaciones abandonadas hace horas

_RE_NOMBRE_ASESOR = re.compile(r"\{\{\s*nombre_del_asesor\s*\}\}", re.IGNORECASE)


def _revisar_espera_asesor_sync():
    """Devuelve [(conversacion_id, texto)] de los avisos que toca mandar ahora
    (ya registrados en la base)."""
    plantilla = (
        supervisor.obtener_configuracion("mensaje_espera_asesor")
        or MENSAJES_AUTOMATICOS["mensaje_espera_asesor"]["default"]
    )
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, a.nombre_display, a.id
                FROM conversaciones c
                JOIN asesores a ON a.id = c.asesor_id
                WHERE c.estado = 'asignada' AND c.asignado_en IS NOT NULL
                  AND c.pausada_en IS NULL
                  AND c.aviso_inactividad_activo = TRUE
                  AND c.asignado_en >= %(desde)s
                  AND NOT EXISTS (
                      SELECT 1 FROM mensajes m
                      WHERE m.conversacion_id = c.id AND m.emisor_tipo IN ('asesor', 'supervisor')
                        AND m.enviado_en >= c.asignado_en)
                  AND (
                      (COALESCE(c.aviso_espera_asesor_en, '-infinity') < c.asignado_en
                           AND c.asignado_en <= %(primer)s)
                      OR (c.aviso_espera_asesor_en >= c.asignado_en
                           AND c.aviso_espera_asesor_en <= %(repetir)s)
                  )
                """,
                {
                    "desde": ahora - timedelta(hours=ESPERA_ASESOR_MAX_HORAS),
                    "primer": ahora - timedelta(seconds=ESPERA_ASESOR_PRIMER_AVISO_SEG),
                    "repetir": ahora - timedelta(minutes=ESPERA_ASESOR_REPETIR_MIN),
                },
            )
            avisos = []
            for conversacion_id, nombre_asesor, asesor_id in cur.fetchall():
                texto = _RE_NOMBRE_ASESOR.sub(nombre_asesor or "tu asesor", plantilla)
                cur.execute(
                    """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                       VALUES (%s, 'bot', 'texto', %s, %s)""",
                    (conversacion_id, texto, datetime.now(LIMA_TZ)),
                )
                cur.execute(
                    "UPDATE conversaciones SET aviso_espera_asesor_en = %s WHERE id = %s",
                    (datetime.now(LIMA_TZ), conversacion_id),
                )
                avisos.append((conversacion_id, texto, asesor_id))
        conn.commit()
        return avisos
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


async def _revisar_espera_asesor():
    avisos = await asyncio.to_thread(_revisar_espera_asesor_sync)
    for conversacion_id, texto, asesor_id in avisos:
        await manager.enviar_a_widget(conversacion_id, {
            "type": "mensaje_bot", "payload": {"texto": texto},
        })
        # El asesor también ve que se le mandó este aviso al cliente.
        await manager.enviar_a_asesor(asesor_id, {
            "type": "aviso_automatico_cliente",
            "payload": {"conversacion_id": conversacion_id, "texto": texto},
        })
        await manager.notificar_escuchas(conversacion_id, {
            "type": "mensaje_conversacion",
            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "bot", "texto": texto},
        })


async def _loop_inactividad():
    # Deja que uvicorn termine de arrancar y pase el health check de Cloud Run
    # antes de tocar la base de datos por primera vez.
    await asyncio.sleep(20)
    vuelta = 0
    while True:
        # El aviso de espera del asesor se revisa cada 10 s (para que el primer
        # aviso salga cerca de los 30 s); la inactividad general, cada 30 s.
        try:
            await _revisar_espera_asesor()
        except Exception:
            logger.exception("Error revisando el aviso de espera del asesor")
        if vuelta % 3 == 0:
            try:
                await _revisar_inactividad()
            except Exception:
                logger.exception("Error revisando inactividad de conversaciones")
        vuelta += 1
        await asyncio.sleep(10)


# Hilos para asyncio.to_thread (base de datos, SMS, SOFI, agente de IA). Por defecto Python usa
# cpu_count + 4 = 5 hilos en Cloud Run (1 vCPU): unas pocas consultas al agente de IA (5-45 s cada una)
# bastaban para que todo lo demás que corre en hilos quedara en fila. Las conexiones a la base las sigue
# limitando el pool de db.py (un hilo que espera conexión no frena al servidor).
_HILOS_TRABAJO = 32


@app.on_event("startup")
async def _iniciar_watcher_inactividad():
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_HILOS_TRABAJO, thread_name_prefix="trabajo"))
    asyncio.create_task(_loop_inactividad())


def _cerrar_conversacion(conversacion_id: int, etiqueta_cierre_id, cerrado_por: str = "asesor"):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE conversaciones
                   SET estado = 'cerrada', cerrado_en = %s, cerrado_por = %s,
                       etiqueta_cierre_id = %s, bandera = NULL
                   WHERE id = %s""",
                (datetime.now(LIMA_TZ), cerrado_por, etiqueta_cierre_id, conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _guardar_mensaje_asesor(conversacion_id: int, texto: str, emisor_tipo: str = "asesor",
                             emisor_id: int | None = None, respondido_a_id: int | None = None) -> int:
    metadata = {"respondido_a_id": respondido_a_id} if respondido_a_id else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, emisor_id, tipo_contenido, contenido, metadata, enviado_en)
                   VALUES (%s, %s, %s, 'texto', %s, %s, %s) RETURNING id""",
                (conversacion_id, emisor_tipo, emisor_id, texto,
                 json.dumps(metadata) if metadata else None, datetime.now(LIMA_TZ)),
            )
            mensaje_id = cur.fetchone()[0]
        conn.commit()
        return mensaje_id
    finally:
        release_connection(conn)


def _guardar_mensaje_staff_archivo(conversacion_id: int, ruta: str, emisor_tipo: str,
                                    emisor_id: int | None = None, respondido_a_id: int | None = None) -> int:
    """Igual que _guardar_mensaje_usuario_archivo pero para una imagen que el
    ASESOR o SUPERVISOR pega/adjunta y envía al cliente — 'ruta' es siempre
    el nombre del objeto en el bucket (nunca una URL firmada, por la misma
    razón que el resto de archivos de este sistema: una firmada guardada
    queda inútil en cuanto expira)."""
    metadata = {"respondido_a_id": respondido_a_id} if respondido_a_id else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, emisor_id, tipo_contenido, contenido, metadata, enviado_en)
                   VALUES (%s, %s, %s, 'archivo', %s, %s, %s) RETURNING id""",
                (conversacion_id, emisor_tipo, emisor_id, ruta,
                 json.dumps(metadata) if metadata else None, datetime.now(LIMA_TZ)),
            )
            mensaje_id = cur.fetchone()[0]
        conn.commit()
        return mensaje_id
    finally:
        release_connection(conn)


def _set_pausa_conversacion(conversacion_id: int, asesor_id: int, pausar: bool) -> bool:
    """Pausa/reanuda MANUALMENTE una conversación — solo el asesor dueño de
    esa conversación puede tocarla. No cambia estado ni asesor_id, es
    puramente una bandeja paralela dentro de su propia consola: no libera
    cupo ni afecta la asignación. Devuelve True si de verdad cambió algo."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if pausar:
                cur.execute(
                    """UPDATE conversaciones SET pausada_en = %s
                       WHERE id = %s AND asesor_id = %s AND pausada_en IS NULL""",
                    (datetime.now(LIMA_TZ), conversacion_id, asesor_id),
                )
            else:
                cur.execute(
                    """UPDATE conversaciones SET pausada_en = NULL
                       WHERE id = %s AND asesor_id = %s AND pausada_en IS NOT NULL""",
                    (conversacion_id, asesor_id),
                )
            cambio = cur.rowcount > 0
        conn.commit()
        return cambio
    finally:
        release_connection(conn)


def _set_aviso_inactividad_conversacion(conversacion_id: int, activo: bool,
                                          asesor_id_esperado: int | None) -> bool:
    """Prende/apaga el aviso automático de inactividad para UNA conversación
    puntual. Toda conversación nace con esto en TRUE (ver columna
    aviso_inactividad_activo, default TRUE); este botón es para apagarlo
    cuando se sabe que el cliente va a demorar en responder (coordinando algo,
    por ejemplo) y no se quiere que el bot le mande igual el aviso de
    "¿sigues ahí?".

    Si asesor_id_esperado viene dado (lo llama la consola de asesor), solo
    puede tocar SUS PROPIAS conversaciones -- si viene None (lo llama la
    consola de supervisor), puede tocar cualquiera. Devuelve True si de
    verdad cambió algo."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if asesor_id_esperado is not None:
                cur.execute(
                    """UPDATE conversaciones SET aviso_inactividad_activo = %s
                       WHERE id = %s AND asesor_id = %s AND aviso_inactividad_activo != %s""",
                    (activo, conversacion_id, asesor_id_esperado, activo),
                )
            else:
                cur.execute(
                    """UPDATE conversaciones SET aviso_inactividad_activo = %s
                       WHERE id = %s AND aviso_inactividad_activo != %s""",
                    (activo, conversacion_id, activo),
                )
            cambio = cur.rowcount > 0
        conn.commit()
        return cambio
    finally:
        release_connection(conn)


def _limpiar_pausa(conversacion_id: int) -> bool:
    """Si la conversación estaba pausada, la reactiva AUTOMÁTICAMENTE (vuelve
    a la bandeja principal) porque el cliente acaba de escribir algo — se
    llama desde cada punto donde llega un mensaje/archivo del cliente para
    una conversación ya derivada (en_cola o asignada). Devuelve True si de
    verdad estaba pausada, para avisarle al asesor en vivo."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversaciones SET pausada_en = NULL WHERE id = %s AND pausada_en IS NOT NULL",
                (conversacion_id,),
            )
            cambio = cur.rowcount > 0
        conn.commit()
        return cambio
    finally:
        release_connection(conn)


# Enlace fijo que lleva el SMS de invitación (página de Sanitas desde donde se
# abre el chat). Es distinto al SMS de una conversación ya iniciada, que lleva
# el enlace directo a esa conversación.
SMS_INVITACION_LINK = "https://sanitasperu.com/?ac=1"

# Límites del SMS de invitación (en memoria, por instancia): evitan que un
# clic repetido o un error de digitación dispare muchos SMS al mismo número,
# y que una sola cuenta mande SMS en masa.
_SMS_INVITACION_MISMO_NUMERO_SEG = 10 * 60
_SMS_INVITACION_MAX_POR_HORA = 20
_sms_invitacion_por_numero: dict[str, float] = {}
_sms_invitacion_por_remitente: dict[int, list] = {}


def _normalizar_celular_pe(crudo) -> str | None:
    """Deja solo dígitos y devuelve el celular con código de país ('51' +
    9 dígitos que empiezan con 9), igual que el celular que guarda el bot.
    None si no es un celular peruano válido."""
    digitos = "".join(c for c in str(crudo or "") if c.isdigit())
    if len(digitos) == 9 and digitos.startswith("9"):
        return "51" + digitos
    if len(digitos) == 11 and digitos.startswith("519"):
        return digitos
    return None


SMS_INVITACION_MAX_CARACTERES = 160


def _texto_sms_invitacion() -> str:
    """Texto que se propone en el SMS de invitación (plantilla configurable
    con el enlace fijo ya puesto)."""
    plantilla = (
        supervisor.obtener_configuracion("mensaje_sms_invitacion")
        or MENSAJES_AUTOMATICOS["mensaje_sms_invitacion"]["default"]
    )
    return plantilla.replace("{{LINK}}", SMS_INVITACION_LINK)


def _campanas_permitidas_para_sms(usuario_id: int):
    """Campañas que puede elegir al mandar un enlace directo: un asesor, las suyas; un supervisor, las de su
    alcance (None = todas)."""
    if _obtener_rol(usuario_id) == "supervisor":
        alcance = supervisor.obtener_alcance(usuario_id)
        return None if alcance is None else set(alcance["ids"])
    return set(enlaces_directos.campanas_del_asesor(usuario_id))


def _destinos_sms_directo(usuario_id: int) -> dict:
    return {"campanas": enlaces_directos.destinos(_campanas_permitidas_para_sms(usuario_id))}


def _enviar_sms_invitacion(celular_crudo, remitente_id: int, texto=None, campana_id=None, asesor_id=None) -> dict:
    """Manda un SMS que INVITA a un número nuevo a escribirnos por el webchat
    (a diferencia de _enviar_sms_saliente, que es para un cliente que ya tiene
    una conversación). El enlace es la URL pública del widget, sin sesión: al
    abrirlo el cliente empieza desde cero. Lo puede disparar un asesor o un
    supervisor. Devuelve {"ok": bool, "motivo": str | None}."""
    celular = _normalizar_celular_pe(celular_crudo)
    if not celular:
        return {"ok": False, "motivo": "Celular no válido. Escribe 9 dígitos que empiecen con 9 (ejemplo: 987654321)."}

    ahora = time.time()
    ultimo = _sms_invitacion_por_numero.get(celular)
    if ultimo and ahora - ultimo < _SMS_INVITACION_MISMO_NUMERO_SEG:
        faltan = int((_SMS_INVITACION_MISMO_NUMERO_SEG - (ahora - ultimo)) // 60) + 1
        return {"ok": False, "motivo": f"A ese número ya se le envió una invitación hace poco. Intenta de nuevo en ~{faltan} min."}
    recientes = [t for t in _sms_invitacion_por_remitente.get(remitente_id, []) if ahora - t < 3600]
    if len(recientes) >= _SMS_INVITACION_MAX_POR_HORA:
        return {"ok": False, "motivo": f"Llegaste al máximo de {_SMS_INVITACION_MAX_POR_HORA} invitaciones por hora."}

    # El texto puede venir editado desde la pantalla; si no, se usa la plantilla.
    mensaje = " ".join(str(texto).split()) if texto and str(texto).strip() else _texto_sms_invitacion()
    if len(mensaje) > SMS_INVITACION_MAX_CARACTERES:
        return {"ok": False, "motivo": f"El mensaje tiene {len(mensaje)} caracteres; el máximo es {SMS_INVITACION_MAX_CARACTERES}."}
    if SMS_INVITACION_LINK not in mensaje:
        return {"ok": False, "motivo": f"El mensaje debe incluir el enlace {SMS_INVITACION_LINK}."}

    token_directo, destino_nombre = None, None
    if campana_id not in (None, "", 0, "0"):
        # Enlace DIRECTO: lleva a la campaña (y al asesor, si se eligió). Solo de las campañas del que envía.
        token_directo, destino_nombre = enlaces_directos.crear_enlace(
            celular, campana_id, asesor_id, remitente_id, _obtener_rol(remitente_id) or "asesor",
            _campanas_permitidas_para_sms(remitente_id))
        if token_directo is None:
            return {"ok": False, "motivo": destino_nombre}
        mensaje = mensaje.replace(SMS_INVITACION_LINK, f"{SMS_INVITACION_LINK.split('?')[0]}?ad={token_directo}")
        if len(mensaje) > SMS_INVITACION_MAX_CARACTERES:
            enlaces_directos.borrar_enlace(token_directo)
            return {"ok": False, "motivo": f"Con el enlace directo el mensaje tiene {len(mensaje)} caracteres; el máximo es {SMS_INVITACION_MAX_CARACTERES}. Acórtalo un poco."}

    if not enviar_sms_danaconnect(celular, mensaje):
        if token_directo:
            enlaces_directos.borrar_enlace(token_directo)
        return {"ok": False, "motivo": "Danaconnect no pudo enviar el SMS. Intenta de nuevo en un momento."}

    _sms_invitacion_por_numero[celular] = ahora
    _sms_invitacion_por_remitente[remitente_id] = recientes + [ahora]
    # Auditoría en el log (la tabla de intervenciones exige una conversación).
    logger.info("SMS de invitación enviado por usuario %s a ***%s%s", remitente_id, celular[-3:],
                f" (enlace directo a {destino_nombre})" if token_directo else "")
    return {"ok": True, "motivo": None, "destino": destino_nombre}


# ---------------------------------------------------------------------
# Enviar un archivo (PDF/imagen) al cliente por SMS: el asesor o supervisor lo sube y al
# cliente le llega un enlace ÚNICO (un solo uso, vence a los 7 días) que abre el webchat ya
# con el archivo dentro de la conversación.
# ---------------------------------------------------------------------
_ARCHIVO_SMS_VIGENCIA_DIAS = 7
_ARCHIVO_SMS_MISMO_NUMERO_SEG = 60
_ARCHIVO_SMS_MAX_POR_HORA = 30
_archivo_sms_por_numero: dict[str, float] = {}
_archivo_sms_por_remitente: dict[int, list] = {}
_EXTENSIONES_ARCHIVO_SMS = {"pdf", "jpg", "jpeg", "png", "webp", "gif", "heic"}
TEXTO_SMS_ARCHIVO = "Sanitas: te enviamos un archivo. Ábrelo aquí y te atendemos: {{LINK}}"


def _enviar_archivo_sms(celular_crudo, remitente_id: int, rol: str, ruta, nombre_archivo=None) -> dict:
    """Crea el enlace único del archivo y lo manda por SMS. 'ruta' es el objeto ya subido al
    bucket (POST /upload con el login del asesor/supervisor). Devuelve {"ok": bool, "motivo": str|None}."""
    celular = _normalizar_celular_pe(celular_crudo)
    if not celular:
        return {"ok": False, "motivo": "Celular no válido. Escribe 9 dígitos que empiecen con 9 (ejemplo: 987654321)."}
    ruta = str(ruta or "")
    extension = ruta.rsplit(".", 1)[-1].lower() if "." in ruta else ""
    if not ruta.startswith("webchat/") or ".." in ruta or extension not in _EXTENSIONES_ARCHIVO_SMS:
        return {"ok": False, "motivo": "Solo se puede enviar un PDF o una imagen."}

    ahora = time.time()
    ultimo = _archivo_sms_por_numero.get(celular)
    if ultimo and ahora - ultimo < _ARCHIVO_SMS_MISMO_NUMERO_SEG:
        return {"ok": False, "motivo": "A ese número se le acaba de enviar un archivo. Espera un minuto."}
    recientes = [x for x in _archivo_sms_por_remitente.get(remitente_id, []) if ahora - x < 3600]
    if len(recientes) >= _ARCHIVO_SMS_MAX_POR_HORA:
        return {"ok": False, "motivo": f"Llegaste al máximo de {_ARCHIVO_SMS_MAX_POR_HORA} archivos por hora."}

    base = (supervisor.obtener_configuracion("widget_url") or "").strip() or SMS_INVITACION_LINK.split("?")[0]
    token = secrets.token_urlsafe(16)
    separador = "&" if "?" in base else "?"
    link = f"{base}{separador}af={token}"
    mensaje = TEXTO_SMS_ARCHIVO.replace("{{LINK}}", link)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO envios_archivo (token, celular, ruta, nombre_archivo, enviado_por, rol_emisor, expira_en)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (token, celular, ruta, (nombre_archivo or "")[:200] or None, remitente_id,
                 "supervisor" if rol == "supervisor" else "asesor",
                 datetime.now(LIMA_TZ) + timedelta(days=_ARCHIVO_SMS_VIGENCIA_DIAS)),
            )
            envio_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("No se pudo registrar el envío de archivo (¿falta migracion_envio_archivo_sms.sql?)")
        return {"ok": False, "motivo": "No se pudo preparar el envío. Avisa al administrador (falta la migración de envío de archivos)."}
    finally:
        release_connection(conn)

    if not enviar_sms_danaconnect(celular, mensaje):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM envios_archivo WHERE id = %s", (envio_id,))
            conn.commit()
        finally:
            release_connection(conn)
        return {"ok": False, "motivo": "Danaconnect no pudo enviar el SMS. Intenta de nuevo en un momento."}

    _archivo_sms_por_numero[celular] = ahora
    _archivo_sms_por_remitente[remitente_id] = recientes + [ahora]
    logger.info("Archivo por SMS enviado por usuario %s a ***%s (envío %s)", remitente_id, celular[-3:], envio_id)
    return {"ok": True, "motivo": None}


class CanjearArchivoSms(BaseModel):
    token: str
    sesion_token: str | None = None


@app.post("/widget/archivo-enlace/canjear")
def canjear_archivo_sms(datos: CanjearArchivoSms):
    """El cliente abre el enlace del SMS: se canjea el token (un solo uso) y se le entrega la sesión
    del webchat de ESE celular. El archivo se muestra al conectarse el chat (ver _entregar_archivos_pendientes)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE envios_archivo SET canjeado_en = %s
                   WHERE token = %s AND canjeado_en IS NULL AND expira_en > %s
                   RETURNING id, celular""",
                (datetime.now(LIMA_TZ), (datos.token or "")[:64], datetime.now(LIMA_TZ)),
            )
            fila = cur.fetchone()
            if not fila:
                conn.commit()
                raise HTTPException(status_code=410, detail="El enlace venció o ya se usó")
            envio_id, celular = fila

            usuario_id, sesion = None, None
            if datos.sesion_token:
                cur.execute("SELECT id, telefono, sesion_token FROM usuarios_webchat WHERE sesion_token = %s",
                            (datos.sesion_token[:100],))
                u = cur.fetchone()
                if u:
                    tel = _normalizar_celular_pe(u[1])
                    if not u[1] or tel == celular:
                        usuario_id, sesion = u[0], u[2]
            if usuario_id is None:
                # Sin sesión, o la sesión de este navegador es de OTRA persona: sesión nueva de este celular.
                sesion = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO usuarios_webchat (sesion_token, creado_en, ultima_actividad) VALUES (%s, %s, %s) RETURNING id",
                    (sesion, datetime.now(LIMA_TZ), datetime.now(LIMA_TZ)),
                )
                usuario_id = cur.fetchone()[0]
            # Recibió el SMS en este celular = celular comprobado.
            cur.execute("UPDATE usuarios_webchat SET telefono = %s WHERE id = %s AND COALESCE(telefono, '') = ''",
                        (celular, usuario_id))
            try:
                cur.execute("SAVEPOINT sp_verif")
                cur.execute("UPDATE usuarios_webchat SET verificado_en = %s WHERE id = %s AND verificado_en IS NULL",
                            (datetime.now(LIMA_TZ), usuario_id))
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_verif")
            cur.execute("UPDATE envios_archivo SET usuario_id = %s WHERE id = %s", (usuario_id, envio_id))
        conn.commit()
        return {"sesion_token": sesion}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        logger.exception("Error canjeando el enlace de archivo")
        raise HTTPException(status_code=500, detail="No se pudo abrir el archivo")
    finally:
        release_connection(conn)


class CanjearEnlaceDirecto(BaseModel):
    token: str
    sesion_token: str | None = None


@app.post("/widget/enlace-directo/canjear")
def canjear_enlace_directo(datos: CanjearEnlaceDirecto):
    """El cliente abre el SMS con enlace directo (?ad=): se canjea el token (un solo uso) y se le entrega la
    sesión del webchat de ESE celular. Al conectarse el chat, ws_widget lo deriva a la campaña/asesor elegido."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE enlaces_directos SET canjeado_en = %s
                   WHERE token = %s AND canjeado_en IS NULL AND expira_en > %s
                   RETURNING id, celular""",
                (datetime.now(LIMA_TZ), (datos.token or "")[:64], datetime.now(LIMA_TZ)),
            )
            fila = cur.fetchone()
            if not fila:
                conn.commit()
                raise HTTPException(status_code=410, detail="El enlace venció o ya se usó")
            envio_id, celular = fila

            usuario_id, sesion = None, None
            if datos.sesion_token:
                cur.execute("SELECT id, telefono, sesion_token FROM usuarios_webchat WHERE sesion_token = %s",
                            (datos.sesion_token[:100],))
                u = cur.fetchone()
                if u:
                    tel = _normalizar_celular_pe(u[1])
                    if not u[1] or tel == celular:
                        usuario_id, sesion = u[0], u[2]
            if usuario_id is None:
                sesion = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO usuarios_webchat (sesion_token, creado_en, ultima_actividad) VALUES (%s, %s, %s) RETURNING id",
                    (sesion, datetime.now(LIMA_TZ), datetime.now(LIMA_TZ)),
                )
                usuario_id = cur.fetchone()[0]
            cur.execute("UPDATE usuarios_webchat SET telefono = %s WHERE id = %s AND COALESCE(telefono, '') = ''",
                        (celular, usuario_id))
            try:
                cur.execute("SAVEPOINT sp_verif")
                cur.execute("UPDATE usuarios_webchat SET verificado_en = %s WHERE id = %s AND verificado_en IS NULL",
                            (datetime.now(LIMA_TZ), usuario_id))
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_verif")
            cur.execute("UPDATE enlaces_directos SET usuario_id = %s WHERE id = %s", (usuario_id, envio_id))
        conn.commit()
        return {"sesion_token": sesion}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        logger.exception("Error canjeando el enlace directo")
        raise HTTPException(status_code=500, detail="No se pudo abrir el enlace")
    finally:
        release_connection(conn)


TEXTO_ENLACE_DIRECTO_CONECTANDO = "Te estamos comunicando con un asesor de *{campana}*. Un momento, por favor 🙌"


def _guardar_mensaje_bot_texto(conversacion_id: int, texto: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'bot', 'texto', %s, %s)""",
                (conversacion_id, texto, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _poner_en_cola_directo(conversacion_id: int, campana_id: int) -> bool:
    """Deja la conversación en la cola de la campaña elegida (si aún no la atiende un asesor)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE conversaciones SET estado = 'en_cola', campana_id = %s, nodo_actual = NULL, entrada_cola = %s
                   WHERE id = %s AND estado IN ('bot', 'en_cola')""",
                (campana_id, datetime.now(LIMA_TZ), conversacion_id),
            )
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    finally:
        release_connection(conn)


async def _decidir_enlace_directo(conversacion_id: int, estado_actual: str, es_nueva: bool, websocket: WebSocket):
    """Revisa si el cliente llegó por un SMS con enlace directo. Devuelve (modo, datos):
    (None, None) -> flujo normal; ("fuera", d) -> fuera de horario; ("directo", d) -> derivar a la campaña/asesor.
    El mensaje de fuera de horario se guarda aquí (y se muestra en vivo si la conversación es nueva)."""
    d = await asyncio.to_thread(enlaces_directos.tomar_pendiente, conversacion_id)
    if not d:
        return None, None
    if estado_actual == "asignada":
        await asyncio.to_thread(enlaces_directos.registrar_resultado, d["id"], "ya_atendido")
        return None, None
    try:
        hor = await asyncio.to_thread(validar_horario_consultas, {"campana_codigo": d["campana_codigo"]}, {})
        resp = hor.get("response") or {}
        fuera = bool(resp.get("outOfTime"))
        texto_fuera = resp.get("messageOutOfTime") or "En este momento estamos fuera de horario de atención."
    except Exception:
        logger.exception("No se pudo validar el horario de la campaña %s", d["campana_codigo"])
        fuera, texto_fuera = False, ""
    if fuera:
        await asyncio.to_thread(_guardar_mensaje_bot_texto, conversacion_id, texto_fuera)
        if es_nueva:
            await websocket.send_json({"type": "mensaje_bot", "payload": {"texto": texto_fuera}})
        await asyncio.to_thread(enlaces_directos.registrar_resultado, d["id"], "fuera_horario")
        return "fuera", d
    return "directo", d


async def _derivar_enlace_directo(conversacion_id: int, d: dict, es_nueva: bool, websocket: WebSocket):
    """Pone la conversación en la campaña elegida y la asigna: primero al asesor elegido (si está disponible),
    luego a otro de la MISMA campaña, y si nadie está disponible queda en cola con el aviso habitual."""
    if es_nueva:
        texto = TEXTO_ENLACE_DIRECTO_CONECTANDO.replace("{campana}", d["campana_nombre"])
        await asyncio.to_thread(_guardar_mensaje_bot_texto, conversacion_id, texto)
        await websocket.send_json({"type": "mensaje_bot", "payload": {"texto": texto}})
    if not await asyncio.to_thread(_poner_en_cola_directo, conversacion_id, d["campana_id"]):
        return
    await websocket.send_json({"type": "en_cola", "payload": {}})
    resultado = "en_cola"
    if d.get("asesor_id"):
        r = await asignacion.asignar_manual(conversacion_id, d["asesor_id"], manager)
        if r.get("ok"):
            resultado = "asignado_directo"
    if resultado == "en_cola":
        asignado = await asignacion.intentar_asignar_conversacion(conversacion_id, manager)
        if asignado is not None:
            resultado = "asignado_otro" if d.get("asesor_id") else "asignado_directo"
        else:
            texto = (await asyncio.to_thread(supervisor.obtener_configuracion, "mensaje_en_cola_guardado")
                     or MENSAJES_AUTOMATICOS["mensaje_en_cola_guardado"]["default"])
            await websocket.send_json({"type": "aviso_cola", "payload": {"texto": texto}})
    await asyncio.to_thread(enlaces_directos.registrar_resultado, d["id"], resultado)


async def _entregar_archivos_pendientes(conversacion_id: int, websocket: WebSocket):
    """Pone en la conversación (y en el widget) los archivos que el staff le envió por SMS a este
    cliente y que ya abrió desde su enlace. Se llama al conectarse el widget."""
    def _tomar():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT e.id, e.ruta, e.enviado_por, e.rol_emisor
                       FROM envios_archivo e JOIN conversaciones c ON c.usuario_id = e.usuario_id
                       WHERE c.id = %s AND e.canjeado_en IS NOT NULL AND e.entregado_en IS NULL
                       ORDER BY e.id FOR UPDATE OF e SKIP LOCKED""",
                    (conversacion_id,),
                )
                filas = cur.fetchall()
                if filas:
                    cur.execute("UPDATE envios_archivo SET entregado_en = %s, conversacion_id = %s WHERE id = ANY(%s)",
                                (datetime.now(LIMA_TZ), conversacion_id, [f[0] for f in filas]))
            conn.commit()
            return filas
        except Exception:
            conn.rollback()
            logger.warning("No se pudieron consultar los archivos pendientes", exc_info=True)
            return []
        finally:
            release_connection(conn)

    for _, ruta, enviado_por, rol_emisor in await asyncio.to_thread(_tomar):
        texto = "📎 Aquí tienes el archivo que te enviamos:"
        await asyncio.to_thread(_guardar_mensaje_sistema, conversacion_id, texto)
        mensaje_id = await asyncio.to_thread(_guardar_mensaje_staff_archivo, conversacion_id, ruta, rol_emisor, enviado_por)
        await websocket.send_json({"type": "mensaje_bot", "payload": {"texto": texto}})
        await websocket.send_json({"type": "archivo_asesor", "payload": {"ruta": ruta, "asesor_nombre": "Sanitas", "mensaje_id": mensaje_id}})
        try:
            await manager.notificar_escuchas(conversacion_id, {
                "type": "mensaje_conversacion",
                "payload": {"conversacion_id": conversacion_id, "emisor_tipo": rol_emisor,
                            "texto": "📎 Archivo enviado por SMS", "ruta": ruta, "mensaje_id": mensaje_id}})
        except Exception:
            pass


def _enviar_sms_saliente(conversacion_id: int, remitente_id: int) -> dict:
    """Manda el SMS de "tienes un mensaje nuevo" al cliente de una
    conversación, con un enlace que lo lleva directo de vuelta a ESA
    conversación en el widget (retoma su sesión vía sesion_token en la URL,
    sin volver a pedirle celular/nombre) — es el reemplazo de los mensajes
    salientes de WhatsApp: un webchat no tiene push nativo, así que este SMS
    es lo que "toca" al cliente aunque haya cerrado la pestaña o el navegador
    entero. Lo puede disparar tanto un asesor como un supervisor.

    Queda registrado en supervisor_intervenciones (auditoría interna) — NO
    se guarda como un mensaje de la conversación, porque no tiene sentido
    mostrarle al cliente, dentro de su propio chat, un aviso sobre el SMS
    que le acaban de mandar a su propio celular.

    Devuelve {"ok": bool, "motivo": str | None}."""
    widget_url = (supervisor.obtener_configuracion("widget_url") or "").strip()
    if not widget_url:
        return {
            "ok": False,
            "motivo": 'Falta configurar la "URL pública donde vive el widget" en el Editor antes de poder mandar SMS salientes.',
        }

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.telefono, u.sesion_token
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s""",
                (conversacion_id,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)

    if row is None:
        return {"ok": False, "motivo": "Conversación no encontrada."}
    telefono, sesion_token = row
    if not telefono:
        return {"ok": False, "motivo": "Este cliente no tiene un celular registrado."}

    separador = "&" if "?" in widget_url else "?"
    link = f"{widget_url}{separador}wcs={sesion_token}"
    plantilla = (
        supervisor.obtener_configuracion("mensaje_sms_saliente")
        or MENSAJES_AUTOMATICOS["mensaje_sms_saliente"]["default"]
    )
    mensaje = plantilla.replace("{{LINK}}", link)

    if not enviar_sms_danaconnect(telefono, mensaje):
        return {"ok": False, "motivo": "Danaconnect no pudo enviar el SMS. Intenta de nuevo en un momento."}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, 'sms_saliente', %s)""",
                (conversacion_id, remitente_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)

    return {"ok": True, "motivo": None}


def _nombre_display(asesor_id: int) -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre_display FROM asesores WHERE id = %s", (asesor_id,))
            return cur.fetchone()[0]
    finally:
        release_connection(conn)


class LoginRequest(BaseModel):
    usuario: str
    password: str
    activar_2fa: bool = False   # la persona pidió configurar su verificación en dos pasos


@app.post("/login")
def login(datos: LoginRequest):
    espera = auth.segundos_bloqueo_restante(datos.usuario)
    if espera > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Intenta de nuevo en {max(1, espera // 60)} minuto(s).",
        )

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash, nombre_display, rol FROM asesores WHERE usuario = %s AND activo = TRUE",
                (datos.usuario,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)

    # Si el usuario no existe, igual se corre bcrypt contra un hash cualquiera
    # (auth.HASH_DUMMY) — así la respuesta tarda aproximadamente lo mismo que
    # cuando el usuario sí existe pero la contraseña es incorrecta, y no se
    # puede usar el tiempo de respuesta para adivinar qué usuarios existen.
    hash_a_comparar = row[1] if row else auth.HASH_DUMMY
    password_ok = auth.verify_password(datos.password, hash_a_comparar)

    if row is None or not password_ok:
        auth.registrar_intento_fallido(datos.usuario)
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    asesor_id, _, nombre_display, rol = row

    # Verificación en dos pasos. Mientras falte el segundo paso NO se borran los
    # fallos acumulados (si no, quien conoce la contraseña podría probar códigos sin límite).
    estado_2fa = totp.obtener_estado(asesor_id)
    if estado_2fa["disponible"]:
        if estado_2fa["activo"]:
            return {"requiere_2fa": True, "pre_token": auth.crear_token_pre2fa(asesor_id, "verificar")}
        if datos.activar_2fa or _dosfa_obligatorio_para(rol):
            return {"requiere_enrolar": True, "pre_token": auth.crear_token_pre2fa(asesor_id, "enrolar"),
                    "obligatorio": _dosfa_obligatorio_para(rol)}

    return _sesion_completa(datos.usuario, asesor_id, nombre_display, rol)


def _sesion_completa(usuario: str, asesor_id: int, nombre_display: str, rol: str) -> dict:
    auth.registrar_login_exitoso(usuario)
    return {"token": auth.crear_token(asesor_id), "asesor_id": asesor_id,
            "nombre_display": nombre_display, "rol": rol}


def _dosfa_obligatorio_para(rol: str) -> bool:
    """Política en configuracion_global['dosfa_obligatorio']: 'no' | 'supervisor' | 'todos'."""
    try:
        politica = (supervisor.obtener_configuracion("dosfa_obligatorio") or "no").strip().lower()
    except Exception:
        return False
    if politica == "todos":
        return True
    return politica == "supervisor" and rol in ("supervisor", "editor")


# ---------------------------------------------------------------- 2FA (TOTP)
class Dosfa(BaseModel):
    pre_token: str
    codigo: str = ""


def _dosfa_contexto(pre_token: str, fase_esperada: str):
    """Valida el token intermedio y el bloqueo; devuelve (asesor_id, usuario, nombre, rol)."""
    dec = auth.decodificar_pre2fa(pre_token)
    if dec is None or dec[1] != fase_esperada:
        raise HTTPException(status_code=401, detail="La sesión de verificación venció. Ingresa de nuevo.")
    asesor_id = dec[0]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usuario, nombre_display, rol FROM asesores WHERE id = %s AND activo = TRUE",
                        (asesor_id,))
            fila = cur.fetchone()
    finally:
        release_connection(conn)
    if not fila:
        raise HTTPException(status_code=401, detail="La sesión de verificación venció. Ingresa de nuevo.")
    espera = auth.segundos_bloqueo_restante(fila[0])
    if espera > 0:
        raise HTTPException(status_code=429, detail=f"Demasiados intentos fallidos. Intenta de nuevo en {max(1, espera // 60)} minuto(s).")
    return asesor_id, fila[0], fila[1], fila[2]


@app.post("/auth/2fa/verificar")
def dosfa_verificar(datos: Dosfa):
    asesor_id, usuario, nombre, rol = _dosfa_contexto(datos.pre_token, "verificar")
    estado = totp.obtener_estado(asesor_id)
    codigo = (datos.codigo or "").strip()
    ok = False
    if estado["activo"]:
        paso = totp.verificar_codigo(estado["secreto"], codigo, estado["ultimo_paso"])
        if paso is not None:
            ok = totp.marcar_paso(asesor_id, paso)
        elif len(codigo.replace("-", "").replace(" ", "")) == 10:
            ok = totp.consumir_respaldo(asesor_id, codigo)
    if not ok:
        auth.registrar_intento_fallido(usuario)
        raise HTTPException(status_code=401, detail="Código incorrecto o ya usado")
    return _sesion_completa(usuario, asesor_id, nombre, rol)


@app.post("/auth/2fa/iniciar")
def dosfa_iniciar(datos: Dosfa):
    """Genera el secreto y el QR para vincular la app autenticadora (aún sin activar)."""
    asesor_id, usuario, _, _ = _dosfa_contexto(datos.pre_token, "enrolar")
    secreto = totp.generar_secreto()
    totp.guardar_pendiente(asesor_id, secreto)
    uri = totp.uri_otpauth(secreto, usuario)
    return {"secreto": secreto, "uri": uri, "qr": totp.qr_data_uri(uri)}


@app.post("/auth/2fa/activar")
def dosfa_activar(datos: Dosfa):
    asesor_id, usuario, nombre, rol = _dosfa_contexto(datos.pre_token, "enrolar")
    estado = totp.obtener_estado(asesor_id)
    if not estado["secreto"] or estado["activo"]:
        raise HTTPException(status_code=409, detail="Primero genera el código QR")
    paso = totp.verificar_codigo(estado["secreto"], datos.codigo)
    if paso is None:
        auth.registrar_intento_fallido(usuario)
        raise HTTPException(status_code=401, detail="Código incorrecto. Revisa la hora de tu celular.")
    codigos = totp.activar(asesor_id, paso)
    sesion = _sesion_completa(usuario, asesor_id, nombre, rol)
    sesion["codigos_respaldo"] = codigos
    return sesion


@app.get("/staff/token-archivo")
def token_archivo_staff(authorization: str | None = Header(None)):
    """
    Para las consolas de asesor/supervisor: entrega un token de solo 5 minutos
    para armar los links de /archivo-receta y /conversacion-pdf (que van en
    una URL, a diferencia del token de sesión de 12h que va en el header
    Authorization). Cualquier cuenta de staff activa puede pedirlo — es el
    mismo criterio que ya usan esos dos endpoints al recibir el token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token")
    asesor_id = auth.decodificar_token(authorization[len("Bearer "):])
    if asesor_id is None or _obtener_rol(asesor_id) is None:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return {"token": auth.crear_token_archivo(asesor_id)}


def _obtener_rol(asesor_id: int) -> str | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rol FROM asesores WHERE id = %s AND activo = TRUE", (asesor_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_connection(conn)


def _verificar_editor(authorization: str | None) -> int:
    """Valida 'Authorization: Bearer <token>' y exige rol 'editor' o 'supervisor'
    (los supervisores también pueden editar contenido). Consulta el rol actual
    en la base en cada llamada — así un cambio de rol aplica al instante."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token")
    token = authorization[len("Bearer "):]
    asesor_id = auth.decodificar_token(token)
    if asesor_id is None:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    rol = _obtener_rol(asesor_id)
    if rol not in ("editor", "supervisor"):
        raise HTTPException(status_code=403, detail="No tienes permiso para editar contenido")
    return asesor_id


def _verificar_supervisor(authorization: str | None) -> int:
    """Como _verificar_editor, pero SOLO rol 'supervisor' (el reporte incluye datos de clientes)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token")
    asesor_id = auth.decodificar_token(authorization[len("Bearer "):])
    if asesor_id is None:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    if _obtener_rol(asesor_id) != "supervisor":
        raise HTTPException(status_code=403, detail="Solo un supervisor puede descargar este reporte")
    return asesor_id


@app.get("/supervisor/reporte-opciones")
def opciones_reporte_conversaciones(authorization: str | None = Header(None)):
    """Asesores y campañas para los filtros del reporte de conversaciones."""
    alcance = supervisor.obtener_alcance(_verificar_supervisor(authorization))
    asesores = [{"id": a["asesor_id"], "nombre": a["nombre_display"] or a["nombre"], "activo": a["activo"],
                 "campana_ids": a["campana_ids"]}
                for a in supervisor.listar_asesores_admin() if a["rol"] != "editor"]
    campanas = supervisor.obtener_campanas()
    if alcance is not None:
        asesores = [a for a in asesores if set(a["campana_ids"]) & set(alcance["ids"])]
        campanas = [c for c in campanas if c["id"] in alcance["ids"]]
    for a in asesores:
        a.pop("campana_ids", None)
    return {"asesores": asesores, "campanas": campanas}


@app.get("/supervisor/reporte-conversaciones")
def reporte_conversaciones(rango: str = "hoy", desde: str | None = None, hasta: str | None = None,
                           asesor_id: int | None = None, campana_id: int | None = None,
                           incluir_bot: bool = False, transcripcion: bool = True,
                           authorization: str | None = Header(None)):
    """Excel de conversaciones por asesor para auditoría de calidad (ver reportes.py)."""
    supervisor_id = _verificar_supervisor(authorization)
    alcance = supervisor.obtener_alcance(supervisor_id)
    campanas_reporte = supervisor.campanas_efectivas(alcance, campana_id)
    if alcance is not None and asesor_id and not supervisor.asesor_en_alcance(asesor_id, alcance):
        raise HTTPException(status_code=403, detail="Ese asesor no atiende ninguna de tus campañas")
    try:
        d1, d2, etiqueta = reportes.resolver_rango(rango, desde, hasta)
        contenido = reportes.generar_reporte(
            d1, d2, etiqueta, _nombre_display(supervisor_id), asesor_id=asesor_id, campana_id=campana_id,
            incluir_solo_bot=incluir_bot, incluir_transcripcion=transcripcion,
            campana_ids=campanas_reporte if alcance is not None else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    nombre = f"reporte_conversaciones_{d1.isoformat()}_a_{d2.isoformat()}.xlsx"
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"', "Cache-Control": "no-store"},
    )


class NodoUpdate(BaseModel):
    texto: str | None = None
    opciones: list | None = None
    imagen_url: str | None = None
    campos: list | None = None


@app.get("/editor/nodos")
def listar_nodos_editor(authorization: str | None = Header(None)):
    """Todos los nodos de bot_menus que tienen texto y/o opciones editables,
    para la consola /editor."""
    _verificar_editor(authorization)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT bm.codigo, bm.tipo, bm.texto, bm.opciones, camp.nombre, bm.imagen_url, bm.campos
                   FROM bot_menus bm
                   LEFT JOIN campanas camp ON camp.id = bm.campana_id
                   WHERE bm.texto IS NOT NULL OR bm.campos IS NOT NULL
                   ORDER BY camp.nombre NULLS FIRST, bm.codigo"""
            )
            columnas = ["codigo", "tipo", "texto", "opciones", "campana", "imagen_url", "campos"]
            return [dict(zip(columnas, row)) for row in cur.fetchall()]
    finally:
        release_connection(conn)


@app.put("/editor/nodos/{codigo}")
def actualizar_nodo_editor(codigo: str, cambios: NodoUpdate, authorization: str | None = Header(None)):
    _verificar_editor(authorization)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_menus WHERE codigo = %s", (codigo,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Nodo no encontrado")
            if cambios.texto is not None:
                cur.execute("UPDATE bot_menus SET texto = %s WHERE codigo = %s", (cambios.texto, codigo))
            if cambios.opciones is not None:
                cur.execute(
                    "UPDATE bot_menus SET opciones = %s WHERE codigo = %s",
                    (json.dumps(cambios.opciones), codigo),
                )
            if cambios.imagen_url is not None:
                # "" (vacío) es la forma de quitar la imagen, no de dejarla igual
                nuevo_valor = cambios.imagen_url or None
                cur.execute("UPDATE bot_menus SET imagen_url = %s WHERE codigo = %s", (nuevo_valor, codigo))
            if cambios.campos is not None:
                cur.execute(
                    "UPDATE bot_menus SET campos = %s WHERE codigo = %s",
                    (json.dumps(cambios.campos), codigo),
                )
        conn.commit()
        return {"ok": True}
    finally:
        release_connection(conn)


@app.get("/editor/campanas")
def listar_campanas_editor(authorization: str | None = Header(None)):
    """Campañas activas, para el selector de 'crear menú nuevo' del editor."""
    _verificar_editor(authorization)
    return supervisor.obtener_campanas()


class NodoNuevo(BaseModel):
    codigo: str
    texto: str
    campana_codigo: str | None = None


@app.post("/editor/nodos")
def crear_nodo_editor(nodo: NodoNuevo, authorization: str | None = Header(None)):
    """Crea un nodo tipo 'menu' desde cero, con una lista de botones vacía —
    para armar un menú nuevo desde /editor en vez de depender de un SQL a
    mano. Los botones se agregan después con PUT /editor/nodos/{codigo},
    igual que a cualquier otro menú (ver editor.html, sección 'opciones')."""
    _verificar_editor(authorization)
    codigo = (nodo.codigo or "").strip()
    texto = (nodo.texto or "").strip()
    if not re.match(r"^[a-z0-9_]+$", codigo):
        raise HTTPException(
            status_code=400,
            detail="El código solo puede tener minúsculas, números y guion bajo (ej: mi_menu_nuevo)",
        )
    if not texto:
        raise HTTPException(status_code=400, detail="Falta el texto del menú")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_menus WHERE codigo = %s", (codigo,))
            if cur.fetchone() is not None:
                raise HTTPException(status_code=400, detail="Ya existe un nodo con ese código")

            campana_id = None
            if nodo.campana_codigo:
                cur.execute("SELECT id FROM campanas WHERE codigo = %s", (nodo.campana_codigo,))
                fila = cur.fetchone()
                if fila is None:
                    raise HTTPException(status_code=400, detail="La campaña indicada no existe")
                campana_id = fila[0]

            cur.execute(
                """INSERT INTO bot_menus (codigo, campana_id, tipo, texto, opciones)
                   VALUES (%s, %s, 'menu', %s, %s)""",
                (codigo, campana_id, texto, json.dumps([])),
            )
        conn.commit()
        return {"ok": True}
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Notas internas de cliente, vinculadas a su DNI/RUC (tabla notas_cliente,
# ver migracion_notas_cliente.sql) -- 100% interno: el paciente nunca las ve.
# Pensadas como orientación para asesor/supervisor cuando el mismo cliente
# vuelve a escribir en el futuro, sin importar en qué conversación puntual
# se haya detectado el documento la primera vez.
# ---------------------------------------------------------------------------

# Cuando el DNI del paciente ya quedó guardado en usuarios_webchat.dni (lo
# hace el propio flujo de bot_menus vía 'guardar_en_usuario' -- ver
# bot_engine.py) se usa ese. Si no, se busca un RUC/DOI entre las variables
# de sesión de la conversación puntual -- flujos como SCTR piden el RUC de
# la EMPRESA en vez del DNI del paciente, y ese no se persiste en
# usuarios_webchat (esa columna es para el DNI del paciente).
VARIABLES_DOCUMENTO_FALLBACK = ("ruc_empresa", "RUC", "ruc", "DOI", "doi")


def _documento_cliente(conversacion_id: int) -> str | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.dni, c.variables_sesion
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s""",
                (conversacion_id,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)

    if not row:
        return None
    dni, variables_sesion = row
    if dni:
        return dni
    for clave in VARIABLES_DOCUMENTO_FALLBACK:
        valor = (variables_sesion or {}).get(clave)
        if valor:
            return str(valor)
    return None


def _verificar_staff(authorization: str | None) -> int:
    """Autentica cualquier rol de staff (asesor, supervisor, editor) -- la
    nota de cliente la puede ver/editar cualquiera que atienda conversaciones,
    igual de permisivo que el token de archivo (ver /staff/token-archivo)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token")
    asesor_id = auth.decodificar_token(authorization[len("Bearer "):])
    if asesor_id is None or _obtener_rol(asesor_id) is None:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return asesor_id


# ---------------------------------------------------------------------------
# Recordatorios SMS para recetas extendidas (ver recordatorios.py)
# ---------------------------------------------------------------------------

class RecordatoriosCrear(BaseModel):
    celular: str
    dias_receta: int
    fechas: list[str]            # 'YYYY-MM-DD', una por cada entrega posterior a la primera
    mensaje: str | None = None   # admite {{NOMBRE}}, {{ENTREGA}}, {{TOTAL}}


def _recordatorios_503():
    return HTTPException(status_code=503, detail="Falta ejecutar migracion_recordatorios_receta.sql en la base de datos.")


@app.get("/recordatorios-receta/{conversacion_id}")
def listar_recordatorios_receta(conversacion_id: int, authorization: str | None = Header(None)):
    _verificar_staff(authorization)
    ctx = recordatorios.contexto_conversacion(conversacion_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    try:
        items = recordatorios.listar(ctx["usuario_id"])
    except recordatorios.TablaNoExiste:
        raise _recordatorios_503()
    return {"nombre": ctx["nombre"], "celular": _normalizar_celular_pe(ctx["telefono"]),
            "documento": ctx["dni"], "hoy": recordatorios.hoy_lima().isoformat(),
            "mensaje_defecto": recordatorios.MENSAJE_DEFECTO, "max_recordatorios": recordatorios.MAX_RECORDATORIOS,
            "items": items}


@app.post("/recordatorios-receta/{conversacion_id}")
def crear_recordatorios_receta(conversacion_id: int, body: RecordatoriosCrear,
                                authorization: str | None = Header(None)):
    asesor_id = _verificar_staff(authorization)
    ctx = recordatorios.contexto_conversacion(conversacion_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    celular = _normalizar_celular_pe(body.celular)
    if not celular:
        raise HTTPException(status_code=400, detail="El celular no es válido (9 dígitos, empieza con 9).")
    if not 1 <= len(body.fechas) <= recordatorios.MAX_RECORDATORIOS:
        raise HTTPException(status_code=400, detail=f"Programa entre 1 y {recordatorios.MAX_RECORDATORIOS} recordatorios.")
    hoy = recordatorios.hoy_lima()
    fechas = []
    for f in body.fechas:
        try:
            d = datetime.strptime(f.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Fecha inválida: {f}")
        if d <= hoy:
            raise HTTPException(status_code=400, detail="Las fechas deben ser posteriores a hoy.")
        if (d - hoy).days > recordatorios.MAX_DIAS_ADELANTE:
            raise HTTPException(status_code=400, detail="Una fecha está demasiado lejos (máximo 200 días).")
        fechas.append(d)
    if len(set(fechas)) != len(fechas):
        raise HTTPException(status_code=400, detail="Hay fechas repetidas.")
    fechas.sort()
    plantilla = (body.mensaje or "").strip() or recordatorios.MENSAJE_DEFECTO
    if len(recordatorios.armar_mensaje(plantilla, ctx["nombre"], 9, 9)) > recordatorios.MAX_CARACTERES_SMS:
        raise HTTPException(status_code=400, detail=f"El mensaje es muy largo (máximo {recordatorios.MAX_CARACTERES_SMS} caracteres).")
    if not 1 <= body.dias_receta <= 400:
        raise HTTPException(status_code=400, detail="Días de receta inválidos.")
    try:
        ids = recordatorios.crear(conversacion_id, ctx["usuario_id"], ctx["dni"], ctx["nombre"], celular,
                                  body.dias_receta, fechas, plantilla, asesor_id)
    except recordatorios.TablaNoExiste:
        raise _recordatorios_503()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "creados": len(ids)}


@app.delete("/recordatorios-receta/{recordatorio_id}")
def cancelar_recordatorio_receta(recordatorio_id: int, authorization: str | None = Header(None)):
    asesor_id = _verificar_staff(authorization)
    try:
        ok = recordatorios.cancelar(recordatorio_id, asesor_id)
    except recordatorios.TablaNoExiste:
        raise _recordatorios_503()
    if not ok:
        raise HTTPException(status_code=404, detail="El recordatorio no existe o ya no se puede cancelar.")
    return {"ok": True}


def _push_entrega(usuario_id, info: dict | None = None):
    """Además del SMS, aviso push (gratis) a quien tenga el app con avisos activos.
    `info`: nombre del paciente y número de entrega, para personalizar el texto."""
    info = info or {}
    try:
        push.avisar_usuario(usuario_id, "entrega", respetar_pausa=False,
                            ctx={"nombre": info.get("nombre"), "n": info.get("entrega_n"), "total": info.get("entregas_total")})
    except Exception:  # noqa: BLE001
        pass


@app.post("/internal/recordatorios/procesar")
async def procesar_recordatorios_receta(authorization: str = Header(None)):
    """Lo llama Cloud Scheduler (cada hora, de 9 a 17 h Lima) con 'Authorization: ApiKey <SOFI_APIS_KEY>'."""
    esperado = os.environ.get("SOFI_APIS_KEY") or ""
    recibido = (authorization or "").strip()
    if recibido.lower().startswith("apikey "):
        recibido = recibido[7:].strip()
    if not esperado or not secrets.compare_digest(recibido.encode(), esperado.encode()):
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        return await asyncio.to_thread(recordatorios.procesar_pendientes, enviar_sms_danaconnect, _push_entrega)
    except recordatorios.TablaNoExiste:
        raise _recordatorios_503()


class NotaClienteUpdate(BaseModel):
    nota: str
    documento: str | None = None  # para forzar el documento a mano (ej. RUC que no se detectó solo)


@app.get("/notas-cliente/{conversacion_id}")
def obtener_nota_cliente(conversacion_id: int, authorization: str | None = Header(None)):
    _verificar_staff(authorization)
    documento = _documento_cliente(conversacion_id)
    if not documento:
        return {"documento": None, "nota": None}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT n.nota, n.actualizado_en, a.nombre_display
                   FROM notas_cliente n LEFT JOIN asesores a ON a.id = n.actualizado_por
                   WHERE n.documento = %s""",
                (documento,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)

    if not row:
        return {"documento": documento, "nota": None}
    nota, actualizado_en, nombre_asesor = row
    return {
        "documento": documento,
        "nota": nota,
        "actualizado_en": actualizado_en.isoformat() if actualizado_en else None,
        "actualizado_por": nombre_asesor,
    }


@app.put("/notas-cliente/{conversacion_id}")
def guardar_nota_cliente(conversacion_id: int, body: NotaClienteUpdate,
                          authorization: str | None = Header(None)):
    """Una sola nota vigente por documento -- cada edición la reemplaza por
    completo (no es un historial de notas). Guardar una nota vacía la borra."""
    asesor_id = _verificar_staff(authorization)
    documento = (body.documento or "").strip() or _documento_cliente(conversacion_id)
    if not documento:
        raise HTTPException(
            status_code=400,
            detail="No se pudo identificar el DNI/RUC de este cliente todavía "
                   "-- indícalo manualmente para poder guardar la nota.",
        )

    nota = (body.nota or "").strip()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if nota:
                cur.execute(
                    """INSERT INTO notas_cliente (documento, nota, actualizado_por, actualizado_en)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (documento) DO UPDATE
                       SET nota = EXCLUDED.nota, actualizado_por = EXCLUDED.actualizado_por,
                           actualizado_en = EXCLUDED.actualizado_en""",
                    (documento, nota, asesor_id, datetime.now(LIMA_TZ)),
                )
            else:
                cur.execute("DELETE FROM notas_cliente WHERE documento = %s", (documento,))
        conn.commit()
    finally:
        release_connection(conn)

    return {"documento": documento, "nota": nota or None}


# ---------------------------------------------------------------------------
# Mensajes automáticos parametrizables (configuracion_global) — editables
# desde la consola /editor, no solo desde el panel de supervisor.
# ---------------------------------------------------------------------------

MENSAJES_AUTOMATICOS = {
    "banner_login": {
        "etiqueta": "Aviso en la barra superior de la pantalla de login de las consolas (asesor, supervisor, editor y dashboard). Déjalo vacío para no mostrar ningún aviso. Quien lo ve puede cerrarlo con la ✕",
        "default": "",
    },
    "mensaje_feriado": {
        "etiqueta": "Mensaje por defecto cuando una campaña no atiende por feriado (se usa si el feriado no trae mensaje propio). Puedes usar {{FECHA}} (ej. jueves 8 de octubre) y {{FERIADO}} (nombre del feriado)",
        "default": "Hoy {{FECHA}} es feriado ({{FERIADO}}) y en este momento no estamos atendiendo por este canal. Retomaremos la atención en nuestro próximo día hábil. ¡Gracias por tu comprensión!",
    },
    "mensaje_en_cola_guardado": {
        "etiqueta": "Aviso al cliente cuando queda en cola esperando un asesor (le explica que su conversación queda guardada si cierra la ventana)",
        "default": "💾 Tu conversación queda guardada. Si prefieres, puedes cerrar esta ventana y volver más tarde: al abrir el chat en este mismo navegador retomarás donde quedaste.",
    },
    "mensaje_subiendo_archivo": {
        "etiqueta": "Mensaje mientras se sube un archivo",
        "default": "Espere un momento por favor !!!",
    },
    "mensaje_inactividad_5min": {
        "etiqueta": "Mensaje automático tras 5 minutos sin actividad en una conversación",
        "default": "Gracias por la espera, disculpe la demora, estamos trabajando, espere unos momentos",
    },
    "mensaje_espera_asesor": {
        "etiqueta": "Aviso al cliente cuando su conversación ya está con un asesor pero este todavía no responde (se manda a los 30 segundos y luego cada 5 minutos hasta que el asesor escriba). Usa {{nombre_del_asesor}} para el nombre del asesor",
        "default": "¡Gracias por tu paciencia! 😊 En este momento estamos atendiendo varias consultas, pero {{nombre_del_asesor}} ya tiene tu conversación y te responderá en unos instantes.",
    },
    "mensaje_bienvenida_asesor": {
        "etiqueta": "Mensaje de bienvenida al asignar un asesor",
        "default": "",
    },
    "mensaje_cierre_conversacion": {
        "etiqueta": "Mensaje al cerrar una conversación",
        "default": "",
    },
    "widget_url": {
        "etiqueta": "URL pública donde vive el widget (la página del sitio de Sanitas que tiene el script del webchat) — se usa para armar el enlace del SMS saliente",
        "default": "",
    },
    "mensaje_sms_saliente": {
        "etiqueta": 'SMS que se manda al cliente para avisarle que le escriban de vuelta en el webchat. Usa {{LINK}} para el enlace directo a su conversación (obligatorio en el texto)',
        "default": "Sanitas: tienes un mensaje nuevo de tu asesor. Ingresa aquí para verlo: {{LINK}}",
    },
    "mensaje_sms_invitacion": {
        "etiqueta": 'SMS que se manda a un número NUEVO (que todavía no ha iniciado conversación) para invitarlo a escribirnos por el webchat. Usa {{LINK}} para el enlace al chat, que es siempre https://sanitasperu.com/?ac=1 (obligatorio en el texto)',
        "default": "Sanitas: te invitamos a escribirnos por nuestro chat. Ingresa aquí y te atendemos: {{LINK}}",
    },
    "sonido_notificacion_widget": {
        "etiqueta": "URL del sonido que se reproduce en el widget del paciente cuando llega un mensaje nuevo y la pestaña no está en primer plano. Para usar un archivo propio: súbelo a static/sonidos/ (en el código) y pon aquí \"/static/sonidos/<nombre-del-archivo>\" (ej. /static/sonidos/notificacion.mp3). También puede ser una URL externa (https://...). Vacío = usar el sonido genérico (dos tonos) en vez de una voz/audio.",
        "default": "/static/sonidos/notificacion.mp3",
    },
    "mensaje_opcion_invalida": {
        "etiqueta": "Mensaje cuando el paciente elige una opción de un menú que no existe (por ejemplo, si escribe en vez de tocar un botón)",
        "default": "No entendí esa opción, por favor elige una de la lista.",
    },
    "titulo_widget": {
        "etiqueta": "Título que aparece en el encabezado del widget (arriba de los mensajes)",
        "default": "Sofi, tu asistente virtual",
    },
}


@app.get("/editor/configuracion")
def listar_configuracion_editor(authorization: str | None = Header(None)):
    """Mensajes automáticos parametrizables — para la sección de "Mensajes
    automáticos" de la consola /editor."""
    _verificar_editor(authorization)
    return [
        {
            "clave": clave,
            "etiqueta": meta["etiqueta"],
            "valor": supervisor.obtener_configuracion(clave) or meta["default"],
        }
        for clave, meta in MENSAJES_AUTOMATICOS.items()
    ]


class ConfiguracionUpdate(BaseModel):
    valor: str


@app.put("/editor/configuracion/{clave}")
def actualizar_configuracion_editor(clave: str, cambios: ConfiguracionUpdate, authorization: str | None = Header(None)):
    _verificar_editor(authorization)
    if clave not in MENSAJES_AUTOMATICOS:
        raise HTTPException(status_code=404, detail="Clave de configuración no reconocida")
    supervisor.guardar_configuracion(clave, cambios.valor)
    return {"ok": True}


# --- Detalle del dashboard: qué hay detrás de "Sin cerrar" y "Sin opción" ---
# nodo del bot donde quedó la conversación -> etiqueta legible
_PASOS_BOT = {
    "webchat_bienvenida": "Vio el saludo y no eligió una opción",
    "webchat_gate": "Eligió una opción y no avanzó",
    "webchat_pide_celular": "No ingresó el celular",
    "webchat_pide_nombre": "Se quedó en la validación (token)",
    "webchat_valida_pide_codigo": "Se quedó en la validación (token)",
    "webchat_menu_principal": "Llegó al menú principal y no eligió",
}
_FLUJOS_BOT = (
    ("consultas_", "Consultas generales"),
    ("sctr_", "Venta de SCTR"),
    ("pases_", "Pase de atención"),
    ("mad_", "Médico a domicilio / Ambulancia"),
    ("med_", "Medicamentos"),
    ("medicamentos_", "Medicamentos"),
)
# Por qué cerró el bot (según su último mensaje) -> (patrón SQL ILIKE, etiqueta)
_MOTIVOS_CIERRE_BOT = (
    ("%es feriado%", "Feriado (sin atención)"),
    ("%fuera de nue%", "Fuera de horario"),
    ("%no estamos disponibles%", "Fuera de horario"),
    ("%no eres cliente%", "No es cliente de Sanitas"),
    ("%no acepto%", "No aceptó la política de datos"),
    ("%completado y firmado los documentos%", "Trámite SCTR completado"),
    ("%Lamentamos no pod%", "No se pudo atender (medicamentos)"),
    ("Continuó en su conversación anterior%", "Duplicada: continuó en su conversación anterior"),
)
# "Escribe "hola" para comenzar" (nodo consultas_despedida_hola) se separa en
# _sql_motivo_cierre_bot según lo último que hizo el cliente.


def _sql_motivo_cierre_bot() -> str:
    casos = " ".join(
        "WHEN m.contenido ILIKE '%s' THEN '%s'" % (
            # %% porque la consulta se ejecuta con parámetros (psycopg2)
            patron.replace("'", "''").replace("%", "%%"), etiqueta.replace("'", "''"))
        for patron, etiqueta in _MOTIVOS_CIERRE_BOT
    )
    despedida = (
        "WHEN m.contenido ILIKE '%%Escribe \"hola\" para comenzar%%' THEN "
        "CASE WHEN LENGTH(COALESCE(u.contenido, '')) <= 4 "
        "THEN 'Consulta general completada' "
        "ELSE 'Cerró tras texto escrito por el cliente' END"
    )
    return f"CASE {despedida} {casos} ELSE 'Otro cierre del bot' END"


def _categoria_conversacion(estado, cerrado_por, nodo, motivo, sin_escribir=False, eligio_opcion=False):
    """Devuelve (grupo, categoría) para una conversación sin etiqueta/campaña."""
    if estado == "bot" and nodo == "webchat_pide_celular" and eligio_opcion:
        # Flujo nuevo: ya vio el saludo y eligió una opción; el celular se le pidió después.
        return "Abandonó", "Eligió una opción pero no ingresó el celular"
    if estado == "bot" and nodo == "webchat_pide_celular":
        return "Abandonó", ("Abrió el chat y no escribió nada" if sin_escribir
                            else "Escribió algo y no completó el celular")
    if estado == "asignada":
        return "En curso", "En curso con un asesor"
    if estado == "en_cola":
        return "En curso", "En cola esperando asesor"
    if estado == "bot":
        if nodo in _PASOS_BOT:
            return "Abandonó", _PASOS_BOT[nodo]
        for prefijo, flujo in _FLUJOS_BOT:
            if (nodo or "").startswith(prefijo):
                return "Abandonó", f"Abandonó dentro de «{flujo}»"
        return "Abandonó", "Abandonó dentro de otro flujo"
    if cerrado_por == "sistema":
        return "El bot cerró", motivo or "Otro cierre del bot"
    return "Cerrada", f"Cerrada por {cerrado_por} sin etiqueta"


def _detalle_conversaciones(cur, desde, hasta, condicion: str):
    """Agrupa las conversaciones que cumplen `condicion` (SQL fijo, no viene
    del usuario) por categoría legible, y por paso exacto del bot."""
    cur.execute(
        f"""SELECT c.estado, COALESCE(c.cerrado_por, '-'), COALESCE(c.nodo_actual, ''),
                   {_sql_motivo_cierre_bot()} AS motivo,
                   (c.estado = 'bot' AND c.nodo_actual = 'webchat_pide_celular'
                    AND NOT EXISTS (SELECT 1 FROM mensajes mu
                                    WHERE mu.conversacion_id = c.id AND mu.emisor_tipo = 'usuario')) AS sin_escribir,
                   (c.variables_sesion->>'DESTINO' IS NOT NULL) AS eligio_opcion,
                   COUNT(*)
            FROM conversaciones c
            LEFT JOIN LATERAL (
                SELECT contenido FROM mensajes
                WHERE conversacion_id = c.id AND emisor_tipo = 'bot'
                ORDER BY id DESC LIMIT 1
            ) m ON c.estado = 'cerrada' AND c.cerrado_por = 'sistema'
            LEFT JOIN LATERAL (
                SELECT contenido FROM mensajes
                WHERE conversacion_id = c.id AND emisor_tipo = 'usuario'
                ORDER BY id DESC LIMIT 1
            ) u ON c.estado = 'cerrada' AND c.cerrado_por = 'sistema'
            WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND {condicion}
            GROUP BY 1, 2, 3, 4, 5, 6""",
        (desde, hasta),
    )
    categorias: dict = {}
    pasos: dict = {}
    for estado, cerrado_por, nodo, motivo, sin_escribir, eligio_opcion, cantidad in cur.fetchall():
        grupo, categoria = _categoria_conversacion(estado, cerrado_por, nodo, motivo, sin_escribir, eligio_opcion)
        clave = (grupo, categoria)
        categorias[clave] = categorias.get(clave, 0) + cantidad
        if estado == "bot" and nodo:
            pasos[nodo] = pasos.get(nodo, 0) + cantidad
    detalle = [
        {"grupo": g, "categoria": c, "cantidad": n}
        for (g, c), n in sorted(categorias.items(), key=lambda kv: -kv[1])
    ]
    por_nodo = [{"nodo": n, "cantidad": q} for n, q in sorted(pasos.items(), key=lambda kv: -kv[1])]
    return detalle, por_nodo


_ETIQUETAS_ORIGEN = {
    "burbuja": "Clic en la burbuja del sitio",
    "invitacion_sms": "SMS de invitación (?ac=1)",
    "enlace_sms": "SMS para volver a su conversación",
    "enlace_directo": "SMS con enlace directo a campaña/asesor",
    "nueva_conversacion": "Volvió a empezar tras cerrar",
    "otro": "Otro",
}


def _estadisticas_origen(conn, cur, desde, hasta):
    """Por origen y por página: cuántas conversaciones, cuántas no escribieron
    nada, cuántas llegaron a un asesor. Si la columna aún no existe (falta
    correr migracion_origen_conversaciones.sql) devuelve listas vacías sin
    romper el resto del dashboard."""
    base = """COUNT(*) AS total,
              COUNT(*) FILTER (WHERE NOT EXISTS (
                  SELECT 1 FROM mensajes mu WHERE mu.conversacion_id = c.id AND mu.emisor_tipo = 'usuario')) AS sin_escribir,
              COUNT(*) FILTER (WHERE c.campana_id IS NOT NULL) AS con_asesor"""
    try:
        cur.execute(
            f"""SELECT c.origen, {base} FROM conversaciones c
                WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                GROUP BY c.origen ORDER BY total DESC""",
            (desde, hasta),
        )
        por_origen = [
            {"origen": _ETIQUETAS_ORIGEN.get(o, "Sin dato (antes del registro de origen)" if o is None else o),
             "total": t, "sin_escribir": se, "con_asesor": ca}
            for o, t, se, ca in cur.fetchall()
        ]
        cur.execute(
            f"""SELECT c.pagina_origen, {base} FROM conversaciones c
                WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND c.pagina_origen IS NOT NULL
                GROUP BY c.pagina_origen ORDER BY total DESC LIMIT 15""",
            (desde, hasta),
        )
        por_pagina = [
            {"pagina": pg, "total": t, "sin_escribir": se, "con_asesor": ca}
            for pg, t, se, ca in cur.fetchall()
        ]
        return por_origen, por_pagina
    except Exception as e:
        logger.warning("Estadísticas de origen no disponibles: %s", e)
        conn.rollback()
        return [], []


@app.get("/dashboard/app")
def estadisticas_app(authorization: str | None = Header(None)):
    """Cuántos clientes instalaron el app y cuántos aceptaron los avisos."""
    _verificar_editor(authorization)
    return push.estadisticas()


class _CursorFiltroCampana:
    """Envuelve un cursor y agrega `AND c.campana_id IN (...)` a toda consulta del
    dashboard que filtre por fecha de inicio (`c.iniciado_en < %s`). Los ids son
    ints ya validados, nunca texto del usuario. Así el filtro por campaña (y el
    alcance de un supervisor de campaña) se aplica igual a todos los gráficos sin
    tocar cada consulta. Lista vacía = ninguna campaña = cero filas."""

    _FECHA = "c.iniciado_en < %s"

    def __init__(self, cur, campana_ids):
        self._cur = cur
        self._extra = " AND " + _sql_campanas(campana_ids)

    def execute(self, sql, params=None):
        if self._FECHA in sql:
            sql = sql.replace(self._FECHA, self._FECHA + self._extra)
        return self._cur.execute(sql, params)

    def __getattr__(self, nombre):
        return getattr(self._cur, nombre)


def _sql_campanas(campana_ids) -> str:
    ids = [int(i) for i in campana_ids]
    return "c.campana_id IN (" + ",".join(str(i) for i in ids) + ")" if ids else "FALSE"


@app.get("/dashboard/campanas")
def campanas_dashboard(authorization: str | None = Header(None)):
    """Campañas para el filtro del dashboard (opciones del menú principal)."""
    alcance = supervisor.obtener_alcance(_verificar_editor(authorization))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre FROM campanas ORDER BY nombre")
            filas = [{"id": i, "nombre": n} for i, n in cur.fetchall()
                     if alcance is None or i in alcance["ids"]]
        conn.rollback()
    finally:
        release_connection(conn)
    return filas


@app.get("/dashboard/estadisticas")
def estadisticas_dashboard(desde: str, hasta: str, campana_id: int | None = None, authorization: str | None = Header(None)):
    """Estadísticas de conversaciones para el dashboard: por día, por asesor,
    por etiqueta de cierre (clasificación: atendido/error/fuera de horario/
    pruebas, según lo que haya configurado en "Etiquetas de cierre") y por
    campaña (la opción del menú principal por la que entró cada usuario:
    "Estado de pedido", "Coord. de receta", etc.).

    `desde`/`hasta` son fechas YYYY-MM-DD (inclusive), interpretadas en hora
    de Lima. Filtran sobre c.iniciado_en — el día en que arrancó cada
    conversación — así que una conversación que sigue abierta también cuenta
    en el día que empezó, aunque todavía no tenga etiqueta de cierre (esas
    caen en el grupo "Sin cerrar")."""
    alcance = supervisor.obtener_alcance(_verificar_editor(authorization))
    campanas_filtro = supervisor.campanas_efectivas(alcance, campana_id)
    try:
        fecha_desde = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=LIMA_TZ)
        fecha_hasta = datetime.strptime(hasta, "%Y-%m-%d").replace(tzinfo=LIMA_TZ) + timedelta(days=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Fechas inválidas (usar YYYY-MM-DD)")
    if fecha_hasta <= fecha_desde:
        raise HTTPException(status_code=400, detail="'hasta' debe ser igual o posterior a 'desde'")

    conn = get_connection()
    try:
        with conn.cursor() as _cur_real:
            cur = _CursorFiltroCampana(_cur_real, campanas_filtro) if campanas_filtro is not None else _cur_real
            cur.execute(
                """SELECT (c.iniciado_en AT TIME ZONE 'America/Lima')::date AS dia, COUNT(*)
                   FROM conversaciones c
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                   GROUP BY dia ORDER BY dia""",
                (fecha_desde, fecha_hasta),
            )
            por_dia = [{"fecha": dia.isoformat(), "cantidad": cantidad} for dia, cantidad in cur.fetchall()]

            # Celulares únicos (personas distintas, por número de celular; las conversaciones sin
            # celular no cuentan). Por día, por mes y el total del período — se respeta el filtro de campaña.
            _cel = "regexp_replace(COALESCE(u.telefono, ''), '[^0-9]', '', 'g')"
            cur.execute(
                f"""SELECT (c.iniciado_en AT TIME ZONE 'America/Lima')::date AS dia, COUNT(DISTINCT {_cel})
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND {_cel} <> ''
                   GROUP BY dia ORDER BY dia""",
                (fecha_desde, fecha_hasta),
            )
            celulares_por_dia = [{"fecha": d.isoformat(), "cantidad": n} for d, n in cur.fetchall()]
            cur.execute(
                f"""SELECT to_char(c.iniciado_en AT TIME ZONE 'America/Lima', 'YYYY-MM') AS mes, COUNT(DISTINCT {_cel})
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND {_cel} <> ''
                   GROUP BY mes ORDER BY mes""",
                (fecha_desde, fecha_hasta),
            )
            celulares_por_mes = [{"mes": mes, "cantidad": n} for mes, n in cur.fetchall()]
            cur.execute(
                f"""SELECT COUNT(DISTINCT {_cel})
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND {_cel} <> ''""",
                (fecha_desde, fecha_hasta),
            )
            celulares_unicos_total = cur.fetchone()[0]

            cur.execute(
                """SELECT COALESCE(a.nombre_display, 'Sin asignar'), COUNT(*)
                   FROM conversaciones c
                   LEFT JOIN asesores a ON a.id = c.asesor_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND c.asesor_id IS NOT NULL
                   GROUP BY a.nombre_display ORDER BY COUNT(*) DESC""",
                (fecha_desde, fecha_hasta),
            )
            por_asesor = [{"asesor": nombre, "cantidad": cantidad} for nombre, cantidad in cur.fetchall()]

            cur.execute(
                """SELECT COALESCE(et.nombre, 'Sin cerrar'), COUNT(*)
                   FROM conversaciones c
                   LEFT JOIN etiquetas_cierre et ON et.id = c.etiqueta_cierre_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                   GROUP BY et.nombre ORDER BY COUNT(*) DESC""",
                (fecha_desde, fecha_hasta),
            )
            por_etiqueta = [{"etiqueta": nombre, "cantidad": cantidad} for nombre, cantidad in cur.fetchall()]

            cur.execute(
                """SELECT COALESCE(cam.nombre, 'Sin opción'), COUNT(*)
                   FROM conversaciones c
                   LEFT JOIN campanas cam ON cam.id = c.campana_id
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                   GROUP BY cam.nombre ORDER BY COUNT(*) DESC""",
                (fecha_desde, fecha_hasta),
            )
            por_campana = [{"campana": nombre, "cantidad": cantidad} for nombre, cantidad in cur.fetchall()]

            cur.execute(
                """SELECT COUNT(*), COUNT(*) FILTER (WHERE c.asesor_id IS NOT NULL)
                   FROM conversaciones c
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s""",
                (fecha_desde, fecha_hasta),
            )
            total, total_atendidas = cur.fetchone()

            # Detalle por hora del día (hora de Lima): total y atendidas por un asesor.
            cur.execute(
                """SELECT EXTRACT(HOUR FROM c.iniciado_en AT TIME ZONE 'America/Lima')::int AS hora,
                          COUNT(*), COUNT(*) FILTER (WHERE c.asesor_id IS NOT NULL)
                   FROM conversaciones c
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                   GROUP BY hora""",
                (fecha_desde, fecha_hasta),
            )
            _h = {hora: (t, a) for hora, t, a in cur.fetchall()}
            por_hora = [{"hora": h, "cantidad": _h.get(h, (0, 0))[0], "atendidas": _h.get(h, (0, 0))[1]}
                        for h in range(24)]

            # Mapa día × hora (solo si el período es de hasta 31 días, para que se pueda leer).
            por_dia_hora = []
            if (fecha_hasta - fecha_desde).days <= 31:
                cur.execute(
                    """SELECT (c.iniciado_en AT TIME ZONE 'America/Lima')::date AS dia,
                              EXTRACT(HOUR FROM c.iniciado_en AT TIME ZONE 'America/Lima')::int AS hora,
                              COUNT(*)
                       FROM conversaciones c
                       WHERE c.iniciado_en >= %s AND c.iniciado_en < %s
                       GROUP BY dia, hora ORDER BY dia, hora""",
                    (fecha_desde, fecha_hasta),
                )
                por_dia_hora = [{"fecha": d.isoformat(), "hora": h, "cantidad": n} for d, h, n in cur.fetchall()]

            por_origen, por_pagina = _estadisticas_origen(conn, cur, fecha_desde, fecha_hasta)

            por_metodo_token = []
            try:
                cur.execute("SAVEPOINT sp_metodo")
                cur.execute(
                    """SELECT h.hito, COUNT(*) FROM conversacion_hitos h
                       JOIN conversaciones c ON c.id = h.conversacion_id
                       WHERE h.hito IN ('metodo_sms', 'metodo_sms_reenviado', 'metodo_llamada', 'metodo_whatsapp')
                         AND c.iniciado_en >= %s AND c.iniciado_en < %s
                       GROUP BY h.hito""",
                    (fecha_desde, fecha_hasta),
                )
                cuentas = {h[len("metodo_"):]: n for h, n in cur.fetchall()}
                cur.execute("RELEASE SAVEPOINT sp_metodo")
                por_metodo_token = [
                    {"metodo": etiqueta, "cantidad": cuentas.get(clave, 0)}
                    for clave, etiqueta in (("sms", "SMS (primer envío)"), ("sms_reenviado", "SMS reenviado"),
                                            ("llamada", "Llamada"), ("whatsapp", "WhatsApp"))
                ]
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_metodo")

            sin_cerrar_detalle, sin_cerrar_pasos = _detalle_conversaciones(
                cur, fecha_desde, fecha_hasta, "c.etiqueta_cierre_id IS NULL")
            sin_opcion_detalle, sin_opcion_pasos = _detalle_conversaciones(
                cur, fecha_desde, fecha_hasta, "c.campana_id IS NULL")

            # Embudo. Si existe la tabla de hitos (migracion_hitos_embudo.sql) se
            # calcula con lo que realmente ocurrió en cada conversación, desde la
            # fecha en que se empezó a registrar. Si no, se usa la aproximación
            # por el nodo donde quedó parada cada conversación.
            embudo = None
            embudo_desde = None
            try:
                cur.execute("SAVEPOINT sp_embudo")
                cur.execute("SELECT MIN(creado_en) FROM conversacion_hitos")
                primero = cur.fetchone()[0]
                if primero is not None:
                    desde_e = max(fecha_desde, primero)
                    cur.execute(
                        """SELECT COUNT(*),
                                  COUNT(*) FILTER (WHERE h.vio_menu),
                                  COUNT(*) FILTER (WHERE h.eligio_opcion),
                                  COUNT(*) FILTER (WHERE h.verificado OR h.reconocido),
                                  COUNT(*) FILTER (WHERE h.verificado),
                                  COUNT(*) FILTER (WHERE h.reconocido),
                                  COUNT(*) FILTER (WHERE h.pidio_codigo),
                                  COUNT(*) FILTER (WHERE h.entro_flujo),
                                  COUNT(*) FILTER (WHERE h.asesor),
                                  COUNT(*) FILTER (WHERE h.cierre_bot),
                                  COUNT(*) FILTER (WHERE NOT EXISTS (
                                      SELECT 1 FROM mensajes mu
                                      WHERE mu.conversacion_id = c.id AND mu.emisor_tipo = 'usuario')
                                      AND NOT h.eligio_opcion)
                           FROM conversaciones c
                           LEFT JOIN LATERAL (
                               SELECT bool_or(hito = 'vio_menu') AS vio_menu,
                                      bool_or(hito = 'eligio_opcion') AS eligio_opcion,
                                      bool_or(hito = 'verificado') AS verificado,
                                      bool_or(hito = 'reconocido') AS reconocido,
                                      bool_or(hito = 'pidio_codigo') AS pidio_codigo,
                                      bool_or(hito = 'entro_flujo') AS entro_flujo,
                                      bool_or(hito = 'asesor') AS asesor,
                                      bool_or(hito = 'cierre_bot') AS cierre_bot
                               FROM conversacion_hitos x WHERE x.conversacion_id = c.id
                           ) h0 ON TRUE
                           CROSS JOIN LATERAL (SELECT COALESCE(h0.vio_menu, FALSE) AS vio_menu,
                                      COALESCE(h0.eligio_opcion, FALSE) AS eligio_opcion,
                                      COALESCE(h0.verificado, FALSE) AS verificado,
                                      COALESCE(h0.reconocido, FALSE) AS reconocido,
                                      COALESCE(h0.pidio_codigo, FALSE) AS pidio_codigo,
                                      COALESCE(h0.entro_flujo, FALSE) AS entro_flujo,
                                      COALESCE(h0.asesor, FALSE) AS asesor,
                                      COALESCE(h0.cierre_bot, FALSE) AS cierre_bot) h
                           WHERE c.iniciado_en >= %s AND c.iniciado_en < %s""",
                        (desde_e, fecha_hasta),
                    )
                    (e_total, e_menu, e_eligio, e_ident, e_sms, e_recon, e_pidio_cod,
                     e_flujo, e_asesor, e_cierre, e_sin_hacer) = cur.fetchone()
                    embudo_desde = desde_e.date().isoformat() if hasattr(desde_e, "date") else str(desde_e)
                    embudo = [
                        {"paso": "Abrió el chat", "cantidad": e_total,
                         "nota": f"{e_sin_hacer} no hicieron nada"},
                        {"paso": "Vio el saludo / menú", "cantidad": e_menu},
                        {"paso": "Eligió una opción", "cantidad": e_eligio},
                        {"paso": "Quedó identificado", "cantidad": e_ident,
                         "nota": f"{e_sms} con código SMS · {e_recon} ya reconocidos (sin pedir datos)"},
                        {"paso": "Entró a un flujo", "cantidad": e_flujo},
                        {"paso": "Llegó a un asesor", "cantidad": e_asesor, "sin_caida": True},
                        {"paso": "Terminó dentro del bot", "cantidad": e_cierre, "sin_caida": True,
                         "nota": f"{e_pidio_cod} conversaciones recibieron SMS"},
                    ]
                cur.execute("RELEASE SAVEPOINT sp_embudo")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_embudo")
                embudo = None

            if embudo is None:
                cur.execute(
                    """SELECT COUNT(*),
                              COUNT(*) FILTER (WHERE c.estado = 'bot' AND c.nodo_actual = 'webchat_pide_celular'),
                              COUNT(*) FILTER (WHERE c.estado = 'bot'
                                               AND c.nodo_actual IN ('webchat_pide_nombre', 'webchat_valida_pide_codigo')),
                              COUNT(*) FILTER (WHERE c.estado = 'bot' AND c.nodo_actual = 'webchat_menu_principal'),
                              COUNT(*) FILTER (WHERE c.campana_id IS NOT NULL),
                              COUNT(*) FILTER (WHERE NOT EXISTS (
                                  SELECT 1 FROM mensajes mu
                                  WHERE mu.conversacion_id = c.id AND mu.emisor_tipo = 'usuario')),
                              COUNT(*) FILTER (WHERE c.estado = 'bot'
                                               AND c.nodo_actual IN ('webchat_bienvenida', 'webchat_gate'))
                       FROM conversaciones c
                       WHERE c.iniciado_en >= %s AND c.iniciado_en < %s""",
                    (fecha_desde, fecha_hasta),
                )
                (f_total, f_celular, f_token, f_menu, f_derivadas,
                 f_sin_escribir, f_bienvenida) = cur.fetchone()
                # Flujo nuevo: saludo con menú -> (al elegir una opción) celular +
                # código -> flujo elegido. Flujo anterior: celular -> código -> menú.
                # Los pasos son acumulativos, así que sirven para los dos (y para
                # periodos que mezclan ambos); "Vio el saludo y eligió" solo resta
                # a quienes se quedaron en la bienvenida.
                paso1b = f_total - f_bienvenida
                paso2 = paso1b - f_celular
                paso3 = paso2 - f_token
                paso4 = paso3 - f_menu
                embudo = [
                    {"paso": "Abrió el chat", "cantidad": f_total, "nota": f"{f_sin_escribir} no hicieron nada"},
                    {"paso": "Eligió una opción del saludo", "cantidad": paso1b,
                     "nota": "solo aplica desde que se activó el saludo con menú"},
                    {"paso": "Ingresó su celular", "cantidad": paso2},
                    {"paso": "Superó la validación (token)", "cantidad": paso3},
                    {"paso": "Entró a un flujo / eligió del menú", "cantidad": paso4},
                    {"paso": "Llegó a un asesor", "cantidad": f_derivadas},
                ]

            # Tiempo promedio de respuesta por asesor y por campaña.
            #
            # mensajes NO guarda qué asesor específico mandó cada mensaje
            # (solo emisor_tipo = 'usuario'/'asesor'/'bot'/'supervisor'), así
            # que cada mensaje 'asesor' se atribuye al asesor ASIGNADO a esa
            # conversación (c.asesor_id) — si hubo una transferencia, los
            # mensajes de antes y después quedan a nombre del asesor actual;
            # es una aproximación razonable dado el esquema.
            #
            # Se mide el tiempo entre cada mensaje del usuario y la siguiente
            # respuesta del asesor (pares consecutivos usuario→asesor), y solo
            # se cuentan los que ocurrieron DESPUÉS de que la conversación ya
            # estaba asignada (mensaje_usuario.enviado_en >= c.asignado_en) —
            # así no se le carga al asesor el tiempo que la conversación
            # esperó en cola antes de que alguien la tomara.
            filtro_campana = ("AND " + _sql_campanas(campanas_filtro)) if campanas_filtro is not None else ""
            cur.execute(
                f"""
                WITH pares AS (
                    SELECT
                        m.conversacion_id,
                        m.enviado_en AS respuesta_en,
                        LAG(m.emisor_tipo) OVER w AS anterior_tipo,
                        LAG(m.enviado_en) OVER w AS anterior_en
                    FROM mensajes m
                    WHERE m.emisor_tipo IN ('usuario', 'asesor')
                    WINDOW w AS (PARTITION BY m.conversacion_id ORDER BY m.enviado_en)
                )
                SELECT
                    COALESCE(a.nombre_display, 'Sin asignar') AS asesor,
                    COALESCE(cam.nombre, 'Sin campaña') AS campana,
                    AVG(EXTRACT(EPOCH FROM (p.respuesta_en - p.anterior_en))) AS promedio_segundos,
                    COUNT(*) AS cantidad
                FROM pares p
                JOIN conversaciones c ON c.id = p.conversacion_id
                LEFT JOIN asesores a ON a.id = c.asesor_id
                LEFT JOIN campanas cam ON cam.id = c.campana_id
                WHERE p.anterior_tipo = 'usuario'
                  AND c.asesor_id IS NOT NULL
                  AND c.asignado_en IS NOT NULL
                  AND p.anterior_en >= c.asignado_en
                  AND p.respuesta_en >= %s AND p.respuesta_en < %s
                  {filtro_campana}
                GROUP BY a.nombre_display, cam.nombre
                ORDER BY promedio_segundos DESC
                """,
                (fecha_desde, fecha_hasta),
            )
            tiempos_respuesta = [
                {
                    "asesor": asesor, "campana": campana,
                    "promedio_segundos": round(float(promedio_segundos), 1),
                    "cantidad": cantidad,
                }
                for asesor, campana, promedio_segundos, cantidad in cur.fetchall()
            ]
    finally:
        release_connection(conn)

    return {
        "total": total,
        "total_atendidas": total_atendidas,
        "por_dia": por_dia,
        "celulares_por_dia": celulares_por_dia,
        "celulares_por_mes": celulares_por_mes,
        "celulares_unicos_total": celulares_unicos_total,
        "por_hora": por_hora,
        "por_dia_hora": por_dia_hora,
        "por_asesor": por_asesor,
        "por_etiqueta": por_etiqueta,
        "por_campana": por_campana,
        "tiempos_respuesta": tiempos_respuesta,
        "sin_cerrar_detalle": sin_cerrar_detalle,
        "sin_cerrar_pasos": sin_cerrar_pasos,
        "sin_opcion_detalle": sin_opcion_detalle,
        "sin_opcion_pasos": sin_opcion_pasos,
        "embudo": embudo,
        "embudo_desde": embudo_desde,
        "por_origen": por_origen,
        "por_metodo_token": por_metodo_token,
        "por_pagina": por_pagina,
    }


def _rango_dashboard(desde: str, hasta: str):
    try:
        d_desde = datetime.strptime(desde, "%Y-%m-%d").date()
        d_hasta = datetime.strptime(hasta, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Fechas inválidas (usar YYYY-MM-DD)")
    if d_hasta < d_desde:
        raise HTTPException(status_code=400, detail="'hasta' debe ser igual o posterior a 'desde'")
    return (d_desde, d_hasta,
            datetime.combine(d_desde, datetime.min.time()).replace(tzinfo=LIMA_TZ),
            datetime.combine(d_hasta, datetime.min.time()).replace(tzinfo=LIMA_TZ) + timedelta(days=1))


@app.get("/dashboard/encuesta")
def encuesta_dashboard(desde: str, hasta: str, campana_id: int | None = None, authorization: str | None = Header(None)):
    """Resultados de la encuesta de satisfacción (encuestas ENVIADAS en el período). Ver encuesta.py."""
    alcance = supervisor.obtener_alcance(_verificar_editor(authorization))
    campanas_filtro = supervisor.campanas_efectivas(alcance, campana_id)
    _, _, f_desde, f_hasta = _rango_dashboard(desde, hasta)
    return encuesta.estadisticas(f_desde, f_hasta, campanas_filtro)


@app.get("/dashboard/encuesta.xlsx")
def encuesta_excel(desde: str, hasta: str, campana_id: int | None = None, authorization: str | None = Header(None)):
    """Excel con cada respuesta (incluye comentarios). Solo supervisores, como el reporte de conversaciones."""
    supervisor_id = _verificar_supervisor(authorization)
    campanas_filtro = supervisor.campanas_efectivas(supervisor.obtener_alcance(supervisor_id), campana_id)
    d1, d2, f_desde, f_hasta = _rango_dashboard(desde, hasta)
    contenido = encuesta.exportar_xlsx(f_desde, f_hasta, campanas_filtro,
                                       f"{d1.strftime('%d/%m/%Y')} al {d2.strftime('%d/%m/%Y')}")
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="encuesta_satisfaccion_{d1.isoformat()}_a_{d2.isoformat()}.xlsx"',
                 "Cache-Control": "no-store"},
    )


@app.get("/dashboard/consultas")
def consultas_dashboard(desde: str, hasta: str, campana_id: int | None = None, authorization: str | None = Header(None)):
    """Qué es lo que más consultan: (1) botones del menú más tocados, (2) último botón
    que tocaron las conversaciones que NO llegaron a un asesor ("Sin opción") y
    (3) temas de lo que escriben, clasificados por palabras clave (temas_consulta.py).
    Va aparte de /dashboard/estadisticas para que, si falla o tarda, no afecte al resto."""
    alcance = supervisor.obtener_alcance(_verificar_editor(authorization))
    campanas_filtro = supervisor.campanas_efectivas(alcance, campana_id)
    try:
        d_desde = datetime.strptime(desde, "%Y-%m-%d").date()
        d_hasta = datetime.strptime(hasta, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Fechas inválidas (usar YYYY-MM-DD)")
    if d_hasta < d_desde:
        raise HTTPException(status_code=400, detail="'hasta' debe ser igual o posterior a 'desde'")
    f_desde = datetime.combine(d_desde, datetime.min.time()).replace(tzinfo=LIMA_TZ)
    f_hasta = datetime.combine(d_hasta, datetime.min.time()).replace(tzinfo=LIMA_TZ) + timedelta(days=1)

    conn = get_connection()
    try:
        with conn.cursor() as _cur_real:
            cur = _CursorFiltroCampana(_cur_real, campanas_filtro) if campanas_filtro is not None else _cur_real
            # (1)+(2) Clics en botones. Cada clic se guarda con el ID de la opción; se traduce al
            # texto del botón con la lista de opciones que el bot mostró justo antes.
            cur.execute(
                """SELECT m.conversacion_id, m.contenido, prev.contenido, (c.campana_id IS NULL)
                   FROM mensajes m
                   JOIN conversaciones c ON c.id = m.conversacion_id
                   LEFT JOIN LATERAL (
                       SELECT contenido FROM mensajes
                       WHERE conversacion_id = m.conversacion_id AND id < m.id
                         AND emisor_tipo = 'bot' AND tipo_contenido = 'opciones'
                       ORDER BY id DESC LIMIT 1) prev ON TRUE
                   WHERE m.tipo_contenido = 'boton_click' AND m.contenido <> ''
                     AND c.iniciado_en >= %s AND c.iniciado_en < %s
                   ORDER BY m.conversacion_id, m.id
                   LIMIT 80000""",
                (f_desde, f_hasta),
            )
            filas_clics = cur.fetchall()
            cur.execute(
                """SELECT COUNT(*) FROM conversaciones c
                   WHERE c.iniciado_en >= %s AND c.iniciado_en < %s AND c.campana_id IS NULL
                     AND NOT EXISTS (SELECT 1 FROM mensajes m WHERE m.conversacion_id = c.id
                                     AND m.tipo_contenido = 'boton_click' AND m.contenido <> '')""",
                (f_desde, f_hasta),
            )
            sin_clic = cur.fetchone()[0]

            # (3) Lo que escriben (solo texto libre; se descartan números/códigos en temas_consulta)
            cur.execute(
                """SELECT m.conversacion_id, m.contenido, (m.enviado_en AT TIME ZONE 'America/Lima')::date
                   FROM mensajes m JOIN conversaciones c ON c.id = m.conversacion_id
                   WHERE m.emisor_tipo = 'usuario' AND m.tipo_contenido = 'texto'
                     AND c.iniciado_en >= %s AND c.iniciado_en < %s
                   ORDER BY m.id LIMIT 60000""",
                (f_desde, f_hasta),
            )
            filas = cur.fetchall()
        conn.rollback()
    finally:
        release_connection(conn)

    resultado = temas_consulta.clasificar(filas, d_desde, d_hasta)
    resultado.update(temas_consulta.resumir_botones(filas_clics, sin_clic))
    resultado["truncado"] = len(filas) >= 60000
    return resultado


@app.get("/consola/aviso")
def aviso_consola():
    """Público (se consulta antes de iniciar sesión): texto del aviso superior del login
    (la ruta NO empieza con /login a propósito: Cloud Armor limita /login a 5 peticiones/min por IP)
    de las consolas, editable desde /editor (clave banner_login). Vacío = sin aviso."""
    try:
        texto = (supervisor.obtener_configuracion("banner_login") or "").strip()
    except Exception:
        texto = ""
    return JSONResponse({"texto": texto[:600]}, headers={"Cache-Control": "no-store"})


@app.get("/widget/textos")
def textos_publicos_widget():
    """Endpoint público (el widget no está autenticado) con textos de UI
    parametrizables desde /editor que el widget necesita ANTES de cualquier
    respuesta del bot."""
    return {
        "mensaje_subiendo_archivo": supervisor.obtener_configuracion("mensaje_subiendo_archivo")
        or MENSAJES_AUTOMATICOS["mensaje_subiendo_archivo"]["default"],
        "sonido_notificacion_widget": supervisor.obtener_configuracion("sonido_notificacion_widget")
        or MENSAJES_AUTOMATICOS["sonido_notificacion_widget"]["default"],
        "titulo_widget": supervisor.obtener_configuracion("titulo_widget")
        or MENSAJES_AUTOMATICOS["titulo_widget"]["default"],
    }


_DIR_SONIDOS = "static/sonidos"
_TIPOS_SONIDO = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}


@app.get("/static/sonidos/{archivo}")
def sonido_widget_por_archivo(archivo: str):
    """Sirve cualquier audio subido a static/sonidos/ (ej. notificacion.mp3).
    Así, para cambiar el sonido del widget solo hay que subir el archivo
    nuevo a esa carpeta y poner "/static/sonidos/<archivo>" en /editor ->
    "Mensajes automáticos" -> sonido_notificacion_widget — sin tocar código
    ni agregar un endpoint por cada sonido."""
    nombre = os.path.basename(archivo)
    if nombre != archivo or nombre in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    _, ext = os.path.splitext(nombre.lower())
    if ext not in _TIPOS_SONIDO:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ruta = os.path.join(_DIR_SONIDOS, nombre)
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="Sonido no encontrado")
    return FileResponse(
        ruta,
        media_type=_TIPOS_SONIDO[ext],
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --- Selector de lugar en el mapa (SERVICIO PROGRAMADO de Médico a domicilio / Ambulancia) -----------------
# /mapa/mapa.html es una página con Leaflet (copia local en static/mapa/) + mosaicos de OpenStreetMap. El widget la abre
# en un iframe y recibe lat/lng por postMessage. Solo se sirven estos archivos (nada de rutas libres).
_DIR_MAPA = "static/mapa"
_ARCHIVOS_MAPA = {"mapa.html": "text/html; charset=utf-8", "leaflet.js": "application/javascript",
                  "leaflet.css": "text/css"}
_IMG_MAPA = {"layers.png", "layers-2x.png", "marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"}


def _headers_mapa() -> dict:
    # Solo las páginas de ALLOWED_ORIGINS (el sitio donde vive el widget) pueden incrustarla.
    ancestros = " ".join(allowed_origins) or "'none'"
    return {
        "Cache-Control": "public, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; object-src 'none'; "
            f"base-uri 'self'; frame-ancestors {ancestros}"),
    }


@app.get("/mapa/img/{archivo}")
def mapa_imagen(archivo: str):
    if archivo not in _IMG_MAPA:
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(os.path.join(_DIR_MAPA, "img", archivo), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/mapa/{archivo}")
def mapa_archivo(archivo: str):
    tipo = _ARCHIVOS_MAPA.get(archivo)
    if tipo is None:
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(os.path.join(_DIR_MAPA, archivo), media_type=tipo, headers=_headers_mapa())


_DIR_IMAGENES = "static/imagenes"
_TIPOS_IMAGEN = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".svg": "image/svg+xml", ".webp": "image/webp", ".woff2": "font/woff2"}


@app.get("/static/imagenes/{archivo}")
def imagen_widget_por_archivo(archivo: str):
    """Mismo mecanismo que /static/sonidos/{archivo} pero para imágenes --
    ej. el ícono de "Sofi" que reemplazó al isotipo de WhatsApp en la
    burbuja del widget (ver widget.js, botón .burbuja)."""
    nombre = os.path.basename(archivo)
    if nombre != archivo or nombre in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    _, ext = os.path.splitext(nombre.lower())
    if ext not in _TIPOS_IMAGEN:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ruta = os.path.join(_DIR_IMAGENES, nombre)
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    return FileResponse(
        ruta,
        media_type=_TIPOS_IMAGEN[ext],
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _reenviar_asignaciones_activas(asesor_id: int) -> list:
    """Conversaciones ya asignadas a este asesor (útil al reconectar, para que
    la consola reconstruya su lista sin perder de vista lo que ya atendía).
    Usa el mismo formato de historial que asignacion.py, no el de _obtener_historial
    (que es para el widget) — para que la consola de asesor los procese igual."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.campana_id, COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO') AS nombre, COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR') AS telefono, COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI') AS dni, c.entrada_cola, c.asignado_en,
                          c.pausada_en, c.bandera, c.aviso_inactividad_activo
                   FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.asesor_id = %s AND c.estado = 'asignada'""",
                (asesor_id,),
            )
            filas = cur.fetchall()
            resultado = []
            for (conversacion_id, campana_id, usuario_nombre, usuario_telefono, usuario_dni,
                 entrada_cola, asignado_en, pausada_en, bandera, aviso_inactividad_activo) in filas:
                resultado.append({
                    "conversacion_id": conversacion_id,
                    "campana": campana_id,
                    "resumen_usuario": usuario_nombre,
                    "telefono": usuario_telefono,
                    "dni": usuario_dni,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "pausada": pausada_en is not None,
                    "bandera": bandera,
                    "aviso_inactividad_activo": aviso_inactividad_activo,
                    "historial": asignacion._historial(cur, conversacion_id),
                })
            return resultado
    finally:
        release_connection(conn)


@app.websocket("/ws/asesor")
async def ws_asesor(websocket: WebSocket, token: str, pausado: int = 0):
    """
    wss://.../ws/asesor?token=<jwt obtenido en POST /login>[&pausado=1]
    Al conectar (primera vez o reconexión): reenvía lo que ya tenía asignado,
    marca presencia, y le asigna de inmediato lo que haya en cola para sus
    campañas, hasta llenar su cupo.

    `pausado=1` lo manda la consola cuando RECONECTA estando en pausa (por una
    caída de red o un deploy): al desconectarse el servidor cierra la pausa,
    así que sin este dato el asesor volvería "activo" por detrás mientras su
    pantalla sigue diciendo "En pausa", y le entrarían conversaciones.
    """
    asesor_id = auth.decodificar_token(token)
    if asesor_id is None:
        await websocket.close(code=4401)  # token inválido o expirado
        return

    await manager.connect_asesor(asesor_id, websocket)
    _marcar_conectado(asesor_id)
    # La pausa se restablece ANTES de enviar nada ni de asignar de la cola.
    _cambiar_pausa_asesor(asesor_id, bool(pausado))
    await supervisor.notificar_cambio_asesor(asesor_id, manager)
    await supervisor.enviar_mis_estadisticas(asesor_id, manager)
    try:
        await supervisor.enviar_etiquetas(asesor_id, manager, a_asesor=True)
        await supervisor.enviar_respuestas_rapidas(asesor_id, manager, a_asesor=True)
        activas_conexion = _reenviar_asignaciones_activas(asesor_id)
        for asignacion_activa in activas_conexion:
            await manager.enviar_a_asesor(asesor_id, {
                "type": "nueva_asignacion", "payload": asignacion_activa,
            })
        await manager.enviar_a_asesor(asesor_id, {
            "type": "conversaciones_vigentes",
            "payload": {"ids": [a["conversacion_id"] for a in activas_conexion]},
        })
        await asignacion.asignar_desde_cola_para_asesor(asesor_id, manager)

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}

            # Envío fiable de la consola: lo escrito sin red se reenvía al reconectar con el mismo cid;
            # si ya lo procesamos solo se confirma (no se duplica).
            if tipo in _TIPOS_FIABLES_STAFF and payload.get("cid"):
                if not _registrar_cid_widget(f"asesor:{asesor_id}", str(payload["cid"])[:64]):
                    await websocket.send_json({"type": "mensaje_propio_guardado", "payload": {
                        "conversacion_id": payload.get("conversacion_id"), "cid": payload.get("cid"), "dup": True}})
                    continue

            if tipo == "mensaje_asesor":
                conversacion_id = payload.get("conversacion_id")
                texto = payload.get("texto")
                respondido_a_id = payload.get("respondido_a_id")
                mensaje_id = _guardar_mensaje_asesor(conversacion_id, texto, "asesor", asesor_id, respondido_a_id)
                respondido_a = _obtener_preview_mensaje(respondido_a_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensaje_asesor",
                    "payload": {"texto": texto, "asesor_nombre": _nombre_display(asesor_id),
                                "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                })
                await websocket.send_json({
                    "type": "mensaje_propio_guardado",
                    "payload": {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id, "cid": payload.get("cid")},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "asesor", "texto": texto,
                                "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                })

            elif tipo == "mensaje_asesor_archivo":
                # Imagen pegada (Ctrl+V) o adjuntada por el asesor — ya subida
                # a /upload antes de mandar este mensaje; acá solo se persiste
                # la ruta y se avisa al widget/supervisores escuchando.
                conversacion_id = payload.get("conversacion_id")
                ruta = payload.get("ruta")
                mensaje_id = _guardar_mensaje_staff_archivo(conversacion_id, ruta, "asesor", asesor_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "archivo_asesor",
                    "payload": {"ruta": ruta, "asesor_nombre": _nombre_display(asesor_id), "mensaje_id": mensaje_id},
                })
                await websocket.send_json({
                    "type": "mensaje_propio_guardado",
                    "payload": {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id, "cid": payload.get("cid")},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "asesor",
                                "texto": "📎 Imagen enviada", "ruta": ruta, "mensaje_id": mensaje_id},
                })

            elif tipo == "editar_mensaje":
                conversacion_id = payload.get("conversacion_id")
                resultado = _editar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                             "asesor", asesor_id, payload.get("texto"))
                if resultado:
                    await _difundir_mensaje_modificado("mensaje_editado", conversacion_id, resultado)

            elif tipo == "eliminar_mensaje":
                conversacion_id = payload.get("conversacion_id")
                resultado = _eliminar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                               "asesor", asesor_id, _nombre_display(asesor_id))
                if resultado:
                    await _difundir_mensaje_modificado("mensaje_eliminado", conversacion_id, resultado)

            elif tipo == "reaccionar_mensaje":
                conversacion_id = payload.get("conversacion_id")
                estado_conv = _obtener_estado_conversacion(conversacion_id) if isinstance(conversacion_id, int) else None
                # Solo en conversaciones que este asesor tiene a su cargo.
                if estado_conv and estado_conv[1] == asesor_id:
                    resultado = _reaccionar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                                     f"asesor:{asesor_id}", payload.get("emoji"))
                    if resultado:
                        await _difundir_mensaje_modificado("mensaje_reaccion", conversacion_id, resultado)

            elif tipo == "cambiar_bandera":
                conversacion_id = payload.get("conversacion_id")
                resultado = _cambiar_bandera(conversacion_id, payload.get("bandera"), asesor_id_esperado=asesor_id)
                if resultado:
                    await _difundir_bandera_cambiada(conversacion_id, resultado["bandera"], resultado["asesor_id"])

            elif tipo == "enviar_sms_saliente":
                conversacion_id = payload.get("conversacion_id")
                resultado = _enviar_sms_saliente(conversacion_id, asesor_id)
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "resultado_sms_saliente",
                    "payload": {"conversacion_id": conversacion_id, **resultado},
                })

            elif tipo == "enviar_sms_invitacion":
                resultado = await asyncio.to_thread(_enviar_sms_invitacion, payload.get("celular"), asesor_id, payload.get("texto"),
                                                    payload.get("campana_id"), payload.get("asesor_destino_id"))
                await manager.enviar_a_asesor(asesor_id, {"type": "resultado_sms_invitacion", "payload": resultado})

            elif tipo == "enviar_archivo_sms":
                resultado = await asyncio.to_thread(_enviar_archivo_sms, payload.get("celular"), asesor_id, "asesor",
                                                    payload.get("ruta"), payload.get("nombre"))
                await manager.enviar_a_asesor(asesor_id, {"type": "resultado_archivo_sms", "payload": resultado})

            elif tipo == "destinos_sms_directo":
                await manager.enviar_a_asesor(asesor_id, {"type": "destinos_sms_directo",
                                                          "payload": await asyncio.to_thread(_destinos_sms_directo, asesor_id)})

            elif tipo == "texto_sms_invitacion":
                texto = await asyncio.to_thread(_texto_sms_invitacion)
                await manager.enviar_a_asesor(asesor_id, {"type": "texto_sms_invitacion", "payload": {"texto": texto, "max": SMS_INVITACION_MAX_CARACTERES}})

            elif tipo == "pausar_conversacion":
                conversacion_id = payload.get("conversacion_id")
                cambio = _set_pausa_conversacion(conversacion_id, asesor_id, True)
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "conversacion_pausada",
                    "payload": {"conversacion_id": conversacion_id, "ok": cambio},
                })

            elif tipo == "reanudar_conversacion":
                conversacion_id = payload.get("conversacion_id")
                cambio = _set_pausa_conversacion(conversacion_id, asesor_id, False)
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "conversacion_reactivada",
                    "payload": {"conversacion_id": conversacion_id, "ok": cambio},
                })

            elif tipo == "cambiar_aviso_inactividad":
                conversacion_id = payload.get("conversacion_id")
                activo = bool(payload.get("activo"))
                _set_aviso_inactividad_conversacion(conversacion_id, activo, asesor_id_esperado=asesor_id)
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "aviso_inactividad_cambiado",
                    "payload": {"conversacion_id": conversacion_id, "activo": activo},
                })

            elif tipo == "cerrar_conversacion":
                conversacion_id = payload.get("conversacion_id")
                etiqueta_cierre_id = payload.get("etiqueta_cierre_id")
                await _cerrar_y_notificar(conversacion_id, etiqueta_cierre_id, "asesor")
                # liberó cupo: intenta tomar la siguiente de la cola
                await asignacion.asignar_desde_cola_para_asesor(asesor_id, manager)
                await supervisor.notificar_cambio_asesor(asesor_id, manager)

            elif tipo == "cambiar_disponibilidad":
                pausado = bool(payload.get("pausado"))
                _cambiar_pausa_asesor(asesor_id, pausado, payload.get("motivo"), payload.get("detalle"))
                await supervisor.notificar_cambio_asesor(asesor_id, manager)
                await supervisor.enviar_mis_estadisticas(asesor_id, manager)
                if not pausado:
                    # al reanudar, intenta tomar de la cola de inmediato
                    await asignacion.asignar_desde_cola_para_asesor(asesor_id, manager)

            elif tipo == "marcar_leido":
                conversacion_id = payload.get("conversacion_id")
                _marcar_leidos_por_asesor(conversacion_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensajes_leidos", "payload": {"conversacion_id": conversacion_id},
                })
                # Supervisores que están viendo la conversación: el asesor leyó (✓✓).
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensajes_leidos",
                    "payload": {"conversacion_id": conversacion_id, "lector": "asesor"},
                })

            elif tipo == "refrescar_conversaciones":
                # Vuelve a mandar el estado autoritativo de todas las conversaciones
                # asignadas — por si el asesor siente que algo no cargó bien.
                activas = _reenviar_asignaciones_activas(asesor_id)
                for asignacion_activa in activas:
                    await manager.enviar_a_asesor(asesor_id, {
                        "type": "nueva_asignacion", "payload": asignacion_activa,
                    })
                # Lista autoritativa: la consola quita las que ya no están asignadas a este asesor
                # (cerradas por un supervisor, transferidas, etc.).
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "conversaciones_vigentes",
                    "payload": {"ids": [a["conversacion_id"] for a in activas]},
                })

            elif tipo == "obtener_historial_usuario":
                conversacion_id_actual = payload.get("conversacion_id")
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "historial_usuario",
                    "payload": {"conversacion_id": conversacion_id_actual,
                                "conversaciones": _obtener_historial_usuario(conversacion_id_actual)},
                })

            elif tipo == "obtener_mensajes_conversacion_pasada":
                conv_id = payload.get("conversacion_id")
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "mensajes_conversacion_pasada",
                    "payload": {"conversacion_id": conv_id, "mensajes": _obtener_historial(conv_id)},
                })

            elif tipo == "buscar_conversaciones":
                # Mismo buscador que tiene el supervisor (celular, DNI o nombre,
                # de cualquier estado). Se exige un mínimo de 3 caracteres para
                # no devolver medio historial con una sola letra.
                consulta = (payload.get("query") or "").strip()
                resultados = _buscar_conversaciones(consulta) if len(consulta) >= 3 else []
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "resultados_busqueda",
                    "payload": {"query": consulta, "resultados": resultados},
                })

            elif tipo == "buscar_conversaciones_bot":
                telefono = (payload.get("telefono") or "").strip()
                resultados = _buscar_conversaciones_bot(telefono) if len(telefono) >= 3 else []
                await manager.enviar_a_asesor(asesor_id, {
                    "type": "resultados_busqueda_bot",
                    "payload": {"telefono": telefono, "resultados": resultados},
                })

            elif tipo == "ayuda_bot_abrir":
                # "Ayudar en el bot": el asesor abre una conversación que todavía
                # está en el chatbot (o en cola) para escribirle a la persona en
                # vivo — igual que el supervisor, pero solo mientras NO tenga
                # asesor asignado (si ya tiene uno, ese asesor es quien la atiende).
                conversacion_id = payload.get("conversacion_id")
                info = _obtener_conversacion_ayuda_bot(conversacion_id)
                if not info:
                    await manager.enviar_a_asesor(asesor_id, {
                        "type": "ayuda_bot_error",
                        "payload": {"conversacion_id": conversacion_id,
                                    "motivo": "Esa conversación ya no está en el bot ni en cola."},
                    })
                else:
                    manager.registrar_escucha_asesor(asesor_id, conversacion_id)
                    await manager.enviar_a_asesor(asesor_id, {
                        "type": "ayuda_bot_historial",
                        "payload": {**info, "historial": _obtener_historial(conversacion_id)},
                    })

            elif tipo == "ayuda_bot_cerrar":
                manager.quitar_escucha_asesor(asesor_id, payload.get("conversacion_id"))

            elif tipo == "ayuda_bot_enviar":
                conversacion_id = payload.get("conversacion_id")
                texto = (payload.get("texto") or "").strip()
                if texto:
                    if not _obtener_conversacion_ayuda_bot(conversacion_id):
                        # Entre tanto la conversación pasó a otro asesor o se cerró.
                        manager.quitar_escucha_asesor(asesor_id, conversacion_id)
                        await manager.enviar_a_asesor(asesor_id, {
                            "type": "ayuda_bot_error",
                            "payload": {"conversacion_id": conversacion_id,
                                        "motivo": "La conversación ya no está en el bot ni en cola; no se envió el mensaje."},
                        })
                    else:
                        mensaje_id = _guardar_mensaje_asesor(conversacion_id, texto, "asesor", asesor_id)
                        await manager.enviar_a_widget(conversacion_id, {
                            "type": "mensaje_asesor",
                            "payload": {"texto": texto, "asesor_nombre": _nombre_display(asesor_id),
                                        "mensaje_id": mensaje_id, "respondido_a": None},
                        })
                        # Llega de vuelta al propio asesor (y a supervisores que
                        # estén escuchando) como evento de la conversación.
                        await manager.notificar_escuchas(conversacion_id, {
                            "type": "mensaje_conversacion",
                            "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "asesor",
                                        "texto": texto, "mensaje_id": mensaje_id, "respondido_a": None},
                        })

            elif tipo == "listar_asesores":
                # Para el selector de "Transferir a..." — otros asesores conectados,
                # con su carga actual, para elegir a quién pasarle la conversación.
                await supervisor.enviar_lista_asesores(asesor_id, manager, a_asesor=True, excluir_id=asesor_id)

            elif tipo == "transferir_conversacion":
                conversacion_id = payload.get("conversacion_id")
                asesor_destino_id = payload.get("asesor_destino_id")
                resultado = await asignacion.transferir_conversacion(
                    conversacion_id, asesor_destino_id, manager, asesor_solicitante_id=asesor_id,
                )
                if not resultado.get("ok"):
                    await manager.enviar_a_asesor(asesor_id, {
                        "type": "error_transferencia",
                        "payload": {"conversacion_id": conversacion_id, "motivo": resultado.get("motivo")},
                    })

    except WebSocketDisconnect:
        # Solo si este socket sigue siendo el vigente: si el asesor ya
        # reconectó (socket nuevo), este es el viejo enterándose tarde y no
        # debe desconectar ni "des-pausar" al nuevo.
        if manager.es_conexion_vigente_asesor(asesor_id, websocket):
            manager.disconnect_asesor(asesor_id)
            _marcar_desconectado(asesor_id)
            await supervisor.notificar_cambio_asesor(asesor_id, manager)


def _cambiar_cupo_asesor(asesor_id: int, nuevo_cupo: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE asesores SET max_conversaciones_simultaneas = %s WHERE id = %s",
                (nuevo_cupo, asesor_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _obtener_asesor_actual(conversacion_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asesor_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_connection(conn)


def _obtener_bandera(conversacion_id: int) -> str | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bandera FROM conversaciones WHERE id = %s", (conversacion_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        release_connection(conn)


def _obtener_aviso_inactividad_activo(conversacion_id: int) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT aviso_inactividad_activo FROM conversaciones WHERE id = %s", (conversacion_id,))
            row = cur.fetchone()
            return row[0] if row else True
    finally:
        release_connection(conn)


def _guardar_susurro(conversacion_id: int, supervisor_id: int, texto: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO susurros (conversacion_id, supervisor_id, mensaje, enviado_en) VALUES (%s, %s, %s, %s)",
                (conversacion_id, supervisor_id, texto, datetime.now(LIMA_TZ)),
            )
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, 'susurro', %s)""",
                (conversacion_id, supervisor_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _tomar_control(conversacion_id: int, supervisor_id: int):
    """Reasigna la conversación al supervisor (que en la tabla asesores es simplemente
    un registro con rol='supervisor') y devuelve quién la atendía antes, para avisarle."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asesor_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            asesor_anterior = cur.fetchone()[0]
            cur.execute(
                "UPDATE conversaciones SET asesor_id = %s WHERE id = %s",
                (supervisor_id, conversacion_id),
            )
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, 'toma_control', %s)""",
                (conversacion_id, supervisor_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
        return asesor_anterior
    finally:
        release_connection(conn)


def _guardar_respuesta_directa(conversacion_id: int, supervisor_id: int, texto: str,
                                respondido_a_id: int | None = None) -> int:
    metadata = {"respondido_a_id": respondido_a_id} if respondido_a_id else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, emisor_id, tipo_contenido, contenido, metadata, enviado_en)
                   VALUES (%s, 'supervisor', %s, 'texto', %s, %s, %s) RETURNING id""",
                (conversacion_id, supervisor_id, texto,
                 json.dumps(metadata) if metadata else None, datetime.now(LIMA_TZ)),
            )
            mensaje_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, 'respuesta_directa', %s)""",
                (conversacion_id, supervisor_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
        return mensaje_id
    finally:
        release_connection(conn)


def _registrar_escucha_db(conversacion_id: int, supervisor_id: int, tipo: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, %s, %s)""",
                (conversacion_id, supervisor_id, tipo, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _buscar_conversaciones(query: str, campana_ids: list | None = None) -> list:
    """Busca conversaciones (de cualquier estado: bot, en_cola, asignada o
    cerrada) por celular, DNI o nombre del cliente — usa ILIKE (contiene, sin
    importar mayúsculas) para no obligar a escribir el dato exacto completo."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            comodin = f"%{query}%"
            cur.execute(
                """
                SELECT c.id, u.nombre, u.telefono, u.dni, c.estado, cam.nombre,
                       c.iniciado_en, c.cerrado_en, c.entrada_cola, c.asignado_en,
                       a.nombre_display, et.nombre
                FROM conversaciones c
                JOIN usuarios_webchat u ON u.id = c.usuario_id
                LEFT JOIN campanas cam ON cam.id = c.campana_id
                LEFT JOIN asesores a ON a.id = c.asesor_id
                LEFT JOIN etiquetas_cierre et ON et.id = c.etiqueta_cierre_id
                WHERE (u.nombre ILIKE %s OR u.telefono ILIKE %s OR u.dni ILIKE %s)
                  AND (%s::int[] IS NULL OR c.campana_id = ANY(%s::int[]))
                ORDER BY c.iniciado_en DESC
                LIMIT 50
                """,
                (comodin, comodin, comodin, campana_ids, campana_ids),
            )
            resultado = []
            for (conv_id, nombre, telefono, dni, estado, campana, iniciado_en, cerrado_en,
                 entrada_cola, asignado_en, asesor_nombre, etiqueta) in cur.fetchall():
                resultado.append({
                    "conversacion_id": conv_id,
                    "usuario_nombre": nombre or f"Usuario {conv_id}",
                    "telefono": telefono,
                    "dni": dni,
                    "estado": estado,
                    "campana": campana,
                    "iniciado_en": iniciado_en.isoformat() if iniciado_en else None,
                    "cerrado_en": cerrado_en.isoformat() if cerrado_en else None,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "asesor_nombre": asesor_nombre,
                    "etiqueta": etiqueta,
                })
            return resultado
    finally:
        release_connection(conn)


def _buscar_conversaciones_bot(telefono: str, campana_ids: list | None = None) -> list:
    """Busca, SOLO por celular, conversaciones que TODAVÍA no llegan a un
    asesor (siguen en el flujo del bot o esperando en cola) — para que un
    supervisor pueda encontrar a alguien que está atorado navegando el
    chatbot y ayudarlo en vivo, sin esperar a que la conversación derive a
    un asesor."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            comodin = f"%{telefono}%"
            cur.execute(
                """
                SELECT c.id, u.nombre, u.telefono, u.dni, c.estado, c.iniciado_en, c.entrada_cola
                FROM conversaciones c
                JOIN usuarios_webchat u ON u.id = c.usuario_id
                WHERE c.estado IN ('bot', 'en_cola') AND u.telefono ILIKE %s
                  AND (%s::int[] IS NULL OR c.campana_id = ANY(%s::int[]))
                ORDER BY c.iniciado_en DESC
                LIMIT 50
                """,
                (comodin, campana_ids, campana_ids),
            )
            resultado = []
            for conv_id, nombre, telefono_u, dni, estado, iniciado_en, entrada_cola in cur.fetchall():
                resultado.append({
                    "conversacion_id": conv_id,
                    "usuario_nombre": nombre or f"Usuario {conv_id}",
                    "telefono": telefono_u,
                    "dni": dni,
                    "estado": estado,
                    "iniciado_en": iniciado_en.isoformat() if iniciado_en else None,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                })
            return resultado
    finally:
        release_connection(conn)


def _obtener_conversacion_ayuda_bot(conversacion_id) -> dict | None:
    """Datos básicos de una conversación SOLO si todavía está en el bot o en
    cola (o sea, sin asesor asignado) — es la condición para que un asesor
    pueda "ayudar en el bot". None si no existe o ya está asignada/cerrada."""
    try:
        conversacion_id = int(conversacion_id)
    except (TypeError, ValueError):
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.estado, COALESCE(NULLIF(u.nombre,''), c.variables_sesion->>'NOMBRE_USUARIO'), COALESCE(NULLIF(u.telefono,''), c.variables_sesion->>'CELULAR'), COALESCE(NULLIF(u.dni,''), c.variables_sesion->>'DNI')
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s""",
                (conversacion_id,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)
    if not row or row[0] not in ("bot", "en_cola"):
        return None
    estado, nombre, telefono, dni = row
    return {"conversacion_id": conversacion_id, "estado": estado,
            "usuario_nombre": nombre or f"Usuario {conversacion_id}",
            "telefono": telefono, "dni": dni}


async def _payload_permisos_supervisor(supervisor_id: int, alcance) -> dict:
    """Permisos que ve la consola. Si es máximo real, también el catálogo para «Ver como»."""
    real = await asyncio.to_thread(supervisor.obtener_alcance, supervisor_id, True)
    p = {"maximo": alcance is None,
         "campanas": [] if alcance is None else alcance["nombres"]}
    if real is None:
        p["puede_ver_como"] = True
        p["campanas_todas"] = await asyncio.to_thread(supervisor.obtener_campanas)
        p["ver_como"] = [] if alcance is None else list(alcance["ids"])
    return {"type": "permisos_supervisor", "payload": p}


@app.websocket("/ws/supervisor")
async def ws_supervisor(websocket: WebSocket, token: str):
    """wss://.../ws/supervisor?token=<jwt de un usuario con rol='supervisor'>"""
    supervisor_id = auth.decodificar_token(token)
    if supervisor_id is None:
        await websocket.close(code=4401)
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rol FROM asesores WHERE id = %s", (supervisor_id,))
            row = cur.fetchone()
    finally:
        release_connection(conn)

    if row is None or row[0] != "supervisor":
        await websocket.close(code=4403)  # autenticado, pero no es supervisor
        return

    alcance = supervisor.obtener_alcance(supervisor_id)
    await manager.connect_supervisor(supervisor_id, websocket, alcance)
    try:
        await manager.enviar_a_supervisor(supervisor_id, await _payload_permisos_supervisor(supervisor_id, alcance))
        await supervisor.enviar_estado_global(supervisor_id, manager)
        await supervisor.enviar_etiquetas(supervisor_id, manager, a_asesor=False)
        await supervisor.enviar_configuracion(supervisor_id, manager)
        await supervisor.enviar_respuestas_rapidas(supervisor_id, manager, a_asesor=False)

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}
            conversacion_id = payload.get("conversacion_id")

            # El alcance se vuelve a leer en cada acción: si el supervisor máximo le cambia las
            # campañas a alguien, vale desde su siguiente clic (sin esperar a que reconecte).
            alcance = await asyncio.to_thread(supervisor.obtener_alcance, supervisor_id)
            manager.supervisor_alcance[supervisor_id] = alcance

            if tipo == "ver_como":
                # Solo el supervisor máximo REAL puede simular el alcance de un supervisor de campaña.
                real = await asyncio.to_thread(supervisor.obtener_alcance, supervisor_id, True)
                if real is not None:
                    await websocket.send_json({"type": "error_permiso", "payload": {
                        "motivo": "Solo el supervisor máximo puede usar «Ver como».", "accion": tipo}})
                    continue
                ids = [i for i in (payload.get("campana_ids") or []) if isinstance(i, int)]
                supervisor.fijar_ver_como(supervisor_id, ids)
                alcance = await asyncio.to_thread(supervisor.obtener_alcance, supervisor_id)
                manager.supervisor_alcance[supervisor_id] = alcance
                await manager.enviar_a_supervisor(supervisor_id, await _payload_permisos_supervisor(supervisor_id, alcance))
                await supervisor.enviar_estado_global(supervisor_id, manager)
                await supervisor.enviar_etiquetas(supervisor_id, manager, a_asesor=False)
                await supervisor.enviar_configuracion(supervisor_id, manager)
                await supervisor.enviar_respuestas_rapidas(supervisor_id, manager, a_asesor=False)
                continue

            if alcance is not None:
                motivo = await asyncio.to_thread(supervisor.motivo_denegado, tipo, payload, alcance)
                if motivo:
                    await websocket.send_json({"type": "error_permiso",
                                               "payload": {"motivo": motivo, "accion": tipo,
                                                           "conversacion_id": conversacion_id}})
                    continue

            if tipo in _TIPOS_FIABLES_STAFF and payload.get("cid"):
                if not _registrar_cid_widget(f"supervisor:{supervisor_id}", str(payload["cid"])[:64]):
                    await websocket.send_json({"type": "mensaje_propio_guardado", "payload": {
                        "conversacion_id": conversacion_id, "cid": payload.get("cid"), "dup": True}})
                    continue

            if tipo == "escuchar_conversacion":
                manager.registrar_escucha(supervisor_id, conversacion_id)
                if not payload.get("reconexion"):  # re-enganche tras caída: no es una escucha nueva
                    _registrar_escucha_db(conversacion_id, supervisor_id, "escucha_inicio")
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "historial_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "historial": _obtener_historial(conversacion_id),
                                "campana_id": _campana_de_conversacion(conversacion_id),
                                "bandera": _obtener_bandera(conversacion_id),
                                "aviso_inactividad_activo": _obtener_aviso_inactividad_activo(conversacion_id)},
                })

            elif tipo == "dejar_de_escuchar":
                manager.quitar_escucha(supervisor_id, conversacion_id)
                _registrar_escucha_db(conversacion_id, supervisor_id, "escucha_fin")

            elif tipo == "enviar_susurro":
                texto = payload.get("texto")
                asesor_actual = _obtener_asesor_actual(conversacion_id)
                _guardar_susurro(conversacion_id, supervisor_id, texto)
                if asesor_actual:
                    await manager.enviar_a_asesor(asesor_actual, {
                        "type": "susurro_recibido",
                        "payload": {"conversacion_id": conversacion_id, "texto": texto,
                                    "supervisor_nombre": _nombre_display(supervisor_id)},
                    })

            elif tipo == "tomar_control":
                asesor_anterior = _tomar_control(conversacion_id, supervisor_id)
                if asesor_anterior:
                    await manager.enviar_a_asesor(asesor_anterior, {
                        "type": "supervisor_tomo_control",
                        "payload": {"conversacion_id": conversacion_id},
                    })
                    await supervisor.notificar_cambio_asesor(asesor_anterior, manager)

            elif tipo == "responder_directo":
                texto = payload.get("texto")
                respondido_a_id = payload.get("respondido_a_id")
                mensaje_id = _guardar_respuesta_directa(conversacion_id, supervisor_id, texto, respondido_a_id)
                respondido_a = _obtener_preview_mensaje(respondido_a_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensaje_asesor",
                    "payload": {"texto": texto,
                                # Sin nombre si el supervisor apagó "Nombre visible": el widget no pone prefijo.
                                "asesor_nombre": _nombre_display(supervisor_id) if payload.get("mostrar_nombre", True) else "",
                                "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                })
                await websocket.send_json({
                    "type": "mensaje_propio_guardado",
                    "payload": {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id, "cid": payload.get("cid")},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "supervisor", "texto": texto,
                                "mensaje_id": mensaje_id, "respondido_a": respondido_a},
                })
                # Si la conversación sigue asignada a un asesor (no se le quitó el
                # control), que también vea en su consola lo que el supervisor
                # le contestó al usuario en su nombre — si no, su vista queda
                # desactualizada respecto a lo que el usuario ya recibió.
                asesor_actual = _obtener_asesor_actual(conversacion_id)
                if asesor_actual:
                    await manager.enviar_a_asesor(asesor_actual, {
                        "type": "mensaje_supervisor_directo",
                        "payload": {"conversacion_id": conversacion_id, "texto": texto,
                                    "supervisor_nombre": _nombre_display(supervisor_id), "mensaje_id": mensaje_id},
                    })

            elif tipo == "responder_directo_archivo":
                ruta = payload.get("ruta")
                mensaje_id = _guardar_mensaje_staff_archivo(conversacion_id, ruta, "supervisor", supervisor_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "archivo_asesor",
                    "payload": {"ruta": ruta, "asesor_nombre": _nombre_display(supervisor_id), "mensaje_id": mensaje_id},
                })
                await websocket.send_json({
                    "type": "mensaje_propio_guardado",
                    "payload": {"conversacion_id": conversacion_id, "mensaje_id": mensaje_id, "cid": payload.get("cid")},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "supervisor",
                                "texto": "📎 Imagen enviada", "ruta": ruta, "mensaje_id": mensaje_id},
                })
                asesor_actual = _obtener_asesor_actual(conversacion_id)
                if asesor_actual:
                    await manager.enviar_a_asesor(asesor_actual, {
                        "type": "archivo_supervisor_directo",
                        "payload": {"conversacion_id": conversacion_id, "ruta": ruta,
                                    "supervisor_nombre": _nombre_display(supervisor_id)},
                    })

            elif tipo == "editar_mensaje":
                resultado = _editar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                             "supervisor", supervisor_id, payload.get("texto"))
                if resultado:
                    await _difundir_mensaje_modificado("mensaje_editado", conversacion_id, resultado)

            elif tipo == "eliminar_mensaje":
                resultado = _eliminar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                               "supervisor", supervisor_id, _nombre_display(supervisor_id))
                if resultado:
                    await _difundir_mensaje_modificado("mensaje_eliminado", conversacion_id, resultado)

            elif tipo == "reaccionar_mensaje":
                resultado = _reaccionar_mensaje(payload.get("mensaje_id"), conversacion_id,
                                                 f"supervisor:{supervisor_id}", payload.get("emoji"))
                if resultado:
                    await _difundir_mensaje_modificado("mensaje_reaccion", conversacion_id, resultado)

            elif tipo == "cambiar_bandera":
                resultado = _cambiar_bandera(conversacion_id, payload.get("bandera"), asesor_id_esperado=None)
                if resultado:
                    await _difundir_bandera_cambiada(conversacion_id, resultado["bandera"], resultado["asesor_id"])

            elif tipo == "cambiar_aviso_inactividad":
                activo = bool(payload.get("activo"))
                _set_aviso_inactividad_conversacion(conversacion_id, activo, asesor_id_esperado=None)
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "aviso_inactividad_cambiado",
                    "payload": {"conversacion_id": conversacion_id, "activo": activo},
                })
                asesor_dueno = _obtener_asesor_actual(conversacion_id)
                if asesor_dueno:
                    await manager.enviar_a_asesor(asesor_dueno, {
                        "type": "aviso_inactividad_cambiado",
                        "payload": {"conversacion_id": conversacion_id, "activo": activo},
                    })

            elif tipo == "cambiar_cupo":
                asesor_id_objetivo = payload.get("asesor_id")
                nuevo_cupo = payload.get("nuevo_cupo")
                _cambiar_cupo_asesor(asesor_id_objetivo, nuevo_cupo)
                await supervisor.notificar_cambio_asesor(asesor_id_objetivo, manager)

            elif tipo == "crear_etiqueta":
                supervisor.crear_etiqueta(payload.get("nombre"), campana_ids=payload.get("campana_ids"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "editar_etiqueta":
                supervisor.editar_etiqueta(payload.get("id"), nombre=payload.get("nombre"),
                                            activo=payload.get("activo"), campana_ids=payload.get("campana_ids"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "eliminar_etiqueta":
                supervisor.eliminar_etiqueta(payload.get("id"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "crear_respuesta_rapida":
                titulo = (payload.get("titulo") or "").strip()
                cuerpo = (payload.get("cuerpo") or "").strip()
                campana_id = payload.get("campana_id")
                if not titulo or not cuerpo or not campana_id:
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_configuracion",
                        "payload": {"motivo": "Falta título, cuerpo o campaña para la respuesta rápida."},
                    })
                else:
                    supervisor.crear_respuesta_rapida(titulo, cuerpo, campana_id)
                    await supervisor.notificar_respuestas_rapidas_a_todos(manager)

            elif tipo == "editar_respuesta_rapida":
                supervisor.editar_respuesta_rapida(
                    payload.get("id"), titulo=payload.get("titulo"), cuerpo=payload.get("cuerpo"),
                    campana_id=payload.get("campana_id"), activo=payload.get("activo"),
                )
                await supervisor.notificar_respuestas_rapidas_a_todos(manager)

            elif tipo == "eliminar_respuesta_rapida":
                supervisor.eliminar_respuesta_rapida(payload.get("id"))
                await supervisor.notificar_respuestas_rapidas_a_todos(manager)

            elif tipo == "guardar_mensaje_cierre":
                supervisor.guardar_configuracion("mensaje_cierre_conversacion", payload.get("texto", ""))

            elif tipo == "guardar_mensaje_bienvenida":
                supervisor.guardar_configuracion("mensaje_bienvenida_asesor", payload.get("texto", ""))

            elif tipo == "guardar_inactividad_minutos":
                try:
                    minutos = int(payload.get("minutos"))
                    if minutos < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_configuracion",
                        "payload": {"motivo": "Ingresa un número de minutos válido (mayor a 0)."},
                    })
                else:
                    supervisor.guardar_configuracion("inactividad_minutos", str(minutos))
                    await supervisor.enviar_configuracion(supervisor_id, manager)

            elif tipo == "refrescar_estado":
                await supervisor.enviar_estado_global(supervisor_id, manager)

            elif tipo == "obtener_asesores_admin":
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "asesores_admin",
                    "payload": {"asesores": supervisor.listar_asesores_admin(),
                                "campanas": supervisor.obtener_campanas()},
                })

            elif tipo == "crear_asesor":
                supervisor.crear_asesor(
                    payload.get("usuario"), payload.get("nombre"), payload.get("nombre_display"),
                    payload.get("password"), payload.get("rol", "asesor"), payload.get("campana_ids", []),
                )
                await supervisor.notificar_asesores_admin_a_todos(manager)
                await supervisor.notificar_estado_global_a_todos(manager)

            elif tipo == "editar_asesor":
                supervisor.editar_asesor(
                    payload.get("asesor_id"), nombre_display=payload.get("nombre_display"),
                    rol=payload.get("rol"), activo=payload.get("activo"), cupo=payload.get("cupo"),
                    campana_ids=payload.get("campana_ids"),
                )
                await supervisor.notificar_asesores_admin_a_todos(manager)
                await supervisor.notificar_estado_global_a_todos(manager)

            elif tipo == "editar_campanas_asesor":
                # Supervisor de campaña (o máximo): agrega/quita campañas a un asesor. No crea usuarios ni toca
                # otros datos. Los permisos ya se revisaron arriba (supervisor.motivo_denegado).
                resultado = await asyncio.to_thread(supervisor.editar_campanas_asesor, payload.get("asesor_id"),
                                                    payload.get("campana_ids") or [], alcance)
                await manager.enviar_a_supervisor(supervisor_id, {"type": "resultado_campanas_asesor", "payload": resultado})
                if resultado.get("ok"):
                    logger.info("Supervisor %s cambió las campañas del asesor %s a %s", supervisor_id,
                                payload.get("asesor_id"), payload.get("campana_ids"))
                    await supervisor.notificar_asesores_admin_a_todos(manager)
                    await supervisor.notificar_estado_global_a_todos(manager)

            elif tipo == "quitar_2fa_asesor":
                totp.desactivar(payload.get("asesor_id"))
                await supervisor.notificar_asesores_admin_a_todos(manager)

            elif tipo == "resetear_password_asesor":
                supervisor.resetear_password_asesor(payload.get("asesor_id"), payload.get("password"))

            elif tipo == "cerrar_conversacion":
                etiqueta_cierre_id = payload.get("etiqueta_cierre_id")
                await _cerrar_y_notificar(conversacion_id, etiqueta_cierre_id, "supervisor")
                manager.quitar_escucha(supervisor_id, conversacion_id)

            elif tipo == "buscar_conversaciones":
                consulta = (payload.get("query") or "").strip()
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "resultados_busqueda",
                    "payload": {"query": consulta, "resultados": _buscar_conversaciones(
                        consulta, None if alcance is None else alcance["ids"]) if consulta else []},
                })

            elif tipo == "buscar_conversaciones_bot":
                telefono = (payload.get("telefono") or "").strip()
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "resultados_busqueda_bot",
                    "payload": {
                        "telefono": telefono,
                        "resultados": _buscar_conversaciones_bot(
                            telefono, None if alcance is None else alcance["ids"]) if telefono else [],
                    },
                })

            elif tipo == "ver_conversacion":
                # Vista de solo lectura para una conversación encontrada en el
                # buscador que YA está cerrada (o en bot/en_cola) — no usa el
                # flujo de "escuchar" porque esas no tienen susurro/control/cierre.
                conv_id = payload.get("conversacion_id")
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "detalle_conversacion",
                    "payload": {"conversacion_id": conv_id, "historial": _obtener_historial(conv_id)},
                })

            elif tipo == "listar_asesores":
                # Para el selector de "Transferir a..." desde el panel de escucha.
                await supervisor.enviar_lista_asesores(supervisor_id, manager, a_asesor=False)

            elif tipo in ("listar_campanas_admin", "crear_campana", "editar_campana"):
                # Solo el supervisor máximo (el resto ya fue rechazado arriba).
                resultado = {"ok": True}
                if tipo == "crear_campana":
                    resultado = await asyncio.to_thread(
                        supervisor.crear_campana, payload.get("nombre"),
                        payload.get("mensaje_fuera_horario"), payload.get("dias"))
                elif tipo == "editar_campana":
                    resultado = await asyncio.to_thread(
                        supervisor.editar_campana, payload.get("id"),
                        payload.get("nombre"), payload.get("activo"), payload.get("encuesta"))
                if not resultado.get("ok"):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_campanas", "payload": {"motivo": resultado.get("motivo")}})
                else:
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "campanas_admin",
                        "payload": {"campanas": await asyncio.to_thread(supervisor.listar_campanas_admin),
                                    "creada": resultado.get("codigo")}})
                    if tipo != "listar_campanas_admin":
                        # Que el resto de pantallas (asesores, horarios, feriados, reporte) vean la campaña ya.
                        await supervisor.notificar_asesores_admin_a_todos(manager)
                        await manager.enviar_a_todos_supervisores({
                            "type": "horarios_actuales",
                            "payload": {"campanas": await asyncio.to_thread(supervisor.listar_horarios)}})
                        await supervisor.notificar_estado_global_a_todos(manager)

            elif tipo == "listar_horarios":
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "horarios_actuales",
                    "payload": {"campanas": supervisor.listar_horarios()},
                })

            elif tipo in ("listar_feriados", "crear_feriado", "eliminar_feriados"):
                if tipo == "crear_feriado":
                    resultado = supervisor.crear_feriado(
                        payload.get("fecha"), payload.get("descripcion"), payload.get("mensaje"),
                        payload.get("campana_ids") or [], supervisor_id)
                elif tipo == "eliminar_feriados":
                    resultado = supervisor.eliminar_feriados(payload.get("ids") or [])
                else:
                    resultado = {"ok": True}
                if resultado.get("ok"):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "feriados_actuales",
                        "payload": {"feriados": supervisor.listar_feriados(),
                                    "campanas": supervisor.obtener_campanas()},
                    })
                else:
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_feriados", "payload": {"motivo": resultado.get("motivo")}})

            elif tipo == "guardar_horarios":
                resultado = supervisor.guardar_horarios(
                    payload.get("campana_codigo"),
                    payload.get("mensaje_fuera_horario"),
                    payload.get("dias") or [],
                )
                if resultado.get("ok"):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "horarios_actuales",
                        "payload": {"campanas": supervisor.listar_horarios()},
                    })
                else:
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_horarios",
                        "payload": {"motivo": resultado.get("motivo")},
                    })

            elif tipo == "transferir_conversacion":
                asesor_destino_id = payload.get("asesor_destino_id")
                resultado = await asignacion.transferir_conversacion(conversacion_id, asesor_destino_id, manager)
                if not resultado.get("ok"):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_transferencia",
                        "payload": {"conversacion_id": conversacion_id, "motivo": resultado.get("motivo")},
                    })

            elif tipo in ("pausar_conversacion", "reanudar_conversacion"):
                # El supervisor pasa la conversación a la bandeja "Pausados" del asesor que la
                # atiende (o la devuelve). No cambia estado ni asesor, igual que la pausa del asesor.
                pausar = tipo == "pausar_conversacion"
                dueno = _obtener_asesor_actual(conversacion_id)
                cambio = _set_pausa_conversacion(conversacion_id, dueno, pausar) if dueno else False
                if cambio:
                    await manager.enviar_a_asesor(dueno, {
                        "type": "conversacion_pausada" if pausar else "conversacion_reactivada",
                        "payload": {"conversacion_id": conversacion_id, "ok": True},
                    })
                    await supervisor.notificar_cambio_asesor(dueno, manager)

            elif tipo == "asignar_conversacion_manual":
                # Desde el panel de "Sin asignar" — conversaciones en_cola que
                # ningún asesor había tomado (por ejemplo, porque nadie de esa
                # campaña estaba conectado cuando el bot las derivó).
                asesor_destino_id = payload.get("asesor_destino_id")
                resultado = await asignacion.asignar_manual(conversacion_id, asesor_destino_id, manager)
                if not resultado.get("ok"):
                    await manager.enviar_a_supervisor(supervisor_id, {
                        "type": "error_asignacion_manual",
                        "payload": {"conversacion_id": conversacion_id, "motivo": resultado.get("motivo")},
                    })

            elif tipo == "buscar_usuarios_sofi":
                # Mantenimiento de pacientes (sofi_usuarios): solo supervisor.
                resultado = await asyncio.to_thread(usuarios_sofi.buscar, payload.get("query"))
                await manager.enviar_a_supervisor(supervisor_id, {"type": "resultados_usuarios_sofi", "payload": resultado})

            elif tipo == "guardar_usuario_sofi":
                resultado = await asyncio.to_thread(
                    usuarios_sofi.guardar, payload.get("doi"), payload.get("cambios"), supervisor_id)
                await manager.enviar_a_supervisor(supervisor_id, {"type": "resultado_guardar_usuario_sofi", "payload": resultado})

            elif tipo == "enviar_sms_invitacion":
                resultado = await asyncio.to_thread(_enviar_sms_invitacion, payload.get("celular"), supervisor_id, payload.get("texto"),
                                                    payload.get("campana_id"), payload.get("asesor_destino_id"))
                await manager.enviar_a_supervisor(supervisor_id, {"type": "resultado_sms_invitacion", "payload": resultado})

            elif tipo == "enviar_archivo_sms":
                resultado = await asyncio.to_thread(_enviar_archivo_sms, payload.get("celular"), supervisor_id, "supervisor",
                                                    payload.get("ruta"), payload.get("nombre"))
                await manager.enviar_a_supervisor(supervisor_id, {"type": "resultado_archivo_sms", "payload": resultado})

            elif tipo == "destinos_sms_directo":
                await manager.enviar_a_supervisor(supervisor_id, {"type": "destinos_sms_directo",
                                                                  "payload": await asyncio.to_thread(_destinos_sms_directo, supervisor_id)})

            elif tipo == "texto_sms_invitacion":
                texto = await asyncio.to_thread(_texto_sms_invitacion)
                await manager.enviar_a_supervisor(supervisor_id, {"type": "texto_sms_invitacion", "payload": {"texto": texto, "max": SMS_INVITACION_MAX_CARACTERES}})

            elif tipo == "enviar_sms_saliente":
                resultado = _enviar_sms_saliente(conversacion_id, supervisor_id)
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "resultado_sms_saliente",
                    "payload": {"conversacion_id": conversacion_id, **resultado},
                })

    except WebSocketDisconnect:
        # Solo si este socket sigue vigente: si el supervisor ya reconectó, el viejo
        # no debe borrar la conexión ni las escuchas del nuevo.
        if manager.es_conexion_vigente_supervisor(supervisor_id, websocket):
            manager.disconnect_supervisor(supervisor_id)
            supervisor.fijar_ver_como(supervisor_id, None)  # "Ver como" no sobrevive a la sesión
