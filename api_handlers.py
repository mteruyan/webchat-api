"""
api_handlers.py — implementación real de cada API invocable desde bot_menus.accion_config.

Cada función recibe (parametros: dict, variables_sesion: dict) y devuelve
{"status": int, "response": {...}} — el mismo shape que Chattigo generaba,
para que {{VAR.status}} y {{VAR.response.campo}} sigan funcionando igual
en los textos de bot_menus.
"""

import json
import os
import random
import re
import string
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import logging

import requests
from db import get_connection, release_connection, get_connection_sofi, release_connection_sofi

LIMA_TZ = timezone(timedelta(hours=-5))

SOFI_APIS_BASE = "https://sofi-apis-249513164794.us-central1.run.app"
PROCESAR_RECETA_BASE = "https://procesar-receta-249513164794.us-central1.run.app"

# Mismo endpoint directo (sin cola/batch) que usa sofi-apis para SMS urgentes
# (reset de password, lead_pases, etc.) — confirmado por Miguel.
DANACONNECT_URL = "https://appserv.danaconnect.com/api/1.0/rest/conversation/517078/start/data"

# Otro "conversation" de Danaconnect (586630) — es el que en el flujo viejo de
# WhatsApp (gcp_inserta_bitacora_ivr) mandaba el CORREO a las asesoras de
# Sanitas avisando que llegó una receta nueva para procesar. No es SMS, por
# eso es una URL/conversation distinta a DANACONNECT_URL de arriba.
DANACONNECT_RECETA_EMAIL_URL = "https://appserv.danaconnect.com/api/1.0/rest/conversation/586630/start/data"
CORREO_ASESORAS_RECETAS = "sofimedicamentos@sanitasperu.com"

# Dominio propio detrás del Load Balancer (ver configurar_cloud_armor.sh) —
# NO el *.run.app directo: la red de Sanitas lo bloquea, así que un link al
# run.app dentro del correo a las asesoras nunca abriría desde su propia red.
WEBCHAT_API_BASE_URL = "https://api.sanitasperu.com"

logger = logging.getLogger("api_handlers")


