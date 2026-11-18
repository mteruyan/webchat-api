"""siniestralidad.py — Autorización de entrega de reporte de siniestralidad, digital (nov-2026).

Antes: el asesor le pedía al cliente llenar un Word/PDF, imprimirlo, firmarlo y enviarlo escaneado.
Ahora:
  1. El asesor, desde su consola (botón "📄 Siniestr."), pone el N° de contrato y el correo del cliente y
     envía un enlace único (vence en 48 h): por correo (Danaconnect) y/o dentro del chat.
  2. El cliente abre /siniestralidad/<token>: (1) sube la foto de su documento (se lee con Gemini para
     autocompletar y avisar si no coincide), (2) completa los datos que faltan (correo, dependientes menores)
     y (3) firma en la pantalla.
  3. Se genera el PDF del formato "AUTORIZACIÓN DE ENTREGA DE REPORTE DE SINIESTROS SALUD" (el mismo texto
     del Word) con la firma y, en la 2.ª hoja, la foto del documento. El PDF llega a la conversación del
     asesor como un archivo del cliente (si la conversación ya se cerró, le llega el aviso con el enlace).

Correo: Danaconnect, conversation DANACONNECT_SINIESTRALIDAD_CONV (variables EMAIL, NOMBRE, URL, VENCE),
con el mismo DANACONNECT_AUTH que ya usa el webchat. Si la variable no está configurada, el envío por
correo queda deshabilitado (el enlace por chat sigue funcionando).

Tabla: migracion_siniestralidad.sql. Archivos en el bucket bajo webchat/siniestralidad/<id>/ (nombres
aleatorios, para que /archivo-receta los pueda abrir desde la consola).
"""

import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturoTimeout
from datetime import datetime, timedelta, timezone

import requests

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

VIGENCIA_HORAS = 48
MAX_DEPENDIENTES = 5
MAX_INTENTOS_DNI = 12
OCR_TIEMPO_MAX_SEG = 25        # el navegador espera la lectura hasta 35 s
DANA_CONV = os.environ.get("DANACONNECT_SINIESTRALIDAD_CONV", "").strip()
URL_PUBLICA = "https://api.sanitasperu.com"        # main.py la reemplaza por la URL pública real
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "imagenes", "logo-sanitas.png")

# main.py conecta estas funciones (Cloud Storage):  SUBIR(ruta, bytes, content_type)  y  DESCARGAR(ruta) -> bytes
SUBIR = None
DESCARGAR = None

TIPOS_DOCUMENTO = {"DNI": "DNI", "CE": "CE", "OTRO": "Otro"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]

_RE_CORREO = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_RE_NOMBRE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{3,120}$")
_RE_CONTRATO = re.compile(r"^[A-Za-z0-9 /\-.]{1,30}$")
_RE_CIUDAD = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{2,40}$")
_RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,40}$")

_ocr_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ocr-sin")
_aviso_migracion = False


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _falta_migracion(e: Exception) -> bool:
    global _aviso_migracion
    if "solicitudes_siniestralidad" in str(e):
        if not _aviso_migracion:
            logger.warning("siniestralidad: falta ejecutar migracion_siniestralidad.sql")
            _aviso_migracion = True
        return True
    return False


def _limpio(valor, largo=120) -> str:
    return re.sub(r"\s+", " ", str(valor or "")).strip()[:largo]


def _mayus(valor, largo=120) -> str:
    return _limpio(valor, largo).upper()


def _validar_correo(valor) -> str | None:
    c = _limpio(valor, 120).lower()
    return c if _RE_CORREO.match(c) else None


def _validar_documento(tipo: str, numero) -> str | None:
    n = re.sub(r"[\s.\-]", "", str(numero or "")).upper()
    if tipo == "DNI":
        return n if re.fullmatch(r"\d{8}", n) else None
    if tipo == "CE":
        return n if re.fullmatch(r"[A-Z0-9]{8,12}", n) else None
    return n if re.fullmatch(r"[A-Z0-9]{5,20}", n) else None


