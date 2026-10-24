"""
api_handlers.py — implementación real de cada API invocable desde bot_menus.accion_config.

Cada función recibe (parametros: dict, variables_sesion: dict) y devuelve
{"status": int, "response": {...}} — el mismo shape que Chattigo generaba,
para que {{VAR.status}} y {{VAR.response.campo}} sigan funcionando igual
en los textos de bot_menus.
"""

import json
import math
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


_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MENSAJE_FERIADO_DEFAULT = (
    "Hoy {{FECHA}} es feriado ({{FERIADO}}) y en este momento no estamos atendiendo por este canal. "
    "Retomaremos la atención en nuestro próximo día hábil. ¡Gracias por tu comprensión!"
)


def _feriado_de_hoy(campana_codigo: str, hoy):
    """Si hoy es feriado para esa campaña devuelve el mensaje ya armado; si no, None.
    Si la tabla todavía no existe (migración sin correr), se ignora: no debe romper
    la validación de horario normal."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT f.descripcion, f.mensaje FROM feriados_campana f
                   JOIN campanas c ON c.id = f.campana_id
                   WHERE c.codigo = %s AND f.fecha = %s""",
                (campana_codigo, hoy),
            )
            fila = cur.fetchone()
            if not fila:
                return None
            descripcion, mensaje = fila
            if not (mensaje or "").strip():
                cur.execute("SELECT valor FROM configuracion_global WHERE clave = 'mensaje_feriado'")
                cfg = cur.fetchone()
                mensaje = (cfg[0] if cfg and (cfg[0] or "").strip() else MENSAJE_FERIADO_DEFAULT)
        fecha_txt = f"{_DIAS_ES[hoy.weekday()]} {hoy.day} de {_MESES_ES[hoy.month - 1]}"
        return (mensaje.replace("{{FECHA}}", fecha_txt)
                       .replace("{{FERIADO}}", (descripcion or "feriado").strip()))
    except Exception as e:
        logger.warning("No se pudo revisar feriados (¿falta migracion_feriados.sql?): %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        if conn:
            release_connection(conn)


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

    # Feriado de esta campaña (configurado por el supervisor): no se atiende todo el día.
    mensaje_feriado = _feriado_de_hoy(campana_codigo, ahora.date())
    if mensaje_feriado:
        return {"status": 200, "response": {"outOfTime": True, "messageOutOfTime": mensaje_feriado}}
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


# --- Agente de IA (preguntas frecuentes) de sofi-apis ------------------------
# Endpoint: POST {SOFI_APIS_BASE}/consulta_agente  body {"pregunta", "celular"} -> {"respuesta"}.
# El agente guarda solo su estado (historial, perfil, sub-flujos como pedir el DNI) por celular;
# el webchat únicamente reenvía el texto y muestra la respuesta.
MENSAJE_RESPALDO_IA = ("Disculpa, tuve un problema para responder. Intenta de nuevo en un momento "
                       "o escribe *menu* para volver al menú principal.")
TIMEOUT_AGENTE_IA = (10, 45)   # (conexión, lectura): Gemini + consultas a BD pueden tardar 5-15 s
MAX_CARACTERES_PREGUNTA_IA = 1500


def consulta_agente_ia(parametros: dict, variables: dict) -> dict:
    """Pregunta al agente de IA. SIEMPRE devuelve {"status", "response": {"respuesta": texto}}:
    si algo falla, `respuesta` es un mensaje amable de respaldo (nunca el error crudo).
    A propósito NO se registra el texto de la pregunta ni de la respuesta (pueden traer DNI u
    otros datos personales): solo longitudes y códigos de estado."""
    pregunta = str(parametros.get("pregunta") or "").strip()[:MAX_CARACTERES_PREGUNTA_IA]
    celular = "".join(ch for ch in str(parametros.get("celular") or "") if ch.isdigit())
    if not pregunta or not celular:
        logger.error("consulta_agente_ia sin pregunta o sin celular (pregunta=%s, celular=%s)",
                     bool(pregunta), bool(celular))
        return {"status": 400, "response": {"respuesta": MENSAJE_RESPALDO_IA}}
    url = f"{SOFI_APIS_BASE}/consulta_agente"
    try:
        resp = requests.post(
            url, json={"pregunta": pregunta, "celular": celular},
            headers={"Authorization": f"ApiKey {os.environ['SOFI_APIS_KEY']}"},
            timeout=TIMEOUT_AGENTE_IA,
        )
        logger.info("consulta_agente_ia -> status=%s (pregunta de %s caracteres)", resp.status_code, len(pregunta))
        if resp.status_code != 200:
            logger.error("consulta_agente_ia respondió HTTP %s", resp.status_code)
            return {"status": resp.status_code, "response": {"respuesta": MENSAJE_RESPALDO_IA}}
        texto = str((resp.json() or {}).get("respuesta") or "").strip()
        if not texto:
            logger.error("consulta_agente_ia devolvió una respuesta vacía")
            return {"status": 502, "response": {"respuesta": MENSAJE_RESPALDO_IA}}
        return {"status": 200, "response": {"respuesta": texto}}
    except Exception:  # noqa: BLE001  (timeout, red, JSON inválido, falta SOFI_APIS_KEY...)
        logger.exception("consulta_agente_ia falló")
        return {"status": 500, "response": {"respuesta": MENSAJE_RESPALDO_IA}}


# --- Médico a domicilio (MAD) / Ambulancia -------------------------------------------------------
# Todo el despacho vive en sofi-apis (búsqueda de proveedores, ofertas, reintentos, PWA del proveedor y
# página de seguimiento). El webchat solo crea la solicitud y consulta su estado.
#   POST {SOFI_APIS_BASE}/solicitudes/crear  {"telefono","lat","lng","tipo_servicio","cliente_nombre"?}
#        -> {"solicitud_id", "tracking_url": ".../track/<token>"}
#   GET  {SOFI_APIS_BASE}/track/<token>/estado -> {"estado","tipo_servicio","proveedor_nombre",...}
TIMEOUT_MAD = 20
MAD_TIPOS_VALIDOS = ("MAD", "AMBULANCIA")
MAD_MINUTOS_BUSCANDO_LARGO = 5       # tras este tiempo en "buscando_proveedor" se ofrece hablar con un asesor
_MAD_TOKEN_VALIDO = re.compile(r"[A-Za-z0-9_\-]{8,200}")
LIMA_UTC_MENOS_5 = timezone(timedelta(hours=-5))


def _headers_sofi_apis() -> dict:
    return {"Authorization": f"ApiKey {os.environ['SOFI_APIS_KEY']}"}


def crear_solicitud_mad(parametros: dict, variables: dict) -> dict:
    """Crea la solicitud de MAD/Ambulancia. Devuelve {"status", "response": {...}}; si algo falla el status
    NO es 200 y el nodo del flujo muestra un mensaje amable (nunca el error crudo). No se registran
    coordenadas ni teléfono en los logs (datos personales)."""
    try:
        telefono = "".join(ch for ch in str(parametros.get("telefono") or "") if ch.isdigit())
        tipo = str(parametros.get("tipo_servicio") or "").strip().upper()
        lat = float(str(parametros.get("lat")).replace(",", "."))
        lng = float(str(parametros.get("lng")).replace(",", "."))
        if not telefono or tipo not in MAD_TIPOS_VALIDOS or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            logger.error("crear_solicitud_mad con datos inválidos (telefono=%s, tipo=%s)", bool(telefono), tipo)
            return {"status": 400, "response": {}}
        cuerpo = {"telefono": telefono, "lat": lat, "lng": lng, "tipo_servicio": tipo}
        nombre = str(parametros.get("cliente_nombre") or "").strip()
        if nombre:
            cuerpo["cliente_nombre"] = nombre   # sofi-apis lo ignora hasta que lo soporte
        # Campos opcionales: ubicación escrita y, si es SERVICIO PROGRAMADO, la hora (de Lima) y el
        # contacto en el lugar. Los vacíos no se mandan, así "ahora" sigue igual que siempre.
        for campo in ("direccion", "distrito", "referencia", "contacto_nombre"):
            valor = str(parametros.get(campo) or "").strip()
            if valor:
                cuerpo[campo] = valor[:300]
        contacto_tel = "".join(ch for ch in str(parametros.get("contacto_telefono") or "") if ch.isdigit())
        if contacto_tel:
            cuerpo["contacto_telefono"] = contacto_tel
        programada_para = str(parametros.get("programada_para") or "").strip()
        if programada_para:
            if not _PROGRAMADA_RE.fullmatch(programada_para):
                logger.error("crear_solicitud_mad con programada_para inválido")
                return {"status": 400, "response": {"error": MENSAJE_HORA_INVALIDA_MAD}}
            cuerpo["programada_para"] = programada_para
        resp = requests.post(f"{SOFI_APIS_BASE}/solicitudes/crear", json=cuerpo,
                             headers=_headers_sofi_apis(), timeout=TIMEOUT_MAD)
        logger.info("crear_solicitud_mad -> status=%s tipo=%s programada=%s", resp.status_code, tipo,
                    bool(programada_para))
        if resp.status_code not in (200, 201):
            logger.error("crear_solicitud_mad respondió HTTP %s", resp.status_code)
            if programada_para and resp.status_code in (400, 409):
                # sofi-apis manda el motivo (hora muy cercana, fuera de horario, más de 14 días, ubicación
                # fuera de Perú, máximo 3 programados) ya redactado para la persona: se muestra tal cual.
                # Cualquier otro error (5xx, timeout...) NO se muestra: sale el mensaje de respaldo del flujo.
                return {"status": resp.status_code, "response": {"error": _error_sofi_apis(resp)}}
            return {"status": resp.status_code if resp.status_code != 200 else 502, "response": {}}
        datos = resp.json() or {}
        url = str(datos.get("tracking_url") or "").strip()
        token = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        if not url or not _MAD_TOKEN_VALIDO.fullmatch(token):
            logger.error("crear_solicitud_mad devolvió una respuesta sin tracking_url válido")
            return {"status": 502, "response": {}}
        return {"status": 200, "response": {
            "solicitud_id": datos.get("solicitud_id"), "tracking_url": url, "tracking_token": token,
            "creada_en": datetime.now(LIMA_UTC_MENOS_5).isoformat(timespec="seconds"),
            "programada": bool(datos.get("programada")),
        }}
    except Exception:  # noqa: BLE001  (timeout, red, JSON inválido, coordenadas no numéricas...)
        logger.exception("crear_solicitud_mad falló")
        return {"status": 500, "response": {}}


# --- Servicio PROGRAMADO -------------------------------------------------------------------------
# La hora se maneja SIEMPRE en hora de Lima, formato AAAA-MM-DDTHH:MM (igual que sofi-apis). El webchat
# solo adelanta lo evidente (formato, mínimo 60 min, máximo 14 días) para no hacer perder tiempo; el horario
# permitido y los topes (3 programados por celular, ubicación) los decide sofi-apis y su mensaje se muestra tal cual.
_PROGRAMADA_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
MAD_PROGRAMADA_MIN_MINUTOS = 60
MAD_PROGRAMADA_MAX_DIAS = 14
MENSAJE_HORA_INVALIDA_MAD = "No pude leer la fecha y la hora. Elige de nuevo el día y la hora."
_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]