def validar_horario_consultas(parametros: dict, variables: dict) -> dict:
    """
    Revisa horarios_atencion para la campaña indicada en parametros['campana_codigo'].
    No depende de ningún servicio externo — es una regla de negocio local.

    Si la campaña no tiene NINGUNA fila configurada en horarios_atencion,
    se trata como fuera de horario a propósito (fail-closed): mejor pedirle
    al usuario que reintente que dejarlo pasar a una cola sin agentes.
    """
    campana_codigo = parametros.get("campana_codigo", "consultas")
    ahora = datetime.now(LIMA_TZ)
    dia_semana = ahora.weekday()  # 0=lunes ... 6=domingo, igual que Python
    hora_actual = ahora.time()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.hora_inicio, h.hora_fin, c.mensaje_fuera_horario
                   FROM campanas c
                   LEFT JOIN horarios_atencion h
                     ON h.campana_id = c.id AND h.dia_semana = %s AND h.activo = TRUE
                   WHERE c.codigo = %s""",
                (dia_semana, campana_codigo),
            )
            filas = cur.fetchall()
    finally:
        release_connection(conn)

    if not filas:
        # campana_codigo no existe en absoluto
        return {"status": 404, "response": {
            "outOfTime": True,
            "messageOutOfTime": "No pudimos validar el horario de atención.",
        }}

    mensaje_fuera_horario = filas[0][2] or "En este momento estamos fuera de horario de atención."
    dentro_de_horario = any(
        hora_inicio is not None and hora_inicio <= hora_actual <= hora_fin
        for hora_inicio, hora_fin, _ in filas
    )

    return {"status": 200, "response": {
        "outOfTime": not dentro_de_horario,
        "messageOutOfTime": mensaje_fuera_horario,
    }}


def _llamar_api_externa(base_url: str, path: str, payload: dict, timeout: int = 8) -> dict:
    """Helper común para cualquier API externa que use el mismo esquema de auth
    (Authorization: ApiKey <SOFI_APIS_KEY>). ASUNCIÓN a confirmar para procesar-receta:
    que usa la misma API key que sofi-apis — si no, ajustar acá."""
    headers = {"Authorization": f"ApiKey {os.environ['SOFI_APIS_KEY']}"}
    url = f"{base_url}/{path}" if path else base_url
    logger.info("Llamando %s con payload=%s", url, payload)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        logger.info("%s -> status=%s body=%s", url, resp.status_code, resp.text[:2000])
        return {"status": resp.status_code, "response": resp.json() if resp.content else {}}
    except requests.RequestException as e:
        logger.error("%s FALLÓ: %s", url, e)
        return {"status": 500, "response": {}}
    except ValueError as e:
        logger.error("%s devolvió algo no-JSON: %s | body=%s", url, e, resp.text[:2000])
        return {"status": resp.status_code, "response": {}}


def _llamar_sofi_apis(path: str, payload: dict) -> dict:
    """ASUNCIÓN a confirmar: reciben POST con JSON — si en realidad son GET
    con query params, cambiar requests.post(...) por requests.get(..., params=payload)."""
    return _llamar_api_externa(SOFI_APIS_BASE, path, payload)


def consulta_doi(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("busca_doi_completo", {
        "doi": parametros.get("doi"),
        "celular_consulta": parametros.get("celular_consulta", ""),
    })


def consulta_doi_potestativo(parametros: dict, variables: dict) -> dict:
    """Flujo 'Consultas generales': valida si el DNI está en la tabla de
    afiliados potestativos — de eso depende qué menú se le muestra
    (con o sin la opción 'Tarifas 2026'). Mismo shape que consulta_doi,
    solo cambia el endpoint (respuesta trae 'contador' y 'mensaje')."""
    return _llamar_sofi_apis("busca_doi_potestativo", {
        "doi": parametros.get("doi"),
        "celular_consulta": parametros.get("celular_consulta", ""),
    })


def consulta_doi_potestativo_carta(parametros: dict, variables: dict) -> dict:
    """Genera/recupera la carta (PDF) de tarifas 2026 para un afiliado
    potestativo — la respuesta trae 'archivo' (nombre de archivo en
    marketeru.pe), 'email', 'estatus' y 'fec_envio'."""
    return _llamar_sofi_apis("busca_doi_potestativo_carta", {
        "doi": parametros.get("doi"),
        "celular_consulta": parametros.get("celular_consulta", ""),
    })


def consulta_carta_rechazada(parametros: dict, variables: dict) -> dict:
    """Flujo 'Consulta estado de Carta de Garantía': busca el estado actual
    de la carta por DNI. Si no la encuentra, response.buscador == 'vacio'."""
    return _llamar_sofi_apis("busca_carta_rechazada_tabla", {
        "celular": parametros.get("celular"),
        "doi": parametros.get("doi"),
    })


def update_cg_rechazados(parametros: dict, variables: dict) -> dict:
    """Marca que el cliente ya vio el estado de su carta por este canal
    (campo='ingreso_wsp') — solo bookkeeping interno de sofi-apis."""
    return _llamar_sofi_apis("update_cg_rechazados", {
        "celular": parametros.get("celular"),
        "campo": parametros.get("campo", "ingreso_wsp"),
        "doi": parametros.get("doi"),
    })


def alta_usuario_programa(parametros: dict, variables: dict) -> dict:
    """Flujo 'Programas de Salud': registra al afiliado en Vive Sano/Krystal.
    Devuelve response.estado ('dni_recibido' = aceptado/pendiente de firma,
    cualquier otro valor = rechazado con response.motivo_rechazo), además de
    response.programa y response.link_firma cuando fue aceptado."""
    return _llamar_sofi_apis("programa-cronico/recibir-dni", {
        "n_documento": parametros.get("n_documento"),
        "correo": parametros.get("correo"),
        "celular": parametros.get("celular"),
        "url_anverso": parametros.get("url_anverso"),
        "url_reverso": parametros.get("url_reverso"),
        "url_receta": parametros.get("url_receta"),
    })


def afiliacion_iniciar(parametros: dict, variables: dict) -> dict:
    """Flujo 'Congreso Afiliación': arranca la solicitud del titular.
    Devuelve response.token (usado por el resto del flujo), response.link
    (enlace final de firma) y response.error si algo falla."""
    return _llamar_sofi_apis("afiliacion/iniciar", {
        "dni_titular": parametros.get("dni_titular"),
        "celular": parametros.get("celular"),
        "tipo_solicitud": "AFILIACION",
    })


def afiliacion_consulta_ruc(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("afiliacion/consulta-ruc", {
        "ruc": parametros.get("ruc", "20161749126"),
        "token": parametros.get("token"),
    })


def afiliacion_registrar_titular(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("afiliacion/registrar-titular", {
        "url_dni_anverso": parametros.get("url_dni_anverso"),
        "correo": parametros.get("correo"),
        "celular": parametros.get("celular"),
        "url_dni_reverso": parametros.get("url_dni_reverso"),
        "token": parametros.get("token"),
    })


def afiliacion_registrar_dependiente(parametros: dict, variables: dict) -> dict:
    """parentesco: CONVIVIENTE | CONYUGE | HIJO | HIJO_INCAPACITADO."""
    return _llamar_sofi_apis("afiliacion/registrar-dependiente", {
        "parentesco": parametros.get("parentesco"),
        "url_dni_anverso": parametros.get("url_dni_anverso"),
        "url_dni_reverso": parametros.get("url_dni_reverso"),
        "token": parametros.get("token"),
    })


def lead_consultas(parametros: dict, variables: dict) -> dict:
    """Guarda un 'lead' cuando alguien pide hablar con un asesor de Consultas
    generales fuera de horario — mismo patrón que lead_pases/graba_bd_leads_sctr."""
    return _llamar_sofi_apis("lead_consultas", {
        "nombre": parametros.get("nombre"),
        "celular": parametros.get("celular"),
    })


def listar_distritos(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("listar_distritos", {
        "distrito": parametros.get("distrito", ""),
    })


def update_doi_distrito(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("update_doi_distrito", {
        "doi": parametros.get("doi"),
        "direccion_nro": parametros.get("direccion_nro"),
        "nombre_ubigeo_distrito": parametros.get("nombre_ubigeo_distrito"),
        "va_nombre_o_ubigeo": parametros.get("va_nombre_o_ubigeo", "nombre"),
    })


def seleccionar_direccion(parametros: dict, variables: dict) -> dict:
    """Marca cuál dirección guardada (1, 2 o 3) se usa para este pedido puntual.
    update_escoge espera el campo como "escoge", no "direccion_nro"."""
    return _llamar_sofi_apis("update_escoge", {
        "doi": parametros.get("doi"),
        "escoge": parametros.get("direccion_nro"),
    })


def registrar_direccion(parametros: dict, variables: dict) -> dict:
    """Agrega una dirección a un paciente que YA EXISTE en sofi_usuarios
    (contador != 0). Para un paciente 100% nuevo, usar crear_doi_nuevo en su
    lugar — update_doi_direccion solo hace UPDATE, no crea filas nuevas.
    Necesita direccion_nro (1/2/3): el casillero exacto donde escribirla."""
    return _llamar_sofi_apis("update_doi_direccion", {
        "doi": parametros.get("doi"),
        "nombres": parametros.get("nombres"),
        "direccion": parametros.get("direccion"),
        "distrito": parametros.get("distrito"),
        "celular": parametros.get("celular"),
        "correo": parametros.get("correo"),
        "direccion_nro": parametros.get("direccion_nro"),
    })


def crear_doi_nuevo(parametros: dict, variables: dict) -> dict:
    """Crea el registro de un paciente que NO existía todavía en sofi_usuarios
    (busca_doi_completo devolvió contador=="0"). Endpoint distinto a
    registrar_direccion — ese es solo para agregar una dirección a alguien
    que ya está en la base."""
    return _llamar_sofi_apis("update_doi", {
        "doi": parametros.get("doi"),
        "nombres": parametros.get("nombres"),
        "apellidos": parametros.get("apellidos"),
        "direccion1": parametros.get("direccion1"),
        "distrito1": parametros.get("distrito1"),
        "nro_contacto1": parametros.get("nro_contacto1"),
        "nro_contacto2": parametros.get("nro_contacto2", ""),
        "email": parametros.get("email"),
    })


def _ruta_objeto_desde_url_firmada(url_firmada: str) -> str:
    """parametros['ruta'] en realidad trae la URL FIRMADA (nombre heredado
    del bot_menus de WhatsApp) — larga, con toda la query string de la firma
    V4 de GCS (X-Goog-Algorithm, X-Goog-Signature, etc.). Para el correo no
    sirve tal cual (ver _enviar_correo_receta_asesoras); acá solo se recupera
    el nombre de objeto puro (ej. "webchat/xxx.jpg") a partir de ella."""
    base = f"https://storage.googleapis.com/{os.environ.get('GCS_BUCKET', '')}/"
    sin_query = (url_firmada or "").split("?", 1)[0]
    return sin_query[len(base):] if sin_query.startswith(base) else ""


def _datos_sin_enmascarar_por_doi(doi: str) -> dict:
    """Trae nombres/apellidos/dirección/distrito/contacto SIN enmascarar,
    consultando directo la tabla sofi_usuarios en la base "sofi_whatsapp"
    (misma instancia Cloud SQL que "webchat", otra base) — igual que hacía
    gcp_inserta_bitacora_ivr por SQL en el flujo viejo de WhatsApp. Esto es
    necesario porque TRAE_ESCOGE.response (lo que ya tenemos en parametros)
    viene enmascarado por sofi-apis cuando detecta que el celular que
    escribe no es el registrado — correcto para lo que ve el paciente, pero
    las asesoras sí necesitan el dato real en el correo.

    Devuelve {} si no encuentra el doi o si algo falla — nunca debe tumbar
    el envío del correo ni el flujo del bot por esto."""
    if not doi:
        return {}
    conn = None
    try:
        conn = get_connection_sofi()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nombres, apellidos,
                       direccion1, distrito1, direccion2, distrito2,
                       direccion3, distrito3, nro_contacto1, escoge
                FROM sofi_usuarios WHERE doi = %s
            """, (doi,))
            row = cur.fetchone()
        if not row:
            return {}
        (nombres, apellidos, direccion1, distrito1, direccion2, distrito2,
         direccion3, distrito3, nro_contacto1, escoge) = row
        escoge = str(escoge) if escoge else "1"
        if escoge == "2":
            direccion, distrito = direccion2, distrito2
        elif escoge == "3":
            direccion, distrito = direccion3, distrito3
        else:
            direccion, distrito = direccion1, distrito1
        return {
            "nombres":   (nombres or "").upper(),
            "apellidos": (apellidos or "").upper(),
            "direccion": (direccion or "").upper(),
            "distrito":  (distrito or "").upper(),
            "contacto":  nro_contacto1 or "",
        }
    except Exception as e:
        logger.error("Error consultando sofi_usuarios (doi=%s) para correo sin enmascarar: %s", doi, e)
        return {}
    finally:
        if conn:
            release_connection_sofi(conn)


