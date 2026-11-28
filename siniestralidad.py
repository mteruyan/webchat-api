"""siniestralidad.py — Formatos que el cliente firma desde el celular (nov-2026). Botón "📄 Formatos" del asesor.

Formatos (FORMATOS): SINIESTRALIDAD (autorización de entrega de reporte de siniestros), LATENCIA (declaración
jurada de latencia), REEMBOLSO (solicitud de reembolso: datos, forma de pago, la hoja 2 llenada por el médico y
los comprobantes), RENOVACION (constancia de aceptación de renovación de contrato: el asesor define el periodo y el
% de incremento) y DESAFILIACION (solicitud de desafiliación: el cliente indica los usuarios a desafiliar).
Todos comparten la tabla, el enlace /siniestralidad/<token>, la foto del documento y la firma.
Los datos que define el asesor (periodo e incremento de la renovación) se guardan en la columna datos al crear el
enlace y se completan con lo que firma el cliente (no hace falta migración).

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

Correo: Danaconnect, conversation 620852 (DANACONNECT_SINIESTRALIDAD_CONV; variables EMAIL, NOMBRE, URL, FORMATO),
con el mismo DANACONNECT_AUTH que ya usa el webchat. Si la variable no está configurada, el envío por
correo queda deshabilitado (el enlace por chat sigue funcionando).

Tabla: migracion_siniestralidad.sql + migracion_formatos.sql (columnas formato y adjuntos). Archivos en el bucket
bajo webchat/siniestralidad/<id>/ (nombres aleatorios, para que /archivo-receta los pueda abrir desde la consola).
"""

import io
import json
import logging
import os
import re
import secrets
import threading
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
DANA_CONV = (os.environ.get("DANACONNECT_SINIESTRALIDAD_CONV") or "620852").strip()
# Nombre del formato que va en el correo (variable FORMATO de la plantilla de Danaconnect)
NOMBRE_FORMATO_CORREO = {
    "SINIESTRALIDAD": "Autorización de Siniestralidad",
    "LATENCIA": "Declaración jurada de latencia",
    "RENOVACION": "Constancia de renovación de contrato",
    "DESAFILIACION": "Solicitud de Desafiliación",
    "REEMBOLSO": "Solicitud de Reembolso",
}
URL_PUBLICA = "https://api.sanitasperu.com"        # main.py la reemplaza por la URL pública real
LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "imagenes", "logo-sanitas.png")

# main.py conecta estas funciones (Cloud Storage):  SUBIR(ruta, bytes, content_type)  y  DESCARGAR(ruta) -> bytes
SUBIR = None
DESCARGAR = None

TIPOS_DOCUMENTO = {"DNI": "DNI", "CE": "CE", "OTRO": "Otro"}

FORMATOS = {
    "SINIESTRALIDAD": {"titulo": "Autorización de entrega de reporte de siniestralidad", "corto": "Siniestralidad",
                       "vigencia_horas": 48, "archivo": "Autorizacion_siniestralidad"},
    "LATENCIA": {"titulo": "Declaración jurada de latencia", "corto": "Latencia",
                 "vigencia_horas": 48, "archivo": "Declaracion_jurada_latencia"},
    "REEMBOLSO": {"titulo": "Solicitud de reembolso", "corto": "Reembolso",
                  "vigencia_horas": 72, "archivo": "Solicitud_reembolso"},
    "RENOVACION": {"titulo": "Constancia de aceptación de renovación de contrato", "corto": "Renovación",
                   "vigencia_horas": 48, "archivo": "Constancia_renovacion"},
    "DESAFILIACION": {"titulo": "Solicitud de desafiliación", "corto": "Desafiliación",
                      "vigencia_horas": 48, "archivo": "Solicitud_desafiliacion"},
}
MAX_DESAFILIAR = 5             # usuarios que el cliente puede indicar en una solicitud de desafiliación
# Reembolso: hoja 2 llenada por el médico y comprobantes (fotos o PDF)
CATEGORIAS_ADJUNTO = {"medico": ("Hoja del médico tratante", 4), "sustento": ("Comprobantes y sustentos", 20)}
BANCOS = {"BCP": "Banco de Crédito BCP", "BBVA": "BBVA Continental", "INTERBANK": "Interbank",
          "SCOTIABANK": "Scotiabank", "OTRO": "Otro"}
# Dígitos del N° de cuenta según la nota 1 del formato de reembolso: {banco: {tipo_cuenta: dígitos}}
# Dígitos de control del CCI (posiciones 19 y 20). No existe una especificación pública oficial: se usa el algoritmo
# de la comunidad (módulo 10, pesos 1-2; el 1.º verifica entidad+oficina y el 2.º los 12 de la cuenta), CONFIRMADO
# con 13 CCI reales (BCP, Interbank, BBVA y Scotiabank, nov-2026). En esos cuatro bancos el control y el cruce
# cuenta-CCI BLOQUEAN siempre.
# En los demás bancos ("Otro") el control solo AVISA (queda en el registro) salvo CCI_CONTROL=bloquear.
CCI_CONTROL = (os.environ.get("CCI_CONTROL") or "avisar").strip().lower()


def _digito_control_cci(bloque: str) -> int:
    suma = 0
    for i, ch in enumerate(bloque):
        p = int(ch) * (1 if i % 2 == 0 else 2)
        suma += p - 9 if p > 9 else p
    return (10 - suma % 10) % 10


def cci_control_ok(cci: str) -> bool:
    """True si los 2 dígitos de control del CCI (20 dígitos) cuadran. Valida el formato, no que la cuenta exista."""
    c = re.sub(r"\D", "", str(cci or ""))
    return (len(c) == 20 and _digito_control_cci(c[:6]) == int(c[18])
            and _digito_control_cci(c[6:18]) == int(c[19]))


BANCOS_VERIFICADOS = ("BCP", "INTERBANK", "BBVA", "SCOTIABANK")   # dígito de control confirmado con CCI reales
BANCOS_CRUCE = ("BCP", "INTERBANK", "SCOTIABANK", "BBVA")         # el cruce cuenta-CCI bloquea


def cci_cuenta_coincide(banco: str, cuenta: str, cci: str) -> bool:
    """El CCI lleva dentro el N° de cuenta (confirmado con CCI reales):
    BCP / Interbank: la oficina (3 primeros dígitos de la cuenta) va en las posiciones 4-6 y el resto de la cuenta
        termina el bloque de la cuenta (posiciones 7-18).
    Scotiabank: el bloque de la cuenta (posiciones 7-18) termina con los 10 dígitos de la cuenta.
    BBVA (cuenta de 18 dígitos 0011+oficina(4)+DC(2)+cuenta(8)): las posiciones 4-6 del CCI son los 3 últimos dígitos
        de la oficina y el bloque termina con DC+cuenta (los 10 últimos dígitos de la cuenta)."""
    if len(cci) != 20:
        return False
    if banco in ("BCP", "INTERBANK"):
        return len(cuenta) > 3 and cci[3:6] == cuenta[:3] and cci[6:18].endswith(cuenta[3:])
    if banco == "SCOTIABANK":
        return cci[6:18].endswith(cuenta)
    if banco == "BBVA":
        if len(cuenta) != 18:
            return True          # cuenta escrita en otra forma: no se cruza
        return cci[3:6] == cuenta[5:8] and cci[8:18] == cuenta[8:18]
    return True


# Código de entidad (3 primeros dígitos del CCI) según la lista de códigos de entidades del BCRP.
CODIGO_ENTIDAD = {"BCP": "002", "INTERBANK": "003", "SCOTIABANK": "009", "BBVA": "011"}


def error_cuenta(banco: str, tipo_cuenta: str, cuenta: str, cci: str) -> str:
    """Valida el FORMATO (no que la cuenta exista): largo del N° de cuenta por banco y tipo, que el CCI empiece con el
    código de entidad del banco elegido y, en BBVA, que la cuenta empiece con 0011 (18 dígitos) o 011 (CCI de 20)."""
    if banco == "BBVA":
        if not ((len(cuenta) == 18 and cuenta.startswith("0011")) or (len(cuenta) == 20 and cuenta.startswith("011"))):
            return "El N° de cuenta BBVA debe tener 18 dígitos y empezar con 0011."
    else:
        digitos = DIGITOS_CUENTA.get(banco, {}).get(tipo_cuenta)
        if digitos and len(cuenta) != digitos:
            return f"El N° de cuenta {tipo_cuenta.lower()} de {BANCOS[banco]} debe tener {digitos} dígitos."
        if not digitos and not 6 <= len(cuenta) <= 20:
            return "Revisa el N° de cuenta."
    if not cci:
        return "El CCI (código de cuenta interbancario, 20 dígitos) es obligatorio."
    if len(cci) != 20:
        return "El CCI debe tener 20 dígitos."
    codigo = CODIGO_ENTIDAD.get(banco)
    if codigo and not cci.startswith(codigo):
        return f"El CCI de {BANCOS[banco]} debe empezar con {codigo}. Revisa el banco o el CCI."
    if banco in BANCOS_VERIFICADOS:
        if not cci_control_ok(cci):
            return "El CCI no es válido (falla su dígito de control). Revisa que lo hayas escrito completo y sin errores."
        if not cci_cuenta_coincide(banco, cuenta, cci):
            if banco in BANCOS_CRUCE:
                return "El N° de cuenta y el CCI no corresponden entre sí. Revisa ambos números."
            logger.warning("siniestralidad: el N° de cuenta %s no cuadra con su CCI (solo aviso)", banco)
    return ""


DIGITOS_CUENTA = {"BBVA": {"AHORROS": 18, "CORRIENTE": 18}, "BCP": {"AHORROS": 14, "CORRIENTE": 13},
                  "SCOTIABANK": {"AHORROS": 10, "CORRIENTE": 10}, "INTERBANK": {"AHORROS": 13, "CORRIENTE": 13}}
PARENTESCOS = {"TITULAR": "El mismo titular", "CONYUGE": "Cónyuge", "HIJO": "Hijo(a)", "PADRE": "Padre/Madre"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]

