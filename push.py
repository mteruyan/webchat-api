"""Avisos push (Web Push) para la PWA y, opcionalmente, el widget del sitio.

Implementa el estándar directamente con `cryptography` + `pyjwt` (ya están en
requirements.txt), sin librerías extra:
  - Cifrado del mensaje: RFC 8291 (aes128gcm) sobre RFC 8188.
  - Identificación del servidor: VAPID (RFC 8292), JWT ES256.

Una sola tabla, `app_dispositivos`, guarda tanto la instalación del app como la
suscripción a avisos (ver migracion_push_avisos.sql).

Privacidad: el texto que viaja en la notificación es SIEMPRE genérico (se ve en
la pantalla de bloqueo). Nunca datos de salud ni contenido del chat.
"""
import base64
import json
import logging
import os
import random
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jwt
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from db import get_connection, release_connection

logger = logging.getLogger("webchat.push")
LIMA_TZ = ZoneInfo("America/Lima")

# No repetir aviso al mismo dispositivo antes de este tiempo (un asesor que
# manda 5 mensajes seguidos genera UN aviso, no cinco).
SEGUNDOS_ENTRE_AVISOS = 90
# Suscripción que falla tantas veces seguidas (sin ser 404/410) se desactiva.
MAX_FALLOS = 5
# Un dispositivo sin abrir el app en tantos días ya no cuenta como "activo".
DIAS_ACTIVO = 30

# Textos de los avisos. Cada tipo tiene VARIAS versiones y se elige una al azar, para que no
# lleguen siempre idénticos. Marcadores:
#   {hola}     "Buenos días, Miguel" (o solo "Buenos días" si no hay nombre)
#   {asesor}   "Vanessa" o "tu asesor"   |   {Asesor}  igual pero con mayúscula inicial
#   {n} {total} número de entrega y total (recordatorio de receta)
# Privacidad: solo nombre de pila, nombre del asesor y número de entrega. NUNCA el contenido del
# chat, la campaña ni nada de salud (la notificación se ve en la pantalla de bloqueo).
# Poner PUSH_PERSONALIZAR=no en el servicio quita también los nombres.
PLANTILLAS = {
    "respuesta": [
        ("{Asesor} te respondió", "{hola}. Tu consulta ya tiene respuesta, toca para verla."),
        ("Novedad de {asesor}", "Hay un mensaje nuevo en tu conversación con Sanitas."),
        ("{Asesor} de Sanitas te escribió", "{hola}. Entra al chat y continúa con tu trámite."),
        ("Tienes una respuesta de Sanitas", "{Asesor} ya revisó tu consulta. Toca para leerla."),
    ],
    "archivo": [
        ("{Asesor} te envió un archivo", "{hola}. Ábrelo en el chat para revisarlo."),
        ("Revisa lo que te compartió {asesor}", "Hay un documento o imagen nuevo en tu conversación."),
    ],
    "entrega": [
        ("Tu entrega {n} de {total} ya está disponible", "{hola}. Escríbenos y coordina el día y la dirección."),
        ("Es momento de coordinar tu entrega {n} de {total}", "Toca aquí y Sofi te ayuda a programarla en minutos."),
        ("Coordina tu entrega {n} de {total}", "{hola}. Quedó habilitada: resuélvela hoy desde el chat."),
    ],
    "entrega_ultima": [
        ("Última entrega de tu ciclo ({n} de {total})", "{hola}. Coordínala hoy con Sofi desde el chat."),
        ("Tu entrega final ya se puede coordinar", "Es la entrega {n} de {total}. Escríbenos cuando quieras programarla."),
    ],
    "prueba": [
        ("Avisos de Sanitas activados", "{hola}. Así te avisaremos cuando tengas una novedad."),
        ("Todo listo con tus avisos", "Te llegarán aquí cuando tu asesor te responda."),
    ],
}


def _saludo() -> str:
    h = datetime.now(LIMA_TZ).hour
    return "Buenos días" if h < 12 else ("Buenas tardes" if h < 19 else "Buenas noches")


def _nombre_pila(nombre: str | None) -> str | None:
    if not nombre or os.environ.get("PUSH_PERSONALIZAR", "si").strip().lower() == "no":
        return None
    p = nombre.strip().split()[0] if nombre.strip() else ""
    p = "".join(ch for ch in p if ch.isalpha() or ch in "'-")[:20]
    return p.capitalize() if p else None


