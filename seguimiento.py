"""seguimiento.py — "cerrar el ciclo" de la encuesta de satisfacción.

Cuando sale a producción una mejora que pidieron los clientes (p. ej. "ahora puedes enviar archivos de hasta
20 MB"), el supervisor máximo la registra con los temas de la encuesta con los que se relaciona. El sistema arma
la lista de quienes respondieron la encuesta ANTES de esa fecha y coinciden (dificultad marcada, mejora pedida o
palabras en sus comentarios); el supervisor la revisa, quita a quien no corresponda y aprueba el envío:
  - SMS corto con el nombre de pila y un enlace que abre SU conversación (?wcs=);
  - al abrir el chat (por ese enlace o cuando vuelva), Sofi le muestra el mensaje completo. Si ya está en el chat
    con el bot, le llega al instante.

Dos tonos: "reclamo" (tuvo la dificultad o calificó 1 a 3: "nos contaste que…") y "sugerencia" (estaba
satisfecho y solo pidió la mejora: "nos sugeriste…").

Reglas: un aviso por persona por mejora (por celular, aunque tenga varias encuestas o sesiones), máximo uno
cada 7 días por celular, solo de 9:00 a 18:59, y el SMS sin datos de salud (se ve en la pantalla bloqueada).

Fase 2 (migracion_seguimiento_fase2.sql):
  - Casos para revisar: si alguien se queja de un tema que YA se corrigió (su conversación es posterior a la
    mejora), el arreglo no le alcanzó: no recibe un "ya lo corregimos", sino que se le avisa al supervisor máximo
    (alerta en su consola y bandeja en el dashboard). También los insatisfechos (nota 1 o 2) cuya queja no tiene
    una mejora asociada, para contactarlos.
  - Agradecimiento por chat a quien dejó un comentario positivo (nota 4 o 5): Sofi se lo muestra la próxima vez
    que abra el chat (desde 2 horas después de responder). Sin SMS. Se activa en el Editor.
Tablas: migracion_seguimiento_encuesta.sql y migracion_seguimiento_fase2.sql.
"""

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from db import get_connection, release_connection
from encuesta import DIFICULTADES, MEJORAS, _celular_valido

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

UNO_CADA_DIAS = 7
HORA_DESDE, HORA_HASTA = 9, 19          # se envía de 9:00 a 18:59
MAX_POR_ENVIO = 200
VIGENCIA_CHAT_DIAS = 30                 # el mensaje en el chat se muestra si vuelve dentro de 30 días
MAX_TEXTO = 1500
CASOS_DIAS = 30                         # se revisan las encuestas respondidas en los últimos 30 días
AGRADECER_TRAS_HORAS = 2                # el agradecimiento se muestra desde 2 h después de responder
AGRADECIMIENTO_DEFAULT = ("Hola {{NOMBRE}} 💙 Gracias por tomarte un momento para dejarnos tu comentario en la "
                          "encuesta. Nos alegra mucho que tu experiencia haya sido buena. Aquí estaremos cuando "
                          "nos necesites.")

_TXT_DIF = dict(DIFICULTADES)
_TXT_MEJ = dict(MEJORAS)

# Ejemplo con el que se abre el formulario de una mejora nueva (la primera fue la de archivos).
EJEMPLO = {
    "titulo": "Envío de archivos de hasta 20 MB",
    "dificultades": ["otra"],
    "mejoras": ["adjuntar"],
    "palabras": ["archivo", "adjuntar", "adjunto", "pdf", "imagen", "foto", "documento", "subir", "escanead"],
    # SMS: el enlace ocupa ~65 caracteres; sin tildes, el texto + enlace debe quedar en 160 (1 solo SMS).
    "sms_reclamo": "Sanitas: {{NOMBRE}}, ya corregimos el envio de archivos que nos reportaste. Gracias! {{LINK}}",
    "chat_reclamo": "Hola {{NOMBRE}} 💙 Hace unos días nos contaste en la encuesta que tuviste problemas para enviar un "
                    "archivo por este chat. Lo revisamos: no se aceptaban archivos de más de 10 MB y tampoco te "
                    "explicábamos por qué. Ya lo corregimos: ahora puedes enviar archivos de hasta 20 MB y, si alguno "
                    "no pasa, te decimos el motivo. Gracias por ayudarnos a mejorar. Si lo necesitas, puedes "
                    "enviarnos tu archivo ahora mismo por aquí.",
    "sms_sugerencia": "Sanitas: {{NOMBRE}}, ya mejoramos el envio de archivos como nos sugeriste. Gracias! {{LINK}}",
    "chat_sugerencia": "Hola {{NOMBRE}} 💙 En la encuesta nos sugeriste que fuera más fácil adjuntar documentos o "
                       "imágenes en este chat. Ya lo mejoramos: ahora puedes enviar archivos de hasta 20 MB y, si "
                       "alguno no pasa, te decimos el motivo. Gracias por ayudarnos a mejorar.",
}

_aviso_migracion = False
_sin_fase2 = False          # True si aún no se corrió migracion_seguimiento_fase2.sql


