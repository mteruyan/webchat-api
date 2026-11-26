"""petitorio.py — "Mantenimiento Petitorio" (consola del supervisor): buscar, agregar y modificar los medicamentos
de la tabla sofi_petitorio de la base "recetas" (misma instancia Cloud SQL que "webchat", otra base).

Tabla (la define otro sistema; aquí solo se lee y escribe):
    id                       integer generated always as identity
    descripcion              varchar(100) not null
    requiere_carta_garantia  boolean not null default false
    requiere_cuida           boolean not null default false
    con_autorizacion         boolean not null default false   (migracion_petitorio_autorizacion.sql, base recetas)
    created_at / updated_at  timestamp (sin zona) default now()

Reglas:
- No se borra nada (solo agregar y modificar).
- La descripción no puede repetirse al crear ni al cambiar la descripción (sin distinguir mayúsculas, tildes ni
  espacios dobles). Un registro que ya está repetido en la tabla sí se puede seguir manteniendo (banderas) mientras
  no cambie su descripción.
- Cada alta o cambio queda auditado en la tabla `auditoria_petitorio` de la base "webchat" (quién, cuándo, valores
  antes y después). Si falta esa tabla (migracion_auditoria_petitorio.sql), el cambio se hace igual y queda en el log.

Funciones SÍNCRONAS (psycopg2): main.py las llama con asyncio.to_thread.
"""

import json
import logging
import re
import unicodedata

from db import get_connection, release_connection, get_connection_recetas, release_connection_recetas

logger = logging.getLogger(__name__)

MAX_DESCRIPCION = 100
MIN_DESCRIPCION = 3
MAX_RESULTADOS = 300
FILTROS = ("todos", "carta", "cuida", "autorizacion", "ninguno")
CAMPOS = ("requiere_carta_garantia", "requiere_cuida", "con_autorizacion")   # banderas editables

# created_at/updated_at no tienen zona: se interpretan con la zona de la base y se muestran en hora de Lima.
_FECHA_LIMA = "to_char((({c}) AT TIME ZONE current_setting('TimeZone')) AT TIME ZONE 'America/Lima', 'DD/MM/YYYY HH24:MI')"
_COLUMNAS = (f"id, descripcion, requiere_carta_garantia, requiere_cuida, con_autorizacion, "
             f"{_FECHA_LIMA.format(c='created_at')}, {_FECHA_LIMA.format(c='updated_at')}")


def _fila(r) -> dict:
    return {"id": r[0], "descripcion": r[1], "requiere_carta_garantia": bool(r[2]), "requiere_cuida": bool(r[3]),
            "con_autorizacion": bool(r[4]), "creado": r[5] or "", "actualizado": r[6] or ""}