_RE_CORREO = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_RE_NOMBRE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{3,120}$")
_RE_CONTRATO = re.compile(r"^[A-Za-z0-9 /\-.]{1,30}$")
_RE_CIUDAD = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{2,40}$")
_RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
_RE_TEXTO_LIBRE = re.compile(r"^[\w ÁÉÍÓÚÜÑáéíóúüñ'.,;:#°º\-/()&\"]{3,200}$")

_ocr_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ocr-sin")
_aviso_migracion = False


def formato_de(valor) -> str:
    f = str(valor or "SINIESTRALIDAD").strip().upper()
    return f if f in FORMATOS else "SINIESTRALIDAD"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _falta_migracion(e: Exception) -> bool:
    global _aviso_migracion
    if "solicitudes_siniestralidad" in str(e) or "formato" in str(e) or "adjuntos" in str(e):
        if not _aviso_migracion:
            logger.warning("siniestralidad: falta ejecutar migracion_siniestralidad.sql / migracion_formatos.sql")
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
def crear_solicitud(conversacion_id: int | None, asesor_id: int, datos: dict) -> dict:
    """Crea la solicitud y envía el correo (si corresponde). Devuelve {"ok", "motivo"|("id","url","vence",...)}.
    El texto para el chat lo publica main.py (necesita el WebSocket). Con conversacion_id=None es un envío desde el
    menú «Formatos» (sin conversación abierta): solo por correo."""
    datos = datos if isinstance(datos, dict) else {}
    formato = formato_de(datos.get("formato"))
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
    if formato == "SINIESTRALIDAD" and not _RE_CONTRATO.match(contrato or ""):
        return {"ok": False, "motivo": "Escribe el N° de contrato (letras, números, - o /)."}
    if formato == "DESAFILIACION" and not _RE_CONTRATO.match(contrato or ""):
        return {"ok": False, "motivo": "Escribe el N° de contrato (letras, números, - o /)."}
    if contrato and not _RE_CONTRATO.match(contrato):
        return {"ok": False, "motivo": "El N° de contrato no es válido (letras, números, - o /)."}
    inicial = {}
    if formato == "RENOVACION":
        inicial, motivo = _parametros_renovacion(datos)
        if not inicial:
            return {"ok": False, "motivo": motivo}
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
    celular = None
    if _limpio(datos.get("celular")):
        celular = celular_9(datos.get("celular"))
        if not celular:
            return {"ok": False, "motivo": "El celular no es válido (9 dígitos, empieza con 9)."}

    token = secrets.token_urlsafe(18)
    ahora = datetime.now(LIMA_TZ)
    vence = ahora + timedelta(hours=FORMATOS[formato]["vigencia_horas"])
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Un enlace vigente por conversación y formato: el anterior (sin firmar) se anula. Sin conversación
            # (menú «Formatos»): por correo del cliente y formato.
            if conversacion_id is not None:
                cur.execute("""UPDATE solicitudes_siniestralidad SET estado = 'anulado', anulado_en = %s
                               WHERE conversacion_id = %s AND formato = %s AND estado IN ('enviado', 'abierto')""",
                            (ahora, conversacion_id, formato))
            else:
                cur.execute("""UPDATE solicitudes_siniestralidad SET estado = 'anulado', anulado_en = %s
                               WHERE conversacion_id IS NULL AND formato = %s AND lower(correo) = %s
                                 AND estado IN ('enviado', 'abierto')""", (ahora, formato, correo))
            columnas = ["token", "conversacion_id", "asesor_id", "correo", "nombre", "tipo_documento", "n_documento",
                        "contrato", "por_correo", "por_chat", "creado_en", "vence_en", "formato", "datos"]
            valores = [token, conversacion_id, asesor_id, correo, nombre or None, tipo, n_doc, contrato or None,
                       por_correo, por_chat, ahora, vence, formato,
                       json.dumps(inicial, ensure_ascii=False) if inicial else None]
            if celular:   # la columna `celular` la crea migracion_formatos_menu.sql
                columnas.append("celular")
                valores.append(celular)
            cur.execute(f"INSERT INTO solicitudes_siniestralidad ({', '.join(columnas)}) "
                        f"VALUES ({', '.join(['%s'] * len(columnas))}) RETURNING id", valores)
            sid = cur.fetchone()[0]
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_formatos.sql y migracion_formatos_menu.sql."}
        logger.exception("siniestralidad: no se pudo crear la solicitud (conversación %s)", conversacion_id)
        return {"ok": False, "motivo": "No se pudo crear la solicitud. Intenta de nuevo."}
    finally:
        release_connection(conn)

    resultado = {"ok": True, "id": sid, "url": _url(token), "vence": _fecha_hora(vence), "formato": formato,
                 "por_chat": por_chat, "por_correo": por_correo, "correo": correo}
    if por_correo:
        ok, error = _enviar_correo(correo, nombre, _url(token), vence, formato)
        _guardar_resultado_correo(sid, ok, error)
        resultado["correo_ok"] = ok
        if not ok:
            resultado["aviso"] = ("No se pudo enviar el correo" + (" (" + error + ")" if error else "") +
                                  (". Revisa la dirección y vuelve a enviarlo." if conversacion_id is None
                                   else ". Puedes enviarlo por el chat."))
    logger.info("siniestralidad: solicitud %s (%s) creada (conversación %s, asesor %s, correo=%s, chat=%s)",
                sid, formato, conversacion_id, asesor_id, por_correo, por_chat)
    return resultado


def _parametros_renovacion(datos: dict) -> tuple[dict, str]:
    """Periodo e incremento (%) que el asesor define para la constancia de renovación."""
    hoy = datetime.now(LIMA_TZ).year
    try:
        periodo = int(str(datos.get("periodo") or "").strip())
    except ValueError:
        return {}, "Indica el periodo de la renovación (año, por ejemplo 2027)."
    if not hoy <= periodo <= hoy + 3:
        return {}, f"El periodo debe estar entre {hoy} y {hoy + 3}."
    try:
        incremento = round(float(str(datos.get("incremento") or "").strip().replace(",", ".")), 2)
    except ValueError:
        return {}, "Indica el % de incremento (por ejemplo 7.5)."
    if not 0 < incremento <= 100:
        return {}, "El % de incremento debe ser mayor que 0 y no pasar de 100."
    return {"periodo": periodo, "incremento": incremento}, ""


def _enviar_correo(correo: str, nombre: str, url: str, vence: datetime, formato: str = "SINIESTRALIDAD"
                   ) -> tuple[bool, str]:
    # Plantilla 620852: NOMBRE, EMAIL, URL y FORMATO (nombre del documento a firmar). `vence` ya no se envía.
    payload = {"EMAIL": correo, "NOMBRE": _primer_nombre(nombre) or "cliente", "URL": url,
               "FORMATO": NOMBRE_FORMATO_CORREO[formato_de(formato)]}
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


def bandeja_asesor(asesor_id: int, dias: int = 15, sin_conversacion: bool = False) -> list:
    """Solicitudes enviadas por el asesor en los últimos días (para su bandeja). sin_conversacion=True: solo las
    enviadas desde el menú «Formatos» (sin conversación)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, conversacion_id, nombre, n_documento, contrato, correo, estado, por_correo,
                                  por_chat, correo_ok, creado_en, vence_en, abierto_en, firmado_en, ruta_pdf, datos,
                                  formato
                           FROM solicitudes_siniestralidad
                           WHERE asesor_id = %s AND creado_en >= now() - make_interval(days => %s)
                             AND (NOT %s OR conversacion_id IS NULL)
                           ORDER BY creado_en DESC LIMIT 40""", (asesor_id, dias, sin_conversacion))
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
         firmado, ruta_pdf, datos, formato) in filas:
        if estado in ("enviado", "abierto") and vence and vence < ahora:
            estado = "vencido"
        d = datos if isinstance(datos, dict) else {}
        salida.append({
            "id": sid, "conversacion_id": conv,
            "nombre": d.get("nombre") or nombre or "", "n_documento": d.get("n_documento") or ndoc or "",
            "contrato": contrato or "", "correo": correo or "", "estado": estado,
            "formato": formato_de(formato), "formato_nombre": FORMATOS[formato_de(formato)]["corto"],
            "por_correo": p_correo, "por_chat": p_chat, "correo_ok": c_ok,
            "creado": _fecha_hora(creado), "vence": _fecha_hora(vence), "abierto": _fecha_hora(abierto),
            "firmado": _fecha_hora(firmado), "ruta_pdf": ruta_pdf or "",
            "dependientes": len(d.get("dependientes") or []),
        })
    return salida


def celular_9(valor) -> str | None:
    """Celular peruano de 9 dígitos que empieza con 9 (acepta el prefijo 51); None si no lo es."""
    d = re.sub(r"\D", "", str(valor or ""))
    if len(d) == 11 and d.startswith("519"):
        d = d[2:]
    return d if re.fullmatch(r"9\d{8}", d) else None


def enviar_por_correo(asesor_id: int, datos: dict) -> dict:
    """Menú «Formatos»: envía el enlace solo por correo (el cliente no tiene una conversación abierta)."""
    datos = dict(datos if isinstance(datos, dict) else {}, por_correo=True, por_chat=False)
    return crear_solicitud(None, asesor_id, datos)


def clasificar_busqueda(texto) -> tuple[str, str]:
    """Detecta qué se buscó: ("correo", x), ("celular", 9 dígitos), ("documento", x) o ("", "") si no se entiende."""
    t = _limpio(texto, 120)
    if "@" in t:
        c = _validar_correo(t)
        return ("correo", c) if c else ("", "")
    n = re.sub(r"[\s.\-]", "", t).upper()
    if celular_9(n):
        return "celular", celular_9(n)
    if re.fullmatch(r"\d{8}", n) or re.fullmatch(r"[A-Z0-9]{9,12}", n):
        return "documento", n
    return "", ""