def _falta_migracion(e: Exception) -> bool:
    global _aviso_migracion
    if any(x in str(e) for x in ("encuesta_mejoras", "encuesta_seguimientos", "encuesta_casos", "visible_desde")):
        if not _aviso_migracion:
            logger.warning("seguimiento: falta ejecutar migracion_seguimiento_encuesta.sql o migracion_seguimiento_fase2.sql")
            _aviso_migracion = True
        return True
    return False


class FaltaMigracion(Exception):
    pass


def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn").lower()


def nombre_de_pila(nombre: str | None) -> str:
    """'CLAUDIA VANESSA AMEZQUITA' -> 'Claudia'. Vacío si no hay nombre o parece un placeholder."""
    partes = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", nombre or "")
    if not partes or partes[0].lower() in ("usuario", "cliente", "visitante"):
        return ""
    return partes[0][:1].upper() + partes[0][1:].lower()


def armar_texto(plantilla: str, nombre: str, link: str | None = None) -> str:
    """{{NOMBRE}} con el nombre de pila (si no hay, se quita: "Hola {{NOMBRE}}, …" -> "Hola, …") y {{LINK}}."""
    t = plantilla or ""
    if nombre:
        t = t.replace("{{NOMBRE}}", nombre)
    else:   # "Hola {{NOMBRE}}, …" -> "Hola, …"   |   "Sanitas: {{NOMBRE}}, ya…" -> "Sanitas: ya…"
        t = re.sub(r":\s*,\s*", ": ", re.sub(r"\s*\{\{NOMBRE\}\}", "", t))
    if link is not None:
        t = t.replace("{{LINK}}", link) if "{{LINK}}" in t else f"{t} {link}"
    return t.strip()


def _validar(d: dict):
    titulo = str(d.get("titulo") or "").strip()[:120]
    if not titulo:
        return None, "Ponle un nombre a la mejora."
    try:
        publicada = date.fromisoformat(str(d.get("publicada_en") or ""))
    except ValueError:
        return None, "Indica la fecha en que la mejora salió a producción."
    if publicada > datetime.now(LIMA_TZ).date():
        return None, "La fecha de la mejora no puede ser futura."
    dif = [c for c in (d.get("dificultades") or []) if c in _TXT_DIF and c != "ninguna"]
    mej = [c for c in (d.get("mejoras") or []) if c in _TXT_MEJ and c != "ninguna"]
    palabras = []
    for p in d.get("palabras") or []:
        p = _sin_tildes(str(p)).strip()
        if len(p) >= 3 and p not in palabras:
            palabras.append(p[:40])
    if not dif and not mej and not palabras:
        return None, "Elige al menos un tema de la encuesta o una palabra clave."
    textos = {}
    for k in ("sms_reclamo", "chat_reclamo", "sms_sugerencia", "chat_sugerencia"):
        v = str(d.get(k) or "").strip()
        if not v:
            return None, "Completa los cuatro textos (SMS y chat, para reclamo y para sugerencia)."
        textos[k] = v[:MAX_TEXTO]
    for k in ("sms_reclamo", "sms_sugerencia"):
        if "{{LINK}}" not in textos[k]:
            return None, "Los SMS deben llevar {{LINK}} (el enlace a su conversación)."
    return {"titulo": titulo, "publicada_en": publicada, "dificultades": dif, "mejoras": mej,
            "palabras": palabras[:30], **textos}, None