def _fecha_hora(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M")


def _primer_nombre(nombre) -> str:
    partes = _limpio(nombre).split(" ")
    return partes[0].capitalize() if partes and partes[0] else ""


def _url(token: str) -> str:
    return f"{URL_PUBLICA}/siniestralidad/{token}"


def correo_configurado() -> bool:
    return bool(DANA_CONV) and bool(os.environ.get("DANACONNECT_AUTH"))


def _enmascarar(numero: str) -> str:
    n = str(numero or "")
    return ("•" * max(0, len(n) - 3) + n[-3:]) if len(n) > 3 else n


# ---------------------------------------------------------------------------
# Asesor: crear, listar y anular
# ---------------------------------------------------------------------------
def crear_solicitud(conversacion_id: int, asesor_id: int, datos: dict) -> dict:
    """Crea la solicitud y envía el correo (si corresponde). Devuelve {"ok", "motivo"|("id","url","vence",...)}.
    El texto para el chat lo publica main.py (necesita el WebSocket)."""
    datos = datos if isinstance(datos, dict) else {}
    por_correo = bool(datos.get("por_correo"))
    por_chat = bool(datos.get("por_chat"))
    if not por_correo and not por_chat:
        return {"ok": False, "motivo": "Elige al menos un medio de envío (correo o chat)."}
    if por_correo and not correo_configurado():
        return {"ok": False, "motivo": "El envío por correo aún no está configurado. Envíalo por el chat."}
    correo = None
    if _limpio(datos.get("correo")):
        correo = _validar_correo(datos.get("correo"))
        if not correo:
            return {"ok": False, "motivo": "El correo no es válido."}
    if por_correo and not correo:
        return {"ok": False, "motivo": "Escribe el correo del cliente."}
    contrato = _mayus(datos.get("contrato"), 30)
    if not _RE_CONTRATO.match(contrato or ""):
        return {"ok": False, "motivo": "Escribe el N° de contrato (letras, números, - o /)."}
    tipo = str(datos.get("tipo_documento") or "DNI").upper()
    tipo = tipo if tipo in TIPOS_DOCUMENTO else "DNI"
    n_doc = None
    if _limpio(datos.get("n_documento")):
        n_doc = _validar_documento(tipo, datos.get("n_documento"))
        if not n_doc:
            return {"ok": False, "motivo": "El N° de documento no es válido para el tipo elegido."}
    nombre = _mayus(datos.get("nombre"))
    if nombre and not _RE_NOMBRE.match(nombre):
        nombre = ""

    token = secrets.token_urlsafe(18)
    ahora = datetime.now(LIMA_TZ)
    vence = ahora + timedelta(hours=VIGENCIA_HORAS)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Un enlace vigente por conversación: el anterior (sin firmar) se anula.
            cur.execute("""UPDATE solicitudes_siniestralidad SET estado = 'anulado', anulado_en = %s
                           WHERE conversacion_id = %s AND estado IN ('enviado', 'abierto')""",
                        (ahora, conversacion_id))
            cur.execute("""INSERT INTO solicitudes_siniestralidad
                             (token, conversacion_id, asesor_id, correo, nombre, tipo_documento, n_documento, contrato,
                              por_correo, por_chat, creado_en, vence_en)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (token, conversacion_id, asesor_id, correo, nombre or None, tipo, n_doc, contrato,
                         por_correo, por_chat, ahora, vence))
            sid = cur.fetchone()[0]
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_siniestralidad.sql."}
        logger.exception("siniestralidad: no se pudo crear la solicitud (conversación %s)", conversacion_id)
        return {"ok": False, "motivo": "No se pudo crear la solicitud. Intenta de nuevo."}
    finally:
        release_connection(conn)

    resultado = {"ok": True, "id": sid, "url": _url(token), "vence": _fecha_hora(vence),
                 "por_chat": por_chat, "por_correo": por_correo, "correo": correo}
    if por_correo:
        ok, error = _enviar_correo(correo, nombre, _url(token), vence)
        _guardar_resultado_correo(sid, ok, error)
        resultado["correo_ok"] = ok
        if not ok:
            resultado["aviso"] = ("No se pudo enviar el correo" + (" (" + error + ")" if error else "") +
                                  ". Puedes enviarlo por el chat.")
    logger.info("siniestralidad: solicitud %s creada (conversación %s, asesor %s, correo=%s, chat=%s)",
                sid, conversacion_id, asesor_id, por_correo, por_chat)
    return resultado


def _enviar_correo(correo: str, nombre: str, url: str, vence: datetime) -> tuple[bool, str]:
    payload = {"EMAIL": correo, "NOMBRE": _primer_nombre(nombre) or "cliente", "URL": url,
               "VENCE": vence.astimezone(LIMA_TZ).strftime("%d/%m/%Y a las %H:%M")}
    headers = {"Content-Type": "application/json", "Authorization": os.environ.get("DANACONNECT_AUTH", "")}
    endpoint = f"https://appserv.danaconnect.com/api/1.0/rest/conversation/{DANA_CONV}/start/data"
    error = ""
    for intento in (1, 2):
        try:
            resp = requests.post(endpoint, data=json.dumps(payload), headers=headers, timeout=15)
            logger.info("siniestralidad: correo (intento %s) -> status=%s body=%s", intento, resp.status_code,
                        resp.text[:300])
            if resp.ok:
                return True, ""
            error = f"Danaconnect respondió {resp.status_code}"
            if resp.status_code < 500:
                break
        except requests.RequestException as e:
            error = "sin respuesta de Danaconnect"
            logger.warning("siniestralidad: error al enviar el correo (intento %s): %s", intento, e)
    return False, error


def _guardar_resultado_correo(sid: int, ok: bool, error: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE solicitudes_siniestralidad SET correo_ok = %s, correo_error = %s WHERE id = %s",
                        (ok, error or None, sid))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        release_connection(conn)


def bandeja_asesor(asesor_id: int, dias: int = 15) -> list:
    """Solicitudes enviadas por el asesor en los últimos días (para su bandeja)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, conversacion_id, nombre, n_documento, contrato, correo, estado, por_correo,
                                  por_chat, correo_ok, creado_en, vence_en, abierto_en, firmado_en, ruta_pdf, datos
                           FROM solicitudes_siniestralidad
                           WHERE asesor_id = %s AND creado_en >= now() - make_interval(days => %s)
                           ORDER BY creado_en DESC LIMIT 40""", (asesor_id, dias))
            filas = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if not _falta_migracion(e):
            logger.warning("siniestralidad: no se pudo leer la bandeja", exc_info=True)
        return []
    finally:
        release_connection(conn)
    ahora = datetime.now(LIMA_TZ)
    salida = []
    for (sid, conv, nombre, ndoc, contrato, correo, estado, p_correo, p_chat, c_ok, creado, vence, abierto,
         firmado, ruta_pdf, datos) in filas:
        if estado in ("enviado", "abierto") and vence and vence < ahora:
            estado = "vencido"
        d = datos if isinstance(datos, dict) else {}
        salida.append({
            "id": sid, "conversacion_id": conv,
            "nombre": d.get("nombre") or nombre or "", "n_documento": d.get("n_documento") or ndoc or "",
            "contrato": contrato, "correo": correo or "", "estado": estado,
            "por_correo": p_correo, "por_chat": p_chat, "correo_ok": c_ok,
            "creado": _fecha_hora(creado), "vence": _fecha_hora(vence), "abierto": _fecha_hora(abierto),
            "firmado": _fecha_hora(firmado), "ruta_pdf": ruta_pdf or "",
            "dependientes": len(d.get("dependientes") or []),
        })
    return salida


def anular(asesor_id: int, sid) -> bool:
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE solicitudes_siniestralidad SET estado = 'anulado', anulado_en = now()
                           WHERE id = %s AND asesor_id = %s AND estado IN ('enviado', 'abierto')""", (sid, asesor_id))
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception:  # noqa: BLE001
        conn.rollback()
        return False
    finally:
        release_connection(conn)


def texto_chat(url: str, vence: str) -> str:
    return ("📄 Para enviarte tu reporte de siniestralidad necesitamos tu autorización firmada. "
            "Complétala aquí desde tu celular (toma unos 2 minutos): sube la foto de tu documento, "
            f"revisa tus datos y firma en la pantalla.\n{url}\nEl enlace vence el {vence}.")


# ---------------------------------------------------------------------------
# Cliente: abrir, subir documento y firmar
# ---------------------------------------------------------------------------
def _cargar(cur, token: str, bloquear: bool = False):
    if not _RE_TOKEN.match(token or ""):
        return None
    cur.execute(f"""SELECT id, conversacion_id, asesor_id, correo, nombre, tipo_documento, n_documento, contrato,
                           estado, vence_en, ruta_dni_anverso, ruta_dni_reverso, ocr, intentos_dni, datos, abierto_en
                    FROM solicitudes_siniestralidad WHERE token = %s {'FOR UPDATE' if bloquear else ''}""", (token,))
    f = cur.fetchone()
    if not f:
        return None
    claves = ("id", "conversacion_id", "asesor_id", "correo", "nombre", "tipo_documento", "n_documento", "contrato",
              "estado", "vence_en", "ruta_dni_anverso", "ruta_dni_reverso", "ocr", "intentos_dni", "datos", "abierto_en")
    return dict(zip(claves, f))


def _estado_no_disponible(sol) -> dict | None:
    """Respuesta para el cliente si el enlace ya no sirve (None si está vigente)."""
    if sol is None:
        return {"status": 404, "body": {"ok": False, "estado": "no_existe",
                                        "motivo": "Este enlace no es válido. Pídele uno nuevo a tu asesor."}}
    if sol["estado"] == "firmado":
        return {"status": 409, "body": {"ok": False, "estado": "firmado",
                                        "motivo": "Ya firmaste esta autorización. Tu asesor ya la tiene."}}
    if sol["estado"] == "anulado":
        return {"status": 410, "body": {"ok": False, "estado": "anulado",
                                        "motivo": "Este enlace fue reemplazado por uno nuevo. Usa el último que te enviamos."}}
    if sol["vence_en"] and sol["vence_en"] < datetime.now(LIMA_TZ):
        return {"status": 410, "body": {"ok": False, "estado": "vencido",
                                        "motivo": "Este enlace venció. Escríbenos por el webchat y tu asesor te enviará uno nuevo."}}
    return None


def datos_cliente(token: str) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token)
            no = _estado_no_disponible(sol)
            if no:
                return no
            if not sol["abierto_en"]:
                cur.execute("""UPDATE solicitudes_siniestralidad SET abierto_en = now(),
                                      estado = CASE WHEN estado = 'enviado' THEN 'abierto' ELSE estado END
                               WHERE id = %s""", (sol["id"],))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if not _falta_migracion(e):
            logger.exception("siniestralidad: error al abrir el enlace")
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos cargar tu formulario. Intenta en unos minutos."}}
    finally:
        release_connection(conn)
    ocr = sol["ocr"] if isinstance(sol["ocr"], dict) else {}
    return {"status": 200, "body": {
        "ok": True, "id": sol["id"],
        "nombre": sol["nombre"] or "", "tipo_documento": sol["tipo_documento"] or "DNI",
        "n_documento": sol["n_documento"] or "", "contrato": sol["contrato"], "correo": sol["correo"] or "",
        "vence": _fecha_hora(sol["vence_en"]),
        "dni_anverso": bool(sol["ruta_dni_anverso"]), "dni_reverso": bool(sol["ruta_dni_reverso"]),
        "ocr": ocr, "max_dependientes": MAX_DEPENDIENTES,
    }}


