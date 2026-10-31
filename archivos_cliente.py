"""archivos_cliente.py — "Archivos de la empresa": el último Excel que envió el cliente y los que le guarda el asesor.

Para que en la próxima renovación (SCTR) el archivo esté a la mano:
  - Automático: lo que el cliente envía en una conversación de SCTR (Excel, CSV, PDF o fotos) queda guardado,
    ligado al RUC y al celular VALIDADO de esa conversación.
  - Asesor: puede subir un archivo adicional (Excel, CSV o PDF) al perfil de la empresa (por RUC) desde su consola.
  - Cliente: al volver a SCTR con su RUC, Sofi le ofrece los archivos de su última solicitud ("La última vez nos
    enviaste…") y, con un toque, se los adjunta al asesor.

Seguridad: el cliente solo ve archivos de un RUC si su celular está validado y ese mismo celular ya envió un
archivo de ese RUC. Se ofrecen archivos de los últimos 13 meses. Tabla: migracion_sctr_mejoras.sql.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from db import get_connection, release_connection

logger = logging.getLogger(__name__)
LIMA_TZ = timezone(timedelta(hours=-5))

VIGENCIA_DIAS = 395                       # 13 meses
CAMPANAS = ("venta_sctr",)                # campañas en las que se guarda automáticamente el archivo del cliente
EXTENSIONES = ("xlsx", "xls", "csv", "pdf", "jpg", "jpeg", "png", "webp")   # lo que se guarda del cliente
EXTENSIONES_ASESOR = ("xlsx", "xls", "csv", "pdf")
MAX_OFRECER = 5                           # archivos de la última solicitud que se ofrecen
MAX_NOMBRE = 120

_aviso_migracion = False


def _falta_migracion(e: Exception) -> bool:
    global _aviso_migracion
    if "clientes_archivos" in str(e):
        if not _aviso_migracion:
            logger.warning("archivos_cliente: falta ejecutar migracion_sctr_mejoras.sql")
            _aviso_migracion = True
        return True
    return False


def _ruc(valor) -> str | None:
    d = re.sub(r"\D", "", str(valor or ""))
    return d if len(d) == 11 else None


def _celular(valor) -> str | None:
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("51") and len(d) == 11:
        d = d[2:]
    return d if re.fullmatch(r"9\d{8}", d) else None


def _extension(ruta: str, nombre: str | None) -> str:
    for t in (nombre, ruta):
        if t and "." in t:
            return t.rsplit(".", 1)[-1].lower()
    return ""


def limpiar_nombre(nombre, ruta: str = "") -> str:
    n = re.sub(r"[\x00-\x1f\x7f<>\"/\\|?*]", "", str(nombre or "")).strip()[:MAX_NOMBRE]
    return n or (ruta.rsplit("/", 1)[-1] if ruta else "archivo")


def _fecha(dt) -> str:
    return dt.astimezone(LIMA_TZ).strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Guardar
# ---------------------------------------------------------------------------
def registrar_desde_cliente(conversacion_id: int, ruta: str, nombre: str | None) -> bool:
    """Si es un Excel/CSV del cliente en una conversación de SCTR con RUC y celular validado, lo guarda."""
    if not ruta or not str(ruta).startswith("webchat/") or _extension(ruta, nombre) not in EXTENSIONES:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.variables_sesion, cam.codigo, u.verificado_en IS NOT NULL, c.campana_id
                   FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   LEFT JOIN campanas cam ON cam.id = c.campana_id WHERE c.id = %s""", (conversacion_id,))
            fila = cur.fetchone()
            if not fila or fila[1] not in CAMPANAS or not fila[2]:
                conn.rollback()
                return False
            variables = fila[0] or {}
            ruc, cel = _ruc(variables.get("RUC")), _celular(variables.get("CELULAR"))
            if not ruc or not cel:
                conn.rollback()
                return False
            cur.execute("""INSERT INTO clientes_archivos (ruc, celular, ruta, nombre, origen, campana_id, conversacion_id)
                           VALUES (%s, %s, %s, %s, 'cliente', %s, %s)""",
                        (ruc, cel, ruta, limpiar_nombre(nombre, ruta), fila[3], conversacion_id))
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if not _falta_migracion(e):
            logger.warning("archivos_cliente: no se pudo guardar el archivo de la conversación %s", conversacion_id,
                           exc_info=True)
        return False
    finally:
        release_connection(conn)


