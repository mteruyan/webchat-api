"""
bot_engine.py — Motor de recorrido del árbol de bot_menus para webchat-api.

Convenciones (iguales a sofi-apis / procesar-receta):
- psycopg2 con placeholders %s
- get_connection() ya existente en tu proyecto (aquí solo se referencia)
- LIMA_TZ para timestamps

Flujo de uso desde el handler de WebSocket:

    # Cuando el usuario abre el widget:
    mensajes = iniciar_conversacion(conversacion_id, nodo_inicial="consultas_pide_pregunta")

    # Cuando el usuario escribe texto libre:
    mensajes = recibir_entrada_usuario(conversacion_id, texto="tengo una consulta sobre...")

    # Cuando el usuario hace click en un botón de menú:
    mensajes = recibir_entrada_usuario(conversacion_id, opcion_id="69052a09bb692909a75b7bb8")

`mensajes` es una lista de eventos listos para mandar por WebSocket al widget,
p.ej. [{"type": "mensaje_bot", "payload": {...}}, {"type": "en_cola", "payload": {}}]
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

from db import get_connection, release_connection
import supervisor
from api_handlers import (
    validar_horario_consultas, consulta_doi, listar_distritos, update_doi_distrito,
    seleccionar_direccion, registrar_direccion, crear_doi_nuevo, procesar_receta, trae_escoge,
    consulta_ruc, buscar_actividad_sctr, graba_bd_leads_sctr, lead_pases,
    crear_solicitud_mad, estado_solicitud_mad, validar_programada_mad, resumen_programada_mad,
    resumen_ubicacion_mad, validar_distrito_mad,
    api_sctr_nuevo, api_sctr_continuidad, api_eps_nuevo,
    detalle_planilla_sctr, generar_excel_sctr, borrar_trabajador_sctr,
    enviar_sms_otp, validar_sms_otp,
    consulta_doi_potestativo, consulta_doi_potestativo_carta,
    consulta_carta_rechazada, update_cg_rechazados, alta_usuario_programa,
    afiliacion_iniciar, afiliacion_consulta_ruc, afiliacion_registrar_titular,
    afiliacion_registrar_dependiente, lead_consultas, consulta_agente_ia,
)

LIMA_TZ = timezone(timedelta(hours=-5))


# ---------------------------------------------------------------------------
# Registro de APIs invocables desde nodos tipo 'accion_api'
# Cada handler recibe (parametros_resueltos, variables_sesion) y devuelve
# un dict {"status": int, "response": {...}} — así {{VAR.status}} y
# {{VAR.response.campo}} funcionan igual que en Chattigo.
# ---------------------------------------------------------------------------
API_HANDLERS = {
    "validar_horario_consultas": validar_horario_consultas,
    "consulta_doi": consulta_doi,
    "listar_distritos": listar_distritos,
    "update_doi_distrito": update_doi_distrito,
    "seleccionar_direccion": seleccionar_direccion,
    "registrar_direccion": registrar_direccion,
    "crear_doi_nuevo": crear_doi_nuevo,
    "procesar_receta": procesar_receta,
    "trae_escoge": trae_escoge,
    "consulta_ruc": consulta_ruc,
    "buscar_actividad_sctr": buscar_actividad_sctr,
    "graba_bd_leads_sctr": graba_bd_leads_sctr,
    "lead_pases": lead_pases,
    "crear_solicitud_mad": crear_solicitud_mad,
    "estado_solicitud_mad": estado_solicitud_mad,
    "validar_programada_mad": validar_programada_mad,
    "resumen_programada_mad": resumen_programada_mad,
    "resumen_ubicacion_mad": resumen_ubicacion_mad,
    "validar_distrito_mad": validar_distrito_mad,
    "api_sctr_nuevo": api_sctr_nuevo,
    "api_sctr_continuidad": api_sctr_continuidad,
    "api_eps_nuevo": api_eps_nuevo,
    "detalle_planilla_sctr": detalle_planilla_sctr,
    "generar_excel_sctr": generar_excel_sctr,
    "borrar_trabajador_sctr": borrar_trabajador_sctr,
    "enviar_sms_otp": enviar_sms_otp,
    "validar_sms_otp": validar_sms_otp,
    "consulta_doi_potestativo": consulta_doi_potestativo,
    "consulta_doi_potestativo_carta": consulta_doi_potestativo_carta,
    "consulta_carta_rechazada": consulta_carta_rechazada,
    "update_cg_rechazados": update_cg_rechazados,
    "alta_usuario_programa": alta_usuario_programa,
    "afiliacion_iniciar": afiliacion_iniciar,
    "afiliacion_consulta_ruc": afiliacion_consulta_ruc,
    "afiliacion_registrar_titular": afiliacion_registrar_titular,
    "afiliacion_registrar_dependiente": afiliacion_registrar_dependiente,
    "lead_consultas": lead_consultas,
    "consulta_agente_ia": consulta_agente_ia,
}


# ---------------------------------------------------------------------------
# Resolución de variables: {{DOI}}, {{CONSULTA_DOI.response.doi}}, etc.
# ---------------------------------------------------------------------------
_VAR_PATTERN = re.compile(r"\{\{([\w.]+)\}\}")


def _resolver_valor(path: str, variables: dict):
    """Navega variables['CONSULTA_DOI']['response']['doi'] a partir de 'CONSULTA_DOI.response.doi'."""
    partes = path.split(".")
    valor = variables
    for parte in partes:
        if isinstance(valor, dict):
            valor = valor.get(parte)
        else:
            return None
    return valor


def _construir_opciones_direcciones(variables: dict, siguiente_codigo: str) -> list:
    """Genera un botón por cada dirección guardada del paciente (CONSULTA_DOI.response.direccionN/distritoN,
    N=1..3), más un botón fijo para agregar una nueva. La cantidad de botones varía según cuántas
    direcciones tenga el paciente. Cada botón guarda su número en DIRECCION_NRO al hacer clic."""
    doi = (variables.get("CONSULTA_DOI") or {}).get("response") or {}
    opciones = []
    for i in range(1, 4):
        direccion = doi.get(f"direccion{i}")
        if not direccion:
            continue
        distrito = doi.get(f"distrito{i}")
        label = f"{i}️⃣ {direccion}" + (f" - {distrito}" if distrito else "")
        opciones.append({
            "id": str(i), "label": label, "next_codigo": siguiente_codigo,
            "guardar_variable": {"nombre": "DIRECCION_NRO", "valor": str(i)},
        })
    # El primer casillero (1-3) que esté vacío es donde debe guardarse la
    # dirección nueva si el usuario elige "Agregar" — update_doi_direccion
    # exige ese número explícito, no acepta crear "la siguiente que sea".
    casillero_libre = next((str(i) for i in range(1, 4) if not doi.get(f"direccion{i}")), "3")
    opciones.append({
        "id": "0", "label": "➕ Agregar dirección nueva", "next_codigo": siguiente_codigo,
        "guardar_variable": [
            {"nombre": "DIRECCION_NRO", "valor": "0"},
            {"nombre": "DIRECCION_NRO_NUEVA", "valor": casillero_libre},
        ],
    })
    return opciones


def _calcular_siguiente_direccion_nro(variables: dict) -> str:
    """El paciente tiene hasta 3 casilleros de dirección (direccion1/2/3 en
    CONSULTA_DOI.response). Devuelve el primer casillero vacío, o "3" como
    último recurso si los tres ya están ocupados (sobrescribe el último)."""
    doi = (variables.get("CONSULTA_DOI") or {}).get("response") or {}
    for i in range(1, 4):
        if not doi.get(f"direccion{i}"):
            return str(i)
    return "3"


AVISO_DATOS_POR_DEFECTO = (
    "🔒 Usaremos este dato solo para atender tu consulta, conforme a la Ley 29733 de "
    "Protección de Datos Personales. Más información: https://www.sanitasperu.com/terminos-y-condiciones"
)


AVISO_DATOS_FORMULARIO_POR_DEFECTO = (
    "🔒 Los datos de este formulario se usarán solo para gestionar este trámite, conforme a la Ley 29733 de "
    "Protección de Datos Personales. Más información: https://www.sanitasperu.com/terminos-y-condiciones"
)


def _enmascarar_documento(valor) -> str:
    """'45678912' -> '•••••912': deja solo los últimos 3 caracteres a la vista."""
    v = str(valor or "").strip()
    if len(v) <= 3:
        return v
    return "•" * (len(v) - 3) + v[-3:]


def _con_derivadas(variables: dict) -> dict:
    """Agrega {{DNI_MASK}} y {{DOI_MASK}} (documento enmascarado)
    para poder mostrar 'el documento que usaste antes' sin exponerlo completo.
    Se calculan al vuelo, así nunca quedan desactualizados."""
    derivadas = None
    for base in ("DNI", "DOI"):
        valor = variables.get(base)
        if valor:
            if derivadas is None:
                derivadas = dict(variables)
            derivadas[base + "_MASK"] = _enmascarar_documento(valor)
    return derivadas or variables


def _ejecutar_seguro(cur, sql: str, params: tuple):
    """Ejecuta un UPDATE auxiliar dentro de un SAVEPOINT: si falla (p. ej. aún
    no existe una columna nueva), se deshace solo eso y el flujo del bot sigue."""
    try:
        cur.execute("SAVEPOINT sp_aux")
        cur.execute(sql, params)
        cur.execute("RELEASE SAVEPOINT sp_aux")
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT sp_aux")


def _hito(cur, conversacion_id: int, hito: str, nodo: str = None):
    """Registra (una sola vez por conversación) un hito del embudo: abrió, vio el
    menú, eligió, se identificó... Alimenta el embudo del dashboard. Si la tabla
    aún no existe, no pasa nada."""
    _ejecutar_seguro(
        cur,
        "INSERT INTO conversacion_hitos (conversacion_id, hito, nodo) VALUES (%s, %s, %s) "
        "ON CONFLICT (conversacion_id, hito) DO NOTHING",
        (conversacion_id, hito, nodo),
    )


_NODOS_MENU_INICIAL = ("webchat_bienvenida", "webchat_menu_principal")


def _hito_por_nodo(cur, conversacion_id: int, nodo: dict):
    """Hito que corresponde a haber llegado a este nodo (si lo hay)."""
    codigo, tipo = nodo["codigo"], nodo["tipo"]
    if codigo in _NODOS_MENU_INICIAL:
        _hito(cur, conversacion_id, "vio_menu", codigo)
    elif codigo == "webchat_pide_celular":
        _hito(cur, conversacion_id, "pidio_celular", codigo)
    elif codigo == "webchat_valida_pide_codigo":
        _hito(cur, conversacion_id, "pidio_codigo", codigo)
    elif tipo == "derivar_asesor":
        _hito(cur, conversacion_id, "asesor", codigo)
    elif tipo == "cierre":
        _hito(cur, conversacion_id, "cierre_bot", codigo)
    elif tipo == "conversacion_ia":
        pass  # modo experimental oculto: no debe contar en el embudo del dashboard
    elif not codigo.startswith("webchat_"):
        _hito(cur, conversacion_id, "entro_flujo", codigo)


_DOC_VALIDO = re.compile(r"[A-Za-z0-9]{6,15}")


def sustituir_variables(texto: str, variables: dict) -> str:
    """Reemplaza todos los {{...}} de un texto con el valor resuelto de variables_sesion."""
    if not texto:
        return texto
    variables = _con_derivadas(variables)

    def _reemplazo(match):
        valor = _resolver_valor(match.group(1), variables)
        return "" if valor is None else str(valor)

    return _VAR_PATTERN.sub(_reemplazo, texto)


def resolver_parametros(accion_config: dict, variables: dict) -> dict:
    """Resuelve los {{...}} dentro de los parámetros de un accion_api antes de llamarlo."""
    parametros = accion_config.get("parametros", {}) or {}
    return {k: sustituir_variables(str(v), variables) for k, v in parametros.items()}


# ---------------------------------------------------------------------------
# Evaluación de condiciones (nodo tipo 'condicion')
# ---------------------------------------------------------------------------
_OPERADORES = {
    # .lower() es crítico: Python convierte True/False a "True"/"False" (mayúscula
    # inicial), pero los valores en bot_menus.condiciones vienen en minúscula
    # ("true"/"false", igual que Chattigo) — sin el .lower(), CUALQUIER condición
    # que compare un booleano (outOfTime, etc.) siempre cae por el default.
    "==": lambda a, b: str(a).lower() == str(b).lower(),
    "!=": lambda a, b: str(a).lower() != str(b).lower(),
    ">": lambda a, b: float(a) > float(b),
    "<": lambda a, b: float(a) < float(b),
    ">=": lambda a, b: float(a) >= float(b),
    "<=": lambda a, b: float(a) <= float(b),
}


def evaluar_condiciones(condiciones: list, variables: dict) -> str:
    """Recorre la lista de condiciones en orden y devuelve el next_codigo de la primera que matchea.
    La condición con "default": true actúa como fallback si ninguna otra matchea."""
    default_codigo = None
    for cond in condiciones:
        if cond.get("default"):
            default_codigo = cond["next_codigo"]
            continue
        valor_actual = _resolver_valor(cond["campo"], variables)
        operador = _OPERADORES.get(cond["operador"])
        if operador and valor_actual is not None and operador(valor_actual, cond["valor"]):
            return cond["next_codigo"]
    return default_codigo


# ---------------------------------------------------------------------------
# Acceso a datos
# ---------------------------------------------------------------------------
def _obtener_nodo(cur, codigo: str) -> dict:
    cur.execute(
        """SELECT codigo, campana_id, tipo, texto, siguiente_codigo, opciones,
                  validacion, accion_config, condiciones, destino_campana_id,
                  subflujo_codigo, variables_a_limpiar, campos, imagen_url
           FROM bot_menus WHERE codigo = %s AND activo = TRUE""",
        (codigo,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = ["codigo", "campana_id", "tipo", "texto", "siguiente_codigo", "opciones",
            "validacion", "accion_config", "condiciones", "destino_campana_id",
            "subflujo_codigo", "variables_a_limpiar", "campos", "imagen_url"]
    return dict(zip(cols, row))


def _obtener_conversacion(cur, conversacion_id: int) -> dict:
    cur.execute(
        "SELECT nodo_actual, variables_sesion FROM conversaciones WHERE id = %s",
        (conversacion_id,),
    )
    nodo_actual, variables_sesion = cur.fetchone()
    return {"nodo_actual": nodo_actual, "variables_sesion": variables_sesion or {}}


def _guardar_estado(cur, conversacion_id: int, nodo_actual: str, variables_sesion: dict):
    cur.execute(
        "UPDATE conversaciones SET nodo_actual = %s, variables_sesion = %s WHERE id = %s",
        (nodo_actual, json.dumps(variables_sesion), conversacion_id),
    )


def _guardar_mensaje(cur, conversacion_id: int, emisor_tipo: str, tipo_contenido: str, contenido: str):
    cur.execute(
        """INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
           VALUES (%s, %s, %s, %s, %s)""",
        (conversacion_id, emisor_tipo, tipo_contenido, contenido, datetime.now(LIMA_TZ)),
    )


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------
# --- Nodo "captura_ubicacion" -------------------------------------------------------------------
# accion_config (todo opcional): {"variable_lat": "UBICACION_LAT", "variable_lng": "UBICACION_LNG",
#   "variable_precision": "UBICACION_PRECISION", "boton": "📍 Compartir mi ubicación",
#   "cancelar_codigo": "webchat_menu_principal", "solo_peru": true}
# siguiente_codigo = nodo al que se pasa cuando la ubicación es válida.
_PERU_LAT = (-18.5, 0.5)
_PERU_LNG = (-81.6, -68.4)
_MENSAJES_UBICACION = {
    "denegado": ("No pude acceder a tu ubicación porque el permiso está bloqueado 😕\n"
                 "Permite el acceso a la ubicación en tu navegador (ícono 🔒 junto a la dirección web → "
                 "Ubicación → Permitir) y toca el botón de nuevo."),
    "no_disponible": ("No pude obtener tu ubicación 😕 Revisa que el GPS o los servicios de ubicación de tu "
                      "dispositivo estén activados e inténtalo de nuevo."),
    "timeout": ("Tardó demasiado en obtener tu ubicación ⏱️ Activa el GPS o muévete a un lugar con mejor "
                "señal e inténtalo de nuevo."),
    "no_soportado": ("Tu navegador no permite compartir la ubicación desde aquí 😕 Prueba abriendo el chat "
                     "en otro navegador, o escribe *menu* para volver al menú principal."),
    "fuera_de_peru": ("Tu ubicación parece estar fuera de Perú 🌎 Este servicio solo funciona dentro del país. "
                      "Si crees que es un error, revisa el GPS de tu dispositivo e inténtalo de nuevo."),
    "texto": ("Para continuar necesito tu ubicación 📍 Toca el botón *Compartir mi ubicación* "
              "(o escribe *menu* para volver al menú principal)."),
}


def _botones_pedir_ubicacion(nodo: dict) -> list:
    cfg = nodo.get("accion_config") or {}
    # modo "mapa": en vez del GPS, el botón abre el mapa con un pin movible (widget.js: abrirMapa).
    if cfg.get("modo") == "mapa":
        return [{"id": "ubicacion_mapa", "label": cfg.get("boton") or "🗺️ Elegir en el mapa", "accion": "mapa"},
                {"id": "ubic_cancelar", "label": cfg.get("cancelar_label") or "🏠 Menú principal"}]
    return [{"id": "ubicacion", "label": cfg.get("boton") or "📍 Compartir mi ubicación", "accion": "ubicacion"},
            {"id": "ubic_cancelar", "label": cfg.get("cancelar_label") or "🏠 Menú principal"}]


def _validar_ubicacion(ubicacion, solo_peru: bool):
    """(lat, lng, precision_m, error). `error` es una clave de _MENSAJES_UBICACION o None."""
    try:
        lat = float((ubicacion or {}).get("lat"))
        lng = float((ubicacion or {}).get("lng"))
    except (TypeError, ValueError):
        return None, None, None, "no_disponible"
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or lat != lat or lng != lng:
        return None, None, None, "no_disponible"
    if solo_peru and not (_PERU_LAT[0] <= lat <= _PERU_LAT[1] and _PERU_LNG[0] <= lng <= _PERU_LNG[1]):
        return lat, lng, None, "fuera_de_peru"
    try:
        precision = max(0, int(round(float((ubicacion or {}).get("precision")))))
    except (TypeError, ValueError):
        precision = None
    return lat, lng, precision, None


def _avanzar(cur, conversacion_id: int, nodo_actual: str, variables: dict) -> list:
    """Ejecuta nodos en cadena hasta toparse con uno que requiera input del usuario
    (captura_dato, menu) o uno terminal (cierre, derivar_asesor). Devuelve la lista
    de eventos WebSocket a enviar al widget, en orden."""
    eventos = []
    # Siempre disponible como {{CONVERSACION_ID}} — útil para APIs que piden un
    # identificador equivalente al chat_id que usaba Chattigo.
    variables.setdefault("CONVERSACION_ID", str(conversacion_id))

    while True:
        nodo = _obtener_nodo(cur, nodo_actual)
        if nodo is None:
            eventos.append({"type": "mensaje_bot", "payload": {
                "texto": "Ocurrió un error. Escribe 'menu' para reiniciar."}})
            break

        tipo = nodo["tipo"]
        _hito_por_nodo(cur, conversacion_id, nodo)

        if tipo == "mensaje":
            texto = sustituir_variables(nodo["texto"], variables)
            eventos.append({"type": "mensaje_bot", "payload": {"texto": texto, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}})
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            nodo_actual = nodo["siguiente_codigo"]

        elif tipo == "menu":
            texto = sustituir_variables(nodo["texto"], variables)
            if (nodo["accion_config"] or {}).get("opciones_dinamicas") == "direcciones_doi":
                opciones_a_mostrar = _construir_opciones_direcciones(variables, nodo["siguiente_codigo"])
            else:
                opciones_a_mostrar = nodo["opciones"]
            eventos.append({"type": "mensaje_bot", "payload": {
                "texto": texto, "opciones": opciones_a_mostrar, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}})
            # El texto del menú también se guarda (antes solo se guardaban las
            # opciones): así, si el widget se reconecta, el historial puede
            # volver a mostrar la pregunta junto con sus botones.
            if texto:
                _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(opciones_a_mostrar))
            _guardar_estado(cur, conversacion_id, nodo["codigo"], variables)
            break  # espera el click del usuario

        elif tipo == "captura_dato":
            texto = sustituir_variables(nodo["texto"], variables)
            # Aviso de tratamiento de datos: se muestra cada vez que se pide un
            # dato personal (celular, DNI/RUC...). Los nodos se marcan con
            # validacion.aviso_datos = true; el texto se edita en la
            # configuración "aviso_datos_personales" (o usa el de por defecto).
            if (nodo["validacion"] or {}).get("aviso_datos"):
                aviso = supervisor.obtener_configuracion("aviso_datos_personales") or AVISO_DATOS_POR_DEFECTO
                texto = f"{texto}\n\n{aviso}"
            # "codigo_nodo" se manda siempre (no solo para el OTP) -- lo usa el
            # widget para reconocer en qué captura_dato puntual está parado y
            # decidir si debe mostrar acciones extra, como los botones de
            # "Reenviar código"/"Llamarme" durante webchat_valida_pide_codigo
            # (ver main.py: _reenviar_codigo_otp / _solicitar_llamada_otp).
            eventos.append({"type": "mensaje_bot", "payload": {
                "texto": texto, "imagen_url": sustituir_variables(nodo["imagen_url"], variables),
                "codigo_nodo": nodo["codigo"]}})
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            _guardar_estado(cur, conversacion_id, nodo["codigo"], variables)
            break  # espera el texto/adjunto del usuario

        elif tipo == "captura_ubicacion":
            # Pide la ubicación del cliente: el widget muestra el botón "📍 Compartir mi ubicación"
            # (navigator.geolocation) y devuelve lat/lng/precisión. Ver _recibir_entrada_usuario_arbol.
            texto = sustituir_variables(nodo["texto"] or "", variables)
            botones = _botones_pedir_ubicacion(nodo)
            eventos.append({"type": "mensaje_bot", "payload": {
                "texto": texto, "opciones": botones, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}})
            if texto:
                _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(botones))
            _guardar_estado(cur, conversacion_id, nodo["codigo"], variables)
            break  # espera que la persona comparta su ubicación

        elif tipo == "conversacion_ia":
            # Conversación libre con el agente de IA: muestra la bienvenida y se queda
            # esperando texto. Es un BUCLE: cada mensaje del usuario se responde en
            # recibir_entrada_usuario (_turno_ia) SIN avanzar de nodo; se sale con "menu"
            # (lo intercepta main.py en cualquier punto del bot).
            texto = sustituir_variables(nodo["texto"] or "", variables)
            botones = _botones_temas_ia(_temas_ia(nodo))
            if texto or botones:
                payload = {"texto": texto, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}
                if botones:
                    payload["opciones"] = botones
                eventos.append({"type": "mensaje_bot", "payload": payload})
                if texto:
                    _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
                if botones:
                    _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(botones))
            _guardar_estado(cur, conversacion_id, nodo["codigo"], variables)
            break  # espera el texto (o el clic en un tema) del usuario

        elif tipo == "formulario":
            texto = sustituir_variables(nodo["texto"], variables) if nodo["texto"] else None
            # Aviso de tratamiento de datos en formularios que piden datos personales
            # (accion_config.aviso_datos = true). Texto editable en la configuración
            # "aviso_datos_formulario".
            if (nodo["accion_config"] or {}).get("aviso_datos"):
                aviso = supervisor.obtener_configuracion("aviso_datos_formulario") or AVISO_DATOS_FORMULARIO_POR_DEFECTO
                texto = f"{texto}\n\n{aviso}" if texto else aviso
            eventos.append({"type": "mostrar_formulario", "payload": {
                "texto": texto, "campos": nodo["campos"]}})
            _guardar_mensaje(cur, conversacion_id, "bot", "formulario", json.dumps(nodo["campos"]))
            _guardar_estado(cur, conversacion_id, nodo["codigo"], variables)
            break  # espera que el usuario complete y envíe el formulario

        elif tipo == "accion_api":
            accion_config = nodo["accion_config"] or {}
            if accion_config.get("calcular_siguiente_direccion_nro"):
                variables["DIRECCION_NRO_NUEVA"] = _calcular_siguiente_direccion_nro(variables)
            handler = API_HANDLERS.get(accion_config.get("api"))
            parametros = resolver_parametros(accion_config, variables)
            resultado = handler(parametros, variables) if handler else {"status": 500, "response": {}}
            variables[accion_config["variable_respuesta"]] = resultado
            if accion_config.get("api") == "validar_sms_otp" and resultado.get("status") == 200:
                # Código SMS correcto: este celular queda verificado para este navegador.
                _hito(cur, conversacion_id, "verificado", nodo["codigo"])
                # Cómo obtuvo el código: whatsapp > llamada > sms reenviado > sms directo.
                if variables.get("OTP_METODO") == "whatsapp":
                    _metodo = "whatsapp"
                elif int(variables.get("OTP_LLAMADAS") or 0) > 0:
                    _metodo = "llamada"
                elif int(variables.get("OTP_REENVIOS") or 0) > 0:
                    _metodo = "sms_reenviado"
                else:
                    _metodo = "sms"
                _hito(cur, conversacion_id, "metodo_" + _metodo, nodo["codigo"])
                _ejecutar_seguro(
                    cur,
                    "UPDATE usuarios_webchat SET verificado_en = NOW() "
                    "WHERE id = (SELECT usuario_id FROM conversaciones WHERE id = %s)",
                    (conversacion_id,),
                )
            nodo_actual = nodo["siguiente_codigo"]

        elif tipo == "condicion":
            nodo_actual = evaluar_condiciones(nodo["condiciones"] or [], variables)

        elif tipo == "limpiar_variables":
            for var in (nodo["variables_a_limpiar"] or []):
                variables.pop(var, None)
            nodo_actual = nodo["siguiente_codigo"]

        elif tipo == "asignar_variables":
            # Nodo "silencioso" (no manda mensaje al widget): copia/deriva
            # valores hacia variables_sesion. Usado, por ejemplo, para reusar
            # el celular/correo que el paciente ya tiene registrado en vez de
            # volver a pedirlo. accion_config: {"asignaciones":[{"variable":"X","valor":"{{...}}"}]}
            for asignacion in (nodo["accion_config"] or {}).get("asignaciones", []):
                valor_crudo = asignacion.get("valor")
                if isinstance(valor_crudo, str):
                    variables[asignacion["variable"]] = sustituir_variables(valor_crudo, variables)
                else:
                    variables[asignacion["variable"]] = valor_crudo
            nodo_actual = nodo["siguiente_codigo"]

        elif tipo == "subflujo":
            nodo_actual = nodo["subflujo_codigo"]

        elif tipo == "derivar_asesor":
            texto = sustituir_variables(nodo["texto"], variables)
            eventos.append({"type": "mensaje_bot", "payload": {"texto": texto, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}})
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            cur.execute(
                """UPDATE conversaciones
                   SET estado = 'en_cola', campana_id = %s, nodo_actual = NULL, entrada_cola = %s
                   WHERE id = %s""",
                (nodo["destino_campana_id"], datetime.now(LIMA_TZ), conversacion_id),
            )
            eventos.append({"type": "en_cola", "payload": {}})
            break  # deja de ser responsabilidad del bot; el motor de asignación toma desde acá

        elif tipo == "cierre":
            texto = sustituir_variables(nodo["texto"], variables)
            eventos.append({"type": "mensaje_bot", "payload": {"texto": texto, "imagen_url": sustituir_variables(nodo["imagen_url"], variables)}})
            eventos.append({"type": "conversacion_cerrada", "payload": {"motivo": "bot"}})
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            cur.execute(
                "UPDATE conversaciones SET estado = 'cerrada', cerrado_en = %s, cerrado_por = 'sistema', nodo_actual = NULL WHERE id = %s",
                (datetime.now(LIMA_TZ), conversacion_id),
            )
            break

        else:
            raise ValueError(f"Tipo de nodo desconocido: {tipo}")

        if nodo_actual is None:
            # nodo lineal sin siguiente_codigo configurado: error de datos, no de código
            eventos.append({"type": "mensaje_bot", "payload": {
                "texto": "Ocurrió un error. Escribe 'menu' para reiniciar."}})
            break

    # Persistimos variables_sesion actualizadas siempre, incluso si el nodo final
    # no llamó a _guardar_estado explícitamente (derivar_asesor / cierre)
    cur.execute(
        "UPDATE conversaciones SET variables_sesion = %s WHERE id = %s",
        (json.dumps(variables), conversacion_id),
    )
    return eventos


def iniciar_conversacion(conversacion_id: int, nodo_inicial: str, variables_iniciales: dict = None) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if (variables_iniciales or {}).get("CELULAR"):
                # Entró un visitante ya reconocido (celular verificado en este navegador).
                _hito(cur, conversacion_id, "reconocido", nodo_inicial)
            eventos = _avanzar(cur, conversacion_id, nodo_inicial, dict(variables_iniciales or {}))
        conn.commit()
        return eventos
    finally:
        release_connection(conn)


class _TurnoIA:
    """Marca interna: el nodo actual es 'conversacion_ia' y el turno se atiende fuera del árbol."""
    def __init__(self, nodo_codigo: str, variables: dict, temas: list = None):
        self.nodo_codigo = nodo_codigo
        self.variables = variables
        self.temas = temas or []


def recibir_entrada_usuario(conversacion_id: int, texto: str = None,
                             opcion_id: str = None, archivo_url: str = None,
                             archivo_ruta: str = None, valores: dict = None,
                             ubicacion: dict = None, ubicacion_error: str = None) -> list:
    """Punto de entrada cuando llega mensaje_usuario, click_opcion o formulario_completado desde el widget."""
    resultado = _recibir_entrada_usuario_arbol(
        conversacion_id, texto, opcion_id, archivo_url, archivo_ruta, valores, ubicacion, ubicacion_error)
    if isinstance(resultado, _TurnoIA):
        # Nodo de IA: la conexión a la BD del árbol ya se liberó (la llamada al agente puede
        # tardar hasta 45 s y no debe tener una conexión del pool tomada todo ese tiempo).
        return _turno_ia(conversacion_id, resultado, texto, opcion_id, archivo_url, valores)
    return resultado


def conversacion_en_ia(conversacion_id: int) -> bool:
    """¿La conversación está parada ahora mismo en un nodo 'conversacion_ia'?"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM conversaciones c JOIN bot_menus b ON b.codigo = c.nodo_actual
                   WHERE c.id = %s AND b.tipo = 'conversacion_ia' AND b.activo = TRUE""",
                (conversacion_id,),
            )
            return cur.fetchone() is not None
    finally:
        release_connection(conn)


def entrar_a_nodo(conversacion_id: int, codigo_nodo: str, variables_extra: dict = None):
    """Salta directo a un nodo (sin pasar por ningún menú), conservando las variables de la
    sesión y sumando `variables_extra`. Devuelve la lista de eventos, o None si el nodo no
    existe o está inactivo (así quien llama puede seguir como si nada hubiera pasado)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if _obtener_nodo(cur, codigo_nodo) is None:
                return None
            estado = _obtener_conversacion(cur, conversacion_id)
            variables = dict(estado["variables_sesion"])
            variables.update(variables_extra or {})
            eventos = _avanzar(cur, conversacion_id, codigo_nodo, variables)
        conn.commit()
        return eventos
    finally:
        release_connection(conn)