def _limpiar(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def _clave(texto) -> str:
    """Para comparar duplicados: sin tildes, en minúsculas y con un solo espacio."""
    t = unicodedata.normalize("NFKD", _limpiar(texto).lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def listar(query=None, filtro="todos") -> dict:
    query = _limpiar(query)[:MAX_DESCRIPCION]
    filtro = filtro if filtro in FILTROS else "todos"
    condiciones, params = [], []
    if query:
        # Cada palabra debe aparecer (en cualquier orden): "amoxi 500" encuentra "AMOXICILINA 500 MG TAB".
        for palabra in query.split(" ")[:6]:
            condiciones.append("descripcion ILIKE %s")
            params.append(f"%{palabra}%")
    if filtro == "carta":
        condiciones.append("requiere_carta_garantia")
    elif filtro == "cuida":
        condiciones.append("requiere_cuida")
    elif filtro == "autorizacion":
        condiciones.append("con_autorizacion")
    elif filtro == "ninguno":
        condiciones.append("NOT requiere_carta_garantia AND NOT requiere_cuida AND NOT con_autorizacion")
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    conn = get_connection_recetas()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM sofi_petitorio {where}", params)
            total = cur.fetchone()[0]
            cur.execute(f"SELECT {_COLUMNAS} FROM sofi_petitorio {where} ORDER BY descripcion, id LIMIT %s",
                        params + [MAX_RESULTADOS])
            filas = [_fila(r) for r in cur.fetchall()]
            cur.execute("""SELECT count(*), count(*) FILTER (WHERE requiere_carta_garantia),
                                  count(*) FILTER (WHERE requiere_cuida), count(*) FILTER (WHERE con_autorizacion)
                           FROM sofi_petitorio""")
            tot, carta, cuida, autorizacion = cur.fetchone()
        conn.rollback()
    finally:
        release_connection_recetas(conn)
    return {"ok": True, "query": query, "filtro": filtro, "total": total, "items": filas,
            "limite": MAX_RESULTADOS, "resumen": {"total": tot, "carta": carta, "cuida": cuida,
                                                         "autorizacion": autorizacion}}


def _validar(datos: dict) -> tuple:
    descripcion = _limpiar((datos or {}).get("descripcion"))
    if len(descripcion) < MIN_DESCRIPCION:
        return None, f"La descripción debe tener al menos {MIN_DESCRIPCION} caracteres."
    if len(descripcion) > MAX_DESCRIPCION:
        return None, f"La descripción puede tener como máximo {MAX_DESCRIPCION} caracteres."
    return {"descripcion": descripcion, **{c: bool((datos or {}).get(c)) for c in CAMPOS}}, None


def _duplicado(cur, descripcion: str, excepto_id=None):
    """id y descripción de otro medicamento con la misma descripción. La comparación se hace en Python (sin tildes ni
    mayúsculas); el petitorio tiene a lo más unos miles de filas cortas, así que traerlas todas es barato."""
    clave = _clave(descripcion)
    cur.execute("SELECT id, descripcion FROM sofi_petitorio WHERE (%s::int IS NULL OR id <> %s)", (excepto_id, excepto_id))
    for pid, desc in cur.fetchall():
        if _clave(desc) == clave:
            return pid, desc
    return None


def _auditar(petitorio_id, accion, antes, despues, supervisor_id):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO auditoria_petitorio (petitorio_id, accion, antes, despues, supervisor_id)
                           VALUES (%s, %s, %s::jsonb, %s::jsonb, %s)""",
                        (petitorio_id, accion, json.dumps(antes, ensure_ascii=False) if antes else None,
                         json.dumps(despues, ensure_ascii=False), supervisor_id))
        conn.commit()
    except Exception as e:  # noqa: BLE001 — el cambio ya se hizo; que al menos quede en el log
        if conn:
            conn.rollback()
        logger.warning("Petitorio: no se pudo auditar (%s) %s %s: antes=%s después=%s (supervisor %s)",
                       e, accion, petitorio_id, antes, despues, supervisor_id)
    finally:
        if conn:
            release_connection(conn)


def guardar(datos: dict, supervisor_id: int) -> dict:
    """Crea (sin id) o modifica (con id) un medicamento. Devuelve {"ok", "item", "creado"} o {"ok": False, "motivo"}."""
    limpios, error = _validar(datos)
    if error:
        return {"ok": False, "motivo": error}
    pid = (datos or {}).get("id")
    try:
        pid = int(pid) if pid not in (None, "") else None
    except (TypeError, ValueError):
        return {"ok": False, "motivo": "Medicamento no válido."}
    conn = get_connection_recetas()
    antes = None
    try:
        with conn.cursor() as cur:
            if pid is not None:
                cur.execute(f"SELECT {_COLUMNAS} FROM sofi_petitorio WHERE id = %s FOR UPDATE", (pid,))
                r = cur.fetchone()
                if r is None:
                    conn.rollback()
                    return {"ok": False, "motivo": "Ese medicamento ya no existe."}
                antes = _fila(r)
                if all(antes[k] == limpios[k] for k in limpios):
                    conn.rollback()
                    return {"ok": True, "item": antes, "creado": False, "sin_cambios": True}
            # Al editar sin cambiar la descripción no se revisa duplicados: así se pueden mantener las banderas de un
            # registro que ya está repetido en la tabla. Solo se bloquea si se crea o si la descripción cambia.
            cambia_descripcion = antes is None or _clave(antes["descripcion"]) != _clave(limpios["descripcion"])
            dup = _duplicado(cur, limpios["descripcion"], pid) if cambia_descripcion else None
            if dup:
                conn.rollback()
                return {"ok": False, "motivo": f"Ya existe en el petitorio: «{dup[1]}» (id {dup[0]}).", "duplicado_id": dup[0]}
            if pid is None:
                cur.execute(f"""INSERT INTO sofi_petitorio (descripcion, requiere_carta_garantia, requiere_cuida,
                                                            con_autorizacion)
                                VALUES (%s, %s, %s, %s) RETURNING {_COLUMNAS}""",
                            (limpios["descripcion"], limpios["requiere_carta_garantia"], limpios["requiere_cuida"],
                             limpios["con_autorizacion"]))
            else:
                cur.execute(f"""UPDATE sofi_petitorio
                                SET descripcion = %s, requiere_carta_garantia = %s, requiere_cuida = %s,
                                    con_autorizacion = %s, updated_at = now()
                                WHERE id = %s RETURNING {_COLUMNAS}""",
                            (limpios["descripcion"], limpios["requiere_carta_garantia"], limpios["requiere_cuida"],
                             limpios["con_autorizacion"], pid))
            item = _fila(cur.fetchone())
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Petitorio: no se pudo guardar %s", datos)
        return {"ok": False, "motivo": "No se pudo guardar. Intenta de nuevo en un momento."}
    finally:
        release_connection_recetas(conn)
    accion = "crear" if pid is None else "editar"
    logger.info("Petitorio: %s id %s por supervisor %s", accion, item["id"], supervisor_id)
    _auditar(item["id"], accion,
             {k: antes[k] for k in limpios} if antes else None, {k: item[k] for k in limpios}, supervisor_id)
    return {"ok": True, "item": item, "creado": pid is None}


def historial(petitorio_id) -> dict:
    """Últimos cambios de un medicamento (auditoria_petitorio, base webchat)."""
    try:
        petitorio_id = int(petitorio_id)
    except (TypeError, ValueError):
        return {"ok": False, "cambios": []}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""SELECT to_char(ap.creado_en AT TIME ZONE 'America/Lima', 'DD/MM/YYYY HH24:MI'), ap.accion,
                                      ap.antes, ap.despues, COALESCE(a.nombre_display, a.nombre, '¿?')
                               FROM auditoria_petitorio ap LEFT JOIN asesores a ON a.id = ap.supervisor_id
                               WHERE ap.petitorio_id = %s ORDER BY ap.creado_en DESC LIMIT 20""", (petitorio_id,))
            except Exception:  # noqa: BLE001 — falta la migración
                conn.rollback()
                return {"ok": True, "petitorio_id": petitorio_id, "cambios": [], "sin_tabla": True}
            cambios = [{"fecha": f, "accion": ac, "antes": an, "despues": de, "quien": q}
                       for f, ac, an, de, q in cur.fetchall()]
        conn.rollback()
    finally:
        release_connection(conn)
    return {"ok": True, "petitorio_id": petitorio_id, "cambios": cambios}
