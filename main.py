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
import uuid
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
import google.auth
import google.auth.transport.requests
from google.cloud import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

from db import get_connection, release_connection
from bot_engine import iniciar_conversacion, recibir_entrada_usuario
import asignacion
import auth
import supervisor

LIMA_TZ = timezone(timedelta(hours=-5))

app = FastAPI(title="webchat-api")

# Permite que el widget, la consola de asesor y la de supervisor (hoy testers
# HTML locales, luego el sitio real de Sanitas) hablen con este servicio.
# TODO: restringir allow_origins a los dominios reales antes de producción
# (hoy "*" es correcto para pruebas locales con archivos file://).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Usado por Cloud Run para healthchecks; también sirve para probar la conexión a BD a mano."""
    try:
        conn = get_connection()
        release_connection(conn)
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        return {"status": "ok", "db": f"error: {e}"}


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


@app.get("/asesor")
def consola_asesor():
    """Consola de asesor — URL fija que cada asesor guarda en favoritos."""
    return FileResponse("static/asesor.html", media_type="text/html")


@app.get("/supervisor")
def consola_supervisor():
    """Consola de supervisor — URL fija."""
    return FileResponse("static/supervisor.html", media_type="text/html")


@app.get("/editor")
def consola_editor():
    """Editor de textos y orden de botones del bot — URL fija."""
    return FileResponse("static/editor.html", media_type="text/html")