def buscar_firmados(texto) -> dict:
    """Formatos FIRMADOS de un cliente por correo, celular o documento (DNI/CE). Búsqueda exacta (no lista a todos)."""
    tipo, valor = clasificar_busqueda(texto)
    if not tipo:
        return {"ok": False, "motivo": "Escribe un correo, un celular de 9 dígitos o un DNI de 8 dígitos."}
    if tipo == "correo":
        cond, params = "(lower(s.correo) = %s OR lower(s.datos->>'correo') = %s)", (valor, valor)
    elif tipo == "documento":
        cond = "(s.n_documento = %s OR upper(s.datos->>'n_documento') = %s OR u.dni = %s)"
        params = (valor, valor, valor)
    else:
        cond = ("(s.celular = %s OR right(regexp_replace(COALESCE(u.telefono, ''), '\\D', '', 'g'), 9) = %s)")
        params = (valor, valor)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""SELECT s.id, s.formato, s.nombre, s.n_documento, s.contrato, s.correo, s.firmado_en,
                                       s.ruta_pdf, s.conversacion_id, a.nombre_display, s.datos, s.celular
                                FROM solicitudes_siniestralidad s
                                LEFT JOIN conversaciones c ON c.id = s.conversacion_id
                                LEFT JOIN usuarios_webchat u ON u.id = c.usuario_id
                                LEFT JOIN asesores a ON a.id = s.asesor_id
                                WHERE s.estado = 'firmado' AND {cond}
                                ORDER BY s.firmado_en DESC LIMIT 50""", params)
            except Exception as e:  # noqa: BLE001 — columna celular aún sin migrar: se busca sin ella
                if "celular" not in str(e) or tipo != "celular":
                    raise
                conn.rollback()
                cur.execute("""SELECT s.id, s.formato, s.nombre, s.n_documento, s.contrato, s.correo, s.firmado_en,
                                      s.ruta_pdf, s.conversacion_id, a.nombre_display, s.datos, NULL
                               FROM solicitudes_siniestralidad s
                               JOIN conversaciones c ON c.id = s.conversacion_id
                               JOIN usuarios_webchat u ON u.id = c.usuario_id
                               LEFT JOIN asesores a ON a.id = s.asesor_id
                               WHERE s.estado = 'firmado'
                                 AND right(regexp_replace(COALESCE(u.telefono, ''), '\\D', '', 'g'), 9) = %s
                               ORDER BY s.firmado_en DESC LIMIT 50""", (valor,))
            filas = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_formatos.sql."}
        logger.exception("siniestralidad: no se pudo buscar formatos firmados")
        return {"ok": False, "motivo": "No se pudo buscar. Intenta de nuevo."}
    finally:
        release_connection(conn)
    resultados = []
    for sid, formato, nombre, ndoc, contrato, correo, firmado, ruta, conv, asesor, datos, celular in filas:
        d = datos if isinstance(datos, dict) else {}
        f = formato_de(formato)
        resultados.append({
            "id": sid, "formato": f, "formato_nombre": FORMATOS[f]["titulo"],
            "nombre": d.get("nombre") or nombre or "", "n_documento": d.get("n_documento") or ndoc or "",
            "contrato": contrato or "", "correo": d.get("correo") or correo or "", "celular": celular or "",
            "firmado": _fecha_hora(firmado), "ruta_pdf": ruta or "", "conversacion_id": conv,
            "enviado_por": asesor or ""})
    etiqueta = {"correo": "correo", "celular": "celular", "documento": "documento"}[tipo]
    logger.info("siniestralidad: búsqueda de formatos firmados por %s -> %s resultado(s)", etiqueta, len(resultados))
    return {"ok": True, "criterio": etiqueta, "valor": valor, "resultados": resultados}


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


def texto_chat(url: str, vence: str, formato: str = "SINIESTRALIDAD") -> str:
    formato = formato_de(formato)
    if formato == "LATENCIA":
        return ("📄 Para continuar con tu trámite necesitamos tu *Declaración Jurada de Latencia* firmada. "
                "Complétala aquí desde tu celular (toma unos 3 minutos): sube la foto de tu documento, completa tus "
                f"datos y firma en la pantalla.\n{url}\nEl enlace vence el {vence}.")
    if formato == "REEMBOLSO":
        return ("📄 Completa tu *Solicitud de Reembolso* desde tu celular (toma unos 5 minutos): sube la foto de tu "
                "documento, completa tus datos y la forma de pago, adjunta la hoja llenada por tu médico y tus "
                f"comprobantes, y firma en la pantalla.\n{url}\nEl enlace vence el {vence}.")
    if formato == "RENOVACION":
        return ("📄 Para completar la renovación de tu contrato necesitamos tu *Constancia de Aceptación* firmada. "
                "Hazlo aquí desde tu celular (toma unos 2 minutos): sube la foto de tu documento, revisa tus datos y "
                f"firma en la pantalla.\n{url}\nEl enlace vence el {vence}.")
    if formato == "DESAFILIACION":
        return ("📄 Para tramitar la desafiliación necesitamos tu *Solicitud de Desafiliación* firmada. Complétala aquí "
                "desde tu celular (toma unos 3 minutos): sube la foto de tu documento, indica a quiénes se desafilia "
                f"y firma en la pantalla.\n{url}\nEl enlace vence el {vence}.")
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
                           estado, vence_en, ruta_dni_anverso, ruta_dni_reverso, ocr, intentos_dni, datos, abierto_en,
                           formato, adjuntos
                    FROM solicitudes_siniestralidad WHERE token = %s {'FOR UPDATE' if bloquear else ''}""", (token,))
    f = cur.fetchone()
    if not f:
        return None
    claves = ("id", "conversacion_id", "asesor_id", "correo", "nombre", "tipo_documento", "n_documento", "contrato",
              "estado", "vence_en", "ruta_dni_anverso", "ruta_dni_reverso", "ocr", "intentos_dni", "datos", "abierto_en",
              "formato", "adjuntos")
    sol = dict(zip(claves, f))
    sol["formato"] = formato_de(sol["formato"])
    adj = sol["adjuntos"]
    if isinstance(adj, str):
        try:
            adj = json.loads(adj)
        except ValueError:
            adj = []
    sol["adjuntos"] = adj if isinstance(adj, list) else []
    return sol


def _estado_no_disponible(sol) -> dict | None:
    """Respuesta para el cliente si el enlace ya no sirve (None si está vigente)."""
    if sol is None:
        return {"status": 404, "body": {"ok": False, "estado": "no_existe",
                                        "motivo": "Este enlace no es válido. Pídele uno nuevo a tu asesor."}}
    if sol["estado"] == "firmado":
        return {"status": 409, "body": {"ok": False, "estado": "firmado",
                                        "motivo": "Ya firmaste este documento. Tu asesor ya lo tiene."}}
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
        "ocr": ocr, "max_dependientes": MAX_DEPENDIENTES, "max_usuarios": MAX_DESAFILIAR,
        "parametros": {k: sol["datos"].get(k) for k in ("periodo", "incremento")}
        if isinstance(sol["datos"], dict) else {},
        "formato": sol["formato"], "titulo": FORMATOS[sol["formato"]]["titulo"],
        "adjuntos": _adjuntos_publicos(sol["adjuntos"]),
        "max_adjuntos": {k: v[1] for k, v in CATEGORIAS_ADJUNTO.items()},
    }}


def _adjuntos_publicos(adjuntos: list) -> list:
    """Lo que ve el cliente de sus adjuntos (sin la ruta del bucket)."""
    return [{"id": a.get("id"), "categoria": a.get("categoria"), "nombre": a.get("nombre") or "archivo",
             "pdf": a.get("tipo") == "application/pdf"} for a in adjuntos if isinstance(a, dict)]


def guardar_adjunto(token: str, categoria: str, contenido: bytes, extension: str, content_type: str,
                    nombre: str) -> dict:
    """Reembolso: guarda una foto o PDF (hoja del médico o comprobante)."""
    if categoria not in CATEGORIAS_ADJUNTO:
        return {"status": 400, "body": {"ok": False, "motivo": "Tipo de archivo inválido."}}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token, bloquear=True)
            no = _estado_no_disponible(sol)
            if no:
                conn.rollback()
                return no
            if sol["formato"] != "REEMBOLSO":
                conn.rollback()
                return {"status": 400, "body": {"ok": False, "motivo": "Este formato no lleva adjuntos."}}
            etiqueta, maximo = CATEGORIAS_ADJUNTO[categoria]
            if sum(1 for a in sol["adjuntos"] if a.get("categoria") == categoria) >= maximo:
                conn.rollback()
                return {"status": 400, "body": {"ok": False,
                                                "motivo": f"Llegaste al máximo de archivos en «{etiqueta}» ({maximo})."}}
            ruta = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.{extension}"
            try:
                SUBIR(ruta, contenido, content_type)
            except Exception:  # noqa: BLE001
                conn.rollback()
                logger.exception("siniestralidad: no se pudo subir el adjunto (solicitud %s)", sol["id"])
                return {"status": 500, "body": {"ok": False, "motivo": "No pudimos guardar el archivo. Intenta de nuevo."}}
            nuevo = {"id": uuid.uuid4().hex[:12], "categoria": categoria, "ruta": ruta, "tipo": content_type,
                     "nombre": _limpio(nombre, 80) or ("documento." + extension)}
            adjuntos = sol["adjuntos"] + [nuevo]
            cur.execute("UPDATE solicitudes_siniestralidad SET adjuntos = %s WHERE id = %s",
                        (json.dumps(adjuntos, ensure_ascii=False), sol["id"]))
        conn.commit()
        threading.Thread(target=_procesar_comprobante, args=(sol["id"], nuevo, contenido, content_type, categoria),
                         daemon=True).start()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("siniestralidad: error al guardar el adjunto")
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos guardar el archivo. Intenta de nuevo."}}
    finally:
        release_connection(conn)
    return {"status": 200, "body": {"ok": True, "adjuntos": _adjuntos_publicos(adjuntos)}}