def guardar_documento(token: str, lado: str, contenido: bytes, extension: str, content_type: str) -> dict:
    """Guarda la foto del documento (anverso/reverso). La lectura con Gemini va aparte (leer_guardado)."""
    if lado not in ("anverso", "reverso"):
        return {"status": 400, "body": {"ok": False, "motivo": "Lado inválido."}}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token, bloquear=True)
            no = _estado_no_disponible(sol)
            if no:
                conn.rollback()
                return no
            if sol["intentos_dni"] >= MAX_INTENTOS_DNI:
                conn.rollback()
                return {"status": 429, "body": {"ok": False, "motivo": "Llegaste al máximo de fotos. Escríbenos por el webchat."}}
            cur.execute("UPDATE solicitudes_siniestralidad SET intentos_dni = intentos_dni + 1 WHERE id = %s", (sol["id"],))
        conn.commit()
    finally:
        release_connection(conn)

    ruta = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.{extension}"
    try:
        SUBIR(ruta, contenido, content_type)
    except Exception:  # noqa: BLE001
        logger.exception("siniestralidad: no se pudo subir el documento (solicitud %s)", sol["id"])
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos guardar la foto. Intenta de nuevo."}}

    columna = "ruta_dni_anverso" if lado == "anverso" else "ruta_dni_reverso"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if lado == "anverso":       # foto nueva: la lectura anterior ya no vale
                cur.execute(f"UPDATE solicitudes_siniestralidad SET {columna} = %s, ocr = NULL WHERE id = %s",
                            (ruta, sol["id"]))
            else:
                cur.execute(f"UPDATE solicitudes_siniestralidad SET {columna} = %s WHERE id = %s", (ruta, sol["id"]))
        conn.commit()
    finally:
        release_connection(conn)
    return {"status": 200, "body": {"ok": True, "lado": lado}}