# ---------------------------------------------------------------------------
# Mejoras
# ---------------------------------------------------------------------------
def listar_mejoras() -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT m.id, m.titulo, m.publicada_en, m.dificultades, m.mejoras, m.palabras,
                              m.sms_reclamo, m.chat_reclamo, m.sms_sugerencia, m.chat_sugerencia,
                              COUNT(s.id), COUNT(s.id) FILTER (WHERE s.sms_ok), COUNT(s.id) FILTER (WHERE s.telefono IS NULL),
                              COUNT(s.id) FILTER (WHERE s.chat_entregado_en IS NOT NULL),
                              COUNT(s.id) FILTER (WHERE EXISTS (SELECT 1 FROM conversaciones c
                                                   WHERE c.usuario_id = s.usuario_id AND c.iniciado_en > s.enviado_en)),
                              MAX(s.enviado_en),
                              -- ¿cómo calificaron DESPUÉS del aviso los que volvieron a responder la encuesta?
                              COUNT(post.id), AVG(post.satisfaccion)
                       FROM encuesta_mejoras m LEFT JOIN encuesta_seguimientos s ON s.mejora_id = m.id
                       LEFT JOIN LATERAL (SELECT e2.id, e2.satisfaccion FROM encuestas_satisfaccion e2
                                          WHERE e2.usuario_id = s.usuario_id AND e2.respondida_en > s.enviado_en
                                          ORDER BY e2.respondida_en LIMIT 1) post ON TRUE
                       GROUP BY m.id ORDER BY m.publicada_en DESC, m.id DESC""")
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if _falta_migracion(e):
                    raise FaltaMigracion() from e
                raise
            filas = cur.fetchall()
        conn.rollback()
    finally:
        release_connection(conn)
    return [{"id": i, "titulo": t, "publicada_en": p.isoformat(), "dificultades": list(d or []), "mejoras": list(m or []),
             "palabras": list(pa or []), "sms_reclamo": sr, "chat_reclamo": cr, "sms_sugerencia": ss, "chat_sugerencia": cs,
             "enviados": n, "sms_ok": sok, "solo_chat": sc, "vistos_chat": vc, "volvieron": vol,
             "ultimo_envio": u.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M") if u else None,
             "encuestas_despues": nd, "satisfaccion_despues": round(float(sd), 1) if sd is not None else None}
            for i, t, p, d, m, pa, sr, cr, ss, cs, n, sok, sc, vc, vol, u, nd, sd in filas]


def guardar_mejora(datos: dict, asesor_id: int, mejora_id: int | None = None) -> dict:
    v, motivo = _validar(datos or {})
    if motivo:
        return {"ok": False, "motivo": motivo}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            params = (v["titulo"], v["publicada_en"], v["dificultades"], v["mejoras"], v["palabras"],
                      v["sms_reclamo"], v["chat_reclamo"], v["sms_sugerencia"], v["chat_sugerencia"])
            if mejora_id:
                cur.execute("""UPDATE encuesta_mejoras SET titulo=%s, publicada_en=%s, dificultades=%s, mejoras=%s,
                                      palabras=%s, sms_reclamo=%s, chat_reclamo=%s, sms_sugerencia=%s,
                                      chat_sugerencia=%s, actualizada_en=now()
                               WHERE id=%s RETURNING id""", params + (mejora_id,))
            else:
                cur.execute("""INSERT INTO encuesta_mejoras (titulo, publicada_en, dificultades, mejoras, palabras,
                                      sms_reclamo, chat_reclamo, sms_sugerencia, chat_sugerencia, creada_por)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", params + (asesor_id,))
            fila = cur.fetchone()
        conn.commit()
        return {"ok": bool(fila), "id": fila[0] if fila else None, **({} if fila else {"motivo": "No existe esa mejora."})}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_seguimiento_encuesta.sql en la base de datos."}
        logger.exception("seguimiento: no se pudo guardar la mejora")
        return {"ok": False, "motivo": "No se pudo guardar la mejora."}
    finally:
        release_connection(conn)


def _mejora(cur, mejora_id: int):
    cur.execute("""SELECT id, titulo, publicada_en, dificultades, mejoras, palabras, sms_reclamo, chat_reclamo,
                          sms_sugerencia, chat_sugerencia FROM encuesta_mejoras WHERE id = %s""", (mejora_id,))
    f = cur.fetchone()
    if not f:
        return None
    return dict(zip(("id", "titulo", "publicada_en", "dificultades", "mejoras", "palabras", "sms_reclamo",
                     "chat_reclamo", "sms_sugerencia", "chat_sugerencia"), f))


# ---------------------------------------------------------------------------
# A quién avisar
# ---------------------------------------------------------------------------
def _coincidencia(m: dict, sat, dif, dif_otra, mejs, mej_otra, com):
    """(motivos, tipo) si la encuesta coincide con los temas de la mejora `m`; ([], None) si no.
    tipo: "reclamo" (tuvo el problema o calificó 1 a 3) o "sugerencia" (satisfecho, solo lo pidió)."""
    dif_set, mej_set = set(m["dificultades"] or []), set(m["mejoras"] or [])
    motivos = []
    if dif in dif_set:
        motivos.append(f"Dificultad: {_TXT_DIF.get(dif, dif)}")
    for x in mejs or []:
        if x in mej_set:
            motivos.append(f"Pidió: {_TXT_MEJ.get(x, x)}")
    texto = _sin_tildes(" ".join(t for t in (dif_otra, mej_otra, com) if t))
    encontradas = [p for p in (m["palabras"] or []) if p and p in texto]
    if encontradas:
        motivos.append("Comentario: " + ", ".join(encontradas))
    # Con solo "Otra dificultad" no alcanza: tiene que decir algo del tema en su comentario.
    if not motivos or all(mo == f"Dificultad: {_TXT_DIF['otra']}" for mo in motivos):
        return [], None
    tipo = "reclamo" if (sat is not None and sat <= 3) or (dif in dif_set) or (encontradas and dif != "ninguna") else "sugerencia"
    return motivos, tipo