def registrar_desde_asesor(conversacion_id: int, ruc, ruta: str, nombre: str | None, asesor_id: int) -> dict:
    ruc = _ruc(ruc)
    if not ruc:
        return {"ok": False, "motivo": "El RUC debe tener 11 dígitos."}
    if not ruta or not str(ruta).startswith("webchat/"):
        return {"ok": False, "motivo": "Archivo no válido."}
    if _extension(ruta, nombre) not in EXTENSIONES_ASESOR:
        return {"ok": False, "motivo": "Solo se pueden guardar archivos Excel (.xlsx, .xls), CSV o PDF."}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campana_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            fila = cur.fetchone()
            cur.execute("""INSERT INTO clientes_archivos (ruc, celular, ruta, nombre, origen, campana_id, conversacion_id,
                                                          subido_por)
                           VALUES (%s, NULL, %s, %s, 'asesor', %s, %s, %s) RETURNING id""",
                        (ruc, ruta, limpiar_nombre(nombre, ruta), fila[0] if fila else None, conversacion_id, asesor_id))
            nuevo = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": nuevo}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if _falta_migracion(e):
            return {"ok": False, "motivo": "Falta ejecutar migracion_sctr_mejoras.sql en la base de datos."}
        logger.exception("archivos_cliente: no se pudo guardar el archivo del asesor")
        return {"ok": False, "motivo": "No se pudo guardar el archivo."}
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Consola del asesor
# ---------------------------------------------------------------------------
def para_asesor(conversacion_id: int, ruc=None) -> dict:
    """RUC de la conversación (o el que escriba el asesor) y los archivos de esa empresa (13 meses)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT variables_sesion FROM conversaciones WHERE id = %s", (conversacion_id,))
            fila = cur.fetchone()
            variables = (fila[0] if fila else None) or {}
            ruc_conv = _ruc(variables.get("RUC"))
            ruc = _ruc(ruc) or ruc_conv
            razon = ((variables.get("CONSULTA_RUC") or {}).get("response") or {}).get("razon_social") \
                if isinstance(variables.get("CONSULTA_RUC"), dict) else None
            if not ruc:
                conn.rollback()
                return {"disponible": True, "ruc": None, "razon_social": None, "archivos": []}
            try:
                cur.execute(
                    """SELECT a.id, a.ruta, a.nombre, a.origen, a.celular, a.creado_en, a.conversacion_id, s.nombre_display
                       FROM clientes_archivos a LEFT JOIN asesores s ON s.id = a.subido_por
                       WHERE a.ruc = %s AND a.creado_en > %s ORDER BY a.creado_en DESC LIMIT 30""",
                    (ruc, datetime.now(LIMA_TZ) - timedelta(days=VIGENCIA_DIAS)))
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if _falta_migracion(e):
                    return {"disponible": False}
                raise
            archivos = [{"id": i, "ruta": r, "nombre": n, "origen": o, "celular": c,
                         "fecha": f.astimezone(LIMA_TZ).strftime("%d/%m/%Y %H:%M"), "conversacion_id": cid,
                         "subido_por": s}
                        for i, r, n, o, c, f, cid, s in cur.fetchall()]
        conn.rollback()
        return {"disponible": True, "ruc": ruc, "ruc_de_la_conversacion": ruc_conv, "razon_social": razon,
                "archivos": archivos}
    finally:
        release_connection(conn)


# ---------------------------------------------------------------------------
# Bot (cliente): ofrecer el archivo anterior y reutilizarlo
# ---------------------------------------------------------------------------
def _ultimos_para_cliente(cur, conversacion_id: int, variables: dict) -> list:
    """Archivos de la última solicitud de este cliente para este RUC (y el último que le guardó el asesor, si es
    más nuevo): [(id, ruta, nombre, origen, creado_en), ...] del más nuevo al más antiguo. [] si no corresponde."""
    ruc, cel = _ruc(variables.get("RUC")), _celular(variables.get("CELULAR"))
    if not ruc or not cel:
        return []
    cur.execute("""SELECT u.verificado_en IS NOT NULL FROM conversaciones c JOIN usuarios_webchat u ON u.id = c.usuario_id
                   WHERE c.id = %s""", (conversacion_id,))
    fila = cur.fetchone()
    if not fila or not fila[0]:
        return []
    desde = datetime.now(LIMA_TZ) - timedelta(days=VIGENCIA_DIAS)
    # Prueba de que esta persona trabaja con ese RUC: su celular validado ya envió archivos de esa empresa.
    cur.execute("""SELECT conversacion_id, MAX(creado_en) FROM clientes_archivos
                   WHERE ruc = %s AND celular = %s AND origen = 'cliente' AND creado_en > %s
                     AND conversacion_id <> %s
                   GROUP BY conversacion_id ORDER BY 2 DESC LIMIT 1""", (ruc, cel, desde, conversacion_id))
    ultima = cur.fetchone()
    if not ultima:
        return []
    cur.execute("""SELECT id, ruta, nombre, origen, creado_en FROM clientes_archivos
                   WHERE ruc = %s AND celular = %s AND origen = 'cliente' AND conversacion_id = %s
                   ORDER BY creado_en DESC LIMIT %s""", (ruc, cel, ultima[0], MAX_OFRECER))
    archivos = cur.fetchall()
    cur.execute("""SELECT id, ruta, nombre, origen, creado_en FROM clientes_archivos
                   WHERE ruc = %s AND origen = 'asesor' AND creado_en > %s ORDER BY creado_en DESC LIMIT 1""",
                (ruc, desde))
    del_asesor = cur.fetchone()
    if del_asesor:
        archivos = [del_asesor] + list(archivos[:MAX_OFRECER - 1])
    # Si el mismo archivo se envió dos veces, se ofrece una sola.
    vistos, unicos = set(), []
    for a in archivos:
        if a[1] not in vistos:
            vistos.add(a[1])
            unicos.append(a)
    return unicos


def accion_archivo_previo(cur, conversacion_id: int, variables: dict) -> dict:
    """Para el nodo accion_api "sctr_archivo_previo". response.hay = "S" | "N"; response.texto para el mensaje."""
    try:
        cur.execute("SAVEPOINT sp_archivo_previo")
        archivos = _ultimos_para_cliente(cur, conversacion_id, variables)
        cur.execute("RELEASE SAVEPOINT sp_archivo_previo")
    except Exception as e:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT sp_archivo_previo")
        if not _falta_migracion(e):
            logger.warning("archivos_cliente: no se pudo buscar el archivo anterior", exc_info=True)
        archivos = []
    if not archivos:
        return {"status": 404, "response": {"hay": "N"}}
    lineas = []
    for _id, _ruta, nombre, origen, creado in archivos:
        lineas.append(f"📎 {nombre} · {_fecha(creado)}" + (" _(preparado por tu asesor)_" if origen == "asesor" else ""))
    texto = ("📁 La última vez nos enviaste " + ("este archivo:" if len(archivos) == 1 else "estos archivos:")
             + "\n" + "\n".join(lineas))
    return {"status": 200, "response": {"hay": "S", "cantidad": len(archivos), "texto": texto}}


def accion_reusar(cur, conversacion_id: int, variables: dict, ahora: datetime) -> tuple[dict, list]:
    """Para "sctr_reusar_archivo": adjunta a ESTA conversación los archivos ofrecidos (vuelve a validar el acceso).
    Devuelve (resultado, eventos_para_el_widget)."""
    try:
        cur.execute("SAVEPOINT sp_reusar")
        archivos = _ultimos_para_cliente(cur, conversacion_id, variables)
        eventos = []
        for aid, ruta, nombre, _origen, _creado in reversed(archivos):
            cur.execute("""INSERT INTO mensajes (conversacion_id, emisor_tipo, tipo_contenido, contenido, metadata, enviado_en)
                           VALUES (%s, 'usuario', 'archivo', %s, %s, %s)""",
                        (conversacion_id, ruta,
                         '{"reutilizado": true, "archivo_cliente_id": %d, "nombre": %s}' % (aid, _json_str(nombre)), ahora))
            eventos.append({"type": "archivo_reutilizado", "payload": {"ruta": ruta, "nombre": nombre}})
        cur.execute("RELEASE SAVEPOINT sp_reusar")
        if not archivos:
            return {"status": 404, "response": {"ok": "N"}}, []
        return {"status": 200, "response": {"ok": "S", "cantidad": len(archivos)}}, eventos
    except Exception:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT sp_reusar")
        logger.warning("archivos_cliente: no se pudo reutilizar el archivo", exc_info=True)
        return {"status": 500, "response": {"ok": "N"}}, []


def _json_str(s: str) -> str:
    import json
    return json.dumps(s or "", ensure_ascii=False)