def leer_guardado(token: str) -> dict:
    """Lee con Gemini la foto (anverso) ya guardada. Va en una petición aparte para que la subida responda al
    instante: si Gemini tarda o falla, el cliente igual continúa y escribe sus datos."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token)
        conn.commit()
    finally:
        release_connection(conn)
    no = _estado_no_disponible(sol)
    if no:
        return no
    if not sol["ruta_dni_anverso"]:
        return {"status": 400, "body": {"ok": False, "motivo": "Primero sube la foto de tu documento."}}
    if isinstance(sol["ocr"], dict) and sol["ocr"]:
        ocr = sol["ocr"]                                   # ya se leyó esta foto
    else:
        try:
            contenido = DESCARGAR(sol["ruta_dni_anverso"])
        except Exception:  # noqa: BLE001
            logger.warning("siniestralidad: no se pudo descargar la foto para leerla", exc_info=True)
            return {"status": 200, "body": {"ok": False, "ocr": {}}}
        tipo = "image/png" if sol["ruta_dni_anverso"].endswith(".png") else (
            "image/webp" if sol["ruta_dni_anverso"].endswith(".webp") else "image/jpeg")
        ocr = leer_documento(contenido, tipo)
        if ocr is None:
            return {"status": 200, "body": {"ok": False, "ocr": {}}}
        ocr["coincide"] = _coincide(sol, ocr)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""UPDATE solicitudes_siniestralidad SET ocr = %s
                               WHERE id = %s AND ruta_dni_anverso = %s""",
                            (json.dumps(ocr, ensure_ascii=False), sol["id"], sol["ruta_dni_anverso"]))
            conn.commit()
        finally:
            release_connection(conn)
    respuesta = {"ok": True, "ocr": ocr}
    if ocr.get("coincide") is False and sol["n_documento"]:
        respuesta["aviso_no_coincide"] = (
            f"El número de la foto no coincide con el que registró tu asesor (termina en {sol['n_documento'][-3:]}). "
            "Revisa que sea tu documento.")
    return {"status": 200, "body": respuesta}