def _candidatos(cur, m: dict, ahora: datetime) -> list[dict]:
    """Personas a las que corresponde avisar de la mejora `m` (una por celular o, sin celular, por usuario)."""
    hasta = datetime.combine(m["publicada_en"] + timedelta(days=1), datetime.min.time(), LIMA_TZ)
    cur.execute(
        """SELECT e.id, e.respondida_en, e.satisfaccion, e.facilidad, e.dificultad, e.dificultad_otra, e.mejoras,
                  e.mejora_otra, e.comentario, e.conversacion_id, COALESCE(cam.nombre, ''), COALESCE(a.nombre_display, ''),
                  u.id, u.nombre, COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR')
           FROM encuestas_satisfaccion e
           JOIN usuarios_webchat u ON u.id = e.usuario_id
           JOIN conversaciones c ON c.id = e.conversacion_id
           LEFT JOIN campanas cam ON cam.id = e.campana_id
           LEFT JOIN asesores a ON a.id = e.asesor_id
           WHERE e.respondida_en IS NOT NULL AND e.respondida_en < %s
           ORDER BY e.respondida_en DESC""", (hasta,))
    filas = cur.fetchall()
    cur.execute("SELECT encuesta_id, usuario_id, telefono, enviado_en FROM encuesta_seguimientos WHERE mejora_id = %s",
                (m["id"],))
    ya = cur.fetchall()
    ya_enc = {r[0]: r[3] for r in ya}
    ya_usu = {r[1] for r in ya}
    ya_tel = {r[2] for r in ya if r[2]}
    cur.execute("""SELECT telefono, MAX(enviado_en) FROM encuesta_seguimientos
                   WHERE telefono IS NOT NULL AND enviado_en > %s AND mejora_id <> %s GROUP BY telefono""",
                (ahora - timedelta(days=UNO_CADA_DIAS), m["id"]))
    recientes = dict(cur.fetchall())

    personas: dict = {}
    for (eid, resp_en, sat, fac, dif, dif_otra, mejs, mej_otra, com, conv_id, campana, asesor,
         usuario_id, nombre, tel) in filas:
        motivos, tipo = _coincidencia(m, sat, dif, dif_otra, mejs, mej_otra, com)
        if not motivos:
            continue
        telefono = _celular_valido(tel)
        clave = telefono or f"u{usuario_id}"
        p = personas.get(clave)
        item = {
            "encuesta_id": eid, "respondida_en": resp_en.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M"),
            "conversacion_id": conv_id, "campana": campana, "asesor": asesor,
            "satisfaccion": sat, "facilidad": fac, "dificultad": _TXT_DIF.get(dif, dif), "dificultad_otra": dif_otra,
            "mejoras": [_TXT_MEJ.get(x, x) for x in (mejs or [])], "mejora_otra": mej_otra, "comentario": com,
            "motivos": motivos, "tipo": tipo,
        }
        if p is None:
            p = personas[clave] = {
                "clave": clave, "usuario_id": usuario_id, "nombre_completo": nombre or "",
                "nombre": nombre_de_pila(nombre), "telefono": telefono, "encuestas": [],
            }
        p["encuestas"].append(item)
        if not p["nombre"] and nombre_de_pila(nombre):
            p["nombre"], p["nombre_completo"] = nombre_de_pila(nombre), nombre

    salida = []
    for p in personas.values():
        principal = p["encuestas"][0]   # la más reciente
        p["tipo"] = "reclamo" if any(x["tipo"] == "reclamo" for x in p["encuestas"]) else "sugerencia"
        p["encuesta_id"] = principal["encuesta_id"]
        enviado = next((ya_enc[x["encuesta_id"]] for x in p["encuestas"] if x["encuesta_id"] in ya_enc), None)
        if enviado or p["usuario_id"] in ya_usu or (p["telefono"] and p["telefono"] in ya_tel):
            p["estado"] = "enviado"
            p["estado_detalle"] = ("Avisado el " + enviado.astimezone(LIMA_TZ).strftime("%d/%m %H:%M")) if enviado else "Ya avisado"
        elif p["telefono"] and p["telefono"] in recientes:
            p["estado"] = "bloqueado"
            p["estado_detalle"] = "Recibió otro aviso el " + recientes[p["telefono"]].astimezone(LIMA_TZ).strftime("%d/%m") + " (máx. 1 cada 7 días)"
        else:
            p["estado"] = "pendiente"
            p["estado_detalle"] = "" if p["telefono"] else "Sin celular: solo verá el mensaje si vuelve al chat"
        tono = p["tipo"]
        p["vista_sms"] = armar_texto(m[f"sms_{tono}"], p["nombre"], "https://sanitasperu.com/?wcs=…") if p["telefono"] else None
        p["vista_chat"] = armar_texto(m[f"chat_{tono}"], p["nombre"])
        salida.append(p)
    orden = {"pendiente": 0, "bloqueado": 1, "enviado": 2}
    salida.sort(key=lambda x: (orden[x["estado"]], x["tipo"] != "reclamo"))
    return salida


def candidatos(mejora_id: int) -> dict:
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            m = _mejora(cur, mejora_id)
            if not m:
                conn.rollback()
                return {"ok": False, "motivo": "No existe esa mejora."}
            lista = _candidatos(cur, m, ahora)
        conn.rollback()
    finally:
        release_connection(conn)
    return {"ok": True, "mejora": {"id": m["id"], "titulo": m["titulo"], "publicada_en": m["publicada_en"].isoformat()},
            "personas": lista, "en_horario": HORA_DESDE <= ahora.hour < HORA_HASTA}