def _enviar_correo_receta_asesoras(parametros: dict) -> None:
    """Avisa por correo a las asesoras de Sanitas que llegó una receta nueva
    para procesar — mismo mecanismo (Danaconnect, conversation 586630) que
    usaba gcp_inserta_bitacora_ivr en el flujo viejo de WhatsApp. Si falla,
    no debe tumbar la respuesta al paciente: es un aviso adicional, no un
    paso bloqueante del flujo del bot.

    El campo URL de Danaconnect rechazaba la URL firmada completa (muy larga,
    "Data too long for column") — en su lugar mandamos un link corto a
    nuestro propio /receta-correo, que genera una firmada fresca al momento
    del clic (igual que /archivo-receta, pero sin pedir login de staff,
    porque este correo va a un buzón compartido, no a la sesión de un
    asesor puntual)."""
    ruta_objeto = _ruta_objeto_desde_url_firmada(parametros.get("ruta") or "")
    url_corta = (
        f"{WEBCHAT_API_BASE_URL}/receta-correo?ruta={quote(ruta_objeto, safe='')}"
        if ruta_objeto else ""
    )
    datos_reales = _datos_sin_enmascarar_por_doi(parametros.get("doi") or "")
    if not datos_reales:
        # Sin esto el correo sale con los datos ENMASCARADOS de TRAE_ESCOGE
        # (lo que ve el paciente) -- queda en el log para detectarlo de
        # inmediato en vez de enterarnos por una prueba.
        logger.error(
            "Correo receta a asesoras: sin datos reales para doi=%s -- se enviará con datos enmascarados",
            parametros.get("doi"),
        )
    payload = json.dumps({
        "DOI":             parametros.get("doi") or "",
        "NOMBRES":         datos_reales.get("nombres") or parametros.get("nombres") or "",
        "APELLIDOS":       datos_reales.get("apellidos") or parametros.get("apellidos") or "",
        "EMAIL":           CORREO_ASESORAS_RECETAS,
        "DIRECCION":       datos_reales.get("direccion") or parametros.get("direccion") or "",
        "DISTRITO":        datos_reales.get("distrito") or parametros.get("distrito") or "",
        "CONTACTO":        datos_reales.get("contacto") or parametros.get("celular") or "",
        "URL":             url_corta,
        "USER_IDENTIFIER": parametros.get("doi") or "",
        "ID_CHAT":         parametros.get("chat_id") or "",
    })
    headers = {
        "Content-Type": "application/json",
        "Authorization": os.environ["DANACONNECT_AUTH"],
    }
    # Hasta 3 intentos: un 5xx o un corte de red de Danaconnect no debe dejar a
    # las asesoras sin enterarse de que llegó una receta.
    for intento in range(1, 4):
        try:
            resp = requests.post(DANACONNECT_RECETA_EMAIL_URL, data=payload, headers=headers, timeout=10)
            logger.info("Correo receta a asesoras (intento %s) -> status=%s body=%s",
                        intento, resp.status_code, resp.text[:500])
            if resp.ok:
                return
        except requests.RequestException as e:
            logger.error("Error al avisar por correo (receta) a asesoras (intento %s): %s", intento, e)
        if intento < 3:
            time.sleep(1.5)
    logger.error("Correo receta a asesoras: falló tras 3 intentos (doi=%s)", parametros.get("doi"))