# Botones del modo IA. Los temas y preguntas viven en bot_menus.accion_config del nodo:
#   {"temas": [{"label": "🩺 Coberturas", "preguntas": [{"label": "...", "pregunta": "..."}]}]}
# ids de botón: ia_t:<n> (tema), ia_q:<n>:<m> (pregunta), ia_temas (volver a los temas), ia_menu (menú principal).
ID_IA_TEMAS = "ia_temas"
ID_IA_MENU = "ia_menu"
ID_IA_ASESOR = "ia_asesor"
# Nodo al que lleva "Hablar c/ asesor" en el menú de consultas (el mismo que usan consultas_menu_todos /
# consultas_menu_potestativos). Se puede cambiar con la variable de entorno IA_NODO_ASESOR.
NODO_ASESOR_IA = os.environ.get("IA_NODO_ASESOR", "consultas_asesor_dni__chk")
# Tope de consultas al agente por conversación (protege el costo si el nodo está abierto a todos).
MAX_CONSULTAS_IA = 20
VENTANA_CONSULTAS_IA = 600   # segundos
_consultas_ia: dict = {}      # conversacion_id -> [instantes]; por instancia, suficiente como freno básico


def _temas_ia(nodo: dict) -> list:
    temas = (nodo.get("accion_config") or {}).get("temas") or []
    return [t for t in temas if isinstance(t, dict) and t.get("label") and t.get("preguntas")]