def enviar(mejora_id: int, claves: list, asesor_id: int, widget_url: str, enviar_sms) -> dict:
    """Envía el aviso a las personas elegidas (claves de candidatos()). Devuelve el resumen y los usuario_id
    avisados (para mostrarles el mensaje al instante si están en el chat)."""
    ahora = datetime.now(LIMA_TZ)
    if not (HORA_DESDE <= ahora.hour < HORA_HASTA):
        return {"ok": False, "motivo": f"Los avisos se envían de {HORA_DESDE}:00 a {HORA_HASTA - 1}:59. Vuelve a intentarlo en ese horario."}
    claves = [str(c) for c in (claves or [])][:MAX_POR_ENVIO]
    if not claves:
        return {"ok": False, "motivo": "Elige al menos una persona."}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            m = _mejora(cur, mejora_id)
            if not m:
                conn.rollback()
                return {"ok": False, "motivo": "No existe esa mejora."}
            elegidas = [p for p in _candidatos(cur, m, ahora) if p["clave"] in set(claves) and p["estado"] == "pendiente"]
            cur.execute("SELECT id, sesion_token FROM usuarios_webchat WHERE id = ANY(%s)",
                        ([p["usuario_id"] for p in elegidas],))
            tokens = dict(cur.fetchall())
            reservadas = []
            for p in elegidas:
                tono = p["tipo"]
                chat = armar_texto(m[f"chat_{tono}"], p["nombre"])
                sms = None
                if p["telefono"] and tokens.get(p["usuario_id"]):
                    sep = "&" if "?" in widget_url else "?"
                    sms = armar_texto(m[f"sms_{tono}"], p["nombre"], f"{widget_url}{sep}wcs={tokens[p['usuario_id']]}")
                cur.execute("""INSERT INTO encuesta_seguimientos (mejora_id, encuesta_id, usuario_id, tipo, telefono,
                                      sms_texto, chat_texto, enviado_en, enviado_por)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (mejora_id, encuesta_id) DO NOTHING RETURNING id""",
                            (m["id"], p["encuesta_id"], p["usuario_id"], tono, p["telefono"] if sms else None,
                             sms, chat, ahora, asesor_id))
                fila = cur.fetchone()
                if fila:
                    reservadas.append((fila[0], p, sms))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_seguimiento_encuesta.sql en la base de datos."}
        logger.exception("seguimiento: no se pudo preparar el envío")
        return {"ok": False, "motivo": "No se pudo preparar el envío."}
    finally:
        release_connection(conn)

    # Los SMS se mandan después de guardar (si se cae a la mitad, nadie recibe dos veces).
    sms_ok = sms_fallidos = solo_chat = 0
    resultados = []
    for sid, p, sms in reservadas:
        ok = None
        if sms:
            try:
                ok = bool(enviar_sms(p["telefono"], sms))
            except Exception:  # noqa: BLE001
                ok = False
            sms_ok += ok
            sms_fallidos += not ok
        else:
            solo_chat += 1
        resultados.append((sid, ok))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for sid, ok in resultados:
                if ok is not None:
                    cur.execute("UPDATE encuesta_seguimientos SET sms_ok = %s WHERE id = %s", (ok, sid))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("seguimiento: no se pudo registrar el resultado de los SMS")
    finally:
        release_connection(conn)
    logger.info("Seguimiento mejora %s: %s avisos (%s SMS ok, %s fallidos, %s solo chat)", mejora_id,
                len(reservadas), sms_ok, sms_fallidos, solo_chat)
    return {"ok": True, "enviados": len(reservadas), "sms_ok": sms_ok, "sms_fallidos": sms_fallidos,
            "solo_chat": solo_chat, "usuarios": [p["usuario_id"] for _, p, _ in reservadas]}


# ---------------------------------------------------------------------------
# Mensaje en el chat
# ---------------------------------------------------------------------------
def entregar_pendientes(conversacion_id: int, usuario_id: int | None = None, sesion_token: str | None = None) -> list[str]:
    """Mensajes de seguimiento que el cliente aún no vio: se guardan como mensaje de Sofi en ESTA conversación y se
    marcan entregados. Devuelve los textos para mostrarlos en el widget."""
    if not usuario_id and not sesion_token:
        return []
    global _sin_fase2
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Los agradecimientos (fase 2) se muestran desde visible_desde; sin esa migración, la condición se omite.
            visible = "" if _sin_fase2 else " AND s.visible_desde <= %s"
            params = (usuario_id, usuario_id, sesion_token, ahora, conversacion_id,
                      ahora - timedelta(days=VIGENCIA_CHAT_DIAS)) + (() if _sin_fase2 else (ahora,))
            try:
                cur.execute(
                    # Por usuario o por su celular: la misma persona puede tener varias sesiones (otro navegador,
                    # el navegador interno del SMS) y debe ver el aviso en cualquiera de ellas, una sola vez.
                    """WITH yo AS (
                           SELECT u.id, right(regexp_replace(COALESCE(u.telefono, ''), '\\D', '', 'g'), 9) AS cel
                           FROM usuarios_webchat u
                           WHERE u.id = %s OR (%s::int IS NULL AND u.sesion_token = %s))
                       UPDATE encuesta_seguimientos s SET chat_entregado_en = %s, chat_conversacion_id = %s
                       FROM yo
                       WHERE s.chat_entregado_en IS NULL AND s.enviado_en > %s
                         AND (s.usuario_id = yo.id OR (s.telefono IS NOT NULL AND s.telefono = yo.cel AND yo.cel <> ''))"""
                    + visible + """
                       RETURNING s.chat_texto, s.enviado_en""", params)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if "visible_desde" in str(e) and not _sin_fase2:
                    _sin_fase2 = True
                    return entregar_pendientes(conversacion_id, usuario_id, sesion_token)
                if not _falta_migracion(e):
                    logger.warning("seguimiento: no se pudo revisar mensajes pendientes", exc_info=True)
                return []
            textos = [t for t, _ in sorted(cur.fetchall(), key=lambda x: x[1])]
            for t in textos:
                cur.execute("""INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, enviado_en)
                               VALUES (%s, 'bot', 'texto', %s, %s)""", (conversacion_id, t, ahora))
        conn.commit()
        return textos
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("seguimiento: no se pudo entregar el mensaje en el chat", exc_info=True)
        return []
    finally:
        release_connection(conn)