def _error_sofi_apis(resp) -> str:
    """Mensaje de un 400/409 de sofi-apis ({"error": "..."} o {"detail": "..."}), recortado. Si no trae
    texto usable, uno genérico: jamás el cuerpo crudo."""
    try:
        datos = resp.json() or {}
        texto = datos.get("error") or datos.get("detail")
        if isinstance(texto, str) and texto.strip():
            return texto.strip()[:400]
    except Exception:  # noqa: BLE001
        pass
    return "No pudimos programar el servicio con esos datos. Revisa la fecha, la hora y el lugar."


def _texto_fecha_hora_mad(dt: datetime) -> str:
    """'domingo 18 de octubre a las 15:30' (hora de Lima, 24 h)."""
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]} a las {dt:%H:%M}"


def validar_programada_mad(parametros: dict, variables: dict) -> dict:
    """Valida la fecha/hora elegida ("AAAA-MM-DDTHH:MM", hora de Lima). 200 -> {"programada_para", "texto"};
    400 -> {"error": mensaje para la persona}."""
    try:
        bruto = str(parametros.get("programada_para") or "").strip()
        if not _PROGRAMADA_RE.fullmatch(bruto):
            return {"status": 400, "response": {"error": MENSAJE_HORA_INVALIDA_MAD}}
        dt = datetime.strptime(bruto, "%Y-%m-%dT%H:%M").replace(tzinfo=LIMA_UTC_MENOS_5)
        ahora = datetime.now(LIMA_UTC_MENOS_5)
        if dt < ahora + timedelta(minutes=MAD_PROGRAMADA_MIN_MINUTOS):
            return {"status": 400, "response": {"error": (
                f"Esa hora es muy cercana 🕒 Para programar necesitamos al menos {MAD_PROGRAMADA_MIN_MINUTOS} "
                "minutos de anticipación. Elige otra hora.")}}
        if dt > ahora + timedelta(days=MAD_PROGRAMADA_MAX_DIAS):
            return {"status": 400, "response": {"error": (
                f"Solo puedes programar hasta {MAD_PROGRAMADA_MAX_DIAS} días desde hoy 📅 Elige una fecha más cercana.")}}
        return {"status": 200, "response": {"programada_para": bruto, "texto": _texto_fecha_hora_mad(dt)}}
    except Exception:  # noqa: BLE001
        logger.exception("validar_programada_mad falló")
        return {"status": 400, "response": {"error": MENSAJE_HORA_INVALIDA_MAD}}