def _botones_temas_ia(temas: list) -> list:
    return [{"id": f"ia_t:{i}", "label": t["label"]} for i, t in enumerate(temas)]


def _botones_seguimiento_ia(temas: list) -> list:
    """Botones que acompañan cada respuesta del agente (solo si el nodo tiene temas configurados)."""
    if not temas:
        return []
    return [{"id": ID_IA_TEMAS, "label": "📚 Ver temas"},
            {"id": ID_IA_ASESOR, "label": "👩‍💼 Hablar con un asesor"},
            {"id": ID_IA_MENU, "label": "🏠 Menú principal"}]


def _celular_para_agente(conversacion_id: int, variables: dict) -> str:
    """Celular 51XXXXXXXXX de la sesión (variable CELULAR o, si falta, el guardado del usuario)."""
    crudo = (variables or {}).get("CELULAR")
    if not crudo:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT u.telefono FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                       WHERE c.id = %s""", (conversacion_id,))
                fila = cur.fetchone()
                crudo = fila[0] if fila else None
        finally:
            release_connection(conn)
    digitos = "".join(ch for ch in str(crudo or "") if ch.isdigit())
    if len(digitos) == 9:
        digitos = "51" + digitos
    return digitos if len(digitos) >= 11 else ""


def _excede_tope_ia(conversacion_id: int) -> bool:
    ahora = datetime.now().timestamp()
    recientes = [t for t in _consultas_ia.get(conversacion_id, []) if ahora - t < VENTANA_CONSULTAS_IA]
    if len(recientes) >= MAX_CONSULTAS_IA:
        _consultas_ia[conversacion_id] = recientes
        return True
    recientes.append(ahora)
    _consultas_ia[conversacion_id] = recientes
    if len(_consultas_ia) > 5000:   # limpieza sencilla para no crecer sin límite
        for k in [k for k, v in _consultas_ia.items() if not v or ahora - v[-1] > VENTANA_CONSULTAS_IA]:
            _consultas_ia.pop(k, None)
    return False


def _pregunta_de_boton_ia(temas: list, opcion_id: str):
    """(label, pregunta) si opcion_id es ia_q:<n>:<m> válido; si no, None."""
    m = re.fullmatch(r"ia_q:(\d+):(\d+)", opcion_id or "")
    if not m:
        return None
    try:
        p = temas[int(m.group(1))]["preguntas"][int(m.group(2))]
        return p["label"], (p.get("pregunta") or p["label"])
    except (IndexError, KeyError, TypeError):
        return None


def _turno_ia(conversacion_id: int, turno: "_TurnoIA", texto, opcion_id, archivo_url, valores) -> list:
    """Un turno del bucle con el agente de IA: guarda lo que escribió la persona (o la pregunta del
    botón que tocó), le pregunta al agente y muestra su respuesta. NO cambia de nodo (se sale con
    "menu" o con el botón de menú)."""
    temas = turno.temas
    texto = (texto or "").strip()

    # --- Botones de temas: no llaman al agente ---------------------------------------------------
    if not texto and opcion_id:
        if opcion_id == ID_IA_TEMAS:
            return _responder_con_botones_ia(conversacion_id, "📚 Ver temas", "¿Sobre qué tema quieres saber? 👇",
                                             _botones_temas_ia(temas))
        if opcion_id == ID_IA_ASESOR:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    _guardar_mensaje(cur, conversacion_id, "usuario", "texto", "👩‍💼 Hablar con un asesor")
                conn.commit()
            finally:
                release_connection(conn)
            eventos = entrar_a_nodo(conversacion_id, NODO_ASESOR_IA)
            if eventos is None:   # el nodo no existe en esta base: se orienta a la persona
                return _responder_con_botones_ia(
                    conversacion_id, None,
                    "Para hablar con un asesor escribe *menu* y elige *Hablar c/ asesor* 😊", [])
            return eventos
        m = re.fullmatch(r"ia_t:(\d+)", opcion_id)
        if m and int(m.group(1)) < len(temas):
            tema = temas[int(m.group(1))]
            botones = [{"id": f"ia_q:{m.group(1)}:{j}", "label": p["label"]} for j, p in enumerate(tema["preguntas"])]
            botones.append({"id": ID_IA_TEMAS, "label": "← Temas"})
            return _responder_con_botones_ia(conversacion_id, tema["label"],
                                             "Elige una pregunta, o escribe la tuya 👇", botones)
        elegida = _pregunta_de_boton_ia(temas, opcion_id)
        if elegida:
            texto = elegida[1]      # la pregunta completa va al agente
        else:
            return []               # botón viejo o desconocido: se ignora

    if not texto:
        if archivo_url:
            return [{"type": "mensaje_bot", "payload": {
                "texto": "Por ahora solo puedo leer mensajes de texto. Cuéntame tu consulta "
                         "escribiéndola, o escribe *menu* para volver al menú principal."}}]
        return []   # formulario viejo en pantalla: se ignora
    celular = _celular_para_agente(conversacion_id, turno.variables)
    if not celular:
        return [{"type": "mensaje_bot", "payload": {
            "texto": "Necesito tu número de celular para ayudarte. Escribe *menu* para volver al menú principal."}}]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _guardar_mensaje(cur, conversacion_id, "usuario", "texto", texto)
        conn.commit()
    finally:
        release_connection(conn)

    if _excede_tope_ia(conversacion_id):
        aviso = ("Estás enviando muchas consultas seguidas 😅 Dame un momento y vuelve a intentarlo, "
                 "o toca *Hablar con un asesor* para que te atienda una persona.")
        botones = _botones_seguimiento_ia(temas)
        return [{"type": "mensaje_bot", "payload": {"texto": aviso, **({"opciones": botones} if botones else {})}}]

    resultado = API_HANDLERS["consulta_agente_ia"]({"pregunta": texto, "celular": celular}, turno.variables)
    respuesta = ((resultado or {}).get("response") or {}).get("respuesta") or ""
    if not respuesta:
        from api_handlers import MENSAJE_RESPALDO_IA
        respuesta = MENSAJE_RESPALDO_IA

    botones = _botones_seguimiento_ia(temas)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", respuesta)
            if botones:
                _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(botones))
        conn.commit()
    finally:
        release_connection(conn)
    payload = {"texto": respuesta}
    if botones:
        payload["opciones"] = botones
    return [{"type": "mensaje_bot", "payload": payload}]


def _responder_con_botones_ia(conversacion_id: int, etiqueta_usuario: str, texto: str, botones: list) -> list:
    """Respuesta fija con botones (sin agente). Guarda el clic de la persona y el mensaje del bot."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if etiqueta_usuario:
                _guardar_mensaje(cur, conversacion_id, "usuario", "texto", etiqueta_usuario)
            _guardar_mensaje(cur, conversacion_id, "bot", "texto", texto)
            if botones:
                _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(botones))
        conn.commit()
    finally:
        release_connection(conn)
    payload = {"texto": texto}
    if botones:
        payload["opciones"] = botones
    return [{"type": "mensaje_bot", "payload": payload}]