def conversaciones_con_bot(usuario_ids: list[int], conectadas: list[int]) -> list[tuple[int, int]]:
    """(conversacion_id, usuario_id) de los avisados que AHORA están en el chat con el bot (no se interrumpe una
    conversación en cola o con un asesor: lo verán la próxima vez)."""
    if not usuario_ids or not conectadas:
        return []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, usuario_id FROM conversaciones
                           WHERE usuario_id = ANY(%s) AND id = ANY(%s) AND estado = 'bot'""",
                        (list(usuario_ids), list(conectadas)))
            filas = cur.fetchall()
        conn.rollback()
        return filas
    finally:
        release_connection(conn)


def opciones_formulario() -> dict:
    return {
        "dificultades": [{"codigo": c, "texto": t} for c, t in DIFICULTADES if c != "ninguna"],
        "mejoras": [{"codigo": c, "texto": t} for c, t in MEJORAS if c != "ninguna"],
        "ejemplo": {**EJEMPLO, "publicada_en": datetime.now(LIMA_TZ).date().isoformat()},
        "horario": f"{HORA_DESDE}:00 a {HORA_HASTA - 1}:59",
    }


# ---------------------------------------------------------------------------
# Fase 2: casos para revisar (el arreglo no le alcanzó / insatisfechos sin mejora asociada)
# ---------------------------------------------------------------------------
def _todas_las_mejoras(cur) -> list[dict]:
    cur.execute("""SELECT id, titulo, publicada_en, dificultades, mejoras, palabras FROM encuesta_mejoras
                   ORDER BY publicada_en DESC, id DESC""")
    return [dict(zip(("id", "titulo", "publicada_en", "dificultades", "mejoras", "palabras"), f)) for f in cur.fetchall()]


def _clasificar(mejoras: list[dict], conv_inicio: datetime, sat, dif, dif_otra, mejs, mej_otra, com):
    """("reincidencia", mejora) si se queja de un tema ya corregido ANTES de su conversación;
    ("insatisfecho", None) si calificó 1 o 2 y su queja no la cubre ninguna mejora; (None, None) si no es un caso.
    Si coincide con una mejora publicada DESPUÉS de su conversación, ya la cubre el aviso de esa mejora."""
    cubierta = False
    for m in mejoras:
        motivos, tipo = _coincidencia(m, sat, dif, dif_otra, mejs, mej_otra, com)
        if not motivos or tipo != "reclamo":
            continue
        corregido_desde = datetime.combine(m["publicada_en"] + timedelta(days=1), datetime.min.time(), LIMA_TZ)
        if conv_inicio and conv_inicio >= corregido_desde:
            return "reincidencia", m
        cubierta = True
    if sat is not None and sat <= 2 and not cubierta:
        return "insatisfecho", None
    return None, None


def _sincronizar_casos(cur, ahora: datetime, encuesta_id: int | None = None) -> list[int]:
    """Crea los casos que falten (encuestas de los últimos 30 días, o solo `encuesta_id`). Devuelve los ids nuevos."""
    mejoras = _todas_las_mejoras(cur)
    filtro, params = ("e.id = %s", [encuesta_id]) if encuesta_id else ("e.respondida_en > %s", [ahora - timedelta(days=CASOS_DIAS)])
    cur.execute(
        f"""SELECT e.id, c.iniciado_en, e.satisfaccion, e.dificultad, e.dificultad_otra, e.mejoras, e.mejora_otra, e.comentario
            FROM encuestas_satisfaccion e JOIN conversaciones c ON c.id = e.conversacion_id
            WHERE e.respondida_en IS NOT NULL AND {filtro}
              AND NOT EXISTS (SELECT 1 FROM encuesta_casos k WHERE k.encuesta_id = e.id)
              -- a quien ya se le avisó por esta encuesta no se le abre un caso de insatisfecho
              AND NOT EXISTS (SELECT 1 FROM encuesta_seguimientos s WHERE s.encuesta_id = e.id AND s.mejora_id IS NOT NULL)""",
        params)
    nuevos = []
    for eid, inicio, sat, dif, dif_otra, mejs, mej_otra, com in cur.fetchall():
        tipo, m = _clasificar(mejoras, inicio.astimezone(LIMA_TZ) if inicio else None, sat, dif, dif_otra, mejs, mej_otra, com)
        if not tipo:
            continue
        cur.execute("""INSERT INTO encuesta_casos (encuesta_id, tipo, mejora_id) VALUES (%s, %s, %s)
                       ON CONFLICT (encuesta_id) DO NOTHING RETURNING id""", (eid, tipo, m["id"] if m else None))
        fila = cur.fetchone()
        if fila:
            nuevos.append(fila[0])
    return nuevos


def _caso_dict(f) -> dict:
    (cid, tipo, estado, nota, atendido_en, atendido_por, creado_en, eid, resp_en, sat, dif, dif_otra, mejs, mej_otra,
     com, conv_id, campana, asesor, nombre, tel, mejora_titulo) = f
    return {
        "id": cid, "tipo": tipo, "estado": estado, "nota": nota,
        "atendido": (atendido_en.astimezone(LIMA_TZ).strftime("%d/%m %H:%M") + (f" · {atendido_por}" if atendido_por else "")) if atendido_en else None,
        "creado_en": creado_en.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M"),
        "encuesta_id": eid, "respondida_en": resp_en.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M") if resp_en else "",
        "satisfaccion": sat, "dificultad": _TXT_DIF.get(dif, dif), "dificultad_otra": dif_otra,
        "mejoras": [_TXT_MEJ.get(x, x) for x in (mejs or [])], "mejora_otra": mej_otra, "comentario": com,
        "conversacion_id": conv_id, "campana": campana, "asesor": asesor,
        "nombre": nombre_de_pila(nombre), "nombre_completo": nombre or "", "telefono": _celular_valido(tel),
        "mejora": mejora_titulo,
    }


_SQL_CASOS = """
    SELECT k.id, k.tipo, k.estado, k.nota, k.atendido_en, ad.nombre_display, k.creado_en,
           e.id, e.respondida_en, e.satisfaccion, e.dificultad, e.dificultad_otra, e.mejoras, e.mejora_otra, e.comentario,
           e.conversacion_id, COALESCE(cam.nombre, ''), COALESCE(a.nombre_display, ''), u.nombre,
           COALESCE(NULLIF(u.telefono, ''), c.variables_sesion->>'CELULAR'), mj.titulo
    FROM encuesta_casos k
    JOIN encuestas_satisfaccion e ON e.id = k.encuesta_id
    JOIN conversaciones c ON c.id = e.conversacion_id
    JOIN usuarios_webchat u ON u.id = e.usuario_id
    LEFT JOIN campanas cam ON cam.id = e.campana_id
    LEFT JOIN asesores a ON a.id = e.asesor_id
    LEFT JOIN asesores ad ON ad.id = k.atendido_por
    LEFT JOIN encuesta_mejoras mj ON mj.id = k.mejora_id"""


def listar_casos(pendientes: bool = True) -> dict:
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                _sincronizar_casos(cur, ahora)
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if _falta_migracion(e):
                    return {"disponible": False}
                raise
            cur.execute(_SQL_CASOS + (" WHERE k.estado = 'pendiente' ORDER BY (k.tipo = 'reincidencia') DESC, e.respondida_en DESC"
                                      if pendientes else
                                      " WHERE k.estado <> 'pendiente' ORDER BY k.atendido_en DESC LIMIT 100"))
            casos = [_caso_dict(f) for f in cur.fetchall()]
            cur.execute("SELECT estado, tipo, COUNT(*) FROM encuesta_casos GROUP BY 1, 2")
            conteo = {}
            for est, tipo, n in cur.fetchall():
                conteo.setdefault(est, {})[tipo] = n
            agr = _resumen_agradecimientos(cur, ahora)
        conn.rollback()
    finally:
        release_connection(conn)
    return {"disponible": True, "casos": casos, "conteo": conteo, "agradecimientos": agr}


def atender_caso(caso_id: int, estado: str, nota: str, asesor_id: int) -> dict:
    if estado not in ("contactado", "descartado", "pendiente"):
        return {"ok": False, "motivo": "Estado no válido."}
    nota = (str(nota or "").strip()[:1000]) or None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if estado == "pendiente":   # reabrir
                cur.execute("""UPDATE encuesta_casos SET estado = 'pendiente', atendido_en = NULL, atendido_por = NULL
                               WHERE id = %s RETURNING id""", (caso_id,))
            else:
                cur.execute("""UPDATE encuesta_casos SET estado = %s, nota = COALESCE(%s, nota), atendido_por = %s,
                                      atendido_en = %s WHERE id = %s RETURNING id""",
                            (estado, nota, asesor_id, datetime.now(LIMA_TZ), caso_id))
            ok = cur.fetchone() is not None
        conn.commit()
        return {"ok": ok} if ok else {"ok": False, "motivo": "No existe ese caso."}
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("seguimiento: no se pudo actualizar el caso %s", caso_id)
        return {"ok": False, "motivo": "No se pudo guardar."}
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Fase 2: agradecimiento por chat a los comentarios positivos
# ---------------------------------------------------------------------------
def _comentario_util(com) -> bool:
    return len(re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", com or "")) >= 3


def _crear_agradecimientos(cur, plantilla: str, visible_desde_fn, filtro: str, params: list) -> int:
    cur.execute(
        f"""SELECT e.id, e.usuario_id, u.nombre, e.respondida_en, e.comentario
            FROM encuestas_satisfaccion e JOIN usuarios_webchat u ON u.id = e.usuario_id
            WHERE e.respondida_en IS NOT NULL AND e.satisfaccion >= 4 AND e.comentario IS NOT NULL AND {filtro}
              AND NOT EXISTS (SELECT 1 FROM encuesta_seguimientos s WHERE s.encuesta_id = e.id AND s.tipo = 'agradecimiento')
              -- un agradecimiento por persona cada 30 días (aunque responda varias encuestas), y no si en la última
              -- semana ya recibió otro mensaje de seguimiento (para no juntarle dos mensajes)
              AND NOT EXISTS (SELECT 1 FROM encuesta_seguimientos s2 WHERE s2.usuario_id = e.usuario_id
                                AND ((s2.tipo = 'agradecimiento' AND s2.enviado_en > %s) OR s2.enviado_en > %s))""",
        params + [datetime.now(LIMA_TZ) - timedelta(days=CASOS_DIAS), datetime.now(LIMA_TZ) - timedelta(days=UNO_CADA_DIAS)])
    n = 0
    vistos = set()
    for eid, usuario_id, nombre, resp_en, com in cur.fetchall():
        if not _comentario_util(com) or usuario_id in vistos:
            continue
        vistos.add(usuario_id)
        cur.execute("""INSERT INTO encuesta_seguimientos (mejora_id, encuesta_id, usuario_id, tipo, chat_texto, enviado_en,
                                                          visible_desde)
                       VALUES (NULL, %s, %s, 'agradecimiento', %s, %s, %s)
                       ON CONFLICT DO NOTHING RETURNING id""",
                    (eid, usuario_id, armar_texto(plantilla or AGRADECIMIENTO_DEFAULT, nombre_de_pila(nombre)),
                     datetime.now(LIMA_TZ), visible_desde_fn(resp_en)))
        n += cur.fetchone() is not None
    return n


def _resumen_agradecimientos(cur, ahora: datetime) -> dict:
    cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE chat_entregado_en IS NOT NULL)
                   FROM encuesta_seguimientos WHERE tipo = 'agradecimiento'""")
    creados, vistos = cur.fetchone()
    cur.execute("""SELECT e.comentario FROM encuestas_satisfaccion e
                   WHERE e.respondida_en > %s AND e.satisfaccion >= 4 AND e.comentario IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM encuesta_seguimientos s WHERE s.encuesta_id = e.id AND s.tipo = 'agradecimiento')""",
                (ahora - timedelta(days=CASOS_DIAS),))
    pendientes = sum(1 for (c,) in cur.fetchall() if _comentario_util(c))
    return {"creados": creados, "vistos": vistos, "pendientes_anteriores": pendientes}


