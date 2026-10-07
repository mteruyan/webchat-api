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

import re
import json
from datetime import datetime, timezone, timedelta

from db import get_connection, release_connection
import supervisor
from api_handlers import (
    validar_horario_consultas, consulta_doi, listar_distritos, update_doi_distrito,
    seleccionar_direccion, registrar_direccion, crear_doi_nuevo, procesar_receta, trae_escoge,
    consulta_ruc, buscar_actividad_sctr, graba_bd_leads_sctr, lead_pases,
    api_sctr_nuevo, api_sctr_continuidad, api_eps_nuevo,
    detalle_planilla_sctr, generar_excel_sctr, borrar_trabajador_sctr,
    enviar_sms_otp, validar_sms_otp,
    consulta_doi_potestativo, consulta_doi_potestativo_carta,
    consulta_carta_rechazada, update_cg_rechazados, alta_usuario_programa,
    afiliacion_iniciar, afiliacion_consulta_ruc, afiliacion_registrar_titular,
    afiliacion_registrar_dependiente, lead_consultas,
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


def sustituir_variables(texto: str, variables: dict) -> str:
    """Reemplaza todos los {{...}} de un texto con el valor resuelto de variables_sesion."""
    if not texto:
        return texto

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

        elif tipo == "formulario":
            texto = sustituir_variables(nodo["texto"], variables) if nodo["texto"] else None
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
            eventos = _avanzar(cur, conversacion_id, nodo_inicial, dict(variables_iniciales or {}))
        conn.commit()
        return eventos
    finally:
        release_connection(conn)


def recibir_entrada_usuario(conversacion_id: int, texto: str = None,
                             opcion_id: str = None, archivo_url: str = None,
                             archivo_ruta: str = None, valores: dict = None) -> list:
    """Punto de entrada cuando llega mensaje_usuario, click_opcion o formulario_completado desde el widget."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            estado = _obtener_conversacion(cur, conversacion_id)
            nodo = _obtener_nodo(cur, estado["nodo_actual"])
            variables = estado["variables_sesion"]

            if nodo is None:
                return [{"type": "mensaje_bot", "payload": {
                    "texto": "Escribe 'menu' para iniciar."}}]

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
                if columna in ("nombre", "telefono", "dni"):
                    cur.execute(
                        f"UPDATE usuarios_webchat SET {columna} = %s "
                        "WHERE id = (SELECT usuario_id FROM conversaciones WHERE id = %s)",
                        (valor, conversacion_id),
                    )

                nodo_siguiente = nodo["siguiente_codigo"]

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