def _coincide(sol, ocr) -> bool | None:
    esperado = re.sub(r"\W", "", str(sol.get("n_documento") or "")).upper()
    leido = re.sub(r"\W", "", str(ocr.get("numero") or "")).upper()
    if not esperado or not leido:
        return None
    return esperado == leido


_PROMPT_OCR = """Eres un lector de documentos de identidad peruanos. Mira la imagen y responde SOLO un JSON con:
{"es_documento": true|false,   // true si es un documento de identidad (DNI, carné de extranjería, pasaporte) legible
 "tipo": "DNI"|"CE"|"OTRO",
 "numero": "número del documento sin espacios ni guiones (en el DNI son 8 dígitos; NO uses el dígito verificador)",
 "nombres": "nombres tal como aparecen",
 "apellidos": "apellido paterno y materno tal como aparecen",
 "legible": true|false}
Si un dato no se lee, déjalo como cadena vacía. No inventes datos."""


def leer_documento(contenido: bytes, content_type: str) -> dict | None:
    """Lee el documento con Gemini (best-effort: None si falla o tarda más de OCR_TIEMPO_MAX_SEG)."""
    def _leer():
        import planes
        from google.genai import types
        r, _modelo = planes._generar([types.Part.from_bytes(data=contenido, mime_type=content_type or "image/jpeg"),
                                      _PROMPT_OCR], paciencia=10, temperature=0,
                                     response_mime_type="application/json", max_output_tokens=400)
        texto = (r.text or "").strip()
        texto = re.sub(r"^```(?:json)?|```$", "", texto).strip()
        return json.loads(texto)
    inicio = time.monotonic()
    try:
        d = _ocr_pool.submit(_leer).result(timeout=OCR_TIEMPO_MAX_SEG)
        logger.info("siniestralidad: documento leído en %.1f s", time.monotonic() - inicio)
    except FuturoTimeout:
        logger.warning("siniestralidad: la lectura del documento tardó más de %s s", OCR_TIEMPO_MAX_SEG)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("siniestralidad: no se pudo leer el documento", exc_info=True)
        return None
    if not isinstance(d, dict):
        return None
    tipo = str(d.get("tipo") or "").upper()
    numero = re.sub(r"[\s.\-]", "", str(d.get("numero") or "")).upper()[:20]
    nombre = _mayus(f"{d.get('nombres') or ''} {d.get('apellidos') or ''}")
    return {"es_documento": bool(d.get("es_documento")), "legible": bool(d.get("legible", True)),
            "tipo": tipo if tipo in TIPOS_DOCUMENTO else "OTRO", "numero": numero,
            "nombre": nombre if _RE_NOMBRE.match(nombre or "") else ""}


def _validar_formulario(f: dict) -> tuple[dict | None, str]:
    f = f if isinstance(f, dict) else {}
    ciudad = _limpio(f.get("ciudad"), 40).title()
    if not _RE_CIUDAD.match(ciudad or ""):
        return None, "Escribe tu ciudad."
    nombre = _mayus(f.get("nombre"))
    if not _RE_NOMBRE.match(nombre or "") or len(nombre.split(" ")) < 2:
        return None, "Escribe tus nombres y apellidos completos."
    tipo = str(f.get("tipo_documento") or "").upper()
    if tipo not in TIPOS_DOCUMENTO:
        return None, "Elige el tipo de documento."
    n_doc = _validar_documento(tipo, f.get("n_documento"))
    if not n_doc:
        return None, "El número de documento no es válido (el DNI tiene 8 dígitos)."
    correo = _validar_correo(f.get("correo"))
    if not correo:
        return None, "Escribe un correo válido para recibir tu reporte."
    dependientes = []
    parentesco = ""
    if f.get("con_dependientes"):
        parentesco = _limpio(f.get("parentesco"), 40).lower()
        if len(parentesco) < 2:
            return None, "Indica el parentesco de tus dependientes (por ejemplo: hijos)."
        for d in (f.get("dependientes") or [])[:MAX_DEPENDIENTES]:
            if not isinstance(d, dict):
                continue
            dn, dd = _mayus(d.get("nombre")), re.sub(r"[\s.\-]", "", str(d.get("n_documento") or "")).upper()
            if not dn and not dd:
                continue
            if not _RE_NOMBRE.match(dn or "") or len(dn.split(" ")) < 2:
                return None, "Escribe nombres y apellidos completos de cada dependiente."
            if not re.fullmatch(r"[A-Z0-9]{5,20}", dd or ""):
                return None, f"Revisa el N° de documento de {dn.title()}."
            dependientes.append({"nombre": dn, "n_documento": dd})
        if not dependientes:
            return None, "Agrega al menos un dependiente o desmarca la opción."
    if not f.get("acepta"):
        return None, "Debes aceptar la autorización para continuar."
    return {"ciudad": ciudad, "nombre": nombre, "tipo_documento": tipo, "n_documento": n_doc, "correo": correo,
            "parentesco": parentesco, "dependientes": dependientes}, ""


