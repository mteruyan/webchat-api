"""
temas_consulta.py — clasifica por tema lo que escribe la gente en el webchat y
cuenta qué opciones del menú tocan, para el dashboard ("¿qué es lo que más consultan?").

La clasificación es por palabras clave (sin IA, sin costo, explicable): un mensaje puede
caer en más de un tema y una conversación se cuenta UNA vez por tema. Lo que no calza con
ningún tema va a "Otros", y se devuelven las palabras más repetidas de ese grupo para
que se pueda ampliar el diccionario TEMAS cuando aparezca un tema nuevo.
"""

import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

# tema -> expresiones regulares sobre el texto normalizado (minúsculas, sin tildes).
TEMAS: dict[str, list[str]] = {
    "Reembolsos": [r"reembols", r"devoluc", r"devolv", r"me (lo )?(re)?pagan"],
    "Recetas y medicamentos": [r"receta", r"medicament", r"pastilla", r"farmac", r"medicina", r"botica", r"tratamiento", r"entrega de (mis )?med"],
    "Estado de pedido / entrega": [r"pedido", r"\bentrega", r"despach", r"courier", r"no (me )?lleg", r"cuando (me )?(lleg|entreg)", r"delivery", r"motorizado"],
    "Citas y atención médica": [r"\bcita", r"consulta medica", r"especialista", r"agendar", r"reservar", r"\bdoctor", r"\bmedico", r"pediatr", r"ginecolog", r"turno", r"cardiolog", r"dermatolog"],
    "Autorizaciones / carta de garantía": [r"autoriz", r"carta de garantia", r"\bgarantia", r"aprobac", r"aprobar"],
    "Coberturas y plan": [r"cobertura", r"beneficio", r"copago", r"deducible", r"poliza", r"\bplan\b", r"\bcubre", r"asegurad", r"\bseguro\b"],
    "Pagos y facturación": [r"\bpago", r"pagar", r"factura", r"boleta", r"cobro", r"cobraron", r"deuda", r"\bcuota", r"recibo", r"\bprima"],
    "Afiliación, altas y bajas": [r"afili", r"inscrib", r"dependiente", r"\balta\b", r"\bbaja\b", r"renov", r"carnet", r"cancelar (mi )?(plan|seguro)"],
    "SCTR": [r"sctr"],
    "Resultados y exámenes": [r"resultado", r"examen", r"laboratorio", r"analisis", r"ecograf", r"radiograf", r"tomograf", r"resonancia"],
    "Clínicas y red de atención": [r"clinica", r"hospital", r"\bsede", r"red de (atencion|clinicas)", r"donde (puedo )?(atender|ir)"],
    "Reclamos y quejas": [r"reclamo", r"queja", r"mal servicio", r"nadie (me )?(responde|atiende)", r"demora", r"inconformidad", r"pesimo"],
    "Documentos y constancias": [r"constancia", r"certificado", r"documento", r"copia de", r"historia clinica"],
    "Actualizar mis datos": [r"actualizar", r"cambiar (mi )?(numero|celular|correo|direccion|datos)", r"corregir"],
    "Hablar con una persona": [r"asesor", r"hablar con", r"ejecutiv", r"comunicar", r"\bllamar", r"\bcontacto"],
    "Emergencias / ambulancia": [r"emergencia", r"ambulancia", r"urgencia", r"\bgrave\b"],
}

_TEMAS_RE = {t: [re.compile(p) for p in pats] for t, pats in TEMAS.items()}

_STOP = set("""
que para por con una uno unos unas los las del como pero este esta esto estoy tengo tiene quiero necesito
favor gracias hola buenas buenos dias tardes noches mas muy mis sus mi su se le lo la el de en y a o no si
puedo pueden puede ayuda saber quisiera queria buen donde cuando cual estan estoy soy fue ser han hay ya
""".split())


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


def temas_de(texto_normalizado: str) -> list[str]:
    return [t for t, pats in _TEMAS_RE.items() if any(p.search(texto_normalizado) for p in pats)]


def _es_consulta(texto: str) -> bool:
    """Descarta lo que no es una consulta: números (celular, DNI, código) y mensajes muy cortos."""
    letras = sum(c.isalpha() for c in texto)
    return letras >= 4 and len(texto.strip()) >= 6


def inicio_semana(d: date) -> date:
    return d - timedelta(days=d.weekday())


