"""
usuarios_sofi.py — mantenimiento (desde la consola de supervisor) de los datos
personales de los pacientes en la tabla `sofi_usuarios` de la base
"sofi_whatsapp" (misma instancia Cloud SQL que "webchat", otra base).

Reglas:
- El DOI identifica al paciente y NUNCA se modifica: solo se usa en el WHERE.
- Solo se pueden editar los campos de la lista blanca CAMPOS_EDITABLES (nombres,
  apellidos, direcciones/distritos y números de contacto). Si alguna de esas
  columnas no existe en la tabla real, simplemente se omite (se consulta
  information_schema, así no hace falta adivinar el esquema).
- Cada cambio queda auditado en la tabla `auditoria_usuarios_sofi` de la base
  "webchat" (quién, a qué DOI, qué campo, valor anterior y nuevo). Como los
  datos son personales, el valor anterior/nuevo queda solo ahí y en el log si
  la auditoría fallara.

Las funciones son SÍNCRONAS (psycopg2) — main.py las llama con
asyncio.to_thread para no congelar el event loop.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from db import (
    get_connection, release_connection,
    get_connection_sofi, release_connection_sofi,
)

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

# campo -> (etiqueta, largo máximo, tipo de validación)
CAMPOS_EDITABLES = {
    "nombres":       ("Nombres", 120, "nombre"),
    "apellidos":     ("Apellidos", 120, "nombre"),
    "direccion1":    ("Dirección 1", 250, "texto"),
    "distrito1":     ("Distrito 1", 100, "texto"),
    "direccion2":    ("Dirección 2", 250, "texto"),
    "distrito2":     ("Distrito 2", 100, "texto"),
    "direccion3":    ("Dirección 3", 250, "texto"),
    "distrito3":     ("Distrito 3", 100, "texto"),
    "nro_contacto1": ("Número de contacto 1", 15, "telefono"),
    "nro_contacto2": ("Número de contacto 2", 15, "telefono"),
    # El correo: según el esquema real la columna se llama de una de estas
    # formas; se usa solo la primera que exista (ver _campos_disponibles).
    "email":              ("Correo electrónico", 120, "correo"),
    "correo":             ("Correo electrónico", 120, "correo"),
    "correo_electronico": ("Correo electrónico", 120, "correo"),
}
_COLUMNAS_CORREO = ("email", "correo", "correo_electronico")

_RE_NOMBRE = re.compile(r"^[A-Za-zÁÉÍÓÚÑÜáéíóúñü'´`.\-\s]+$")
_columnas_cache: set | None = None


def _columnas_existentes(cur) -> set:
    global _columnas_cache
    if _columnas_cache is None:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'sofi_usuarios' AND table_schema = current_schema()"""
        )
        _columnas_cache = {r[0] for r in cur.fetchall()}
    return _columnas_cache


def _campos_disponibles(cur) -> list:
    existentes = _columnas_existentes(cur)
    correo_usado = next((c for c in _COLUMNAS_CORREO if c in existentes), None)
    return [c for c in CAMPOS_EDITABLES
            if c in existentes and (c not in _COLUMNAS_CORREO or c == correo_usado)]


def _fila_a_dict(campos: list, fila) -> dict:
    d = {"doi": fila[0]}
    for nombre, valor in zip(campos, fila[1:]):
        d[nombre] = valor if valor is not None else ""
    return d


def buscar(consulta: str) -> dict:
    """Busca pacientes por DOI, nombres, apellidos o número de contacto
    (contiene, sin importar mayúsculas). Mínimo 3 caracteres, máximo 30
    resultados. Devuelve {"ok", "motivo", "resultados": [...], "campos": [...]}"""
    consulta = (consulta or "").strip()
    if len(consulta) < 3:
        return {"ok": False, "motivo": "Escribe al menos 3 caracteres.", "resultados": [], "campos": []}
    conn = None
    try:
        conn = get_connection_sofi()
        with conn.cursor() as cur:
            campos = _campos_disponibles(cur)
            existentes = _columnas_existentes(cur)
            select_cols = ", ".join(["doi"] + campos)
            condiciones = ["doi::text ILIKE %(q)s"]
            for c in ("nombres", "apellidos", "nro_contacto1", "nro_contacto2") + _COLUMNAS_CORREO:
                if c in existentes:
                    condiciones.append(f"{c} ILIKE %(q)s")
            if "nombres" in existentes and "apellidos" in existentes:
                condiciones.append("(nombres || ' ' || apellidos) ILIKE %(q)s")
            orden = "apellidos, nombres" if "apellidos" in existentes and "nombres" in existentes else "doi"
            cur.execute(
                f"SELECT {select_cols} FROM sofi_usuarios WHERE {' OR '.join(condiciones)} "
                f"ORDER BY {orden} LIMIT 30",
                {"q": f"%{consulta}%"},
            )
            resultados = [_fila_a_dict(campos, f) for f in cur.fetchall()]
        conn.rollback()  # solo lectura: no dejar la transacción abierta
        return {"ok": True, "motivo": None, "resultados": resultados,
                "campos": [{"campo": c, "etiqueta": CAMPOS_EDITABLES[c][0]} for c in campos]}
    except Exception as e:
        logger.exception("Error buscando en sofi_usuarios: %s", e)
        return {"ok": False, "motivo": "No se pudo consultar la base de usuarios. Intenta de nuevo.",
                "resultados": [], "campos": []}
    finally:
        if conn:
            release_connection_sofi(conn)