def resumen_programada_mad(parametros: dict, variables: dict) -> dict:
    """Prepara los textos de la confirmación y valida el contacto en el lugar (opcional). El celular se acepta
    con espacios, guiones o +51 y debe quedar de 9 dígitos. 200 -> {"contacto_telefono", "contacto_texto"};
    400 -> {"error"}."""
    try:
        nombre = " ".join(str(parametros.get("contacto_nombre") or "").split())[:80]
        digitos = "".join(ch for ch in str(parametros.get("contacto_telefono") or "") if ch.isdigit())
        if len(digitos) == 11 and digitos.startswith("51"):
            digitos = digitos[2:]
        if digitos and not (len(digitos) == 9 and digitos.startswith("9")):
            return {"status": 400, "response": {"error": (
                "El celular de contacto debe tener 9 dígitos y empezar con 9 (ejemplo: 987654321).")}}
        if nombre and not digitos:
            return {"status": 400, "response": {"error": (
                "Falta el celular de la persona que recibirá al profesional. Escríbelo de nuevo.")}}
        if digitos and not nombre:
            return {"status": 400, "response": {"error": (
                "Falta el nombre de la persona que recibirá al profesional. Escríbelo de nuevo.")}}
        texto = f"{nombre} - {digitos}" if digitos else "No indicaste una persona de contacto"
        referencia = " ".join(str(parametros.get("referencia") or "").split())[:200]
        return {"status": 200, "response": {"contacto_nombre": nombre, "contacto_telefono": digitos,
                                            "contacto_texto": texto, "referencia_texto": referencia or "Sin referencia"}}
    except Exception:  # noqa: BLE001
        logger.exception("resumen_programada_mad falló")
        return {"status": 400, "response": {"error": "No pude leer los datos de contacto. Escríbelos de nuevo."}}