def armar_texto(tipo: str, ctx: dict | None = None) -> tuple[str, str]:
    """(titulo, cuerpo) de un aviso. `ctx`: nombre (cliente), asesor, n, total."""
    ctx = ctx or {}
    if tipo == "entrega" and ctx.get("n") and ctx.get("n") == ctx.get("total"):
        tipo = "entrega_ultima"
    titulo, cuerpo = random.choice(PLANTILLAS.get(tipo) or PLANTILLAS["respuesta"])
    nombre = _nombre_pila(ctx.get("nombre"))
    asesor = _nombre_pila(ctx.get("asesor"))
    datos = {
        "hola": f"{_saludo()}, {nombre}" if nombre else _saludo(),
        "asesor": asesor or "tu asesor",
        "Asesor": asesor or "Tu asesor",
        "n": ctx.get("n") or "", "total": ctx.get("total") or "",
    }
    return titulo.format(**datos), cuerpo.format(**datos)


class PushNoConfigurado(Exception):
    pass


# ---------------------------------------------------------------- utilidades
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _clave_privada() -> ec.EllipticCurvePrivateKey:
    """VAPID_PRIVATE_KEY = los 32 bytes de la clave privada en base64url
    (o un PEM). Se genera con generar_vapid.py y se guarda en Secret Manager."""
    crudo = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if not crudo:
        raise PushNoConfigurado("Falta VAPID_PRIVATE_KEY")
    if crudo.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(crudo.encode(), password=None)
    return ec.derive_private_key(int.from_bytes(_b64u_dec(crudo), "big"), ec.SECP256R1())


def clave_publica_b64u() -> str:
    """Clave pública VAPID (65 bytes sin comprimir, base64url): la usa el navegador para suscribirse."""
    pub = _clave_privada().public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64u(pub)


def generar_par() -> tuple[str, str]:
    """(privada_b64u, publica_b64u) — para generar_vapid.py."""
    k = ec.generate_private_key(ec.SECP256R1())
    priv = k.private_numbers().private_value.to_bytes(32, "big")
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64u(priv), _b64u(pub)


# ------------------------------------------------------------------- cifrado
def cifrar(payload: bytes, p256dh_b64u: str, auth_b64u: str,
           _efimera: ec.EllipticCurvePrivateKey | None = None, _sal: bytes | None = None) -> bytes:
    """Cifra `payload` para un navegador (RFC 8291, contenido aes128gcm).
    Los parámetros con guion bajo existen solo para probar con el vector oficial."""
    ua_pub = _b64u_dec(p256dh_b64u)
    auth = _b64u_dec(auth_b64u)
    efimera = _efimera or ec.generate_private_key(ec.SECP256R1())
    as_pub = efimera.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    secreto = efimera.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + ua_pub + as_pub).derive(secreto)
    sal = _sal or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=sal, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=sal, info=b"Content-Encoding: nonce\x00").derive(ikm)
    # Un solo registro: datos + delimitador 0x02 (último registro), sin relleno.
    cifrado = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    cabecera = sal + struct.pack(">IB", 4096, len(as_pub)) + as_pub
    return cabecera + cifrado


def _cabecera_vapid(endpoint: str) -> str:
    u = urlparse(endpoint)
    claims = {
        "aud": f"{u.scheme}://{u.netloc}",
        "exp": int(time.time()) + 12 * 3600,
        "sub": os.environ.get("VAPID_SUBJECT") or "https://www.sanitasperu.com",
    }
    token = jwt.encode(claims, _clave_privada(), algorithm="ES256")
    return f"vapid t={token}, k={clave_publica_b64u()}"


def _validar_endpoint(endpoint: str) -> None:
    """Solo https y nunca hacia direcciones internas (evita que nos usen para pegarle a la red interna)."""
    u = urlparse(endpoint or "")
    if u.scheme != "https" or not u.hostname or len(endpoint) > 1000:
        raise ValueError("endpoint inválido")
    host = u.hostname.lower()
    if host in ("localhost",) or host.endswith(".internal") or host.endswith(".local") \
            or host.replace(".", "").isdigit() or ":" in host:
        raise ValueError("endpoint inválido")