def borrar_adjunto(token: str, adjunto_id: str) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sol = _cargar(cur, token, bloquear=True)
            no = _estado_no_disponible(sol)
            if no:
                conn.rollback()
                return no
            cur.execute("DELETE FROM reembolso_comprobantes WHERE solicitud_id = %s AND adjunto_id = %s",
                        (sol["id"], str(adjunto_id or "")))
            adjuntos = [a for a in sol["adjuntos"] if a.get("id") != str(adjunto_id or "")]
            cur.execute("UPDATE solicitudes_siniestralidad SET adjuntos = %s WHERE id = %s",
                        (json.dumps(adjuntos, ensure_ascii=False), sol["id"]))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("siniestralidad: error al borrar el adjunto")
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos quitar el archivo. Intenta de nuevo."}}
    finally:
        release_connection(conn)
    return {"status": 200, "body": {"ok": True, "adjuntos": _adjuntos_publicos(adjuntos)}}


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
    nombres, apellidos = _mayus(d.get("nombres")), _mayus(d.get("apellidos"))
    return {"es_documento": bool(d.get("es_documento")), "legible": bool(d.get("legible", True)),
            "tipo": tipo if tipo in TIPOS_DOCUMENTO else "OTRO", "numero": numero,
            "nombre": nombre if _RE_NOMBRE.match(nombre or "") else "",
            "nombres": nombres if _RE_NOMBRE.match(nombres or "") else "",
            "apellidos": apellidos if _RE_NOMBRE.match(apellidos or "") else ""}


# ---------- Reembolso: datos, DNI y comprobantes a tablas (migracion_reembolsos.sql) ----------
_PROMPT_COMPROBANTE = """Eres un lector de comprobantes de pago peruanos (factura, boleta o recibo por honorarios).
Mira la imagen y responde SOLO un JSON con:
{"es_comprobante": true|false,
 "legible": true|false,
 "tipo": "FACTURA"|"BOLETA"|"RECIBO"|"OTRO",
 "ruc_emisor": "RUC del emisor, 11 dígitos",
 "serie": "serie tal como aparece, por ejemplo F001 o B001",
 "numero": "número correlativo tal como aparece",
 "fecha": "fecha de emisión en formato AAAA-MM-DD",
 "moneda": "PEN"|"USD"|"OTRO",
 "monto_total": número con punto decimal (importe total a pagar),
 "igv": número con punto decimal (IGV; 0 si no aparece),
 "concepto": "descripción principal del servicio o producto, máximo 200 caracteres"}
Si un dato no se lee, déjalo como cadena vacía o null. No inventes datos."""
_RE_FECHA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _numero_o_none(valor):
    try:
        return round(float(str(valor).replace(",", "")), 2) if str(valor or "").strip() else None
    except (TypeError, ValueError):
        return None


def leer_comprobante(contenido: bytes, content_type: str) -> dict | None:
    """Lee una factura, boleta o recibo con Gemini. None si falla o tarda más de OCR_TIEMPO_MAX_SEG."""
    def _leer():
        import planes
        from google.genai import types
        r, _modelo = planes._generar([types.Part.from_bytes(data=contenido, mime_type=content_type or "image/jpeg"),
                                      _PROMPT_COMPROBANTE], paciencia=10, temperature=0,
                                     response_mime_type="application/json", max_output_tokens=600)
        texto = re.sub(r"^```(?:json)?|```$", "", (r.text or "").strip()).strip()
        return json.loads(texto)
    try:
        d = _ocr_pool.submit(_leer).result(timeout=OCR_TIEMPO_MAX_SEG)
    except FuturoTimeout:
        logger.warning("reembolso: la lectura del comprobante tardó más de %s s", OCR_TIEMPO_MAX_SEG)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("reembolso: no se pudo leer el comprobante", exc_info=True)
        return None
    if not isinstance(d, dict):
        return None
    tipo = str(d.get("tipo") or "").upper()
    ruc = re.sub(r"\D", "", str(d.get("ruc_emisor") or ""))
    fecha = str(d.get("fecha") or "")[:10]
    moneda = str(d.get("moneda") or "").upper()
    return {"es_comprobante": bool(d.get("es_comprobante")), "legible": bool(d.get("legible", True)),
            "tipo": tipo if tipo in ("FACTURA", "BOLETA", "RECIBO") else "OTRO",
            "ruc_emisor": ruc if len(ruc) == 11 else "",
            "serie": re.sub(r"[^A-Za-z0-9]", "", str(d.get("serie") or ""))[:10].upper(),
            "numero": re.sub(r"[^A-Za-z0-9]", "", str(d.get("numero") or ""))[:20].upper(),
            "fecha": fecha if _RE_FECHA_ISO.match(fecha) else "",
            "moneda": moneda if moneda in ("PEN", "USD") else "OTRO",
            "monto_total": _numero_o_none(d.get("monto_total")), "igv": _numero_o_none(d.get("igv")),
            "concepto": re.sub(r"\s+", " ", str(d.get("concepto") or "")).strip()[:200]}