def agradecer_anteriores(plantilla: str) -> dict:
    """Agradecimiento por chat a los comentarios positivos de los últimos 30 días que aún no lo tienen."""
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            n = _crear_agradecimientos(cur, plantilla, lambda _r: ahora, "e.respondida_en > %s",
                                       [ahora - timedelta(days=CASOS_DIAS)])
        conn.commit()
        return {"ok": True, "creados": n}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_seguimiento_fase2.sql en la base de datos."}
        logger.exception("seguimiento: no se pudieron crear los agradecimientos")
        return {"ok": False, "motivo": "No se pudo crear los agradecimientos."}
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Al responder una encuesta (widget o enlace del SMS)
# ---------------------------------------------------------------------------
def al_responder(encuesta_id: int, plantilla_agradecimiento: str | None) -> dict | None:
    """Revisa la encuesta recién respondida: si es un caso para revisar, lo crea y lo devuelve (para alertar al
    supervisor máximo); si es un comentario positivo y el agradecimiento está activo, lo deja programado."""
    ahora = datetime.now(LIMA_TZ)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if plantilla_agradecimiento:
                _crear_agradecimientos(cur, plantilla_agradecimiento,
                                       lambda r: (r or ahora) + timedelta(hours=AGRADECER_TRAS_HORAS), "e.id = %s", [encuesta_id])
            nuevos = _sincronizar_casos(cur, ahora, encuesta_id)
            caso = None
            if nuevos:
                cur.execute(_SQL_CASOS + " WHERE k.id = %s", (nuevos[0],))
                f = cur.fetchone()
                caso = _caso_dict(f) if f else None
        conn.commit()
        return caso
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if not _falta_migracion(e):
            logger.warning("seguimiento: no se pudo revisar la encuesta %s", encuesta_id, exc_info=True)
        return None
    finally:
        release_connection(conn)


def texto_alerta(caso: dict) -> str:
    """Texto corto para la alerta en la consola del supervisor máximo (sin datos de salud)."""
    quien = caso.get("nombre") or "Un cliente"
    dijo = (caso.get("dificultad_otra") or caso.get("comentario") or caso.get("mejora_otra") or "").strip()
    dijo = f" «{dijo[:140]}{'…' if len(dijo) > 140 else ''}»" if dijo else ""
    if caso["tipo"] == "reincidencia":
        return (f"⚠️ {quien} se quejó de «{caso.get('mejora')}», que ya habíamos corregido (nota {caso.get('satisfaccion')})."
                f"{dijo} · conv. #{caso['conversacion_id']}")
    return f"😞 {quien} calificó la atención con {caso.get('satisfaccion')}.{dijo} · conv. #{caso['conversacion_id']}"