def procesar_receta(parametros: dict, variables: dict) -> dict:
    """Llama al servicio procesar-receta (Document AI + Gemini) — timeout más
    largo porque hace OCR y extracción, no es una consulta simple de BD.

    Aparte, avisa por correo a las asesoras (ver _enviar_correo_receta_asesoras)
    — equivalente al correo que mandaba gcp_inserta_bitacora_ivr en WhatsApp."""
    resultado = _llamar_api_externa(PROCESAR_RECETA_BASE, "", {
        "ruta": parametros.get("ruta"),
        "doi": parametros.get("doi"),
        "celular": parametros.get("celular"),
        "chat_id": parametros.get("chat_id"),
    }, timeout=30)
    _enviar_correo_receta_asesoras(parametros)
    return _ajustar_textos_receta(resultado)


# El servicio procesar-receta (compartido con el flujo de WhatsApp) responde
# "✅ Receta #1234 recibida correctamente ..."; en el webchat ese primer
# mensaje debe decir otra cosa. Se cambia acá, en el webchat, para no tocar lo
# que ve WhatsApp.
_RE_RECETA_RECIBIDA = re.compile(r"Receta\s*#\s*(\S+?)\s+recibida\s+correctamente", re.IGNORECASE)


def _ajustar_textos_receta(valor):
    """Recorre la respuesta (dicts/listas/textos) y reemplaza el encabezado
    "Receta #X recibida correctamente" por "Estamos leyendo tu receta #X"
    (el ✅ del inicio se conserva)."""
    if isinstance(valor, str):
        return _RE_RECETA_RECIBIDA.sub(r"Estamos leyendo tu receta #\1", valor)
    if isinstance(valor, dict):
        return {k: _ajustar_textos_receta(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_ajustar_textos_receta(v) for v in valor]
    return valor


def trae_escoge(parametros: dict, variables: dict) -> dict:
    """PENDIENTE: confirmar la URL real (asumo /trae_escoge en sofi-apis,
    siguiendo el mismo patrón de nombres que los otros 5 endpoints, pero
    sin confirmar). Trae los datos de la dirección/contacto ya seleccionada,
    para el resumen final."""
    return _llamar_sofi_apis("trae_escoge", {
        "doi": parametros.get("doi"),
        "celular_consulta": parametros.get("celular_consulta", ""),
    })


# --- SCTR y Pase de atención: 6 handlers pendientes de confirmar URL ---

def consulta_ruc(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("consulta_ruc2", {
        "ruc": parametros.get("ruc"),
        "celular": parametros.get("celular", ""),
    })


def buscar_actividad_sctr(parametros: dict, variables: dict) -> dict:
    """Usado tanto para buscar (sin 'selec') como para confirmar una actividad elegida (con 'selec')."""
    payload = {"ruc": parametros.get("ruc"), "palabra": parametros.get("palabra")}
    if parametros.get("selec"):
        payload["selec"] = parametros.get("selec")
    return _llamar_sofi_apis("busca_actividad", payload)


def graba_bd_leads_sctr(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("lead_sctr", {
        "nombre": parametros.get("nombre"),
        "celular": parametros.get("celular"),
    })


def lead_pases(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("lead_pases", {
        "nombre": parametros.get("nombre"),
        "celular": parametros.get("celular"),
    })


def api_sctr_nuevo(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("admision/sctr_nuevo", parametros)


def api_sctr_continuidad(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("admision/sctr_continuidad", parametros)


def api_eps_nuevo(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("admision/eps_nuevo", parametros)


# --- SCTR: Generar Plantilla Excel — 3 handlers pendientes de confirmar URL ---

def detalle_planilla_sctr(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("sctr/whatsapp/resumen", {
        "ruc": parametros.get("ruc"),
        "telefono": parametros.get("telefono"),
    })


def generar_excel_sctr(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("sctr/whatsapp/excel", {
        "codigo": parametros.get("codigo"),
    })


def borrar_trabajador_sctr(parametros: dict, variables: dict) -> dict:
    return _llamar_sofi_apis("sctr/whatsapp/borrar", {
        "ruc": parametros.get("ruc"),
        "numero": parametros.get("numero"),
        "telefono": parametros.get("telefono"),
    })


# --- Validación de celular por SMS (token OTP) al inicio del webchat ---

def _generar_codigo_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def enviar_sms_danaconnect(celular: str, mensaje: str) -> bool:
    """Envío DIRECTO de SMS a Danaconnect (mismo patrón que usa sofi-apis
    para SMS urgentes: reset de password, lead_pases, etc.), no pasa por
    cola/batch. Reutilizado tanto por el OTP de validación de celular como
    por el "SMS saliente" que mandan asesor/supervisor para avisarle al
    cliente que le escriban de vuelta (nuestro reemplazo de los mensajes
    salientes de WhatsApp, ya que un webchat no tiene push nativo)."""
    payload = {"NUM": f" {celular}", "TEXT": mensaje, "URL": ""}
    headers = {
        "Content-Type": "application/json",
        "Authorization": os.environ["DANACONNECT_AUTH"],
    }
    try:
        resp = requests.post(DANACONNECT_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Error al enviar SMS a %s: %s", celular, e)
        return False


# Otro "conversation" de Danaconnect (609623) -- en vez de SMS, hace una
# LLAMADA de voz y le dicta el token a la persona. Se ofrece como alternativa
# al SMS del OTP cuando ya pidió reenviar dos veces y probablemente no le
# esté llegando (ver main.py: botón "Prefiero que me llamen" durante
# webchat_valida_pide_codigo).
DANACONNECT_LLAMADA_OTP_URL = "https://appserv.danaconnect.com/api/1.0/rest/conversation/609623/start/data"


def enviar_llamada_otp(celular: str, token: str) -> bool:
    """Pide a Danaconnect que llame por voz a `celular` y le dicte `token`.
    No genera un código nuevo -- se le pasa el mismo que ya se le mandó por
    SMS (el que la persona todavía debe ingresar), para no invalidarlo."""
    payload = json.dumps({"CELULAR": celular, "TOKEN": token})
    headers = {
        "Content-Type": "application/json",
        "Authorization": os.environ["DANACONNECT_AUTH"],
    }
    try:
        resp = requests.post(DANACONNECT_LLAMADA_OTP_URL, data=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Error al llamar con el código OTP a %s: %s", celular, e)
        return False


def enviar_sms_otp(parametros: dict, variables: dict) -> dict:
    """Genera un código de 6 dígitos y lo manda por SMS vía Danaconnect.

    El código generado se devuelve en el response (nunca se persiste en la
    base) para que el bot lo compare más adelante contra lo que la persona
    escriba — bot_engine lo guarda solo, de forma temporal, en la variable de
    sesión {{ENVIO_SMS.response.codigo}} (variables_sesion de la conversación),
    igual que cualquier otra respuesta de accion_api."""
    celular = (parametros.get("celular") or "").strip()
    if not celular:
        return {"status": 400, "response": {"enviado": False}}

    codigo = _generar_codigo_otp()
    mensaje = f"Tu código de verificación Sanitas es: {codigo}"
    if not enviar_sms_danaconnect(celular, mensaje):
        return {"status": 500, "response": {"enviado": False}}

    return {"status": 200, "response": {"enviado": True, "codigo": codigo}}


def validar_sms_otp(parametros: dict, variables: dict) -> dict:
    """Compara el código que la persona escribió contra el que se le mandó
    por SMS (ambos ya resueltos desde variables de sesión por el motor de
    bot_menus antes de llamar este handler — ver accion_config.parametros
    del nodo webchat_valida_evalua_codigo). No llama a ningún servicio
    externo, es una comparación local."""
    ingresado = "".join(c for c in (parametros.get("codigo_ingresado") or "") if c.isdigit())
    enviado = (parametros.get("codigo_enviado") or "").strip()
    valido = bool(ingresado) and bool(enviado) and ingresado == enviado
    return {"status": 200 if valido else 400, "response": {"valido": valido}}