def _procesar_comprobante(solicitud_id: int, adjunto: dict, contenido: bytes, content_type: str,
                          categoria: str) -> None:
    """Hilo aparte (la subida ya respondió al cliente): lee el comprobante si es sustento y guarda su fila en
    reembolso_comprobantes. Si algo falla, queda registrado en el log y la fila sin OCR."""
    ocr = leer_comprobante(contenido, content_type) if categoria == "sustento" else None
    campos = ocr or {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reembolso_comprobantes
                             (solicitud_id, adjunto_id, categoria, nombre, ruta, tipo_archivo, ocr, legible, tipo,
                              ruc_emisor, serie, numero, fecha_emision, moneda, monto_total, igv, concepto, leido_en)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (adjunto_id) DO UPDATE SET
                             ocr = EXCLUDED.ocr, legible = EXCLUDED.legible, tipo = EXCLUDED.tipo,
                             ruc_emisor = EXCLUDED.ruc_emisor, serie = EXCLUDED.serie, numero = EXCLUDED.numero,
                             fecha_emision = EXCLUDED.fecha_emision, moneda = EXCLUDED.moneda,
                             monto_total = EXCLUDED.monto_total, igv = EXCLUDED.igv,
                             concepto = EXCLUDED.concepto, leido_en = EXCLUDED.leido_en""",
                        (solicitud_id, adjunto["id"], categoria, adjunto.get("nombre") or "",
                         adjunto.get("ruta") or "", content_type,
                         json.dumps(ocr, ensure_ascii=False) if ocr else None, campos.get("legible"),
                         campos.get("tipo"), campos.get("ruc_emisor") or None, campos.get("serie") or None,
                         campos.get("numero") or None, campos.get("fecha") or None, campos.get("moneda"),
                         campos.get("monto_total"), campos.get("igv"), campos.get("concepto") or None,
                         datetime.now(LIMA_TZ) if ocr else None))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("reembolso: no se pudo guardar el comprobante (solicitud %s)", solicitud_id)
    finally:
        release_connection(conn)


def _guardar_reembolso_tablas(cur, solicitud_id: int, datos: dict, ocr: dict, coincide) -> None:
    """Dentro de la transacción de la firma: datos del formulario y lectura del DNI a sus tablas."""
    cur.execute("""INSERT INTO reembolso_solicitud (solicitud_id, datos, actualizado_en)
                   VALUES (%s, %s, now())
                   ON CONFLICT (solicitud_id) DO UPDATE SET datos = EXCLUDED.datos, actualizado_en = now()""",
                (solicitud_id, json.dumps(datos, ensure_ascii=False)))
    ocr = ocr if isinstance(ocr, dict) else {}
    cur.execute("""INSERT INTO reembolso_dni (solicitud_id, tipo_documento, numero_documento, numero_leido,
                                              nombres, apellidos, coincide, ocr, actualizado_en)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (solicitud_id) DO UPDATE SET
                     tipo_documento = EXCLUDED.tipo_documento, numero_documento = EXCLUDED.numero_documento,
                     numero_leido = EXCLUDED.numero_leido, nombres = EXCLUDED.nombres,
                     apellidos = EXCLUDED.apellidos, coincide = EXCLUDED.coincide, ocr = EXCLUDED.ocr,
                     actualizado_en = now()""",
                (solicitud_id, str(datos.get("tipo_documento") or ocr.get("tipo") or "DNI")[:10],
                 str(datos.get("n_documento") or "")[:20], ocr.get("numero") or None,
                 ocr.get("nombres") or None, ocr.get("apellidos") or None, coincide,
                 json.dumps(ocr, ensure_ascii=False) if ocr else None))


def _validar_formulario(f: dict, formato: str = "SINIESTRALIDAD") -> tuple[dict | None, str]:
    if formato == "LATENCIA":
        return _validar_latencia(f if isinstance(f, dict) else {})
    if formato == "REEMBOLSO":
        return _validar_reembolso(f if isinstance(f, dict) else {})
    if formato == "RENOVACION":
        return _validar_renovacion(f if isinstance(f, dict) else {})
    if formato == "DESAFILIACION":
        return _validar_desafiliacion(f if isinstance(f, dict) else {})
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


def _texto(valor, minimo=3, maximo=200) -> str | None:
    t = _limpio(valor, maximo)
    return t if len(t) >= minimo and _RE_TEXTO_LIBRE.match(t) else None


def _validar_latencia(f: dict) -> tuple[dict | None, str]:
    nombre = _mayus(f.get("nombre"))
    if not _RE_NOMBRE.match(nombre or "") or len(nombre.split(" ")) < 2:
        return None, "Escribe tus nombres y apellidos completos."
    tipo = str(f.get("tipo_documento") or "").upper()
    if tipo not in ("DNI", "CE"):
        return None, "Elige DNI o carné de extranjería."
    n_doc = _validar_documento(tipo, f.get("n_documento"))
    if not n_doc:
        return None, "El número de documento no es válido (el DNI tiene 8 dígitos)."
    domicilio = _texto(f.get("domicilio"), 8)
    if not domicilio:
        return None, "Escribe tu domicilio completo (calle, número, distrito)."
    empleador = _texto(f.get("empleador"), 2, 150)
    if not empleador:
        return None, "Escribe el nombre de tu última entidad empleadora."
    try:
        cese = datetime.strptime(str(f.get("fecha_cese") or ""), "%Y-%m-%d").date()
    except ValueError:
        return None, "Indica la fecha en que cesaste."
    hoy = datetime.now(LIMA_TZ).date()
    if cese > hoy or cese.year < 1950:
        return None, "La fecha de cese no es válida."
    try:
        aportes = int(str(f.get("aportes") or "").strip())
    except ValueError:
        return None, "Indica cuántos aportes hiciste a EsSalud (en números)."
    if not 0 <= aportes <= 36:
        return None, "Los aportes de los últimos tres años van de 0 a 36."
    if not f.get("acepta"):
        return None, "Debes aceptar la declaración para continuar."
    return {"nombre": nombre, "tipo_documento": tipo, "n_documento": n_doc, "domicilio": domicilio.upper(),
            "empleador": empleador.upper(), "fecha_cese": cese.isoformat(), "aportes": aportes, "correo": ""}, ""


def _validar_titular_basico(f: dict) -> tuple[dict | None, str]:
    """Nombre, tipo y número de documento de quien firma (renovación y desafiliación)."""
    nombre = _mayus(f.get("nombre"))
    if not _RE_NOMBRE.match(nombre or "") or len(nombre.split(" ")) < 2:
        return None, "Escribe tus nombres y apellidos completos."
    tipo = str(f.get("tipo_documento") or "").upper()
    if tipo not in ("DNI", "CE"):
        return None, "Elige DNI o carné de extranjería."
    n_doc = _validar_documento(tipo, f.get("n_documento"))
    if not n_doc:
        return None, "El número de documento no es válido (el DNI tiene 8 dígitos)."
    return {"nombre": nombre, "tipo_documento": tipo, "n_documento": n_doc, "correo": ""}, ""


def _validar_renovacion(f: dict) -> tuple[dict | None, str]:
    datos, motivo = _validar_titular_basico(f)
    if not datos:
        return None, motivo
    if not f.get("acepta"):
        return None, "Debes aceptar la constancia para continuar."
    return datos, ""


def _validar_desafiliacion(f: dict) -> tuple[dict | None, str]:
    datos, motivo = _validar_titular_basico(f)
    if not datos:
        return None, motivo
    usuarios = []
    for u in (f.get("usuarios") or [])[:MAX_DESAFILIAR]:
        if not isinstance(u, dict):
            continue
        nombre, doc = _mayus(u.get("nombre")), re.sub(r"[\s.\-]", "", str(u.get("n_documento") or "")).upper()
        if not nombre and not doc:
            continue
        if not _RE_NOMBRE.match(nombre or "") or len(nombre.split(" ")) < 2:
            return None, "Escribe nombres y apellidos completos de cada usuario a desafiliar."
        if not re.fullmatch(r"[A-Z0-9]{5,20}", doc or ""):
            return None, f"Revisa el N° de documento de {nombre.title()}."
        usuarios.append({"nombre": nombre, "n_documento": doc})
    if not usuarios:
        return None, "Agrega al menos un usuario a desafiliar."
    if len({u["n_documento"] for u in usuarios}) != len(usuarios):
        return None, "Hay un usuario repetido en la lista."
    if not f.get("acepta"):
        return None, "Debes aceptar la solicitud para continuar."
    datos["usuarios"] = usuarios
    return datos, ""


def _validar_persona(p: dict, quien: str, con_sexo: bool = False) -> tuple[dict | None, str]:
    p = p if isinstance(p, dict) else {}
    paterno, materno, nombres = _mayus(p.get("paterno"), 60), _mayus(p.get("materno"), 60), _mayus(p.get("nombres"), 80)
    if not _RE_NOMBRE.match(paterno or "") or not _RE_NOMBRE.match(nombres or ""):
        return None, f"Escribe el apellido paterno y los nombres del {quien}."
    if materno and not _RE_NOMBRE.match(materno):
        return None, f"Revisa el apellido materno del {quien}."
    tipo = str(p.get("tipo_documento") or "DNI").upper()
    tipo = tipo if tipo in TIPOS_DOCUMENTO else "DNI"
    n_doc = _validar_documento(tipo, p.get("n_documento"))
    if not n_doc:
        return None, f"Revisa el N° de documento del {quien} (el DNI tiene 8 dígitos)."
    try:
        edad = int(str(p.get("edad") or "").strip())
    except ValueError:
        return None, f"Indica la edad del {quien}."
    if not 0 <= edad <= 120:
        return None, f"La edad del {quien} no es válida."
    persona = {"paterno": paterno, "materno": materno, "nombres": nombres, "tipo_documento": tipo,
               "n_documento": n_doc, "edad": edad}
    if con_sexo:
        sexo = str(p.get("sexo") or "").upper()
        if sexo not in ("M", "F"):
            return None, f"Indica el sexo del {quien}."
        persona["sexo"] = sexo
    return persona, ""


def _validar_reembolso(f: dict) -> tuple[dict | None, str]:
    titular, motivo = _validar_persona(f.get("titular"), "titular")
    if not titular:
        return None, motivo
    es_titular = bool(f.get("paciente_es_titular"))
    if es_titular:
        sexo = str((f.get("paciente") or {}).get("sexo") or "").upper()
        if sexo not in ("M", "F"):
            return None, "Indica el sexo del paciente."
        paciente = dict(titular, sexo=sexo, parentesco="TITULAR")
    else:
        paciente, motivo = _validar_persona(f.get("paciente"), "paciente", con_sexo=True)
        if not paciente:
            return None, motivo
        parentesco = str((f.get("paciente") or {}).get("parentesco") or "").upper()
        if parentesco not in PARENTESCOS or parentesco == "TITULAR":
            return None, "Indica el parentesco del paciente con el titular."
        paciente["parentesco"] = parentesco
    registrada = bool(f.get("cuenta_registrada"))
    pago = {}
    if not registrada:
        g = f.get("pago") if isinstance(f.get("pago"), dict) else {}
        # Forma de pago: abono en cuenta o cheque. En ambos casos la cuenta y el CCI son obligatorios.
        forma = str(g.get("forma") or "").upper()
        if forma not in ("ABONO", "CHEQUE"):
            return None, "Elige la forma de pago: abono en cuenta o cheque."
        pago = {"forma": forma}
        banco = str(g.get("banco") or "").upper()
        if banco not in BANCOS:
            return None, "Elige el banco."
        banco_otro = _mayus(g.get("banco_otro"), 40) if banco == "OTRO" else ""
        if banco == "OTRO" and len(banco_otro) < 3:
            return None, "Escribe el nombre del banco."
        tipo_cuenta = str(g.get("tipo_cuenta") or "").upper()
        if tipo_cuenta not in ("AHORROS", "CORRIENTE"):
            return None, "Elige el tipo de cuenta."
        cuenta = re.sub(r"\D", "", str(g.get("n_cuenta") or ""))
        cci = re.sub(r"\D", "", str(g.get("cci") or ""))
        falla = error_cuenta(banco, tipo_cuenta, cuenta, cci)
        if falla:
            return None, falla
        cci_ok = cci_control_ok(cci)
        if cci_ok is False:
            if CCI_CONTROL == "bloquear" or banco in BANCOS_VERIFICADOS:
                return None, "El CCI no parece correcto (falla su dígito de control). Revísalo con tu banco."
            logger.warning("siniestralidad: CCI con dígitos de control que no cuadran (solicitud en firma)")
        titular_cuenta = _mayus(g.get("titular_cuenta"))
        if not _RE_NOMBRE.match(titular_cuenta or "") or len(titular_cuenta.split(" ")) < 2:
            return None, "Escribe el nombre completo del titular de la cuenta."
        doc_tipo = str(g.get("doc_tipo") or "").upper()
        if doc_tipo not in ("DNI", "RUC", "CE"):
            return None, "Elige el documento con el que se abrió la cuenta."
        doc_num = re.sub(r"[\s.\-]", "", str(g.get("doc_numero") or "")).upper()
        if not ((doc_tipo == "DNI" and re.fullmatch(r"\d{8}", doc_num)) or
                (doc_tipo == "RUC" and re.fullmatch(r"\d{11}", doc_num)) or
                (doc_tipo == "CE" and re.fullmatch(r"[A-Z0-9]{8,12}", doc_num))):
            return None, "Revisa el N° del documento con el que se abrió la cuenta."
        correo = _validar_correo(g.get("correo"))
        if not correo:
            return None, "Escribe un correo válido para el aviso del abono."
        pago.update({"banco": banco, "banco_otro": banco_otro, "tipo_cuenta": tipo_cuenta, "n_cuenta": cuenta,
                     "cci": cci, "cci_control_ok": cci_ok, "titular_cuenta": titular_cuenta, "doc_tipo": doc_tipo, "doc_numero": doc_num,
                     "correo": correo})
    if not f.get("acepta"):
        return None, "Debes aceptar las declaraciones para continuar."
    nombre = " ".join(x for x in (titular["nombres"], titular["paterno"], titular["materno"]) if x)
    return {"titular": titular, "paciente": paciente, "paciente_es_titular": es_titular,
            "cuenta_registrada": registrada, "pago": pago, "nombre": nombre,
            "tipo_documento": titular["tipo_documento"], "n_documento": titular["n_documento"],
            "correo": pago.get("correo", "")}, ""


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
    formato = sol["formato"]
    datos, motivo = _validar_formulario(formulario, formato)
    if not datos:
        return {"status": 400, "body": {"ok": False, "motivo": motivo}}
    if formato == "RENOVACION":
        # Periodo e incremento los fijó el asesor al crear el enlace: el cliente no los puede cambiar.
        fijos = sol["datos"] if isinstance(sol["datos"], dict) else {}
        if not fijos.get("periodo") or not fijos.get("incremento"):
            return {"status": 400, "body": {"ok": False, "motivo": "Esta solicitud no tiene periodo o incremento. "
                                                                    "Pide a tu asesor un enlace nuevo."}}
        datos = dict(datos, periodo=fijos["periodo"], incremento=fijos["incremento"])
    if not _validar_firma(firma_png):
        return {"status": 400, "body": {"ok": False, "motivo": "Dibuja tu firma en el recuadro."}}
    if not sol["ruta_dni_anverso"]:
        return {"status": 400, "body": {"ok": False, "motivo": "Primero sube la foto de tu documento."}}
    if formato == "REEMBOLSO":
        categorias = [a.get("categoria") for a in sol["adjuntos"]]
        if "medico" not in categorias:
            return {"status": 400, "body": {"ok": False,
                                            "motivo": "Adjunta la foto de la hoja llenada y sellada por tu médico."}}
        if "sustento" not in categorias:
            return {"status": 400, "body": {"ok": False, "motivo": "Adjunta al menos un comprobante (factura o recibo)."}}

    ahora = datetime.now(LIMA_TZ)
    ocr = sol["ocr"] if isinstance(sol["ocr"], dict) else {}
    try:
        fotos = [DESCARGAR(sol["ruta_dni_anverso"])]
        if sol["ruta_dni_reverso"]:
            fotos.append(DESCARGAR(sol["ruta_dni_reverso"]))
        if formato == "LATENCIA":
            pdf = _pdf_latencia(datos, firma_png, fotos, ahora, sol["id"])
        elif formato == "REEMBOLSO":
            adjuntos = [(a, DESCARGAR(a["ruta"])) for a in sol["adjuntos"] if a.get("ruta")]
            pdf = _pdf_reembolso(datos, firma_png, fotos, adjuntos, ahora, sol["id"])
        elif formato == "RENOVACION":
            pdf = _pdf_renovacion(datos, sol["contrato"] or "", firma_png, fotos, ahora, sol["id"])
        elif formato == "DESAFILIACION":
            pdf = _pdf_desafiliacion(datos, sol["contrato"] or "", firma_png, fotos, ahora, sol["id"])
        else:
            pdf = generar_pdf(datos, sol["contrato"], firma_png, fotos, ahora, sol["id"])
        ruta_pdf = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.pdf"
        ruta_firma = f"webchat/siniestralidad/{sol['id']}/{uuid.uuid4().hex}.png"
        SUBIR(ruta_firma, firma_png, "image/png")
        SUBIR(ruta_pdf, pdf, "application/pdf")
    except Exception:  # noqa: BLE001
        logger.exception("siniestralidad: no se pudo generar o guardar el PDF (solicitud %s)", sol["id"])
        return {"status": 500, "body": {"ok": False, "motivo": "No pudimos generar tu documento. Intenta de nuevo."}}

    nombre_pdf = f"{FORMATOS[formato]['archivo']}_{datos['n_documento']}.pdf"
    datos_guardar = dict(datos, ocr_numero=ocr.get("numero") or "",
                         ocr_coincide=(re.sub(r"\W", "", ocr.get("numero") or "").upper() == datos["n_documento"])
                         if ocr.get("numero") else None)
    titulo = FORMATOS[formato]["titulo"]
    texto_asesor = (f"✍️ {datos['nombre'] or 'El cliente'} firmó la autorización de siniestralidad "
                    f"(contrato {sol['contrato']})." if formato == "SINIESTRALIDAD" else
                    f"✍️ {datos['nombre'] or 'El cliente'} firmó la {titulo.lower()}.")
    entrega = {"solicitud_id": sol["id"], "conversacion_id": sol["conversacion_id"], "asesor_id": sol["asesor_id"],
               "ruta_pdf": ruta_pdf, "nombre_pdf": nombre_pdf, "nombre": datos["nombre"], "contrato": sol["contrato"],
               "n_documento": datos["n_documento"], "dependientes": len(datos.get("dependientes") or []),
               "firmado": _fecha_hora(ahora), "mensaje_id": None, "asesor_actual": None, "estado_conv": None,
               "texto_cliente": None, "alerta": None, "formato": formato,
               "titulo_mensaje": ("📎 Autorización de siniestralidad firmada" if formato == "SINIESTRALIDAD"
                                  else f"📎 {titulo} firmada"), "texto_asesor": texto_asesor}
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
                                                "motivo": "Este documento ya fue firmado."}}
            cur.execute("""UPDATE solicitudes_siniestralidad
                           SET estado = 'firmado', firmado_en = %s, datos = %s, ruta_pdf = %s, ruta_firma = %s,
                               ip_firma = %s, agente_firma = %s
                           WHERE id = %s""",
                        (ahora, json.dumps(datos_guardar, ensure_ascii=False), ruta_pdf, ruta_firma,
                         (ip or "")[:60], (agente or "")[:300], sol["id"]))
            if formato == "REEMBOLSO":
                _guardar_reembolso_tablas(cur, sol["id"], datos, ocr, datos_guardar["ocr_coincide"])
            if sol["conversacion_id"]:
                cur.execute("SELECT estado, asesor_id FROM conversaciones WHERE id = %s", (sol["conversacion_id"],))
                conv = cur.fetchone()
                if conv:
                    entrega["estado_conv"], entrega["asesor_actual"] = conv[0], conv[1]
                if conv and conv[0] != "cerrada":
                    cur.execute("""INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, metadata, enviado_en)
                                   VALUES (%s, 'usuario', 'archivo', %s, %s, %s) RETURNING id""",
                                (sol["conversacion_id"], ruta_pdf,
                                 json.dumps({"siniestralidad_id": sol["id"], "formato": formato, "nombre": nombre_pdf},
                                            ensure_ascii=False),
                                 ahora))
                    entrega["mensaje_id"] = cur.fetchone()[0]
                    if formato == "LATENCIA":
                        texto = ("✅ Recibimos tu Declaración Jurada de Latencia firmada. "
                                 "Tu asesor continuará con tu atención.")
                    elif formato == "REEMBOLSO":
                        texto = ("✅ Recibimos tu Solicitud de Reembolso firmada con tus documentos. "
                                 "Tu asesor continuará con tu atención.")
                    elif formato == "RENOVACION":
                        texto = ("✅ Recibimos tu Constancia de Aceptación de Renovación firmada. "
                                 "Tu asesor continuará con tu atención.")
                    elif formato == "DESAFILIACION":
                        texto = ("✅ Recibimos tu Solicitud de Desafiliación firmada. "
                                 "Tu asesor continuará con tu atención.")
                    else:
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
    logger.info("siniestralidad: solicitud %s (%s) firmada (conversación %s)", sol["id"], formato,
                sol["conversacion_id"])
    return {"status": 200, "body": {"ok": True, "correo": datos.get("correo") or "", "formato": formato},
            "entrega": entrega}


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


# ---------------------------------------------------------------------------
# PDF de la declaración jurada de latencia y de la solicitud de reembolso
# ---------------------------------------------------------------------------
_UNIDADES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce",
             "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
             "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho",
             "veintinueve"]
_DECENAS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}


def numero_letras(n: int) -> str:
    """0..99 en letras ("treinta y uno")."""
    n = int(n)
    if n < 30:
        return _UNIDADES[n]
    d, u = divmod(n, 10)
    return _DECENAS[d] + (f" y {_UNIDADES[u]}" if u else "")


def _fecha_larga(fecha) -> str:
    return f"{fecha.day} de {MESES[fecha.month - 1]} de {fecha.year}"


def _estilos():
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10.5, leading=15, alignment=TA_JUSTIFY)
    return {
        "base": base,
        "titulo": ParagraphStyle("titulo", parent=base, fontName="Helvetica-Bold", fontSize=12.5, alignment=TA_CENTER,
                                 spaceAfter=14),
        "seccion": ParagraphStyle("seccion", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=13,
                                  textColor=colors.white),
        "celda": ParagraphStyle("celda", parent=base, fontSize=9.5, leading=12, alignment=0),
        "etiqueta": ParagraphStyle("etiqueta", parent=base, fontSize=8.5, leading=11, alignment=0,
                                   textColor=colors.HexColor("#4A5B70")),
        "chico": ParagraphStyle("chico", parent=base, fontSize=8, leading=10.5),
        "pie": ParagraphStyle("pie", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#6B7A82")),
        "centro": ParagraphStyle("centro", parent=base, alignment=TA_CENTER, leading=13),
    }


def _logo(h, ancho_cm=4.6):
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage, Spacer
    if os.path.isfile(LOGO):
        w, hh = ImageReader(LOGO).getSize()
        ancho = ancho_cm * cm
        h.append(RLImage(LOGO, width=ancho, height=ancho * hh / w, hAlign="LEFT"))
        h.append(Spacer(1, 8))


def _bloque_firma(firma_png: bytes, lineas: list, est):
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage, Paragraph, Table, TableStyle
    fw, fh = ImageReader(io.BytesIO(firma_png)).getSize()
    escala = min(6.5 * cm / fw, 2.6 * cm / fh)
    filas = [[RLImage(io.BytesIO(firma_png), width=fw * escala, height=fh * escala)]]
    filas += [[Paragraph(t, est["centro"])] for t in lineas]
    t = Table(filas, colWidths=[9 * cm], hAlign="CENTER")
    t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("LINEABOVE", (0, 1), (0, 1), 0.8, colors.black),
                           ("TOPPADDING", (0, 1), (0, 1), 4)]))
    return t


def _paginas_documento(fotos: list, est, titulo="Documento de identidad del titular") -> list:
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage, PageBreak, Paragraph, Spacer
    h = [PageBreak(), Paragraph(titulo, est["titulo"])]
    for i, foto in enumerate(fotos):
        try:
            w, hh = ImageReader(io.BytesIO(foto)).getSize()
            esc = min(16 * cm / w, 10.5 * cm / hh)
            h += [Paragraph("Anverso" if i == 0 else "Reverso", est["centro"]), Spacer(1, 4),
                  RLImage(io.BytesIO(foto), width=w * esc, height=hh * esc), Spacer(1, 12)]
        except Exception:  # noqa: BLE001
            logger.warning("siniestralidad: no se pudo poner la foto %s en el PDF", i, exc_info=True)
            h.append(Paragraph("(No se pudo mostrar la imagen del documento.)", est["centro"]))
    return h


def _construir(h, titulo: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate
    salida = io.BytesIO()
    SimpleDocTemplate(salida, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=1.5 * cm,
                      bottomMargin=1.5 * cm, title=titulo, author="Sanitas Perú S.A. EPS").build(h)
    return salida.getvalue()


def _pie_firma(ahora: datetime, solicitud_id, extra: str = "") -> str:
    return (f"Documento firmado electrónicamente por el titular a través del webchat de Sanitas Perú el "
            f"{ahora.strftime('%d/%m/%Y a las %H:%M')} (hora de Lima). Solicitud N° {solicitud_id}. {extra}").strip()


def _pdf_renovacion(datos: dict, contrato: str, firma_png: bytes, fotos: list, ahora: datetime,
                    solicitud_id) -> bytes:
    """Mismo texto de la "CONSTANCIA DE ACEPTACIÓN DE RENOVACIÓN DE CONTRATO"; periodo e incremento los definió el asesor."""
    from reportlab.platypus import KeepTogether, Paragraph, Spacer
    est = _estilos()
    base = est["base"]

    def b(t):
        return f"<b>{_esc(t)}</b>"

    tipo_txt = "DNI" if datos["tipo_documento"] == "DNI" else "CE"
    incremento = f"{float(datos['incremento']):g}"
    h = []
    _logo(h)
    h.append(Paragraph("CONSTANCIA DE ACEPTACIÓN DE RENOVACIÓN DE CONTRATO", est["titulo"]))
    h.append(Spacer(1, 6))
    h.append(Paragraph(
        f"Yo, {b(datos['nombre'])}, identificado(a) con {tipo_txt} N.° {b(datos['n_documento'])}, manifiesto "
        "expresamente mi conformidad y aceptación de los términos y condiciones establecidos para la renovación de mi "
        f"contrato correspondiente al periodo {b(datos['periodo'])}, incluyendo el incremento del {b(incremento)} %.",
        base))
    h.append(Spacer(1, 10))
    h.append(Paragraph("En señal de conformidad, suscribo el presente documento.", base))
    h.append(Spacer(1, 14))
    h.append(Paragraph(f"Fecha: {ahora.strftime('%d / %m / %Y')}", base))
    h.append(Spacer(1, 26))
    h.append(KeepTogether([_bloque_firma(firma_png, [
        _esc(datos["nombre"]), f"{tipo_txt} N° {_esc(datos['n_documento'])}"], est)]))
    h.append(Spacer(1, 16))
    extra = (f"Contrato N° {contrato}. " if contrato else "") + "Se adjunta la imagen del documento de identidad."
    h.append(Paragraph(_pie_firma(ahora, solicitud_id, extra), est["pie"]))
    h += _paginas_documento(fotos, est, "Documento de identidad del titular")
    return _construir(h, "Constancia de aceptación de renovación de contrato")


def _pdf_desafiliacion(datos: dict, contrato: str, firma_png: bytes, fotos: list, ahora: datetime,
                       solicitud_id) -> bytes:
    """Mismo texto de la "SOLICITUD DE DESAFILIACIÓN": el titular y la lista de usuarios a desafiliar."""
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle
    est = _estilos()
    base = est["base"]

    def b(t):
        return f"<b>{_esc(t)}</b>"

    tipo_txt = "DNI" if datos["tipo_documento"] == "DNI" else "CE"
    h = []
    _logo(h)
    h.append(Paragraph("SOLICITUD DE DESAFILIACIÓN", est["titulo"]))
    h.append(Spacer(1, 6))
    h.append(Paragraph(
        f"Por medio del presente, yo, {b(datos['nombre'])}, identificado(a) con {tipo_txt} N.° "
        f"{b(datos['n_documento'])}, solicito formalmente la desafiliación del/de los usuario(s) que se detallan a "
        "continuación:", base))
    h.append(Spacer(1, 8))
    h.append(Paragraph("<b>Nombres completos y DNI de los usuarios a desafiliar:</b>", base))
    h.append(Spacer(1, 4))
    filas = [[Paragraph("<b>Nombres y apellidos</b>", est["celda"]), Paragraph("<b>N° de documento</b>", est["celda"])]]
    filas += [[Paragraph(_esc(u["nombre"]), est["celda"]), Paragraph(_esc(u["n_documento"]), est["celda"])]
              for u in datos["usuarios"]]
    t = Table(filas, colWidths=[11 * cm, 5.4 * cm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9AA8B0")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF3F6")),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    h.append(t)
    h.append(Spacer(1, 10))
    h.append(Paragraph(
        "La presente solicitud corresponde a la desafiliación del/de los usuario(s) antes mencionado(s), quien(es) se "
        f"encuentra(n) registrado(s) bajo el contrato N.° {b(contrato)}.", base))
    h.append(Spacer(1, 8))
    h.append(Paragraph("Agradezco se sirvan realizar las gestiones correspondientes para efectuar la desafiliación "
                       "solicitada.", base))
    h.append(Spacer(1, 8))
    h.append(Paragraph("En señal de conformidad, suscribo el presente documento.", base))
    h.append(Spacer(1, 10))
    h.append(Paragraph(f"Fecha: {ahora.strftime('%d / %m / %Y')}", base))
    h.append(Spacer(1, 22))
    h.append(KeepTogether([_bloque_firma(firma_png, [
        _esc(datos["nombre"]), f"{tipo_txt} N° {_esc(datos['n_documento'])}"], est)]))
    h.append(Spacer(1, 16))
    h.append(Paragraph(_pie_firma(ahora, solicitud_id, "Se adjunta la imagen del documento de identidad."),
                       est["pie"]))
    h += _paginas_documento(fotos, est, "Documento de identidad del titular")
    return _construir(h, "Solicitud de desafiliación")


def _pdf_latencia(datos: dict, firma_png: bytes, fotos: list, ahora: datetime, solicitud_id) -> bytes:
    """Mismo texto del formato "DECLARACIÓN JURADA" de latencia, con los datos del cliente."""
    from reportlab.platypus import KeepTogether, Paragraph, Spacer
    est = _estilos()
    base = est["base"]

    def b(t):
        return f"<b>{_esc(t)}</b>"

    tipo_txt = "Documento Nacional de Identidad" if datos["tipo_documento"] == "DNI" else "Carné de Extranjería"
    cese = datetime.strptime(datos["fecha_cese"], "%Y-%m-%d").date()
    h = []
    _logo(h)
    h.append(Paragraph("<u>DECLARACIÓN JURADA</u>", est["titulo"]))
    h.append(Paragraph(
        f"Por el presente documento yo, {b(datos['nombre'])}, identificado/a con {tipo_txt} N° "
        f"{b(datos['n_documento'])}, con domicilio real en {b(datos['domicilio'])}, <b>DECLARO BAJO JURAMENTO</b> "
        "lo siguiente:", base))
    h.append(Spacer(1, 8))
    for n, texto in enumerate([
            f"Mi última entidad empleadora fue {b(datos['empleador'])}.",
            f"Haber cesado de dicha entidad empleadora el {b(_fecha_larga(cese))}.",
            f"Haber realizado {b(numero_letras(datos['aportes']))} ({b(datos['aportes'])}) aportes a ESSALUD en los "
            "últimos tres años previos a la fecha de mi cese."], 1):
        h.append(Paragraph(f"{n}.&nbsp;&nbsp;{texto}", base))
        h.append(Spacer(1, 4))
    h.append(Spacer(1, 6))
    h.append(Paragraph(
        "Ello de conformidad con lo establecido en el Art. 37 del Decreto Supremo N° 009-97-SA Reglamento de la Ley "
        "de Modernización de la Seguridad Social en Salud, relativo a la Latencia.", base))
    h.append(Spacer(1, 8))
    h.append(Paragraph(
        "Formulo la presente declaración en honor a la verdad y al amparo de lo dispuesto por la ley N° 27444, ley de "
        "simplificación administrativa, sus modificatorias y su reglamento, asumiendo expresa responsabilidad por la "
        "veracidad de la presente declaración, relevando a terceros de ulteriores responsabilidades y en caso de "
        "falsedad me someto a la normatividad administrativa y/o penal vigente, y las sanciones que establece la ley.",
        base))
    h.append(Spacer(1, 8))
    h.append(Paragraph(
        f"En señal de conformidad firmo y acompaño con la copia de mi documento de identidad, a los "
        f"{numero_letras(ahora.day)} ({ahora.day}) días del mes de {MESES[ahora.month - 1]} del año {ahora.year}.",
        base))
    h.append(Spacer(1, 26))
    h.append(KeepTogether([_bloque_firma(firma_png, [
        _esc(datos["nombre"]), f"{tipo_txt} N° {_esc(datos['n_documento'])}"], est)]))
    h.append(Spacer(1, 16))
    h.append(Paragraph(_pie_firma(ahora, solicitud_id, "Se adjunta la imagen del documento de identidad."),
                       est["pie"]))
    h += _paginas_documento(fotos, est, "Documento de identidad del declarante")
    return _construir(h, "Declaración jurada de latencia")


def _tabla_campos(filas: list, est, ancho: float) -> list:
    """filas: [[(etiqueta, valor), ...], ...] -> una tabla por fila (cada fila reparte el ancho entre sus campos)."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    tablas = []
    for i, fila in enumerate(filas):
        celdas = [Paragraph(f"<font size=7.5 color='#4A5B70'>{_esc(e)}</font><br/>{_esc(v) or '—'}", est["celda"])
                  for e, v in fila]
        t = Table([celdas], colWidths=[ancho / len(fila)] * len(fila))
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C7D3")),
                               ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C7D3")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        tablas.append(t)
    return tablas