def clasificar(filas, desde: date, hasta: date) -> dict:
    """filas: iterable de (conversacion_id, texto, fecha). `hasta` inclusive.
    Devuelve {'buckets': [...], 'temas': [...], 'otros': {...}, 'conversaciones_con_texto': n}."""
    dias = (hasta - desde).days + 1
    semanal = dias > 31
    clave = (lambda d: inicio_semana(d)) if semanal else (lambda d: d)
    buckets = []
    cur = clave(desde)
    while cur <= hasta:
        buckets.append(cur)
        cur += timedelta(days=7 if semanal else 1)
    idx = {b: i for i, b in enumerate(buckets)}

    por_tema: dict[str, dict] = {}
    convs_con_texto = set()
    convs_otros = set()
    palabras_otros = Counter()
    convs_clasificadas = set()
    vistos = set()  # (conversacion, tema) — una conversación cuenta una vez por tema

    for conv_id, texto, fecha in filas:
        if not texto or not _es_consulta(texto):
            continue
        convs_con_texto.add(conv_id)
        n = normalizar(texto)
        ts = temas_de(n)
        if not ts:
            for w in re.findall(r"[a-z]{5,}", n):
                if w not in _STOP:
                    palabras_otros[w] += 1
            continue
        convs_clasificadas.add(conv_id)
        b = clave(fecha if isinstance(fecha, date) and not isinstance(fecha, datetime) else fecha.date())
        for t in ts:
            if (conv_id, t) in vistos:
                continue
            vistos.add((conv_id, t))
            d = por_tema.setdefault(t, {"tema": t, "conversaciones": 0, "serie": [0] * len(buckets)})
            d["conversaciones"] += 1
            if b in idx:
                d["serie"][idx[b]] += 1

    convs_otros = convs_con_texto - convs_clasificadas
    temas = sorted(por_tema.values(), key=lambda x: -x["conversaciones"])
    return {
        "granularidad": "semana" if semanal else "dia",
        "buckets": [b.isoformat() for b in buckets],
        "temas": temas,
        "conversaciones_con_texto": len(convs_con_texto),
        "otros": {"conversaciones": len(convs_otros),
                  "palabras": [{"palabra": w, "veces": n} for w, n in palabras_otros.most_common(15)]},
    }


# ---------------------------------------------------------------------------
# Navegación por botones
# ---------------------------------------------------------------------------
import json as _json


def _etiqueta_boton(contenido: str, opciones_json: str | None) -> str:
    """Cada clic se guarda con el ID de la opción ('2', 'si', 'sctr'), no con el texto
    del botón. Se traduce usando la lista de opciones que el bot mostró justo antes."""
    contenido = (contenido or "").strip()
    if opciones_json:
        try:
            lista = _json.loads(opciones_json)
        except (TypeError, ValueError):
            lista = None
        if isinstance(lista, list):
            for o in lista:
                if not isinstance(o, dict):
                    continue
                if str(o.get("id", "")).strip() == contenido or (o.get("label") or "").strip() == contenido:
                    return (o.get("label") or o.get("texto") or contenido).strip()
    return contenido


_GENERICOS = re.compile(r"^(si|no|volver|atras|regresar|menu|menu principal|inicio|cancelar|continuar|aceptar|salir|otro|ok)\b")


def _es_generico(etiqueta: str) -> bool:
    """Confirmaciones y navegación (Sí / No, usar otro / Volver al menú...): no son un tema de consulta."""
    n = normalizar(etiqueta)
    n = re.sub(r"^[^a-z0-9]+", "", n)
    return bool(_GENERICOS.match(n))


def resumir_botones(filas, sin_conversaciones_sin_clic: int = 0) -> dict:
    """filas: (conversacion_id, contenido, opciones_json_previas, sin_opcion: bool) ordenadas por
    conversación y orden de clic. Devuelve botones más tocados y, para las conversaciones sin
    opción (no llegaron a un asesor), la primera opción que eligieron y el último botón que tocaron."""
    por_boton_convs: dict[str, set] = {}
    por_boton_clics: Counter = Counter()
    primero: dict = {}
    ultimo: dict = {}
    for conv, contenido, opciones, sin_opcion in filas:
        etiqueta = _etiqueta_boton(contenido, opciones)
        if not etiqueta:
            continue
        por_boton_clics[etiqueta] += 1
        por_boton_convs.setdefault(etiqueta, set()).add(conv)
        if sin_opcion:
            primero.setdefault(conv, etiqueta)
            ultimo[conv] = etiqueta
    botones = sorted(
        ({"opcion": e[:80], "clics": por_boton_clics[e], "conversaciones": len(cs)}
         for e, cs in por_boton_convs.items() if not _es_generico(e)),
        key=lambda x: (-x["conversaciones"], -x["clics"]))[:30]

    def contar(d, sin_clic_label):
        c = Counter(d.values())
        if sin_conversaciones_sin_clic:
            c[sin_clic_label] += sin_conversaciones_sin_clic
        return [{"opcion": k[:80], "conversaciones": n} for k, n in c.most_common(25)]

    return {
        "botones": botones,
        "sin_opcion_primer_boton": contar(primero, "(no tocó ningún botón)"),
        "sin_opcion_ultimo_boton": contar(ultimo, "(no tocó ningún botón)"),
    }
