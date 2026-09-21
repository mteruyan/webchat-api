"""
api_handlers.py — implementación real de cada API invocable desde bot_menus.accion_config.

Cada función recibe (parametros: dict, variables_sesion: dict) y devuelve
{"status": int, "response": {...}} — el mismo shape que Chattigo generaba,
para que {{VAR.status}} y {{VAR.response.campo}} sigan funcionando igual
en los textos de bot_menus.
"""

import os
from datetime import datetime, timezone, timedelta
import logging

import requests
from db import get_connection, release_connection

LIMA_TZ = timezone(timedelta(hours=-5))

SOFI_APIS_BASE = "https://sofi-apis-249513164794.us-central1.run.app"
PROCESAR_RECETA_BASE = "https://procesar-receta-249513164794.us-central1.run.app"

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


def procesar_receta(parametros: dict, variables: dict) -> dict:
    """Llama al servicio procesar-receta (Document AI + Gemini) — timeout más
    largo porque hace OCR y extracción, no es una consulta simple de BD."""
    return _llamar_api_externa(PROCESAR_RECETA_BASE, "", {
        "ruta": parametros.get("ruta"),
        "doi": parametros.get("doi"),
        "celular": parametros.get("celular"),
        "chat_id": parametros.get("chat_id"),
    }, timeout=30)


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