def _barra(texto: str, est):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    t = Table([[Paragraph(_esc(texto), est["seccion"])]], colWidths=["100%"])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1F4E79")),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _pdf_reembolso(datos: dict, firma_png: bytes, fotos: list, adjuntos: list, ahora: datetime, solicitud_id) -> bytes:
    """Hoja 1 del formato "SOLICITUD DE REEMBOLSO" con los datos del cliente y su firma; luego la hoja del médico,
    los comprobantes (fotos o PDF) y el documento de identidad."""
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer
    est = _estilos()
    t, p, g = datos["titular"], datos["paciente"], datos.get("pago") or {}
    ancho = 16.6 * cm

    def doc_txt(x):
        return f"{TIPOS_DOCUMENTO.get(x['tipo_documento'], x['tipo_documento'])} {x['n_documento']}"

    h = []
    _logo(h, 4.2)
    h.append(Paragraph("SOLICITUD DE REEMBOLSO", est["titulo"]))
    h.append(Paragraph("<i>Completada por el afiliado en el webchat de Sanitas Perú</i>", est["etiqueta"]))
    h.append(Spacer(1, 6))
    h.append(_barra("DATOS DEL TITULAR", est))
    h += _tabla_campos([[("Apellido paterno", t["paterno"]), ("Apellido materno", t["materno"]),
                         ("Nombres", t["nombres"])],
                        [("DNI/CE/Pasaporte", doc_txt(t)), ("Edad", str(t["edad"]))]], est, ancho)
    h.append(Spacer(1, 6))
    h.append(_barra("DATOS DEL PACIENTE", est))
    h += _tabla_campos([[("Apellido paterno", p["paterno"]), ("Apellido materno", p["materno"]),
                         ("Nombres", p["nombres"])],
                        [("DNI/CE/Pasaporte", doc_txt(p)), ("Edad", str(p["edad"])),
                         ("Sexo", "Masculino" if p["sexo"] == "M" else "Femenino"),
                         ("Parentesco", PARENTESCOS.get(p["parentesco"], p["parentesco"]))]], est, ancho)
    h.append(Spacer(1, 6))
    h.append(Paragraph(
        "De conformidad con lo establecido con la Ley Nro. 29733 – Ley de Protección de Datos Personales y su "
        "Reglamento, quien suscribe el presente documento, queda informado y da su consentimiento libre, expreso, "
        "inequívoco e informado, para el tratamiento y transferencia, nacional e internacional de sus datos "
        "personales a la base de datos de SANITAS PERÚ S.A., conjuntamente con cualquier otro dato que pudiera "
        "facilitarse a lo largo de la relación jurídica y aquellos obtenidos en fuentes accesibles al público, se "
        "tratarán con las finalidades de analizar las circunstancias al celebrar contratos con SANITAS PERÚ S.A., "
        "gestionar la contratación y evaluar la calidad del servicio. Los datos suministrados son esenciales para "
        "las finalidades indicadas. Las bases de datos donde ellos se almacenan cuentan con estrictas medidas de "
        "seguridad. En caso se decida no proporcionarlos, no será posible la prestación de servicios por parte de "
        "SANITAS PERÚ S.A. Conforme a ley, el titular de la información está facultado a ejercitar los derechos de "
        "información, acceso, rectificación, supresión o cancelación y oposición que se detallan en la Ley Nro. "
        "29733 – Ley de Protección de Datos Personales y su Reglamento, mediante comunicación dirigida a SANITAS "
        "PERÚ S.A.", est["chico"]))
    h.append(Spacer(1, 6))
    h.append(_barra("FORMA DE PAGO", est))
    if datos.get("cuenta_registrada"):
        h += _tabla_campos([[("¿Ya registraste tu cuenta en Sanitas?", "Sí (se usa la cuenta registrada)")]],
                           est, ancho)
    else:
        cheque = g.get("forma") == "CHEQUE"
        banco = g.get("banco_otro") if g.get("banco") == "OTRO" else BANCOS.get(g.get("banco"), g.get("banco"))
        h += _tabla_campos([
            [("¿Ya registraste tu cuenta en Sanitas?", "No"), ("Forma de pago", "Cheque" if cheque else "Abono en cuenta"),
             ("Correo", g.get("correo"))],
            [("Banco", banco), ("Tipo de cuenta", "Cuenta de ahorros" if g.get("tipo_cuenta") == "AHORROS"
                                else "Cuenta corriente")],
            [("N° de cuenta", g.get("n_cuenta")), ("CCI", g.get("cci"))],
            [("Nombre del titular de la cuenta", g.get("titular_cuenta")),
             ("Documento con el que abrió la cuenta", f"{g.get('doc_tipo')} {g.get('doc_numero')}")]], est, ancho)
        if cheque:
            h.append(Paragraph("El cheque deberá ser recogido en nuestras oficinas; solo se emiten los días martes y "
                               "jueves a partir de las 03:00 p. m.", est["chico"]))
    h.append(Spacer(1, 4))
    h.append(Paragraph(
        "<sup>1</sup> Formato del número de cuenta: Banco Continental (cuenta corriente o de ahorro de 20 dígitos), "
        "Banco de Crédito (cuenta corriente de 13 dígitos y de ahorro de 14 dígitos), Scotiabank (10 dígitos), "
        "Interbank (13 dígitos). <sup>2</sup> El afiliado declara que todos los datos descritos en la presente "
        "solicitud son válidos y que cualquier error en la información brindada sin previo aviso a Sanitas podrá "
        "afectar el pago o el tiempo del reembolso; en dichos casos la responsabilidad recaerá directamente en el "
        "AFILIADO.", est["chico"]))
    h.append(Spacer(1, 6))
    h.append(Paragraph(
        "Las declaraciones anteriores son verdaderas y, en tal sentido, el afiliado/asegurado suscribe la presente "
        "declaración, la cual tendrá el carácter de declaración jurada. Asimismo, de conformidad con el artículo 25 "
        "inciso a) de la Ley General de Salud 26842, el afiliado/asegurado presta su consentimiento para que las "
        "clínicas o médicos tratantes proporcionen a la compañía cualquier información del acto médico, relacionado "
        "a la atención, tales como historia clínica, certificados e informes, dispensándolos de la reserva de "
        "información. Sin perjuicio de lo expuesto el afiliado/asegurado reconoce que las clínicas o médicos "
        "tratantes están exceptuados de la reserva de información, por lo que estos podrán brindar a la aseguradora "
        "o a la administradora del financiamiento toda la información relacionada con el acto médico del "
        "afiliado/asegurado, siempre que fuese con fines de reembolso, pago de beneficios, fiscalización o auditoría.",
        est["chico"]))
    h.append(Spacer(1, 8))
    h.append(KeepTogether([_bloque_firma(firma_png, [
        "Firma del titular de la Póliza de Salud",
        f"{_esc(datos['nombre'])}<br/>{_esc(doc_txt(t))}"], est)]))
    h.append(Spacer(1, 10))
    n_med = sum(1 for a, _ in adjuntos if a.get("categoria") == "medico")
    n_sus = sum(1 for a, _ in adjuntos if a.get("categoria") == "sustento")
    h.append(Paragraph(_pie_firma(ahora, solicitud_id,
                                  f"Se adjuntan: hoja del médico tratante ({n_med} archivo(s)), comprobantes y "
                                  f"sustentos ({n_sus} archivo(s)) y el documento de identidad del titular."),
                       est["pie"]))
    principal = _construir(h, "Solicitud de reembolso")

    partes = [principal]
    for categoria in ("medico", "sustento"):
        for a, contenido in adjuntos:
            if a.get("categoria") == categoria:
                partes.append(_pdf_adjunto(a, contenido, est))
    partes.append(_construir(_paginas_documento(fotos, est)[1:], "Documento de identidad"))
    return _unir_pdfs(partes)