def _recibir_entrada_usuario_arbol(conversacion_id: int, texto: str = None,
                                    opcion_id: str = None, archivo_url: str = None,
                                    archivo_ruta: str = None, valores: dict = None,
                                    ubicacion: dict = None, ubicacion_error: str = None):
    """Procesa la entrada contra el árbol de bot_menus (todo lo que no es el nodo de IA)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            estado = _obtener_conversacion(cur, conversacion_id)
            nodo = _obtener_nodo(cur, estado["nodo_actual"])
            variables = estado["variables_sesion"]

            if nodo is None:
                return [{"type": "mensaje_bot", "payload": {
                    "texto": "Escribe 'menu' para iniciar."}}]

            if nodo["tipo"] == "conversacion_ia":
                return _TurnoIA(nodo["codigo"], variables, _temas_ia(nodo))

            if nodo["tipo"] == "captura_dato":
                validacion = nodo["validacion"] or {}
                variable_nombre = validacion.get("variable")
                tipo_campo = validacion.get("tipo")
                valor = archivo_url if tipo_campo in ("imagen", "ubicacion") else texto

                # Números (celular u otro): deja solo dígitos — borra espacios,
                # guiones, paréntesis, "+", etc. antes de guardar. Exige mínimo
                # 6 dígitos porque está pensado para celulares/teléfonos.
                if tipo_campo in ("numero", "telefono") and valor:
                    valor = "".join(c for c in valor if c.isdigit())
                    if len(valor) < 6:
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese número no parece válido. Escríbelo solo con dígitos, "
                                     "sin letras (ejemplo: 987654321)."}}]

                # Celular peruano (ej. webchat_pide_celular): a diferencia de
                # "numero"/"telefono" de arriba, acá NO se limpian espacios,
                # letras, guiones ni símbolos -- se rechaza directo si no
                # viene como puros dígitos, mínimo 9 (lo normal es 9, pero se
                # acepta más por si alguien ya escribe con código de país).
                # Evita que pasen números mal escritos que después fallen en
                # Danaconnect o sofi-apis.
                if tipo_campo == "telefono_pe" and valor:
                    valor_recortado = valor.strip()
                    # El mensaje del bot dice "con o sin +51": se acepta un "+"
                    # al inicio (se descarta); cualquier otro símbolo se sigue rechazando.
                    if valor_recortado.startswith("+"):
                        valor_recortado = valor_recortado[1:]
                    if not re.match(r"^\d{9,}$", valor_recortado):
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese celular no parece válido. Escribe solo números, "
                                     "mínimo 9 dígitos, sin espacios, letras, guiones ni otros "
                                     "símbolos (ejemplo: 987654321)."}}]
                    # Si viene sin código de país (9 dígitos exactos), se le agrega "51"
                    # -- igual que hacía el flujo de WhatsApp. Si ya trae más dígitos,
                    # se asume que ya incluye el código y se deja tal cual.
                    valor = "51" + valor_recortado if len(valor_recortado) == 9 else valor_recortado

                # Número corto (ej. el número de un registro de una lista,
                # como "3" o "12" — NO un celular): también deja solo dígitos,
                # pero solo exige que no esté vacío, sin mínimo de 6.
                if tipo_campo == "numero_corto" and valor:
                    valor = "".join(c for c in valor if c.isdigit())
                    if len(valor) < 1:
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese no es un número válido. Escribe solo el número "
                                     "correspondiente (ejemplo: 3)."}}]

                # Correo: valida formato básico con una expresión regular
                # antes de aceptarlo — si no calza, pide que lo reescriba.
                if tipo_campo == "correo" and valor:
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", valor.strip()):
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese correo no parece válido. Escríbelo de nuevo "
                                     "(ejemplo: nombre@correo.com)."}}]
                    valor = valor.strip()

                # Nombres/apellidos: solo letras (con tildes/ñ), espacios,
                # apóstrofes y guiones — nada de dígitos ni otros símbolos.
                if tipo_campo in ("nombre", "apellido") and valor:
                    valor = valor.strip()
                    if not re.match(r"^[A-Za-zÁÉÍÓÚÑÜáéíóúñü'´\-\s]+$", valor):
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese dato solo puede tener letras (sin números ni símbolos). "
                                     "Por favor, escríbelo de nuevo."}}]

                # Código de verificación SMS: solo dígitos, exactamente 6 —
                # si no calza, se pide de nuevo antes de siquiera comparar
                # contra el código enviado (eso lo hace el nodo accion_api
                # validar_sms_otp más adelante en el flujo).
                if tipo_campo == "codigo_sms" and valor:
                    valor = "".join(c for c in valor if c.isdigit())
                    if len(valor) != 6:
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "Ese código debe tener 6 dígitos. Ingresa el código que te llegó por SMS."}}]

                # Distrito: solo letras — nada de números. La validación de que
                # el distrito EXISTA en la base de datos ocurre más adelante
                # (nodo médico de resolución de distrito), que vuelve a pedirlo
                # si no encuentra coincidencias.
                if tipo_campo == "distrito" and valor:
                    valor = valor.strip()
                    if not re.match(r"^[A-Za-zÁÉÍÓÚÑÜáéíóúñü'´\-\s]+$", valor):
                        return [{"type": "mensaje_bot", "payload": {
                            "texto": "El distrito solo puede tener letras (sin números). "
                                     "Por favor, escríbelo de nuevo."}}]

                # Los archivos (imagen/ubicación) se guardan como tipo_contenido
                # 'archivo', NUNCA como 'texto' — si no, la ruta cruda del storage
                # queda guardada como si fuera un mensaje de texto normal y se
                # filtra al cliente cuando el widget reconstruye el historial
                # (el enmascarado de "📎 Archivo enviado" solo aplica a tipo 'archivo').
                #
                # Lo que se PERSISTE es la ruta cruda del objeto (archivo_ruta),
                # NUNCA la URL firmada (archivo_url) — una firmada guardada para
                # siempre queda inútil en cuanto expira (1 hora). archivo_url
                # sigue siendo el valor de la VARIABLE de sesión, porque el
                # próximo paso (accion_api → procesar_receta) sí necesita
                # descargar el archivo YA, con una firmada que todavía sirva.
                es_archivo = bool(archivo_url) and tipo_campo in ("imagen", "ubicacion")
                tipo_contenido_guardar = "archivo" if es_archivo else "texto"
                contenido_a_guardar = (archivo_ruta or archivo_url) if es_archivo else (texto or "")
                _guardar_mensaje(cur, conversacion_id, "usuario", tipo_contenido_guardar, contenido_a_guardar)
                variables[variable_nombre] = valor

                # Columnas seguras que un captura_dato puede pedir persistir en
                # usuarios_webchat (no cualquier nombre, por seguridad — whitelist).
                columna = validacion.get("guardar_en_usuario")
                if variable_nombre in ("DNI", "DOI") and valor and _DOC_VALIDO.fullmatch(str(valor).strip()):
                    # Documento de identidad: DNI y DOI son el mismo dato en
                    # distintos flujos -> se mantienen iguales, y se recuerda
                    # para la próxima vez que esta persona vuelva.
                    valor = str(valor).strip()
                    variables["DNI"] = valor
                    variables["DOI"] = valor
                    columna = "dni"
                if columna == "ruc" and valor and re.fullmatch(r"\d{11}", str(valor).strip()):
                    # Último RUC usado: se recuerda para ofrecerlo la próxima vez
                    # (nodos marcados con validacion.guardar_en_usuario = "ruc").
                    variables["RUC_RECORDADO"] = str(valor).strip()
                    _ejecutar_seguro(
                        cur,
                        "UPDATE usuarios_webchat SET ruc = %s "
                        "WHERE id = (SELECT usuario_id FROM conversaciones WHERE id = %s)",
                        (str(valor).strip(), conversacion_id),
                    )
                elif columna in ("nombre", "telefono", "dni"):
                    cur.execute(
                        f"UPDATE usuarios_webchat SET {columna} = %s "
                        "WHERE id = (SELECT usuario_id FROM conversaciones WHERE id = %s)",
                        (valor, conversacion_id),
                    )
                    if columna == "telefono":
                        _hito(cur, conversacion_id, "celular_ingresado", nodo["codigo"])
                        # Número nuevo = todavía sin verificar (hasta que valide el código SMS).
                        _ejecutar_seguro(
                            cur,
                            "UPDATE usuarios_webchat SET verificado_en = NULL "
                            "WHERE id = (SELECT usuario_id FROM conversaciones WHERE id = %s)",
                            (conversacion_id,),
                        )

                nodo_siguiente = nodo["siguiente_codigo"]

            elif nodo["tipo"] == "captura_ubicacion":
                cfg = nodo["accion_config"] or {}
                if opcion_id == "ubic_cancelar":
                    _guardar_mensaje(cur, conversacion_id, "usuario", "boton_click", "🏠 Menú principal")
                    nodo_siguiente = cfg.get("cancelar_codigo") or "webchat_menu_principal"
                else:
                    clave_error = None
                    if ubicacion is not None:
                        lat, lng, precision, clave_error = _validar_ubicacion(ubicacion, cfg.get("solo_peru", True))
                        if clave_error is None:
                            # Las coordenadas son datos personales: viven en variables_sesion, no en el historial.
                            _guardar_mensaje(cur, conversacion_id, "usuario", "texto",
                                             "🗺️ Lugar elegido en el mapa" if (ubicacion or {}).get("origen") == "mapa"
                                             else "📍 Ubicación compartida")
                            variables[cfg.get("variable_lat") or "UBICACION_LAT"] = f"{lat:.6f}"
                            variables[cfg.get("variable_lng") or "UBICACION_LNG"] = f"{lng:.6f}"
                            variables[cfg.get("variable_precision") or "UBICACION_PRECISION"] = (
                                str(precision) if precision is not None else "")
                            if cfg.get("variable_origen"):   # "mapa" (pin movible) o "gps": la confirmación lo muestra
                                variables[cfg["variable_origen"]] = "mapa" if (ubicacion or {}).get("origen") == "mapa" else "gps"
                            nodo_siguiente = nodo["siguiente_codigo"]
                    elif ubicacion_error is not None:
                        clave_error = ubicacion_error if ubicacion_error in _MENSAJES_UBICACION else "no_disponible"
                    else:
                        clave_error = "texto"
                    if clave_error is not None:
                        # Mensaje claro + el botón de nuevo (reintento). Se guarda para que, si el widget
                        # se reconecta, el botón siga disponible.
                        botones = _botones_pedir_ubicacion(nodo)
                        mensaje = _MENSAJES_UBICACION[clave_error]
                        if clave_error == "texto" and cfg.get("modo") == "mapa":
                            mensaje = ("Para continuar necesito que marques el lugar 🗺️ Toca el botón *Elegir en el "
                                       "mapa* y confirma el pin.")
                        if clave_error == "texto" and texto:
                            _guardar_mensaje(cur, conversacion_id, "usuario", "texto", texto)
                        _guardar_mensaje(cur, conversacion_id, "bot", "texto", mensaje)
                        _guardar_mensaje(cur, conversacion_id, "bot", "opciones", json.dumps(botones))
                        conn.commit()
                        return [{"type": "mensaje_bot", "payload": {"texto": mensaje, "opciones": botones}}]

            elif nodo["tipo"] == "menu":
                if (nodo["accion_config"] or {}).get("opciones_dinamicas") == "direcciones_doi":
                    opciones = _construir_opciones_direcciones(variables, nodo["siguiente_codigo"])
                else:
                    opciones = nodo["opciones"] or []
                opcion = next((o for o in opciones if o.get("id") == opcion_id
                               or o.get("label") == texto), None)
                _guardar_mensaje(cur, conversacion_id, "usuario", "boton_click", texto or opcion_id or "")
                if opcion is None:
                    texto_opcion_invalida = (
                        supervisor.obtener_configuracion("mensaje_opcion_invalida")
                        or "No entendí esa opción, por favor elige una de la lista."
                    )
                    return [{"type": "mensaje_bot", "payload": {"texto": texto_opcion_invalida}}]
                guardar = opcion.get("guardar_variable")
                if guardar:
                    for g in (guardar if isinstance(guardar, list) else [guardar]):
                        variables[g["nombre"]] = g["valor"]
                if nodo["codigo"] in _NODOS_MENU_INICIAL:
                    _hito(cur, conversacion_id, "eligio_opcion", nodo["codigo"])
                nodo_siguiente = opcion["next_codigo"]

            elif nodo["tipo"] == "formulario":
                campos_formulario = nodo["campos"] or []
                # "valores" solo llega poblado cuando el widget manda
                # "formulario_completado" (el submit real del <form>). Si en
                # cambio llega texto suelto, un clic, etc. (por ejemplo porque
                # el formulario desapareció de pantalla tras una reconexión y
                # la persona escribió en la caja de texto normal), NO hay que
                # tratarlo como un envío válido — antes esto avanzaba el flujo
                # con todos los campos vacíos, sin validar nada.
                if valores is None:
                    return [{"type": "mostrar_formulario", "payload": {
                        "texto": "Por favor completa el formulario de arriba para continuar.",
                        "campos": campos_formulario}}]

                faltantes = [c.get("label") or c.get("variable") for c in campos_formulario
                             if c.get("obligatorio") and not str(valores.get(c.get("variable"), "")).strip()]
                if faltantes:
                    return [{"type": "mostrar_formulario", "payload": {
                        "texto": "Faltan completar campos obligatorios: " + ", ".join(faltantes),
                        "campos": campos_formulario}}]

                _guardar_mensaje(cur, conversacion_id, "usuario", "formulario", json.dumps(valores))
                variables.update(valores)
                nodo_siguiente = nodo["siguiente_codigo"]

            else:
                # el usuario escribió algo mientras el bot no esperaba input
                # (ej. mensaje suelto durante un accion_api en curso) — se ignora
                return []

            eventos = _avanzar(cur, conversacion_id, nodo_siguiente, variables)
        conn.commit()
        return eventos
    finally:
        release_connection(conn)