def _validar_firma(png: bytes) -> bool:
    try:
        from PIL import Image
        if not png or len(png) > 2_000_000:
            return False
        img = Image.open(io.BytesIO(png))
        if img.format != "PNG" or img.width < 150 or img.height < 60 or img.width * img.height > 8_000_000:
            return False
        alfa = img.convert("RGBA").getchannel("A")
        tinta = sum(alfa.histogram()[40:])           # píxeles con trazo
        return tinta >= 150
    except Exception:  # noqa: BLE001
        return False


def firmar(token: str, formulario: dict, firma_png: bytes, ip: str, agente: str) -> dict:
    """Valida, genera el PDF, lo guarda y lo deja en la conversación (si no está cerrada).
    Devuelve {"status", "body"} para el cliente y, si salió bien, "entrega" con lo que main.py debe avisar."""
    datos, motivo = _validar_formulario(formulario)
    if not datos:
        return {"status": 400, "body": {"ok": False, "motivo": motivo}}
    if not _validar_firma(firma_png):
        return {"status": 400, "body": {"ok": False, "motivo": "Dibuja tu firma en el recuadro."}}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token)
        conn.commit()
    finally:
        release_connection(conn)
    no = _estado_no_disponible(sol)
    if no:
        return no
    if not sol["ruta_dni_anverso"]:
        return {"status": 400, "body": {"ok": False, "motivo": "Primero sube la foto de tu documento."}}

    ahora = datetime.now(LIMA_TZ)
    ocr = sol["ocr"] if isinstance(sol["ocr"], dict) else {}
    try:
        fotos = [DESCARGAR(sol["ruta_dni_anverso"])]
        if sol["ruta_dni_reverso"]:
            fotos.append(DESCARGAR(sol["ruta_dni_reverso"]))
        pdf = generar_pdf(datos, sol["contrato"], firma_png, fotos, ahora, sol["id"])
        ruta_pdf = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.pdf"
        ruta_firma = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.png"
        SUBIR(ruta_firma, firma_png, "image/png")
        SUBIR(ruta_pdf, pdf, "application/pdf")
    except Exception:  # noqa: BLE001
        logger.exception("siniestralidad: no se pudo generar o guardar el PDF (solicitud %s)", sol["id"])
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos generar tu documento. Intenta de nuevo."}}

    nombre_pdf = f"Autorizacion_siniestralidad_{datos['n_documento']}.pdf"
    datos_guardar = dict(datos, ocr_numero=ocr.get("numero") or "",
                         ocr_coincide=(re.sub(r"\W", "", ocr.get("numero") or "").upper() == datos["n_documento"])
                         if ocr.get("numero") else None)
    entrega = {"solicitud_id": sol["id"], "conversacion_id": sol["conversacion_id"], "asesor_id": sol["asesor_id"],
               "ruta_pdf": ruta_pdf, "nombre_pdf": nombre_pdf, "nombre": datos["nombre"], "contrato": sol["contrato"],
               "n_documento": datos["n_documento"], "dependientes": len(datos["dependientes"]),
               "firmado": _fecha_hora(ahora), "mensaje_id": None, "asesor_actual": None, "estado_conv": None,
               "texto_cliente": None, "alerta": None}
    if datos_guardar["ocr_coincide"] is False:
        entrega["alerta"] = (f"El N° leído en la foto del documento ({datos_guardar['ocr_numero']}) no coincide con el "
                             f"N° escrito ({datos['n_documento']}). Revisa el PDF.")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT estado FROM solicitudes_siniestralidad WHERE id = %s FOR UPDATE", (sol["id"],))
            fila = cur.fetchone()
            if not fila or fila[0] not in ("enviado", "abierto"):
                conn.rollback()
                return {"status": 409, "body": {"ok": False, "estado": "firmado",
                                                "motivo": "Esta autorización ya fue firmada."}}
            cur.execute("""UPDATE solicitudes_siniestralidad
                           SET estado = 'firmado', firmado_en = %s, datos = %s, ruta_pdf = %s, ruta_firma = %s,
                               ip_firma = %s, agente_firma = %s
                           WHERE id = %s""",
                        (ahora, json.dumps(datos_guardar, ensure_ascii=False), ruta_pdf, ruta_firma,
                         (ip or "")[:60], (agente or "")[:300], sol["id"]))
            if sol["conversacion_id"]:
                cur.execute("SELECT estado, asesor_id FROM conversaciones WHERE id = %s", (sol["conversacion_id"],))
                conv = cur.fetchone()
                if conv:
                    entrega["estado_conv"], entrega["asesor_actual"] = conv[0], conv[1]
                if conv and conv[0] != "cerrada":
                    cur.execute("""INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, metadata, enviado_en)
                                   VALUES (%s, 'usuario', 'archivo', %s, %s, %s) RETURNING id""",
                                (sol["conversacion_id"], ruta_pdf,
                                 json.dumps({"siniestralidad_id": sol["id"], "nombre": nombre_pdf}, ensure_ascii=False),
                                 ahora))
                    entrega["mensaje_id"] = cur.fetchone()[0]
                    texto = ("✅ Recibimos tu autorización firmada para la entrega de tu reporte de siniestralidad "
                             f"(contrato N° {sol['contrato']}). Tu asesor continuará con tu atención.")
                    cur.execute("""INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                                   VALUES (%s, 'bot', 'texto', %s, %s)""", (sol["conversacion_id"], texto, ahora))
                    entrega["texto_cliente"] = texto
                    try:
                        cur.execute("SAVEPOINT sp_nombre_pdf")
                        cur.execute("""INSERT INTO archivos_nombres (ruta, nombre, conversacion_id, creado_en)
                                       VALUES (%s, %s, %s, %s) ON CONFLICT (ruta) DO NOTHING""",
                                    (ruta_pdf, nombre_pdf, sol["conversacion_id"], ahora))
                        cur.execute("RELEASE SAVEPOINT sp_nombre_pdf")
                    except Exception:  # noqa: BLE001 — sin la tabla de nombres solo se ve "Documento PDF"
                        cur.execute("ROLLBACK TO SAVEPOINT sp_nombre_pdf")
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("siniestralidad: no se pudo registrar la firma (solicitud %s)", sol["id"])
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos registrar tu firma. Intenta de nuevo."}}
    finally:
        release_connection(conn)
    logger.info("siniestralidad: solicitud %s firmada (conversación %s)", sol["id"], sol["conversacion_id"])
    return {"status": 200, "body": {"ok": True, "correo": datos["correo"]}, "entrega": entrega}