@app.post("/editor/subir-imagen")
async def subir_imagen_editor(file: UploadFile = File(...), authorization: str | None = Header(None)):
    """
    Sube una imagen de contenido (menús del bot, bienvenida, etc.) a un bucket
    DISTINTO al de las recetas — este es de lectura pública y permanente,
    porque son imágenes de marca/contenido, no datos de salud de un paciente.
    Necesita la variable de entorno GCS_CONTENIDO_BUCKET.
    """
    _verificar_editor(authorization)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes")

    contenido = await file.read()
    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Imagen muy grande (máximo 5 MB)")

    extension = (file.filename or "").split(".")[-1] if "." in (file.filename or "") else "jpg"
    nombre_archivo = f"bot-content/{uuid.uuid4()}.{extension}"

    client = storage.Client()
    bucket_nombre = os.environ.get("GCS_CONTENIDO_BUCKET", "sanitas-webchat-contenido")
    bucket = client.bucket(bucket_nombre)
    blob = bucket.blob(nombre_archivo)
    blob.upload_from_string(contenido, content_type=file.content_type)

    return {"url": f"https://storage.googleapis.com/{bucket_nombre}/{nombre_archivo}"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Recibe un archivo (imagen o PDF de receta) y lo sube a Cloud Storage.
    Necesita la variable de entorno GCS_BUCKET con el nombre del bucket
    (privado — sin acceso público, ver notas de deploy).

    Devuelve una URL FIRMADA con expiración de 1 hora (no una URL pública
    permanente) porque el archivo puede contener datos de salud — procesar-receta
    la descarga casi de inmediato, así que 1 hora es tiempo de sobra y evita
    dejar recetas médicas accesibles indefinidamente por URL.
    """
    extension = (file.filename or "").split(".")[-1] if "." in (file.filename or "") else "bin"
    nombre_archivo = f"webchat/{uuid.uuid4()}.{extension}"

    client = storage.Client()
    bucket = client.bucket(os.environ["GCS_BUCKET"])
    blob = bucket.blob(nombre_archivo)
    contenido = await file.read()
    blob.upload_from_string(contenido, content_type=file.content_type)

    # Cloud Run no tiene una llave privada local para firmar — hay que pasarle
    # explícitamente el email + access_token para que use la IAM Credentials API
    # (requiere el rol roles/iam.serviceAccountTokenCreator sobre sí mismo).
    credentials, _ = google.auth.default()
    credentials.refresh(google.auth.transport.requests.Request())
    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET",
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    return {"url": url}


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
        self.escuchas: dict[int, set] = {}                       # conversacion_id -> {supervisor_id, ...}

    async def connect_widget(self, conversacion_id: int, ws: WebSocket):
        await ws.accept()
        self.widget_connections[conversacion_id] = ws

    def reasignar_widget(self, conversacion_id: int, ws: WebSocket):
        """Igual que connect_widget pero SIN aceptar de nuevo — para cuando se
        reusa la misma conexión ya aceptada bajo un conversacion_id nuevo
        (iniciar_nueva_conversacion / reiniciar_datos)."""
        self.widget_connections[conversacion_id] = ws

    def disconnect_widget(self, conversacion_id: int):
        self.widget_connections.pop(conversacion_id, None)

    async def connect_asesor(self, asesor_id: int, ws: WebSocket):
        await ws.accept()
        self.asesor_connections[asesor_id] = ws

    def disconnect_asesor(self, asesor_id: int):
        self.asesor_connections.pop(asesor_id, None)

    async def connect_supervisor(self, supervisor_id: int, ws: WebSocket):
        await ws.accept()
        self.supervisor_connections[supervisor_id] = ws

    def disconnect_supervisor(self, supervisor_id: int):
        self.supervisor_connections.pop(supervisor_id, None)
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

    async def enviar_a_asesor(self, asesor_id: int, evento: dict):
        ws = self.asesor_connections.get(asesor_id)
        if ws:
            await ws.send_json(evento)

    async def enviar_a_supervisor(self, supervisor_id: int, evento: dict):
        ws = self.supervisor_connections.get(supervisor_id)
        if ws:
            await ws.send_json(evento)

    async def enviar_a_todos_supervisores(self, evento: dict):
        for ws in self.supervisor_connections.values():
            await ws.send_json(evento)

    async def enviar_a_todos_asesores(self, evento: dict):
        for ws in self.asesor_connections.values():
            await ws.send_json(evento)

    async def notificar_escuchas(self, conversacion_id: int, evento: dict):
        for supervisor_id in self.escuchas.get(conversacion_id, set()):
            await self.enviar_a_supervisor(supervisor_id, evento)


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


async def _procesar_eventos_bot(conversacion_id: int, websocket: WebSocket, eventos: list):
    """Manda los eventos del bot al widget y, si alguno es 'en_cola', dispara
    el intento de asignación inmediata."""
    for evento in eventos:
        await websocket.send_json(evento)
        if evento.get("type") == "en_cola":
            await asignacion.intentar_asignar_conversacion(conversacion_id, manager)


def _obtener_historial(conversacion_id: int) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT emisor_tipo, tipo_contenido, contenido, enviado_en, leido_en FROM mensajes
                   WHERE conversacion_id = %s ORDER BY enviado_en ASC""",
                (conversacion_id,),
            )
            return [
                {"emisor_tipo": e, "tipo_contenido": t, "contenido": c, "enviado_en": ts.isoformat(),
                 "leido": leido is not None}
                for e, t, c, ts, leido in cur.fetchall()
            ]
    finally:
        release_connection(conn)


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
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nombre, telefono FROM usuarios_webchat WHERE sesion_token = %s",
                (sesion_token,),
            )
            row = cur.fetchone()
            return {"nombre": row[0], "telefono": row[1]} if row else {}
    finally:
        release_connection(conn)


def _limpiar_datos_usuario(sesion_token: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE usuarios_webchat SET nombre = NULL, telefono = NULL WHERE sesion_token = %s",
                (sesion_token,),
            )
        conn.commit()
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