# --- Confirmación con la dirección y el mapa (ahora y programado) --------------------------------------
# sofi-apis exige "direccion" (mín. 5 caracteres) y "distrito" al crear la solicitud. Esta función prepara lo que
# se muestra antes de confirmar: dirección limpia, referencia, precisión del GPS, enlace al mapa y, si se puede,
# un aviso cuando el punto del GPS queda lejos del distrito escrito.
MAD_DIRECCION_MIN_CARACTERES = 5
MAD_AVISO_DISTRITO_MARGEN_KM = 3.0

# Distritos de Lima Metropolitana y Callao: (latitud, longitud, radio aproximado en km). Valores APROXIMADOS (centro
# y "tamaño" del distrito) solo para avisar si el punto queda claramente lejos; el aviso nunca bloquea. Un distrito
# que no esté aquí (otras provincias) simplemente no genera aviso.
_DISTRITOS_LIMA = {
    "ANCON": (-11.773, -77.175, 12), "ATE": (-12.026, -76.918, 9), "BARRANCO": (-12.140, -77.021, 1.5),
    "BRENA": (-12.057, -77.051, 1.2), "CARABAYLLO": (-11.865, -77.036, 12), "CHACLACAYO": (-11.975, -76.764, 6),
    "CHORRILLOS": (-12.170, -77.012, 4), "CIENEGUILLA": (-12.100, -76.770, 10), "COMAS": (-11.938, -77.052, 5),
    "EL AGUSTINO": (-12.044, -76.997, 2), "INDEPENDENCIA": (-11.990, -77.054, 2.5),
    "JESUS MARIA": (-12.070, -77.048, 1.3), "LA MOLINA": (-12.080, -76.930, 6), "LA VICTORIA": (-12.065, -77.015, 2),
    "LIMA": (-12.046, -77.030, 3), "CERCADO DE LIMA": (-12.046, -77.030, 3), "LINCE": (-12.084, -77.036, 1),
    "LOS OLIVOS": (-11.970, -77.072, 3), "LURIGANCHO": (-11.940, -76.693, 12), "CHOSICA": (-11.940, -76.693, 12),
    "LURIN": (-12.276, -76.870, 9), "MAGDALENA DEL MAR": (-12.095, -77.072, 1.5), "MIRAFLORES": (-12.121, -77.030, 2),
    "PACHACAMAC": (-12.257, -76.862, 14), "PUCUSANA": (-12.478, -76.796, 5), "PUEBLO LIBRE": (-12.072, -77.064, 1.5),
    "PUENTE PIEDRA": (-11.860, -77.074, 7), "PUNTA HERMOSA": (-12.334, -76.823, 4), "PUNTA NEGRA": (-12.365, -76.795, 4),
    "RIMAC": (-12.029, -77.043, 3), "SAN BARTOLO": (-12.388, -76.778, 3), "SAN BORJA": (-12.100, -77.000, 2.5),
    "SAN ISIDRO": (-12.097, -77.036, 2), "SAN JUAN DE LURIGANCHO": (-11.983, -77.002, 9),
    "SAN JUAN DE MIRAFLORES": (-12.155, -76.970, 4), "SAN LUIS": (-12.077, -77.000, 1.2),
    "SAN MARTIN DE PORRES": (-12.002, -77.087, 5), "SAN MIGUEL": (-12.077, -77.091, 2),
    "SANTA ANITA": (-12.046, -76.971, 2), "SANTA MARIA DEL MAR": (-12.399, -76.777, 2), "SANTA ROSA": (-11.800, -77.164, 6),
    "SANTIAGO DE SURCO": (-12.145, -76.993, 6), "SURQUILLO": (-12.111, -77.017, 1.5),
    "VILLA EL SALVADOR": (-12.213, -76.933, 6), "VILLA MARIA DEL TRIUNFO": (-12.165, -76.940, 5),
    "CALLAO": (-12.056, -77.118, 4), "BELLAVISTA": (-12.063, -77.126, 1.5),
    "CARMEN DE LA LEGUA REYNOSO": (-12.042, -77.093, 1.2), "LA PERLA": (-12.071, -77.117, 1.5),
    "LA PUNTA": (-12.072, -77.164, 1), "VENTANILLA": (-11.874, -77.128, 8), "MI PERU": (-11.855, -77.123, 2),
}