# ---------------------------------------------------------------------------
# PDF (mismo texto del Word "SINIESTRALIDAD - FORMATO 3.5")
# ---------------------------------------------------------------------------
def _esc(t) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def generar_pdf(datos: dict, contrato: str, firma_png: bytes, fotos: list, ahora: datetime, solicitud_id) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (Image as RLImage, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10.5, leading=15, alignment=TA_JUSTIFY)
    titulo = ParagraphStyle("titulo", parent=base, fontName="Helvetica-Bold", fontSize=12, alignment=TA_CENTER,
                            spaceAfter=14)
    derecha = ParagraphStyle("derecha", parent=base, alignment=TA_RIGHT)
    negrita = ParagraphStyle("negrita", parent=base, fontName="Helvetica-Bold")
    pie = ParagraphStyle("pie", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#6B7A82"))
    centro = ParagraphStyle("centro", parent=base, alignment=TA_CENTER, leading=13)

    def b(t):
        return f"<b>{_esc(t)}</b>"

    fecha_larga = f"{ahora.day} de {MESES[ahora.month - 1]} del {ahora.year}"
    tipo_txt = TIPOS_DOCUMENTO.get(datos["tipo_documento"], datos["tipo_documento"])
    marca = {k: ("(X)" if datos["tipo_documento"] == k else "(&nbsp;&nbsp;)") for k in TIPOS_DOCUMENTO}

    h = []
    if os.path.isfile(LOGO):
        lg = ImageReader(LOGO)
        w, hh = lg.getSize()
        ancho = 4.6 * cm
        h.append(RLImage(LOGO, width=ancho, height=ancho * hh / w, hAlign="LEFT"))
        h.append(Spacer(1, 10))
    h.append(Paragraph("AUTORIZACIÓN DE ENTREGA DE REPORTE DE SINIESTROS SALUD", titulo))
    h.append(Paragraph(f"{_esc(datos['ciudad'])}, {fecha_larga}", derecha))
    h.append(Spacer(1, 10))
    h.append(Paragraph("Señores,", base))
    h.append(Paragraph("SANITAS PERÚ S.A. EPS PRESENTE.-", negrita))
    h.append(Spacer(1, 10))
    h.append(Paragraph(
        f"Yo, {b(datos['nombre'])} identificado/a con documento de identidad {marca['DNI']}&nbsp;DNI, {marca['CE']}&nbsp;CE, "
        f"{marca['OTRO']}&nbsp;Otro N°&nbsp;{b(datos['n_documento'])} solicito a la compañía Sanitas Perú S.A. EPS hacerme "
        f"entrega de mi reporte de siniestralidad correspondiente al Plan de Salud con contrato N° {b(contrato)}.",
        base))
    h.append(Spacer(1, 8))
    h.append(Paragraph(
        "Asimismo dejo constancia y autorizo la remisión de mi constancia de siniestralidad al correo electrónico "
        f"{b(datos['correo'])} para mantener una copia del mismo por no poder recogerlo de forma física.", base))
    if datos["dependientes"]:
        h.append(Spacer(1, 8))
        h.append(Paragraph(
            "Asimismo requiero que me hagan entrega de los reportes de siniestralidad de mis dependientes menores de "
            f"edad con grado de parentesco de {b(datos['parentesco'])} respecto de mi persona y de los cuales se "
            "indican sus datos personales y se adjuntan los documentos de identidad en señal de autorización:", base))
        h.append(Spacer(1, 6))
        filas = [[Paragraph("<b>Nombres y Apellidos</b>", base), Paragraph("<b>N° Documento de Identidad</b>", base)]]
        filas += [[Paragraph(_esc(d["nombre"]), base), Paragraph(_esc(d["n_documento"]), base)]
                  for d in datos["dependientes"]]
        t = Table(filas, colWidths=[11 * cm, 5.5 * cm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9AA8B0")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF3F6")),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        h.append(t)
    h.append(Spacer(1, 12))
    h.append(Paragraph("Ley N° 29733.- Ley de Protección de Datos Personales", negrita))
    h.append(Spacer(1, 4))
    h.append(Paragraph(
        "De conformidad en lo establecido en la Ley N° 29733.- Ley de Protección de Datos Personales y su Reglamento, "
        "quienes suscriben el presente documento quedan informados y dan su conocimiento libre, previo, expreso, "
        "inequívoco e informado a Sanitas Perú S.A. EPS, para la transferencia (entrega) sin limitación alguna de su "
        "información personal de salud consistente en el detalle de atenciones y diagnósticos médicos, o a quien el "
        "titular designe. La información que será entregada será aquella que obre en el poder de Sanitas Perú SA EPS "
        "desde el inicio de vigencia de la póliza hasta la emisión del presente documento.", base))
    h.append(Spacer(1, 10))
    h.append(Paragraph(f"Fecha: {ahora.strftime('%d / %m / %Y')}", base))
    h.append(Spacer(1, 6))

    firma = ImageReader(io.BytesIO(firma_png))
    fw, fh = firma.getSize()
    escala = min(6.5 * cm / fw, 2.6 * cm / fh)
    bloque_firma = Table(
        [[RLImage(io.BytesIO(firma_png), width=fw * escala, height=fh * escala)],
         [Paragraph("Firma del titular SOLICITANTE", centro)],
         [Paragraph(f"{_esc(datos['nombre'])}<br/>{_esc(tipo_txt)} N° {_esc(datos['n_documento'])}", centro)]],
        colWidths=[8 * cm], hAlign="CENTER")
    bloque_firma.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                      ("LINEABOVE", (0, 1), (0, 1), 0.8, colors.black),
                                      ("TOPPADDING", (0, 1), (0, 1), 4)]))
    h.append(KeepTogether([bloque_firma]))
    h.append(Spacer(1, 14))
    h.append(Paragraph(
        f"Documento firmado electrónicamente por el titular a través del webchat de Sanitas Perú el "
        f"{ahora.strftime('%d/%m/%Y a las %H:%M')} (hora de Lima). Solicitud N° {solicitud_id}. "
        "Se adjunta la imagen del documento de identidad del titular.", pie))

    h.append(PageBreak())
    h.append(Paragraph("Documento de identidad del titular", titulo))
    for i, foto in enumerate(fotos):
        try:
            ir = ImageReader(io.BytesIO(foto))
            w, hh = ir.getSize()
            esc = min(16 * cm / w, 10.5 * cm / hh)
            h.append(Paragraph("Anverso" if i == 0 else "Reverso", centro))
            h.append(Spacer(1, 4))
            h.append(RLImage(io.BytesIO(foto), width=w * esc, height=hh * esc))
            h.append(Spacer(1, 12))
        except Exception:  # noqa: BLE001
            logger.warning("siniestralidad: no se pudo poner la foto %s en el PDF", i, exc_info=True)
            h.append(Paragraph("(No se pudo mostrar la imagen del documento.)", centro))

    salida = io.BytesIO()
    doc = SimpleDocTemplate(salida, pagesize=A4, leftMargin=2.3 * cm, rightMargin=2.3 * cm, topMargin=1.6 * cm,
                            bottomMargin=1.6 * cm, title="Autorización de entrega de reporte de siniestros salud",
                            author="Sanitas Perú S.A. EPS")
    doc.build(h)
    return salida.getvalue()