def _validar_valor(campo: str, valor) -> tuple:
    """Devuelve (valor_limpio, error)."""
    etiqueta, maximo, tipo = CAMPOS_EDITABLES[campo]
    valor = re.sub(r"\s+", " ", str(valor if valor is not None else "")).strip()
    if len(valor) > maximo:
        return None, f"{etiqueta}: máximo {maximo} caracteres."
    if tipo == "nombre":
        if not valor:
            return None, f"{etiqueta} no puede quedar vacío."
        if not _RE_NOMBRE.match(valor):
            return None, f"{etiqueta}: solo letras, espacios, apóstrofes, puntos y guiones."
    elif tipo == "telefono":
        if valor:
            digitos = re.sub(r"\D", "", valor)
            if valor != digitos and valor.lstrip("+") != digitos:
                return None, f"{etiqueta}: solo números."
            if not 6 <= len(digitos) <= 15:
                return None, f"{etiqueta}: debe tener entre 6 y 15 dígitos."
            valor = digitos
    elif tipo == "correo":
        # Vacío = borrar el correo; si tiene algo, formato básico válido.
        if valor and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", valor):
            return None, f"{etiqueta}: no parece un correo válido (ejemplo: nombre@correo.com)."
        valor = valor.lower()
    return valor, None


def guardar(doi: str, cambios: dict, supervisor_id: int) -> dict:
    """Actualiza SOLO los campos editables que cambiaron, para ese DOI.
    `cambios` = {campo: nuevo_valor}. El DOI no se puede cambiar (no existe
    forma de pasarlo: solo se usa para ubicar la fila)."""
    doi = (doi or "").strip()
    if not doi:
        return {"ok": False, "motivo": "Falta el DOI."}
    if not isinstance(cambios, dict) or not cambios:
        return {"ok": False, "motivo": "No hay cambios para guardar."}
    desconocidos = [c for c in cambios if c not in CAMPOS_EDITABLES]
    if desconocidos:
        return {"ok": False, "motivo": "Hay campos que no se pueden modificar."}

    conn = None
    try:
        conn = get_connection_sofi()
        with conn.cursor() as cur:
            campos = _campos_disponibles(cur)
            cur.execute(f"SELECT {', '.join(['doi'] + campos)} FROM sofi_usuarios WHERE doi = %s FOR UPDATE", (doi,))
            fila = cur.fetchone()
            if not fila:
                conn.rollback()
                return {"ok": False, "motivo": "No se encontró un paciente con ese DOI."}
            if cur.fetchone() is not None:
                conn.rollback()
                return {"ok": False, "motivo": "Hay más de un registro con ese DOI; no se modificó nada."}
            actual = _fila_a_dict(campos, fila)

            nuevos = {}
            for campo, valor in cambios.items():
                if campo not in campos:
                    conn.rollback()
                    return {"ok": False, "motivo": f"El campo {CAMPOS_EDITABLES[campo][0]} no existe en la tabla."}
                limpio, error = _validar_valor(campo, valor)
                if error:
                    conn.rollback()
                    return {"ok": False, "motivo": error}
                if limpio != (actual.get(campo) or ""):
                    nuevos[campo] = limpio
            if not nuevos:
                conn.rollback()
                return {"ok": False, "motivo": "No cambió ningún dato."}

            sets = ", ".join(f"{c} = %({c})s" for c in nuevos)
            cur.execute(f"UPDATE sofi_usuarios SET {sets} WHERE doi = %(_doi)s", {**nuevos, "_doi": doi})
            if cur.rowcount != 1:
                conn.rollback()
                return {"ok": False, "motivo": "No se pudo actualizar el registro."}
        conn.commit()
        antes = {c: actual.get(c, "") for c in nuevos}
        actualizado = {**actual, **nuevos}
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("Error actualizando sofi_usuarios (doi=%s): %s", doi, e)
        return {"ok": False, "motivo": "No se pudo guardar. Si el problema continúa, revisa los permisos de la base."}
    finally:
        if conn:
            release_connection_sofi(conn)

    _auditar(supervisor_id, doi, antes, nuevos)
    return {"ok": True, "motivo": None, "usuario": actualizado, "campos_modificados": list(nuevos)}


def _auditar(supervisor_id: int, doi: str, antes: dict, despues: dict):
    """Un registro por campo cambiado, en la base webchat. Si falla (p. ej.
    todavía no se creó la tabla), el cambio YA está hecho: se deja constancia
    completa en el log para no perder la trazabilidad."""
    ahora = datetime.now(LIMA_TZ)
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            for campo, nuevo in despues.items():
                cur.execute(
                    """INSERT INTO auditoria_usuarios_sofi
                       (supervisor_id, doi, campo, valor_anterior, valor_nuevo, creado_en)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (supervisor_id, doi, campo, antes.get(campo, ""), nuevo, ahora),
                )
        conn.commit()
    except Exception as e:
        logger.error("AUDITORÍA no registrada en BD (%s). supervisor=%s doi=%s antes=%s despues=%s",
                     e, supervisor_id, doi, antes, despues)
    finally:
        if conn:
            release_connection(conn)