def _guardar_mensaje_usuario(conversacion_id: int, texto: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'usuario', 'texto', %s, %s)""",
                (conversacion_id, texto, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _guardar_mensaje_usuario_archivo(conversacion_id: int, url: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'usuario', 'archivo', %s, %s)""",
                (conversacion_id, url, datetime.now(LIMA_TZ)),
            )
        conn.commit()
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
    conversacion_id, estado_actual, es_nueva = _crear_o_reanudar_conversacion(nuevo_token)
    await manager.connect_widget(conversacion_id, websocket)

    async def arrancar(conv_id: int, es_nueva_flag: bool, estado: str, forzar_pedir_datos: bool = False):
        if es_nueva_flag:
            datos = None if forzar_pedir_datos else _obtener_datos_usuario(nuevo_token)
            if datos and datos.get("nombre") and datos.get("telefono"):
                # Ya conocemos a esta persona (mismo navegador) — se salta
                # las preguntas de celular/nombre e inicia directo en el menú.
                variables_iniciales = {"CELULAR": datos["telefono"], "NOMBRE_USUARIO": datos["nombre"]}
                eventos = iniciar_conversacion(conv_id, nodo_inicial="webchat_menu_principal",
                                                variables_iniciales=variables_iniciales)
            else:
                eventos = iniciar_conversacion(conv_id, nodo_inicial="webchat_pide_celular")
            await _procesar_eventos_bot(conv_id, websocket, eventos)
        else:
            # Reanudando: manda el historial para reconstruir la pantalla,
            # y el estado actual (en cola / asignado) si corresponde.
            historial = _obtener_historial(conv_id)
            await websocket.send_json({"type": "historial", "payload": {"mensajes": historial}})
            if estado == "asignada":
                nombre = _nombre_asesor_de_conversacion(conv_id)
                if nombre:
                    await websocket.send_json({"type": "asignado", "payload": {"asesor_nombre": nombre}})
            elif estado == "en_cola":
                await websocket.send_json({"type": "en_cola", "payload": {}})

    try:
        await websocket.send_json({
            "type": "sesion_iniciada",
            "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id},
        })
        try:
            await arrancar(conversacion_id, es_nueva, estado_actual)
        except Exception:
            logger.exception("Error en el arranque inicial de la conversación %s", conversacion_id)
            await websocket.send_json({
                "type": "mensaje_bot",
                "payload": {"texto": "⚠️ Ocurrió un error inesperado."},
            })
            await websocket.send_json({"type": "conversacion_cerrada", "payload": {"motivo": "error"}})

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}

            try:
                if tipo == "iniciar_nueva_conversacion":
                    # La anterior ya está cerrada (o no existía) — crea/reanuda y arranca de nuevo
                    # sobre la MISMA conexión, sin pedir de nuevo celular/nombre si ya se conocen.
                    manager.disconnect_widget(conversacion_id)
                    conversacion_id, estado_actual, es_nueva = _crear_o_reanudar_conversacion(nuevo_token)
                    manager.reasignar_widget(conversacion_id, websocket)
                    await websocket.send_json({
                        "type": "sesion_iniciada",
                        "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id},
                    })
                    await arrancar(conversacion_id, es_nueva, estado_actual)
                    continue

                if tipo == "reiniciar_datos":
                    _limpiar_datos_usuario(nuevo_token)
                    manager.disconnect_widget(conversacion_id)
                    conversacion_id, estado_actual, es_nueva = _crear_o_reanudar_conversacion(nuevo_token)
                    manager.reasignar_widget(conversacion_id, websocket)
                    await websocket.send_json({
                        "type": "sesion_iniciada",
                        "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id},
                    })
                    await arrancar(conversacion_id, es_nueva, estado_actual, forzar_pedir_datos=True)
                    continue

                estado, asesor_id = _obtener_estado_conversacion(conversacion_id)

                if (tipo == "mensaje_usuario" and estado in ("cerrada", "bot")
                        and (payload.get("texto") or "").strip().lower() in ("hola", "inicio", "menu", "menú")):
                    # Mismo mecanismo que el botón "Iniciar nueva conversación" —
                    # esta es la palabra clave que en WhatsApp manejaba Chattigo.
                    # Funciona en cualquier punto del bot (no solo cerrada), pero
                    # NO interrumpe una conversación ya en cola o con un asesor real.
                    if estado == "cerrada":
                        manager.disconnect_widget(conversacion_id)
                        conversacion_id, estado_actual, es_nueva = _crear_o_reanudar_conversacion(nuevo_token)
                        manager.reasignar_widget(conversacion_id, websocket)
                        await websocket.send_json({
                            "type": "sesion_iniciada",
                            "payload": {"sesion_token": nuevo_token, "conversacion_id": conversacion_id},
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
                    _marcar_leidos_por_usuario(conversacion_id)
                    if asesor_id:
                        await manager.enviar_a_asesor(asesor_id, {
                            "type": "mensajes_leidos", "payload": {"conversacion_id": conversacion_id},
                        })
                    continue

                if estado == "bot":
                    # Todavía navegando el árbol de bot_menus
                    if tipo == "mensaje_usuario":
                        eventos = recibir_entrada_usuario(conversacion_id, texto=payload.get("texto"))
                    elif tipo == "click_opcion":
                        eventos = recibir_entrada_usuario(conversacion_id, opcion_id=payload.get("opcion_codigo"))
                    elif tipo == "archivo_subido":
                        eventos = recibir_entrada_usuario(conversacion_id, archivo_url=payload.get("url"))
                    elif tipo == "formulario_completado":
                        eventos = recibir_entrada_usuario(conversacion_id, valores=payload.get("valores"))
                    else:
                        eventos = []
                    if tipo in ("mensaje_usuario", "click_opcion", "archivo_subido", "formulario_completado"):
                        await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {}})
                    await _procesar_eventos_bot(conversacion_id, websocket, eventos)

                elif tipo == "mensaje_usuario":
                    # Ya derivado (en_cola o asignada): el mensaje va directo al asesor, no al bot
                    texto = payload.get("texto")
                    _guardar_mensaje_usuario(conversacion_id, texto)
                    if asesor_id:
                        await manager.enviar_a_asesor(asesor_id, {
                            "type": "mensaje_usuario",
                            "payload": {"conversacion_id": conversacion_id, "texto": texto},
                        })
                        # "Recibido" real (✓✓ gris) solo cuando de verdad llegó a un
                        # asesor conectado — si sigue en cola, se queda en un solo ✓.
                        await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {}})
                    await manager.notificar_escuchas(conversacion_id, {
                        "type": "mensaje_conversacion",
                        "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "usuario", "texto": texto},
                    })

                elif tipo == "archivo_subido":
                    # Igual que mensaje_usuario pero para un archivo: al asesor le llega
                    # como link clicable (se abre en pestaña aparte), no como texto plano.
                    url = payload.get("url")
                    _guardar_mensaje_usuario_archivo(conversacion_id, url)
                    if asesor_id:
                        await manager.enviar_a_asesor(asesor_id, {
                            "type": "archivo_usuario",
                            "payload": {"conversacion_id": conversacion_id, "url": url},
                        })
                        await websocket.send_json({"type": "mensaje_recibido_servidor", "payload": {}})
                    await manager.notificar_escuchas(conversacion_id, {
                        "type": "mensaje_conversacion",
                        "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "usuario", "texto": f"📎 {url}"},
                    })
                    # Si sigue en_cola (sin asesor_id todavía), el mensaje queda guardado
                    # en el historial y el asesor lo verá al asignarse — no se pierde,
                    # pero no hay entrega en vivo hasta que haya asignación.

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
        manager.disconnect_widget(conversacion_id)