def _sin_tildes_mayusculas(texto: str) -> str:
    t = str(texto or "").upper()
    for a, b in (("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U"), ("Ü", "U"), ("Ñ", "N")):
        t = t.replace(a, b)
    return " ".join(t.split())


def _km_entre(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia en km entre dos puntos (fórmula de haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlng = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _aviso_gps_lejos_del_distrito(distrito: str, lat: float, lng: float) -> str:
    """Texto de aviso (con salto de línea inicial) si el punto está claramente lejos del distrito escrito;
    cadena vacía si está bien, si el distrito no está en la tabla o si no se puede saber."""
    try:
        clave = _sin_tildes_mayusculas(distrito)
        dato = _DISTRITOS_LIMA.get(clave)
        if dato is None:   # "San Isidro, Lima": se prueba con el texto antes de la coma
            dato = _DISTRITOS_LIMA.get(_sin_tildes_mayusculas(str(distrito).split(",")[0]))
        if dato is None:
            return ""
        km = _km_entre(lat, lng, dato[0], dato[1])
        if km <= dato[2] + MAD_AVISO_DISTRITO_MARGEN_KM:
            return ""
        return (f"\n\n⚠️ El punto del mapa parece estar a unos {km:.0f} km de *{' '.join(str(distrito).split())}*. "
                "Si el distrito o el lugar no son correctos, corrígelos antes de confirmar.")
    except Exception:  # noqa: BLE001
        return ""


# Distritos de Lima Metropolitana y del Callao: el servicio MAD/Ambulancia es solo ahí, así que el distrito
# se valida contra esta lista local (sin pasar por los códigos de ubigeo de sofi-apis, que son para Medicamentos).
_DISTRITOS_PROVINCIA_LIMA = [
    "Ancón", "Ate", "Barranco", "Breña", "Carabayllo", "Chaclacayo", "Chorrillos", "Cieneguilla", "Comas",
    "El Agustino", "Independencia", "Jesús María", "La Molina", "La Victoria", "Lima", "Lince", "Los Olivos",
    "Lurigancho", "Lurín", "Magdalena del Mar", "Miraflores", "Pachacámac", "Pucusana", "Pueblo Libre",
    "Puente Piedra", "Punta Hermosa", "Punta Negra", "Rímac", "San Bartolo", "San Borja", "San Isidro",
    "San Juan de Lurigancho", "San Juan de Miraflores", "San Luis", "San Martín de Porres", "San Miguel",
    "Santa Anita", "Santa María del Mar", "Santa Rosa", "Santiago de Surco", "Surquillo", "Villa El Salvador",
    "Villa María del Triunfo",
    # Provincia Constitucional del Callao
    "Callao", "Bellavista", "Carmen de la Legua Reynoso", "La Perla", "La Punta", "Mi Perú", "Ventanilla",
]
# Cómo la gente suele escribirlos (siglas y nombres alternativos).
_ALIAS_DISTRITOS_LIMA = {
    "SJL": "San Juan de Lurigancho", "SJM": "San Juan de Miraflores", "SMP": "San Martín de Porres",
    "VES": "Villa El Salvador", "VMT": "Villa María del Triunfo", "CERCADO": "Lima", "CERCADO DE LIMA": "Lima",
    "LIMA CERCADO": "Lima", "CHOSICA": "Lurigancho", "LURIGANCHO CHOSICA": "Lurigancho", "SURCO": "Santiago de Surco",
    "MAGDALENA": "Magdalena del Mar", "CALLAO CERCADO": "Callao", "CERCADO DEL CALLAO": "Callao", "MI PERU": "Mi Perú", "MAGDALENA DEL MAR": "Magdalena del Mar",
}


def validar_distrito_mad(parametros: dict, variables: dict) -> dict:
    """Valida el distrito escrito contra los de Lima Metropolitana. Siempre 200 con
    {"estado": "ok"|"varios"|"no", "distrito": nombre oficial (si ok), "lista": viñetas (si varios)}."""
    try:
        bruto = " ".join(str(parametros.get("distrito") or "").split())[:80]
        q = _sin_tildes_mayusculas(bruto)
        for prefijo in ("DISTRITO DE ", "DISTRITO "):
            if q.startswith(prefijo):
                q = q[len(prefijo):]
        q = q.split(",")[0].strip()
        for sufijo in (" LIMA LIMA", " LIMA"):
            if q.endswith(sufijo) and q != "LIMA" and q[:-len(sufijo)].strip() in {_sin_tildes_mayusculas(d) for d in _DISTRITOS_PROVINCIA_LIMA}:
                q = q[:-len(sufijo)].strip()
        if not q:
            return {"status": 200, "response": {"estado": "no"}}
        oficiales = {_sin_tildes_mayusculas(d): d for d in _DISTRITOS_PROVINCIA_LIMA}
        if q in oficiales:
            return {"status": 200, "response": {"estado": "ok", "distrito": oficiales[q]}}
        if q in _ALIAS_DISTRITOS_LIMA:
            return {"status": 200, "response": {"estado": "ok", "distrito": _ALIAS_DISTRITOS_LIMA[q]}}
        candidatos = []
        if len(q) >= 3:
            candidatos = [d for k, d in oficiales.items() if k.startswith(q)] or [d for k, d in oficiales.items() if q in k]
        if len(candidatos) == 1:
            return {"status": 200, "response": {"estado": "ok", "distrito": candidatos[0]}}
        if len(candidatos) > 1:
            return {"status": 200, "response": {"estado": "varios", "lista": "\n".join(f"• {d}" for d in candidatos[:8])}}
        return {"status": 200, "response": {"estado": "no"}}
    except Exception:  # noqa: BLE001
        logger.exception("validar_distrito_mad falló")
        return {"status": 200, "response": {"estado": "no"}}


def resumen_ubicacion_mad(parametros: dict, variables: dict) -> dict:
    """Prepara la confirmación: dirección normalizada (mín. 5 caracteres), línea de referencia, línea de precisión
    del GPS (o "lugar marcado en el mapa"), enlace a Google Maps y aviso si el punto queda lejos del distrito.
    200 -> {"direccion", "referencia_linea", "precision_linea", "mapa_url", "aviso"}; 400 -> {"error"}.
    Las coordenadas solo van en el enlace al mapa; no se escriben en los logs (datos personales)."""
    try:
        direccion = " ".join(str(parametros.get("direccion") or "").split())[:200]
        if len(direccion) < MAD_DIRECCION_MIN_CARACTERES:
            return {"status": 400, "response": {"error": (
                "La dirección es muy corta 🤔 Escribe la dirección exacta: calle, número y departamento o piso "
                f"(mínimo {MAD_DIRECCION_MIN_CARACTERES} caracteres).")}}
        distrito = " ".join(str(parametros.get("distrito") or "").split())[:80]
        lat = float(str(parametros.get("lat")).replace(",", "."))
        lng = float(str(parametros.get("lng")).replace(",", "."))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("coordenadas fuera de rango")
        referencia = " ".join(str(parametros.get("referencia") or "").split())[:200]
        if str(parametros.get("origen") or "").strip() == "mapa":
            precision_linea = "\n📌 Lugar marcado por ti en el mapa"
        else:
            try:
                metros = int(round(float(str(parametros.get("precision")).replace(",", "."))))
            except (TypeError, ValueError):
                metros = None
            precision_linea = f"\n🎯 Precisión del GPS: ±{metros} m" if metros is not None and metros >= 0 else ""
        return {"status": 200, "response": {
            "direccion": direccion,
            "referencia_linea": f"\n🧭 Ref: {referencia}" if referencia else "",
            "precision_linea": precision_linea,
            "mapa_url": f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}",
            "aviso": _aviso_gps_lejos_del_distrito(distrito, lat, lng),
        }}
    except Exception:  # noqa: BLE001
        logger.exception("resumen_ubicacion_mad falló")
        return {"status": 400, "response": {"error": (
            "No pude leer la ubicación o la dirección. Escribe de nuevo la dirección o comparte otra ubicación.")}}


def _texto_estado_mad(datos: dict, tipo: str, minutos_buscando) -> str:
    """Texto amigable para el cliente según el estado que devuelve sofi-apis."""
    estado = str(datos.get("estado") or "").strip()
    quien = "tu ambulancia" if str(datos.get("tipo_servicio") or tipo).upper() == "AMBULANCIA" else "tu médico"
    proveedor = str(datos.get("proveedor_nombre") or "").strip()
    nombre = f"*{proveedor}*" if proveedor else quien
    if estado == "programada":
        cuando = str(datos.get("programada_texto") or "").strip()
        return (f"📅 Tu servicio está *programado para {cuando}*. Un profesional se asignará aproximadamente "
                "45 minutos antes y te avisaremos por SMS." if cuando else
                "📅 Tu servicio está programado. Un profesional se asignará aproximadamente 45 minutos antes "
                "y te avisaremos por SMS.")
    if estado == "cancelada":
        return "❌ Este servicio fue cancelado. Si necesitas uno nuevo, escribe *menu* para volver a empezar."
    if estado == "buscando_proveedor":
        texto = "🔎 Seguimos buscando al profesional más cercano. Te avisaremos apenas alguien acepte tu solicitud."
        if minutos_buscando is not None and minutos_buscando >= MAD_MINUTOS_BUSCANDO_LARGO:
            texto += ("\n\n⏳ Está tardando más de lo normal. Si prefieres, toca *Hablar c/ asesor* "
                      "y te ayudamos de inmediato.")
        return texto
    if estado == "asignado":
        return f"✅ {nombre} aceptó tu solicitud y se está preparando para salir."
    if estado == "en_camino":
        return f"🚗 {nombre} va en camino a tu ubicación."
    if estado == "llego":
        return f"📍 {nombre} llegó a tu ubicación."
    if estado == "en_atencion":
        return f"🩺 Estás siendo atendido por {nombre}."
    if estado == "finalizado":
        return "🏁 El servicio finalizó. ¡Gracias por confiar en Sanitas! 💙"
    if estado == "sin_cobertura":
        return ("😔 En este momento no encontramos un profesional disponible cerca de tu ubicación. "
                "Toca *Hablar c/ asesor* para que te ayudemos.")
    return "Tu solicitud está registrada. Consulta de nuevo en unos instantes."


def estado_solicitud_mad(parametros: dict, variables: dict) -> dict:
    """Consulta el estado de la solicitud (GET /track/<token>/estado). Devuelve
    {"status", "response": {"estado", "mensaje", ...}}; status != 200 si no se pudo consultar."""
    try:
        token = str(parametros.get("token") or "").strip()
        if not _MAD_TOKEN_VALIDO.fullmatch(token):   # evita rutas raras en la URL
            logger.error("estado_solicitud_mad sin token válido")
            return {"status": 400, "response": {}}
        resp = requests.get(f"{SOFI_APIS_BASE}/track/{token}/estado",
                            headers=_headers_sofi_apis(), timeout=TIMEOUT_MAD)
        logger.info("estado_solicitud_mad -> status=%s", resp.status_code)
        if resp.status_code != 200:
            logger.error("estado_solicitud_mad respondió HTTP %s", resp.status_code)
            return {"status": resp.status_code, "response": {}}
        datos = resp.json() or {}
        # Si es programado, el texto "Programado para <fecha y hora>" lo arma el webchat (hora de Lima) a partir de
        # lo que la persona eligió; y el aviso de "está tardando" cuenta desde la hora programada, no desde que
        # se creó la solicitud (días antes).
        datos["programada_texto"] = str(parametros.get("programada_texto") or "").strip()
        minutos = None
        try:
            base = None
            programada = str(parametros.get("programada_para") or "").strip()
            if programada and _PROGRAMADA_RE.fullmatch(programada):
                base = datetime.strptime(programada, "%Y-%m-%dT%H:%M").replace(tzinfo=LIMA_UTC_MENOS_5)
            else:
                base = datetime.fromisoformat(str(parametros.get("creada_en") or ""))
            minutos = (datetime.now(base.tzinfo or LIMA_UTC_MENOS_5) - base).total_seconds() / 60
            if minutos < 0:
                minutos = None
        except ValueError:
            pass
        return {"status": 200, "response": {
            "estado": datos.get("estado"),
            "proveedor_nombre": datos.get("proveedor_nombre"),
            "mensaje": _texto_estado_mad(datos, str(parametros.get("tipo") or ""), minutos),
        }}
    except Exception:  # noqa: BLE001
        logger.exception("estado_solicitud_mad falló")
        return {"status": 500, "response": {}}


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