def enviar_a_suscripcion(endpoint: str, p256dh: str, auth: str, datos: dict, ttl: int = 3600) -> int:
    """Devuelve el código HTTP del servicio push (201 = aceptado; 404/410 = suscripción muerta)."""
    cuerpo = cifrar(json.dumps(datos, ensure_ascii=False).encode(), p256dh, auth)
    r = requests.post(endpoint, data=cuerpo, timeout=10, headers={
        "Authorization": _cabecera_vapid(endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": "high",
    })
    return r.status_code


# ---------------------------------------------------------- registro (base)
def _plataforma(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "iOS"
    if "android" in ua:
        return "Android"
    if "windows" in ua or "macintosh" in ua or "linux" in ua or "cros" in ua:
        return "Escritorio"
    return "Otro"


def usuario_id_de_sesion(cur, sesion_token: str | None):
    if not sesion_token:
        return None
    cur.execute("SELECT id FROM usuarios_webchat WHERE sesion_token = %s", (sesion_token,))
    fila = cur.fetchone()
    return fila[0] if fila else None


def registrar_visita(dispositivo_id: str, sesion_token: str | None, modo: str, permiso: str | None,
                     user_agent: str, url_abrir: str | None = None) -> None:
    """Se llama cada vez que se abre el app (o el widget con avisos habilitados).
    `modo`: 'app' (instalada, abierta como app), 'navegador' (pestaña) o 'widget' (sitio web)."""
    if modo not in ("app", "navegador", "widget"):
        modo = "navegador"
    if permiso not in ("granted", "denied", "default", "no_soportado"):
        permiso = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            usuario_id = usuario_id_de_sesion(cur, sesion_token)
            cur.execute(
                """INSERT INTO app_dispositivos (dispositivo_id, usuario_id, plataforma, modo, instalada, permiso, url_abrir)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (dispositivo_id) DO UPDATE SET
                     usuario_id = COALESCE(EXCLUDED.usuario_id, app_dispositivos.usuario_id),
                     plataforma = EXCLUDED.plataforma,
                     modo = EXCLUDED.modo,
                     instalada = app_dispositivos.instalada OR EXCLUDED.instalada,
                     instalada_en = CASE WHEN EXCLUDED.instalada AND NOT app_dispositivos.instalada THEN NOW() ELSE app_dispositivos.instalada_en END,
                     permiso = COALESCE(EXCLUDED.permiso, app_dispositivos.permiso),
                     url_abrir = COALESCE(EXCLUDED.url_abrir, app_dispositivos.url_abrir),
                     ultima_vez = NOW(),
                     visitas = app_dispositivos.visitas + 1""",
                (dispositivo_id, usuario_id, _plataforma(user_agent), modo, modo == "app", permiso,
                 (url_abrir or None) and url_abrir[:500]))
            # Si el navegador ya no tiene permiso, la suscripción guardada no sirve.
            if permiso in ("denied", "default"):
                cur.execute("""UPDATE app_dispositivos SET push_activo = FALSE,
                                 push_revocado_en = COALESCE(push_revocado_en, NOW())
                               WHERE dispositivo_id = %s AND push_activo""", (dispositivo_id,))
        conn.commit()
    finally:
        release_connection(conn)


def guardar_suscripcion(dispositivo_id: str, sesion_token: str | None, suscripcion: dict, modo: str,
                        user_agent: str, url_abrir: str | None = None) -> None:
    endpoint = (suscripcion or {}).get("endpoint")
    claves = (suscripcion or {}).get("keys") or {}
    p256dh, auth = claves.get("p256dh"), claves.get("auth")
    if not endpoint or not p256dh or not auth:
        raise ValueError("suscripción incompleta")
    _validar_endpoint(endpoint)
    registrar_visita(dispositivo_id, sesion_token, modo, "granted", user_agent, url_abrir)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Un mismo endpoint no puede estar en dos dispositivos (ej. se limpió el almacenamiento).
            cur.execute("UPDATE app_dispositivos SET push_activo = FALSE, push_endpoint = NULL "
                        "WHERE push_endpoint = %s AND dispositivo_id <> %s", (endpoint, dispositivo_id))
            cur.execute(
                """UPDATE app_dispositivos SET push_endpoint = %s, push_p256dh = %s, push_auth = %s,
                          push_activo = TRUE, push_aceptado_en = COALESCE(push_aceptado_en, NOW()),
                          push_revocado_en = NULL, fallos = 0, ultimo_error = NULL
                   WHERE dispositivo_id = %s""", (endpoint, p256dh, auth, dispositivo_id))
        conn.commit()
    finally:
        release_connection(conn)


def cancelar_suscripcion(dispositivo_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE app_dispositivos SET push_activo = FALSE, push_revocado_en = NOW()
                           WHERE dispositivo_id = %s AND push_activo""", (dispositivo_id,))
        conn.commit()
    finally:
        release_connection(conn)


# ------------------------------------------------------------------- envío
def _dispositivos_de_usuario(cur, usuario_id: int):
    cur.execute(
        """SELECT dispositivo_id, push_endpoint, push_p256dh, push_auth, COALESCE(url_abrir, '/app?ac=1'), ultimo_aviso_en
           FROM app_dispositivos
           WHERE usuario_id = %s AND push_activo AND push_endpoint IS NOT NULL""", (usuario_id,))
    return cur.fetchall()


def avisar_usuario(usuario_id: int, tipo: str = "respuesta", conversacion_id: int | None = None,
                   respetar_pausa: bool = True, ctx: dict | None = None) -> dict:
    """Manda el aviso a todos los dispositivos con avisos activos de ese usuario.
    Síncrono (usa red): llamarlo con asyncio.to_thread. Nunca lanza por un fallo de envío."""
    resumen = {"enviados": 0, "omitidos": 0, "fallidos": 0, "desactivados": 0}
    if not usuario_id:
        return resumen
    ctx = dict(ctx or {})
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            dispositivos = _dispositivos_de_usuario(cur, usuario_id)
            if not ctx.get("nombre"):
                cur.execute("SELECT nombre FROM usuarios_webchat WHERE id = %s", (usuario_id,))
                f = cur.fetchone()
                ctx["nombre"] = f[0] if f else None
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        if "app_dispositivos" in str(e):      # migración aún no ejecutada: el chat sigue normal
            logger.warning("push: falta ejecutar migracion_push_avisos.sql")
        else:
            logger.exception("push: no se pudo leer dispositivos")
        return resumen
    finally:
        release_connection(conn)

    ahora = datetime.now(LIMA_TZ)
    for disp_id, endpoint, p256dh, auth, url, ultimo in dispositivos:
        if respetar_pausa and ultimo and (ahora - ultimo.astimezone(LIMA_TZ)).total_seconds() < SEGUNDOS_ENTRE_AVISOS:
            resumen["omitidos"] += 1
            continue
        titulo, cuerpo = armar_texto(tipo, ctx)       # cada envío elige una versión distinta
        tag = f"sanitas-{tipo.split('_')[0]}" + (f"-{conversacion_id}" if conversacion_id else "")
        datos = {"titulo": titulo, "cuerpo": cuerpo, "url": url, "tag": tag}
        try:
            codigo = enviar_a_suscripcion(endpoint, p256dh, auth, datos)
            error = None if codigo in (200, 201, 202) else f"HTTP {codigo}"
        except PushNoConfigurado:
            raise
        except Exception as e:  # noqa: BLE001
            codigo, error = 0, f"{type(e).__name__}: {e}"[:200]
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if error is None:
                    cur.execute("""UPDATE app_dispositivos SET avisos_enviados = avisos_enviados + 1,
                                     ultimo_aviso_en = NOW(), fallos = 0, ultimo_error = NULL WHERE dispositivo_id = %s""", (disp_id,))
                    resumen["enviados"] += 1
                elif codigo in (404, 410):
                    # El usuario quitó el permiso o desinstaló: suscripción muerta.
                    cur.execute("""UPDATE app_dispositivos SET push_activo = FALSE, push_revocado_en = NOW(),
                                     ultimo_error = %s WHERE dispositivo_id = %s""", (error, disp_id))
                    resumen["desactivados"] += 1
                else:
                    cur.execute("""UPDATE app_dispositivos SET fallos = fallos + 1, ultimo_error = %s,
                                     push_activo = (fallos + 1) < %s WHERE dispositivo_id = %s""", (error, MAX_FALLOS, disp_id))
                    resumen["fallidos"] += 1
            conn.commit()
        finally:
            release_connection(conn)
    return resumen


def avisar_dispositivo(dispositivo_id: str, tipo: str = "prueba") -> dict:
    """Aviso a UN dispositivo (botón de prueba). Devuelve {'ok': bool, 'detalle': str}."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT d.push_endpoint, d.push_p256dh, d.push_auth, COALESCE(d.url_abrir, '/app?ac=1'), u.nombre
                           FROM app_dispositivos d LEFT JOIN usuarios_webchat u ON u.id = d.usuario_id
                           WHERE d.dispositivo_id = %s AND d.push_activo""", (dispositivo_id,))
            f = cur.fetchone()
    finally:
        release_connection(conn)
    if not f:
        return {"ok": False, "detalle": "Este dispositivo no tiene avisos activos"}
    titulo, cuerpo = armar_texto(tipo, {"nombre": f[4]})
    try:
        codigo = enviar_a_suscripcion(f[0], f[1], f[2], {"titulo": titulo, "cuerpo": cuerpo, "url": f[3], "tag": "sanitas-prueba"})
    except PushNoConfigurado:
        return {"ok": False, "detalle": "Avisos no configurados en el servidor"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalle": f"{type(e).__name__}"}
    return {"ok": codigo in (200, 201, 202), "detalle": f"HTTP {codigo}"}


def usuario_de_conversacion(conversacion_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usuario_id FROM conversaciones WHERE id = %s", (conversacion_id,))
            f = cur.fetchone()
            return f[0] if f else None
    finally:
        release_connection(conn)


def avisar_conversacion(conversacion_id: int, tipo: str = "respuesta", ctx: dict | None = None) -> dict:
    """Atajo: aviso al dueño de la conversación. Silencioso si push no está configurado o la tabla no existe."""
    try:
        return avisar_usuario(usuario_de_conversacion(conversacion_id), tipo, conversacion_id, ctx=ctx)
    except PushNoConfigurado:
        return {"enviados": 0, "omitidos": 0, "fallidos": 0, "desactivados": 0, "sin_configurar": True}
    except Exception:  # noqa: BLE001  (p. ej. la tabla todavía no existe: no debe romper el chat)
        logger.exception("push: fallo al avisar conversación %s", conversacion_id)
        return {"enviados": 0, "omitidos": 0, "fallidos": 0, "desactivados": 0}


# --------------------------------------------------------------- estadísticas
def estadisticas() -> dict:
    """Cifras para el dashboard. Si la tabla aún no existe devuelve {'disponible': False}."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT
                         COUNT(*) AS dispositivos,
                         COUNT(*) FILTER (WHERE instalada) AS instalaron,
                         COUNT(*) FILTER (WHERE instalada AND ultima_vez >= NOW() - make_interval(days => %s)) AS instalaron_activos,
                         COUNT(*) FILTER (WHERE push_activo) AS avisos_activos,
                         COUNT(*) FILTER (WHERE push_aceptado_en IS NOT NULL) AS avisos_aceptaron_alguna_vez,
                         COUNT(*) FILTER (WHERE permiso = 'denied') AS avisos_rechazaron,
                         COUNT(*) FILTER (WHERE permiso = 'no_soportado') AS sin_soporte,
                         COUNT(*) FILTER (WHERE instalada AND push_activo) AS instalaron_y_avisos,
                         COALESCE(SUM(avisos_enviados), 0) AS avisos_enviados,
                         COUNT(*) FILTER (WHERE creado_en >= NOW() - INTERVAL '7 days') AS nuevos_7d,
                         COUNT(*) FILTER (WHERE instalada AND instalada_en >= NOW() - INTERVAL '7 days') AS instalaron_7d,
                         COUNT(*) FILTER (WHERE push_activo AND push_aceptado_en >= NOW() - INTERVAL '7 days') AS avisos_7d
                       FROM app_dispositivos""", (DIAS_ACTIVO,))
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                if "app_dispositivos" in str(e):
                    return {"disponible": False}
                raise
            c = [d[0] for d in cur.description]
            r = dict(zip(c, cur.fetchone()))
            cur.execute(
                """SELECT plataforma, COUNT(*) AS dispositivos,
                          COUNT(*) FILTER (WHERE instalada) AS instalaron,
                          COUNT(*) FILTER (WHERE push_activo) AS avisos
                   FROM app_dispositivos GROUP BY plataforma ORDER BY dispositivos DESC""")
            r["por_plataforma"] = [{"plataforma": p, "dispositivos": int(d), "instalaron": int(i), "avisos": int(a)}
                                   for p, d, i, a in cur.fetchall()]
            cur.execute(
                """SELECT (creado_en AT TIME ZONE 'America/Lima')::date AS dia,
                          COUNT(*) FILTER (WHERE instalada) AS instalaron,
                          COUNT(*) FILTER (WHERE push_aceptado_en IS NOT NULL) AS avisos
                   FROM app_dispositivos WHERE creado_en >= NOW() - INTERVAL '30 days'
                   GROUP BY 1 ORDER BY 1""")
            r["por_dia"] = [{"dia": d.isoformat(), "instalaron": int(i), "avisos": int(a)} for d, i, a in cur.fetchall()]
        for k, v in list(r.items()):
            if isinstance(v, (int,)) or hasattr(v, "__int__") and not isinstance(v, (list, dict, str, bool)):
                r[k] = int(v)
        r["disponible"] = True
        return r
    finally:
        release_connection(conn)