def _marcar_conectado(asesor_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
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


def _cambiar_pausa_asesor(asesor_id: int, pausado: bool):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE asesores SET en_pausa = %s WHERE id = %s", (pausado, asesor_id))
            if pausado:
                cur.execute(
                    "INSERT INTO asesor_pausas (asesor_id, inicio) VALUES (%s, %s)",
                    (asesor_id, datetime.now(LIMA_TZ)),
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


def _cerrar_conversacion(conversacion_id: int, etiqueta_cierre_id, cerrado_por: str = "asesor"):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE conversaciones
                   SET estado = 'cerrada', cerrado_en = %s, cerrado_por = %s,
                       etiqueta_cierre_id = %s
                   WHERE id = %s""",
                (datetime.now(LIMA_TZ), cerrado_por, etiqueta_cierre_id, conversacion_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def _guardar_mensaje_asesor(conversacion_id: int, texto: str, emisor_tipo: str = "asesor"):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, %s, 'texto', %s, %s)""",
                (conversacion_id, emisor_tipo, texto, datetime.now(LIMA_TZ)),
            )
        conn.commit()
    finally:
        release_connection(conn)


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


@app.post("/login")
def login(datos: LoginRequest):
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

    if row is None or not auth.verify_password(datos.password, row[1]):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    asesor_id, _, nombre_display, rol = row
    token = auth.crear_token(asesor_id)
    return {"token": token, "asesor_id": asesor_id, "nombre_display": nombre_display, "rol": rol}


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


class NodoUpdate(BaseModel):
    texto: str | None = None
    opciones: list | None = None
    imagen_url: str | None = None


@app.get("/editor/nodos")
def listar_nodos_editor(authorization: str | None = Header(None)):
    """Todos los nodos de bot_menus que tienen texto y/o opciones editables,
    para la consola /editor."""
    _verificar_editor(authorization)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT bm.codigo, bm.tipo, bm.texto, bm.opciones, camp.nombre, bm.imagen_url
                   FROM bot_menus bm
                   LEFT JOIN campanas camp ON camp.id = bm.campana_id
                   WHERE bm.texto IS NOT NULL
                   ORDER BY camp.nombre NULLS FIRST, bm.codigo"""
            )
            columnas = ["codigo", "tipo", "texto", "opciones", "campana", "imagen_url"]
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
        conn.commit()
        return {"ok": True}
    finally:
        release_connection(conn)


def _reenviar_asignaciones_activas(asesor_id: int) -> list:
    """Conversaciones ya asignadas a este asesor (útil al reconectar, para que
    la consola reconstruya su lista sin perder de vista lo que ya atendía).
    Usa el mismo formato de historial que asignacion.py, no el de _obtener_historial
    (que es para el widget) — para que la consola de asesor los procese igual."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.campana_id, u.nombre, u.telefono, c.entrada_cola, c.asignado_en
                   FROM conversaciones c
                   JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.asesor_id = %s AND c.estado = 'asignada'""",
                (asesor_id,),
            )
            filas = cur.fetchall()
            resultado = []
            for conversacion_id, campana_id, usuario_nombre, usuario_telefono, entrada_cola, asignado_en in filas:
                resultado.append({
                    "conversacion_id": conversacion_id,
                    "campana": campana_id,
                    "resumen_usuario": usuario_nombre,
                    "telefono": usuario_telefono,
                    "entrada_cola": entrada_cola.isoformat() if entrada_cola else None,
                    "asignado_en": asignado_en.isoformat() if asignado_en else None,
                    "historial": asignacion._historial(cur, conversacion_id),
                })
            return resultado
    finally:
        release_connection(conn)


@app.websocket("/ws/asesor")
async def ws_asesor(websocket: WebSocket, token: str):
    """
    wss://.../ws/asesor?token=<jwt obtenido en POST /login>
    Al conectar (primera vez o reconexión): reenvía lo que ya tenía asignado,
    marca presencia, y le asigna de inmediato lo que haya en cola para sus
    campañas, hasta llenar su cupo.
    """
    asesor_id = auth.decodificar_token(token)
    if asesor_id is None:
        await websocket.close(code=4401)  # token inválido o expirado
        return

    await manager.connect_asesor(asesor_id, websocket)
    _marcar_conectado(asesor_id)
    await supervisor.notificar_cambio_asesor(asesor_id, manager)
    try:
        await supervisor.enviar_etiquetas(asesor_id, manager, a_asesor=True)
        for asignacion_activa in _reenviar_asignaciones_activas(asesor_id):
            await manager.enviar_a_asesor(asesor_id, {
                "type": "nueva_asignacion", "payload": asignacion_activa,
            })
        await asignacion.asignar_desde_cola_para_asesor(asesor_id, manager)

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}

            if tipo == "mensaje_asesor":
                conversacion_id = payload.get("conversacion_id")
                texto = payload.get("texto")
                _guardar_mensaje_asesor(conversacion_id, texto)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensaje_asesor",
                    "payload": {"texto": texto, "asesor_nombre": _nombre_display(asesor_id)},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "asesor", "texto": texto},
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
                _cambiar_pausa_asesor(asesor_id, pausado)
                await supervisor.notificar_cambio_asesor(asesor_id, manager)
                if not pausado:
                    # al reanudar, intenta tomar de la cola de inmediato
                    await asignacion.asignar_desde_cola_para_asesor(asesor_id, manager)

            elif tipo == "marcar_leido":
                conversacion_id = payload.get("conversacion_id")
                _marcar_leidos_por_asesor(conversacion_id)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensajes_leidos", "payload": {"conversacion_id": conversacion_id},
                })

            elif tipo == "refrescar_conversaciones":
                # Vuelve a mandar el estado autoritativo de todas las conversaciones
                # asignadas — por si el asesor siente que algo no cargó bien.
                for asignacion_activa in _reenviar_asignaciones_activas(asesor_id):
                    await manager.enviar_a_asesor(asesor_id, {
                        "type": "nueva_asignacion", "payload": asignacion_activa,
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

    except WebSocketDisconnect:
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


def _guardar_respuesta_directa(conversacion_id: int, supervisor_id: int, texto: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                   VALUES (%s, 'supervisor', 'texto', %s, %s)""",
                (conversacion_id, texto, datetime.now(LIMA_TZ)),
            )
            cur.execute(
                """INSERT INTO supervisor_intervenciones (conversacion_id, supervisor_id, tipo, creado_en)
                   VALUES (%s, %s, 'respuesta_directa', %s)""",
                (conversacion_id, supervisor_id, datetime.now(LIMA_TZ)),
            )
        conn.commit()
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

    await manager.connect_supervisor(supervisor_id, websocket)
    try:
        await supervisor.enviar_estado_global(supervisor_id, manager)
        await supervisor.enviar_etiquetas(supervisor_id, manager, a_asesor=False)
        await supervisor.enviar_configuracion(supervisor_id, manager)

        while True:
            data = await websocket.receive_json()
            tipo = data.get("type")
            payload = data.get("payload", {}) or {}
            conversacion_id = payload.get("conversacion_id")

            if tipo == "escuchar_conversacion":
                manager.registrar_escucha(supervisor_id, conversacion_id)
                _registrar_escucha_db(conversacion_id, supervisor_id, "escucha_inicio")
                await manager.enviar_a_supervisor(supervisor_id, {
                    "type": "historial_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "historial": _obtener_historial(conversacion_id)},
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
                _guardar_respuesta_directa(conversacion_id, supervisor_id, texto)
                await manager.enviar_a_widget(conversacion_id, {
                    "type": "mensaje_asesor",
                    "payload": {"texto": texto, "asesor_nombre": _nombre_display(supervisor_id)},
                })
                await manager.notificar_escuchas(conversacion_id, {
                    "type": "mensaje_conversacion",
                    "payload": {"conversacion_id": conversacion_id, "emisor_tipo": "supervisor", "texto": texto},
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
                                    "supervisor_nombre": _nombre_display(supervisor_id)},
                    })

            elif tipo == "cambiar_cupo":
                asesor_id_objetivo = payload.get("asesor_id")
                nuevo_cupo = payload.get("nuevo_cupo")
                _cambiar_cupo_asesor(asesor_id_objetivo, nuevo_cupo)
                await supervisor.notificar_cambio_asesor(asesor_id_objetivo, manager)

            elif tipo == "crear_etiqueta":
                supervisor.crear_etiqueta(payload.get("nombre"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "editar_etiqueta":
                supervisor.editar_etiqueta(payload.get("id"), nombre=payload.get("nombre"),
                                            activo=payload.get("activo"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "eliminar_etiqueta":
                supervisor.eliminar_etiqueta(payload.get("id"))
                await supervisor.notificar_etiquetas_a_todos(manager)

            elif tipo == "guardar_mensaje_cierre":
                supervisor.guardar_configuracion("mensaje_cierre_conversacion", payload.get("texto", ""))

            elif tipo == "guardar_mensaje_bienvenida":
                supervisor.guardar_configuracion("mensaje_bienvenida_asesor", payload.get("texto", ""))

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

            elif tipo == "resetear_password_asesor":
                supervisor.resetear_password_asesor(payload.get("asesor_id"), payload.get("password"))

            elif tipo == "cerrar_conversacion":
                etiqueta_cierre_id = payload.get("etiqueta_cierre_id")
                await _cerrar_y_notificar(conversacion_id, etiqueta_cierre_id, "supervisor")
                manager.quitar_escucha(supervisor_id, conversacion_id)

    except WebSocketDisconnect:
        manager.disconnect_supervisor(supervisor_id)