def _pdf_adjunto(adjunto: dict, contenido: bytes, est) -> bytes:
    """Un adjunto como PDF: si ya es PDF va tal cual; si es foto, en una hoja con su título."""
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image as RLImage, Paragraph, Spacer
    titulo = (CATEGORIAS_ADJUNTO.get(adjunto.get("categoria"), ("Adjunto", 0))[0] + " — " +
              (adjunto.get("nombre") or "archivo"))
    if adjunto.get("tipo") == "application/pdf" or contenido[:5] == b"%PDF-":
        return contenido
    try:
        w, hh = ImageReader(io.BytesIO(contenido)).getSize()
        esc = min(16.6 * cm / w, 23 * cm / hh)
        return _construir([Paragraph(_esc(titulo), est["etiqueta"]), Spacer(1, 4),
                           RLImage(io.BytesIO(contenido), width=w * esc, height=hh * esc)], titulo)
    except Exception:  # noqa: BLE001
        logger.warning("siniestralidad: no se pudo poner el adjunto %s en el PDF", adjunto.get("id"), exc_info=True)
        return _construir([Paragraph(_esc(titulo), est["etiqueta"]),
                           Paragraph("(No se pudo mostrar este archivo.)", est["centro"])], titulo)


def _unir_pdfs(partes: list) -> bytes:
    from pypdf import PdfReader, PdfWriter
    salida = PdfWriter()
    for parte in partes:
        try:
            lector = PdfReader(io.BytesIO(parte))
            if lector.is_encrypted:
                lector.decrypt("")
            for pagina in lector.pages:
                salida.add_page(pagina)
        except Exception:  # noqa: BLE001
            logger.warning("siniestralidad: no se pudo unir un PDF adjunto", exc_info=True)
            est = _estilos()
            from reportlab.platypus import Paragraph
            aviso = _construir([Paragraph("(Un archivo PDF adjunto no se pudo incluir: está dañado o protegido. "
                                          "Pídele al cliente que lo envíe por el chat.)", est["centro"])], "Aviso")
            for pagina in PdfReader(io.BytesIO(aviso)).pages:
                salida.add_page(pagina)
    salida.add_metadata({"/Title": "Solicitud de reembolso", "/Author": "Sanitas Perú S.A. EPS"})
    buf = io.BytesIO()
    salida.write(buf)
    return buf.getvalue()
