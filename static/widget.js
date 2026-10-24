/**
 * widget.js — Webchat Sanitas, embebible en cualquier sitio.
 *
 * Uso en el sitio de Sanitas:
 *   <script src="https://TU-DOMINIO/widget.js"
 *           data-api-base="https://webchat-api-249513164794.us-central1.run.app"
 *           data-logo="https://.../logo-sanitas.svg"
 *           defer></script>
 *
 * Todo el CSS vive dentro de un Shadow DOM para no chocar con los estilos
 * del sitio donde se embeba (WordPress de Sanitas u otro). No usa ninguna
 * variable/función global del sitio anfitrión.
 */
(function () {
  "use strict";

  const script = document.currentScript;
  const API_BASE = (script && script.dataset.apiBase) || "";
  const LOGO_URL = (script && script.dataset.logo) || "";
  const WS_BASE = API_BASE.replace(/^https/, "wss").replace(/^http/, "ws");

  if (!API_BASE) {
    console.error("[webchat] Falta data-api-base en el <script> del widget.");
    return;
  }

  // ---------------------------------------------------------------------
  // Tokens de marca — un solo lugar para ajustar cuando Miguel confirme
  // el hex exacto y el logo definitivo.
  // ---------------------------------------------------------------------
  const TOKENS = {
    azul: "#0A4C8C",
    azulOscuro: "#073A6B",
    verde: "#2FB88B",
    fondo: "#F7F9FB",
    texto: "#1A2B3C",
    textoSuave: "#5B6B7C",
    burbujaUsuario: "#D9FDD3",
    burbujaBot: "#EEF3F8",
    borde: "#E1E8EF",
    checkLeido: "#53BDEB",
  };

  // ---------------------------------------------------------------------
  // Shadow DOM host
  // ---------------------------------------------------------------------
  const host = document.createElement("div");
  host.id = "sanitas-webchat-host";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  // Detección de celular y factor de corrección (--k) para páginas sin meta viewport.
  let ajustarMovil = function () {
    const w = window.innerWidth, sw = (window.screen && window.screen.width) || w;
    const tactil = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    const movil = w <= 480 || (tactil && Math.min(sw, (window.screen && window.screen.height) || sw) <= 820);
    host.classList.toggle("movil", !!movil);
    let k = 1;
    if (movil && sw > 0 && w > sw * 1.2) k = Math.min(w / sw, 3.5);
    host.style.setProperty("--k", String(k));
  };
  // Tamaño del texto (solo celular): 3 niveles -- normal, +1 pt y +1.5 pt (máximo).
  const FS_NIVELES_PX = [0, 1.33, 2];
  const CLAVE_FUENTE = "sanitas_webchat_fuente";
  let fsNivel = 0;
  try { const g = parseInt(localStorage.getItem(CLAVE_FUENTE), 10); if (g >= 0 && g < FS_NIVELES_PX.length) fsNivel = g; } catch (e) { /* sin almacenamiento */ }
  function aplicarFuente() {
    host.style.setProperty("--fs", (host.classList.contains("movil") ? FS_NIVELES_PX[fsNivel] : 0) + "px");
    root.querySelectorAll(".tam-op").forEach((b) => b.classList.toggle("activo", Number(b.dataset.nivel) === fsNivel));
  }
  const _ajustarMovilBase = ajustarMovil;
  ajustarMovil = function () { _ajustarMovilBase(); aplicarFuente(); };
  ajustarMovil();
  window.addEventListener("resize", ajustarMovil);
  window.addEventListener("orientationchange", ajustarMovil);
  // Algunos sitios cambian el ancho de la página después de cargar (imágenes, menús,
  // scripts): se vuelve a medir unos segundos después y cuando la ventana visible cambia.
  [300, 1000, 2500, 5000].forEach(function (ms) { setTimeout(ajustarMovil, ms); });
  window.addEventListener("load", ajustarMovil);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", ajustarMovil);
  // Diagnóstico: abrir la página con ?sofidebug=1 muestra las medidas en pantalla.
  if (/[?&]sofidebug=1/.test(location.search)) {
    const dbg = document.createElement("div");
    dbg.style.cssText = "position:fixed;left:0;top:0;z-index:2147483647;background:#000;color:#0f0;font:11px monospace;padding:4px;white-space:pre;pointer-events:none;transform-origin:top left;";
    document.body.appendChild(dbg);
    const pintar = function () {
      const b = host.shadowRoot && host.shadowRoot.querySelector(".burbuja");
      const r = b ? b.getBoundingClientRect() : { width: 0 };
      const vv = window.visualViewport || {};
      const k = parseFloat(host.style.getPropertyValue("--k")) || 1;
      dbg.style.transform = "scale(" + k + ")";
      dbg.textContent = "inner=" + window.innerWidth + " client=" + document.documentElement.clientWidth +
        " screen=" + (window.screen && window.screen.width) + "\nvv=" + Math.round(vv.width || 0) + " scale=" + (vv.scale || "-") +
        " k=" + k.toFixed(2) + " movil=" + host.classList.contains("movil") + "\nburbuja=" + Math.round(r.width) + "px css";
    };
    setInterval(pintar, 500);
  }

  const style = document.createElement("style");
  style.textContent = `
    :host, * { box-sizing: border-box; }
    .burbuja {
      /* Imagen circular de Sofi ("Chatea con Sofi, tu asistente virtual"): el
         propio botón es un círculo del mismo tamaño que la imagen, con una
         sombra suave para despegarlo de la página, separado 20px de los bordes
         de la ventana. En celular se reduce un poco (ver @media al final). */
      position: fixed; bottom: 20px; right: 20px; width: 190px; height: 190px;
      border-radius: 50%; background: transparent; color: white; border: none;
      padding: 0; cursor: pointer; box-shadow: 0 8px 24px rgba(10,30,50,.28);
      display: flex; align-items: center; justify-content: center;
      z-index: 999999; transition: transform .15s ease, box-shadow .15s ease; overflow: visible;
    }
    .burbuja img { width: 100%; height: 100%; display: block; border-radius: 50%; object-fit: cover; }
    .burbuja.oculta { display: none; }
    .burbuja:hover { transform: scale(1.05); box-shadow: 0 12px 30px rgba(10,30,50,.34); }
    .burbuja .badge {
      position: absolute; top: 6px; right: 6px; background: #E0483E;
      color: white; border-radius: 999px; font-size: 13px; font-weight: 700;
      line-height: 1; min-width: 24px; height: 24px; box-sizing: border-box;
      display: flex; align-items: center; justify-content: center;
      padding: 0 6px; border: 2px solid white; white-space: nowrap;
      box-shadow: 0 1px 3px rgba(0,0,0,.25);
    }
    .panel {
      /* La burbuja se oculta (.burbuja.oculta) apenas el panel abre, así
         que no hace falta dejarle un hueco debajo -- el panel se abre
         directo sobre/encima de donde estaba la imagen, igual que antes de
         agrandarla a 170px, en vez de flotar más arriba con un espacio
         vacío entre los dos. */
      position: fixed; bottom: 20px; right: 24px; width: 380px; max-width: calc(100vw - 32px);
      height: 560px; max-height: calc(100vh - 140px); background: white;
      border-radius: 16px; box-shadow: 0 12px 40px rgba(10,30,50,.22);
      display: none; flex-direction: column; overflow: hidden; z-index: 999999;
      font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: ${TOKENS.texto};
    }
    .panel.abierto { display: flex; }
    /* Selector de lugar en el mapa (SERVICIO PROGRAMADO): una página aparte dentro de un iframe, que cubre el panel. */
    .mapa-overlay { position: absolute; inset: 0; z-index: 50; background: #fff; display: flex; flex-direction: column; }
    .mapa-overlay iframe { flex: 1; width: 100%; border: 0; }
    /* Aviso flotante de "mensaje nuevo": aparece cuando llega un mensaje del
       bot/asesor/supervisor mientras la persona está revisando mensajes
       viejos más arriba (no pegada al final) — así se entera sin que la
       bajemos de un jalón y le hagamos perder el lugar en el que estaba
       leyendo. */
    .nuevo-mensaje {
      display: none; position: absolute; left: 50%; bottom: 78px;
      transform: translateX(-50%) translateY(6px);
      background: ${TOKENS.azul}; color: white; border: none; border-radius: 999px;
      padding: 8px 16px; font-size: 13px; font-family: inherit; cursor: pointer;
      align-items: center; gap: 6px; box-shadow: 0 4px 14px rgba(10,30,50,.28);
      opacity: 0; transition: opacity .15s ease, transform .15s ease; z-index: 5;
    }
    .nuevo-mensaje.visible { display: flex; opacity: 1; transform: translateX(-50%) translateY(0); }
    .cabecera {
      background: linear-gradient(135deg, ${TOKENS.azul}, ${TOKENS.azulOscuro});
      color: white; padding: 16px; display: flex; align-items: center; gap: 10px;
      flex-shrink: 0;
    }
    .cabecera img { height: 26px; }
    .cabecera .info { flex: 1; min-width: 0; }
    .cabecera .titulo { font-weight: 600; font-size: 15px; }
    .cabecera .estado { font-size: 12px; opacity: .85; display: flex; align-items: center; gap: 5px; }
    .cabecera .dot { width: 7px; height: 7px; border-radius: 50%; background: ${TOKENS.verde}; }
    .cabecera .cerrar {
      background: none; border: none; color: white; font-size: 20px; cursor: pointer;
      opacity: .85; padding: 4px;
    }
    .cabecera .cerrar:hover { opacity: 1; }
    .cabecera .info-guardada {
      display: none; align-items: center; justify-content: center; flex-shrink: 0;
      width: 24px; height: 24px; padding: 0; border-radius: 50%; cursor: pointer;
      background: none; border: 1.5px solid rgba(255,255,255,.75); color: white;
      font-size: 14px; font-weight: 700; font-family: inherit; line-height: 1; opacity: .9;
    }
    .cabecera .info-guardada.visible { display: inline-flex; }
    .cabecera .info-guardada:hover, .cabecera .info-guardada.activo { opacity: 1; background: rgba(255,255,255,.2); }
    .cabecera .tam-fuente {
      display: none; align-items: center; justify-content: center; flex-shrink: 0;
      height: 24px; padding: 0 8px; border-radius: 999px; cursor: pointer;
      background: none; border: 1.5px solid rgba(255,255,255,.75); color: white;
      font-size: 12px; font-weight: 700; font-family: inherit; line-height: 1; opacity: .9; letter-spacing: .2px;
    }
    :host(.movil) .cabecera .tam-fuente { display: inline-flex; }
    .cabecera .tam-fuente:hover, .cabecera .tam-fuente.activo { opacity: 1; background: rgba(255,255,255,.2); }
    /* Maximizar: solo en laptop/tablet (en celular el panel ya es pantalla completa). */
    .cabecera .maximizar {
      display: none; align-items: center; justify-content: center; flex-shrink: 0;
      width: 28px; height: 28px; padding: 0; border: none; border-radius: 6px; cursor: pointer;
      background: none; color: white; opacity: .85;
    }
    .cabecera .maximizar svg { width: 16px; height: 16px; display: block; }
    .cabecera .maximizar .ico-restaurar { display: none; }
    :host(:not(.movil)) .cabecera .maximizar { display: inline-flex; }
    .cabecera .maximizar:hover { opacity: 1; background: rgba(255,255,255,.2); }
    :host(:not(.movil)) .panel.maximizado {
      width: min(960px, calc(100vw - 48px)); max-width: none;
      height: calc(100vh - 48px); max-height: none;
      bottom: 24px; right: 24px;
    }
    :host(:not(.movil)) .panel.maximizado .maximizar .ico-maximizar { display: none; }
    :host(:not(.movil)) .panel.maximizado .maximizar .ico-restaurar { display: block; }
    :host(:not(.movil)) .panel.maximizado .msg { max-width: 70%; }
    .tam-panel {
      display: none; position: absolute; top: 62px; right: 12px; z-index: 25; background: white;
      border-radius: 14px; padding: 10px 12px 12px; box-shadow: 0 10px 28px rgba(10,30,50,.22);
      border: 1px solid ${TOKENS.borde}; flex-direction: column; gap: 8px; align-items: center;
    }
    .tam-panel.visible { display: flex; }
    .tam-panel::before {
      content: ""; position: absolute; top: -6px; right: 26px; width: 10px; height: 10px; background: white;
      border-left: 1px solid ${TOKENS.borde}; border-top: 1px solid ${TOKENS.borde}; transform: rotate(45deg);
    }
    .tam-titulo { font-size: 11px; letter-spacing: .4px; text-transform: uppercase; color: ${TOKENS.textoSuave}; font-weight: 600; }
    .tam-opciones { display: flex; gap: 6px; align-items: center; background: ${TOKENS.burbujaBot}; padding: 4px; border-radius: 999px; }
    .tam-op {
      width: 38px; height: 34px; border: 0; border-radius: 999px; background: transparent; cursor: pointer;
      color: ${TOKENS.textoSuave}; font-family: inherit; font-weight: 600; display: inline-flex; align-items: center; justify-content: center;
      transition: background .15s, color .15s, box-shadow .15s;
    }
    .tam-op.n0 { font-size: 14px; } .tam-op.n1 { font-size: 16px; } .tam-op.n2 { font-size: 18px; }
    .tam-op.activo { background: white; color: ${TOKENS.azul}; box-shadow: 0 1px 4px rgba(10,30,50,.18); }
    .sep-hist, .sep-actual, .sep-fin { display: flex; align-items: center; gap: 10px; margin: 16px 0 10px; color: ${TOKENS.textoSuave}; font-size: 11.5px; letter-spacing: .2px; }
    .sep-hist::before, .sep-hist::after, .sep-actual::before, .sep-actual::after { content: ""; flex: 1; height: 1px; background: ${TOKENS.borde}; }
    .sep-hist span, .sep-actual span { text-align: center; line-height: 1.3; }
    .sep-actual { color: ${TOKENS.azul}; font-weight: 600; }
    .sep-actual::before, .sep-actual::after { background: ${TOKENS.azul}; opacity: .35; }
    .sep-fin { justify-content: center; font-style: italic; opacity: .85; margin-top: 4px; }
    .cargando-previo { text-align: center; color: ${TOKENS.textoSuave}; font-size: 12px; padding: 6px 0 10px; }
    .msg.previo .archivo-no-disponible { display: block; font-style: italic; color: ${TOKENS.textoSuave}; opacity: .9; }
    .cuerpo { flex: 1; overflow-y: auto; padding: 16px; background: ${TOKENS.fondo}; scroll-behavior: smooth; }
    .cuerpo.arrastrando { outline: 2px dashed ${TOKENS.azul}; outline-offset: -6px; }
    .msg {
      position: relative; max-width: 82%; margin: 6px 0; padding: 10px 13px; border-radius: 14px;
      font-size: calc(14px + var(--fs, 0px)); line-height: 1.45; white-space: pre-wrap; word-wrap: break-word;
    }
    .msg.bot { background: ${TOKENS.burbujaBot}; color: ${TOKENS.texto}; margin-right: auto; border-bottom-left-radius: 4px; }
    .msg.escribiendo { display: flex; align-items: center; gap: 5px; width: fit-content; padding: 13px 15px; }
    .msg.escribiendo .punto { width: 7px; height: 7px; border-radius: 50%; background: #8A9BB0; animation: sofi-punto 1.2s infinite ease-in-out; }
    .msg.escribiendo .punto:nth-child(2) { animation-delay: .18s; }
    .msg.escribiendo .punto:nth-child(3) { animation-delay: .36s; }
    @keyframes sofi-punto { 0%, 60%, 100% { transform: translateY(0); opacity: .45; } 30% { transform: translateY(-4px); opacity: 1; } }
    @media (prefers-reduced-motion: reduce) { .msg.escribiendo .punto { animation: none; opacity: .7; } }
    .msg.usuario { background: ${TOKENS.burbujaUsuario}; color: ${TOKENS.texto}; margin-left: auto; border-bottom-right-radius: 4px; text-align: right; }
    .msg.sistema { background: transparent; color: ${TOKENS.textoSuave}; font-size: 12px; text-align: center; margin: 10px auto; max-width: 100%; }
    .msg .hora { display: block; font-size: 10.5px; margin-top: 3px; font-weight: 400; }
    .msg .editado-tag { font-size: 10.5px; opacity: .65; font-style: italic; margin-left: 4px; }
    .msg.eliminado { font-style: italic; opacity: .65; }
    .msg-acciones {
      position: absolute; top: -10px; background: white; border: 1px solid #B8C4D0;
      border-radius: 999px; width: 22px; height: 22px; font-size: 13px; line-height: 1;
      cursor: pointer; opacity: .6; display: flex; align-items: center; justify-content: center;
      color: ${TOKENS.textoSuave}; box-shadow: 0 1px 3px rgba(10,20,30,.18);
    }
    .msg.usuario .msg-acciones { left: -8px; }
    .msg.bot .msg-acciones, .msg.asesor .msg-acciones { right: -8px; }
    .msg-acciones:hover, .msg-acciones.abierto { opacity: 1; }
    .msg-menu {
      display: none; position: absolute; top: 14px; z-index: 20; background: white;
      border-radius: 10px; box-shadow: 0 4px 16px rgba(10,30,50,.2); padding: 4px; min-width: 140px;
    }
    .msg.usuario .msg-menu { left: -8px; }
    .msg.bot .msg-menu, .msg.asesor .msg-menu { right: -8px; }
    .msg-menu.abierto { display: block; }
    .msg-menu button {
      display: block; width: 100%; text-align: left; background: none; border: none;
      padding: 8px 10px; font-size: 13px; font-family: inherit; color: ${TOKENS.texto};
      cursor: pointer; border-radius: 6px; white-space: nowrap;
    }
    .msg-menu button:hover { background: ${TOKENS.fondo}; }
    .msg-reacciones-barra { display: flex; gap: 2px; padding: 2px 2px 6px; border-bottom: 1px solid ${TOKENS.borde}; margin-bottom: 4px; }
    .msg-menu .msg-reacciones-barra button { display: inline-block; width: auto; padding: 3px 5px; font-size: 19px; border-radius: 8px; }
    .reacciones { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
    .msg.usuario .reacciones { justify-content: flex-end; }
    .reaccion-chip {
      background: white; color: ${TOKENS.texto}; border: 1px solid ${TOKENS.borde}; border-radius: 999px;
      padding: 0 7px; font-size: 12.5px; line-height: 20px; box-shadow: 0 1px 2px rgba(10,20,30,.1);
    }
    .msg-cita {
      display: block; border-left: 3px solid rgba(0,0,0,.2); background: rgba(0,0,0,.05);
      border-radius: 6px; padding: 5px 8px; margin-bottom: 6px; font-size: 12.5px;
      opacity: .85; text-align: left; white-space: normal;
    }
    .responder-banner {
      display: none; align-items: center; gap: 8px; background: ${TOKENS.fondo};
      border-top: 1px solid ${TOKENS.borde}; padding: 8px 14px; font-size: 12.5px;
    }
    .responder-banner.abierto { display: flex; }
    .responder-banner .texto-cita { flex: 1; border-left: 3px solid ${TOKENS.azul}; padding-left: 8px; opacity: .85; }
    .responder-banner button { background: none; border: none; font-size: 15px; cursor: pointer; color: ${TOKENS.textoSuave}; }
    .msg .msg-imagen { display: block; width: 100%; max-width: 260px; border-radius: 10px; margin-bottom: 6px; cursor: pointer; }
    .msg.subiendo .msg-imagen { opacity: .55; }
    .msg .msg-imagen.sin-previa { min-height: 140px; background: rgba(0,0,0,.08); }
    .msg .estado-subida { font-size: 12px; opacity: .8; }
    .msg .msg-archivo-link { display: inline-flex; align-items: center; gap: 6px; background: rgba(0,0,0,.06); border-radius: 8px; padding: 8px 12px; margin-bottom: 6px; text-decoration: none; color: inherit; font-weight: 600; font-size: 13.5px; }
    .msg.bot .msg-archivo-link { background: #EAF1F9; color: var(--azul, #1a73e8); }
    .lightbox-overlay {
      position: fixed; inset: 0; background: rgba(10,20,30,.85); z-index: 1000000;
      display: none; align-items: center; justify-content: center; padding: 24px;
    }
    .lightbox-overlay.abierto { display: flex; }
    .lightbox-overlay img { max-width: 100%; max-height: 100%; border-radius: 8px; box-shadow: 0 8px 32px rgba(0,0,0,.4); transform-origin: center center; user-select: none; -webkit-user-select: none; -webkit-user-drag: none; touch-action: none; cursor: zoom-in; }
    .lightbox-overlay img.zoom { cursor: grab; }
    .lightbox-overlay img.arrastrando { cursor: grabbing; }
    .lightbox-overlay .lb-barra { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 4px; background: rgba(20,30,45,.88); border-radius: 999px; padding: 6px 10px; box-shadow: 0 4px 16px rgba(0,0,0,.4); }
    .lightbox-overlay .lb-barra button { background: none; border: none; color: white; font-size: 20px; line-height: 1; min-width: 38px; height: 38px; border-radius: 999px; cursor: pointer; font-family: inherit; padding: 0; }
    .lightbox-overlay .lb-barra button:hover { background: rgba(255,255,255,.18); }
    .lightbox-overlay .lb-barra .lb-pct { color: white; font-size: 13px; min-width: 48px; text-align: center; }
    .lightbox-cerrar {
      position: absolute; top: 16px; right: 20px; color: white; font-size: 30px; line-height: 1;
      cursor: pointer; background: none; border: none; opacity: .85;
    }
    .lightbox-cerrar:hover { opacity: 1; }
    .msg.bot .hora { color: rgba(26,43,60,.4); }
    .msg.usuario .hora { color: rgba(26,43,60,.55); }
    .msg.usuario .check.leido { color: ${TOKENS.checkLeido}; }
    .opciones { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 10px; }
    .opciones button {
      background: white; color: ${TOKENS.azul}; border: 1.5px solid ${TOKENS.azul};
      border-radius: 999px; padding: 7px 14px; font-size: calc(13px + var(--fs, 0px)); cursor: pointer;
      transition: background .12s;
    }
    .opciones button:hover { background: ${TOKENS.burbujaBot}; }
    .no-soy-yo {
      display: flex; align-items: center; justify-content: center; gap: 6px; flex-wrap: wrap;
      padding: 5px 12px; font-size: 12px; color: ${TOKENS.textoSuave};
      background: white; border-bottom: 1px solid ${TOKENS.borde};
    }
    .no-soy-yo button {
      background: none; border: 0; padding: 0; font-size: 12px; cursor: pointer;
      color: ${TOKENS.azul}; text-decoration: underline;
    }
    /* Aviso "tu conversación queda guardada": tarjeta suave sobre el fondo del chat */
    .guardada { display: none; flex-shrink: 0; background: ${TOKENS.fondo}; padding: 10px 12px 2px; }
    .guardada.visible { display: block; }
    .guardada-card {
      position: relative; display: flex; align-items: flex-start; gap: 11px;
      background: #fff; border: 1px solid #DCE7F2; border-radius: 14px;
      padding: 11px 34px 11px 12px; box-shadow: 0 4px 16px rgba(10,76,140,.10);
      animation: guardada-entra .28s ease both;
    }
    @keyframes guardada-entra { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }
    .guardada .g-icono {
      flex-shrink: 0; width: 30px; height: 30px; border-radius: 50%;
      background: #E4F6EF; color: ${TOKENS.verde}; display: flex; align-items: center; justify-content: center;
    }
    .guardada .g-texto { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
    .guardada .g-titulo { font-size: 13px; font-weight: 600; color: ${TOKENS.texto}; line-height: 1.3; }
    .guardada .g-detalle { font-size: 12px; color: ${TOKENS.textoSuave}; line-height: 1.4; }
    .guardada .abrir-qr {
      align-self: flex-start; margin-top: 6px; background: ${TOKENS.burbujaBot}; color: ${TOKENS.azul};
      border: 0; border-radius: 999px; padding: 5px 12px; font-size: 12px; font-weight: 600;
      font-family: inherit; cursor: pointer; transition: background .15s;
    }
    .guardada .abrir-qr:hover { background: #DCE7F2; }
    .guardada .activar-avisos {
      align-self: flex-start; margin-top: 6px; background: ${TOKENS.burbujaBot}; color: ${TOKENS.azul};
      border: 0; border-radius: 999px; padding: 5px 12px; font-size: 12px; font-weight: 600;
      font-family: inherit; cursor: pointer; transition: background .15s;
    }
    .guardada .activar-avisos:hover { background: #DCE7F2; }
    .guardada .ocultar-guardada {
      position: absolute; top: 7px; right: 7px; width: 22px; height: 22px; padding: 0;
      background: none; border: 0; border-radius: 50%; color: #9AA8B6; font-size: 12px;
      cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background .15s, color .15s;
    }
    .guardada .ocultar-guardada:hover { background: ${TOKENS.burbujaBot}; color: ${TOKENS.texto}; }
    .qr-panel {
      display: none; position: absolute; inset: 0; z-index: 20; background: rgba(255,255,255,.98);
      flex-direction: column; align-items: center; justify-content: center; gap: 12px;
      padding: 24px; text-align: center; color: ${TOKENS.texto};
    }
    .qr-panel.visible { display: flex; }
    .qr-panel h3 { margin: 0; font-size: 16px; }
    .qr-panel p { margin: 0; font-size: 13px; color: ${TOKENS.textoSuave}; line-height: 1.45; max-width: 280px; }
    .qr-panel img { width: 200px; height: 200px; border: 1px solid ${TOKENS.borde}; border-radius: 12px; padding: 6px; background: #fff; }
    .qr-panel button {
      background: white; color: ${TOKENS.azul}; border: 1.5px solid ${TOKENS.azul};
      border-radius: 999px; padding: 8px 18px; font-size: 13px; cursor: pointer; font-family: inherit;
    }
    .wa-card {
      margin: 10px 0; padding: 14px; border-radius: 14px; background: white;
      border: 1.5px solid ${TOKENS.verde}; box-shadow: 0 2px 10px rgba(10,30,50,.08);
      display: flex; flex-direction: column; align-items: center; gap: 10px; text-align: center;
    }
    .wa-card h4 { margin: 0; font-size: 14px; color: ${TOKENS.texto}; }
    .wa-card p { margin: 0; font-size: calc(12.5px + var(--fs, 0px)); line-height: 1.45; color: ${TOKENS.textoSuave}; }
    .wa-card img { width: 170px; height: 170px; border: 1px solid ${TOKENS.borde}; border-radius: 12px; padding: 6px; background: #fff; }
    .wa-card .wa-abrir {
      display: inline-block; background: #25D366; color: white; text-decoration: none; font-weight: 600;
      border-radius: 999px; padding: 10px 20px; font-size: 14px;
    }
    .wa-card .wa-codigo { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; background: ${TOKENS.burbujaBot}; border-radius: 8px; padding: 4px 10px; color: ${TOKENS.texto}; }
    .wa-card .wa-estado { font-size: 12.5px; color: ${TOKENS.azul}; font-weight: 600; }
    .wa-card .wa-estado.ok { color: ${TOKENS.verde}; }
    .wa-card .wa-nuevo {
      background: white; color: ${TOKENS.azul}; border: 1.5px solid ${TOKENS.azul};
      border-radius: 999px; padding: 6px 14px; font-size: 12.5px; cursor: pointer; font-family: inherit;
    }
    .acciones-cierre { display: flex; flex-direction: column; gap: 6px; margin: 10px 0; }
    .acciones-cierre button {
      background: white; color: ${TOKENS.azul}; border: 1.5px solid ${TOKENS.azul};
      border-radius: 10px; padding: 9px 14px; font-size: 13.5px; cursor: pointer;
    }
    .acciones-cierre button.secundario { color: ${TOKENS.textoSuave}; border-color: ${TOKENS.borde}; font-size: 12.5px; }
    .formulario {
      background: white; border: 1px solid ${TOKENS.borde}; border-radius: 12px;
      padding: 14px; margin: 8px 0;
    }
    .formulario label { display: block; font-size: calc(12.5px + var(--fs, 0px)); margin-top: 10px; color: ${TOKENS.textoSuave}; font-weight: 600; }
    .formulario input, .formulario select, .formulario textarea {
      width: 100%; padding: 8px 10px; border: 1px solid ${TOKENS.borde}; border-radius: 8px;
      margin-top: 4px; font-size: calc(14px + var(--fs, 0px)); font-family: inherit; color: ${TOKENS.texto};
    }
    .formulario button[type="submit"] {
      margin-top: 14px; width: 100%; background: ${TOKENS.azul}; color: white; border: none;
      border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600; cursor: pointer;
    }
    .pie { border-top: 1px solid ${TOKENS.borde}; padding: 10px; display: flex; align-items: flex-end; gap: 8px; flex-shrink: 0; background: white; position: relative; }
    .pie .emoji-btn { background: ${TOKENS.burbujaBot}; color: ${TOKENS.azul}; font-size: 19px; }
    .emoji-panel {
      display: none; position: absolute; bottom: 58px; left: 10px; right: 10px; max-height: 210px; overflow-y: auto;
      background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(10,30,50,.2); padding: 8px; z-index: 12;
      grid-template-columns: repeat(8, 1fr); gap: 2px;
    }
    .emoji-panel.abierto { display: grid; }
    .pie .emoji-panel button {
      background: none; border-radius: 6px; width: auto; height: auto; padding: 4px 0; font-size: 21px; line-height: 1.2;
    }
    .pie .emoji-panel button:hover { background: ${TOKENS.fondo}; }
    .pie.grabando .emoji-btn { display: none; }
    .pie textarea.texto-input {
      flex: 1; border: 1px solid ${TOKENS.borde}; border-radius: 20px; padding: 10px 16px;
      font-size: calc(14px + var(--fs, 0px)); font-family: inherit; outline: none;
      resize: none; overflow-y: hidden; line-height: 1.4; min-height: 40px; max-height: 120px;
      height: 40px; display: block;
    }
    .pie textarea.texto-input:focus { border-color: ${TOKENS.azul}; }
    .pie button {
      background: ${TOKENS.azul}; color: white; border: none; border-radius: 50%;
      width: 40px; height: 40px; flex-shrink: 0; cursor: pointer; font-size: 17px;
      display: flex; align-items: center; justify-content: center;
    }
    .pie button:disabled { background: #B7C4D1; cursor: not-allowed; }
    .pie .adjuntar { background: ${TOKENS.burbujaBot}; color: ${TOKENS.azul}; }
    .pie .enviar.grabando { background: #E0483E; }
    .pie .texto-input, .pie .grabacion-indicador { flex: 1; min-width: 0; }
    .grabacion-indicador {
      display: none; align-items: center; gap: 8px; border: 1px solid ${TOKENS.borde};
      border-radius: 999px; padding: 0 6px 0 4px; height: 40px; background: ${TOKENS.fondo};
    }
    .pie.grabando .texto-input, .pie.grabando .adjuntar-wrap { display: none; }
    .pie.grabando .grabacion-indicador { display: flex; }
    .grabacion-cancelar {
      width: 26px; height: 26px; border-radius: 50%; background: white; color: ${TOKENS.textoSuave};
      border: 1px solid ${TOKENS.borde}; font-size: 13px; flex-shrink: 0;
    }
    .grabacion-punto {
      width: 9px; height: 9px; border-radius: 50%; background: #E0483E; flex-shrink: 0;
      animation: sanitasPuntoGrabando 1.1s ease-in-out infinite;
    }
    @keyframes sanitasPuntoGrabando { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
    .grabacion-tiempo { font-size: 13px; font-variant-numeric: tabular-nums; color: ${TOKENS.texto}; flex-shrink: 0; }
    .grabacion-ayuda { font-size: 12.5px; color: ${TOKENS.textoSuave}; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .adjuntar-wrap { position: relative; flex-shrink: 0; }
    .adjuntar-menu {
      display: none; position: absolute; bottom: 50px; left: 0;
      background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(10,30,50,.18);
      padding: 6px; min-width: 160px; z-index: 10;
    }
    .adjuntar-menu.abierto { display: block; }
    .adjuntar-menu button {
      display: flex; align-items: center; justify-content: flex-start; gap: 10px; width: 100%; background: none;
      border: none; color: ${TOKENS.texto}; font-size: 14px; padding: 10px 12px;
      border-radius: 8px; cursor: pointer; text-align: left; font-family: inherit;
    }
    .adjuntar-menu button:hover { background: ${TOKENS.fondo}; }
    /* Cámara a pantalla completa, al estilo WhatsApp: la vista ocupa toda la pantalla y se ve COMPLETA
       (object-fit: contain = exactamente lo que saldrá en la foto, sin recortes). */
    .camara-overlay {
      position: fixed; inset: 0; background: #000; z-index: 1000000; overflow: hidden;
      font-family: inherit; color: white; touch-action: none; user-select: none; -webkit-user-select: none;
    }
    .camara-overlay .cam-vista { position: absolute; inset: 0; }
    .camara-overlay .cam-vista video, .camara-overlay .cam-vista .cam-revision {
      position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000;
    }
    .camara-overlay.espejo .cam-vista video { transform: scaleX(-1); }
    .camara-overlay .cam-destello { position: absolute; inset: 0; background: white; opacity: 0; pointer-events: none; transition: opacity .25s ease-out; }
    .camara-overlay .cam-destello.activo { opacity: .85; transition: none; }
    .camara-overlay .cam-arriba {
      position: absolute; top: 0; left: 0; right: 0; display: flex; justify-content: space-between; align-items: center;
      padding: max(14px, env(safe-area-inset-top)) 16px 28px; background: linear-gradient(rgba(0,0,0,.55), transparent);
    }
    .camara-overlay .cam-abajo {
      position: absolute; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-around; align-items: center;
      padding: 34px 20px max(22px, env(safe-area-inset-bottom)); background: linear-gradient(transparent, rgba(0,0,0,.6));
    }
    .camara-overlay .cam-btn {
      width: 48px; height: 48px; border-radius: 50%; border: none; background: rgba(0,0,0,.4); color: white;
      font-size: 22px; line-height: 1; cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
      font-family: inherit; padding: 0;
    }
    .camara-overlay .cam-btn:hover { background: rgba(255,255,255,.2); }
    .camara-overlay .cam-btn.encendido { background: #FFD54F; color: #000; }
    .camara-overlay .cam-btn.oculto { visibility: hidden; }
    .camara-overlay .cam-disparo {
      width: 76px; height: 76px; border-radius: 50%; border: 5px solid white; background: rgba(255,255,255,.25);
      cursor: pointer; padding: 0; position: relative; transition: transform .1s;
    }
    .camara-overlay .cam-disparo::after { content: ""; position: absolute; inset: 5px; border-radius: 50%; background: white; }
    .camara-overlay .cam-disparo:active { transform: scale(.92); }
    .camara-overlay .cam-disparo:disabled { opacity: .5; cursor: default; }
    .camara-overlay .cam-zoom {
      position: absolute; left: 50%; bottom: calc(max(22px, env(safe-area-inset-bottom)) + 108px); transform: translateX(-50%);
      background: rgba(0,0,0,.5); border-radius: 999px; padding: 5px 12px; font-size: 13px; font-weight: 600; display: none;
    }
    .camara-overlay .cam-zoom.visible { display: block; }
    .camara-overlay .cam-revision-botones { justify-content: space-between; gap: 12px; }
    .camara-overlay .cam-revision-botones button {
      flex: 1; max-width: 200px; padding: 14px; border-radius: 999px; border: none; font-size: 15px; font-weight: 600;
      cursor: pointer; font-family: inherit;
    }
    .camara-overlay .cam-repetir { background: rgba(255,255,255,.18); color: white; }
    .camara-overlay .cam-enviar { background: ${TOKENS.verde}; color: white; }
    .camara-overlay .cam-oculto { display: none !important; }
    /* Celular: la clase "movil" y la variable --k las pone el JS (ver ajustarMovil).
       --k compensa páginas sin <meta viewport>, donde el navegador móvil dibuja la
       página a ~980px y encoge todo (burbuja y panel se ven diminutos). */
    :host(.movil) .panel {
      bottom: 0; right: 0; width: calc(100vw / var(--k, 1)); max-width: none;
      height: calc(100vh / var(--k, 1)); height: calc(100dvh / var(--k, 1));
      max-height: none; border-radius: 0;
      transform: scale(var(--k, 1)); transform-origin: bottom right;
    }
    :host(.movil) .camara-overlay {
      inset: auto; top: 0; left: 0; width: calc(100vw / var(--k, 1));
      height: calc(100vh / var(--k, 1)); height: calc(100dvh / var(--k, 1));
      transform: scale(var(--k, 1)); transform-origin: top left;
    }
    :host(.movil) .cabecera { padding-top: max(16px, env(safe-area-inset-top)); }
    :host(.movil) .pie { padding-bottom: max(10px, env(safe-area-inset-bottom)); }
    :host(.movil) .burbuja {
      width: min(calc(170px * var(--k, 1)), 46vw); height: min(calc(170px * var(--k, 1)), 46vw);
      bottom: calc(12px * var(--k, 1)); right: calc(12px * var(--k, 1));
    }
  `;
  root.appendChild(style);

  root.innerHTML += `
    <button class="burbuja" aria-label="Abrir chat"><img src="${API_BASE}/static/imagenes/sofiwebchat.png" alt="Sofi" /><span class="badge" style="display:none;"></span></button>
    <div class="panel">
      <div class="cabecera">
        ${LOGO_URL ? `<img src="${LOGO_URL}" alt="Sanitas" />` : ""}
        <div class="info">
          <div class="titulo">Sofi, tu asistente virtual</div>
          <div class="estado"><span class="dot"></span><span class="estado-texto">Conectando...</span></div>
        </div>
        <button class="maximizar" type="button" aria-label="Maximizar ventana" title="Maximizar ventana" aria-pressed="false">
          <svg class="ico-maximizar" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2H14v4.5M6.5 14H2V9.5M14 2L9 7M2 14l5-5"/></svg>
          <svg class="ico-restaurar" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 6.5H9.5V2M2 9.5h4.5V14M9.5 6.5L14 2M6.5 9.5L2 14"/></svg>
        </button>
        <button class="tam-fuente" type="button" aria-label="Tamaño del texto" title="Tamaño del texto" aria-haspopup="true" aria-expanded="false">Aa</button>
        <button class="info-guardada" type="button" aria-label="¿Se pierde mi conversación?" title="¿Se pierde mi conversación?">i</button>
        <button class="cerrar" aria-label="Cerrar chat">✕</button>
      </div>
      <div class="tam-panel" role="group" aria-label="Tamaño del texto">
        <span class="tam-titulo">Tamaño del texto</span>
        <div class="tam-opciones">
          <button type="button" data-nivel="0" class="tam-op n0" aria-label="Normal">A</button>
          <button type="button" data-nivel="1" class="tam-op n1" aria-label="Grande">A</button>
          <button type="button" data-nivel="2" class="tam-op n2" aria-label="Más grande">A</button>
        </div>
      </div>
      <div class="guardada">
        <div class="guardada-card">
          <span class="g-icono" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19a4.5 4.5 0 1 0-1.4-8.78A6 6 0 1 0 6 18.5"/><path d="m9 14.5 2.2 2.2L15.5 12"/></svg>
          </span>
          <div class="g-texto">
            <span class="g-titulo">Tu conversación queda guardada</span>
            <span class="g-detalle">Aunque cierres esta ventana, podrás retomarla cuando quieras.</span>
            <button type="button" class="abrir-qr" style="display:none;">📱 Seguir en mi celular</button>
            <button type="button" class="activar-avisos" style="display:none;">🔔 Avisarme cuando me respondan</button>
          </div>
          <button type="button" class="ocultar-guardada" aria-label="Ocultar aviso">✕</button>
        </div>
      </div>
      <div class="cuerpo"></div>
      <div class="qr-panel">
        <h3>📱 Continúa en tu celular</h3>
        <p class="qr-texto">Abre la cámara de tu celular y escanea este código. Tu conversación seguirá allí.</p>
        <img class="qr-img" alt="Código QR para continuar en el celular" style="display:none;" />
        <p class="qr-estado"></p>
        <button type="button" class="qr-regenerar" style="display:none;">Generar otro código</button>
        <button type="button" class="qr-cerrar">Cerrar</button>
      </div>
      <button class="nuevo-mensaje" type="button">Mensaje nuevo ⬇️</button>
      <div class="responder-banner">
        <span class="texto-cita"></span>
        <button class="cancelar-responder" type="button" aria-label="Cancelar respuesta">✕</button>
      </div>
      <div class="pie">
        <input type="file" class="archivo-input-documento" accept=".pdf,.doc,.docx,.xls,.xlsx,.csv,application/pdf" style="display:none" />
        <input type="file" class="archivo-input-fotos" accept="image/*,.heic,.heif" style="display:none" />
        <input type="file" class="archivo-input-camara" accept="image/*" capture="environment" style="display:none" />
        <div class="adjuntar-wrap">
          <div class="adjuntar-menu">
            <button type="button" data-tipo="fotos">🖼️ <span>Fotos</span></button>
            <button type="button" data-tipo="documento">📄 <span>Documento</span></button>
            <button type="button" data-tipo="camara">📷 <span>Cámara</span></button>
          </div>
          <button class="adjuntar" title="Adjuntar archivo">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
        </div>
        <button class="emoji-btn" type="button" aria-label="Emojis" title="Emojis">😊</button>
        <div class="emoji-panel"></div>
        <textarea class="texto-input" rows="1" placeholder="Escribe un mensaje..." disabled></textarea>
        <div class="grabacion-indicador">
          <button class="grabacion-cancelar" type="button" aria-label="Cancelar audio">✕</button>
          <span class="grabacion-punto"></span>
          <span class="grabacion-tiempo">0:00</span>
          <span class="grabacion-ayuda">Grabando audio...</span>
        </div>
        <button class="enviar" disabled>➤</button>
      </div>
    </div>
    <div class="lightbox-overlay">
      <button class="lightbox-cerrar" aria-label="Cerrar">✕</button>
      <img src="" alt="" />
      <div class="lb-barra">
        <button type="button" class="lb-menos" title="Alejar" aria-label="Alejar">&minus;</button>
        <span class="lb-pct">100%</span>
        <button type="button" class="lb-mas" title="Acercar" aria-label="Acercar">+</button>
        <button type="button" class="lb-reset" title="Tamaño original" aria-label="Restablecer zoom" style="font-size:13px;">100%</button>
      </div>
    </div>
  `;

  const $burbuja = root.querySelector(".burbuja");
  const $badge = root.querySelector(".badge");
  const $panel = root.querySelector(".panel");
  const $cerrar = root.querySelector(".cerrar");
  const $cuerpo = root.querySelector(".cuerpo");
  const $nuevoMensaje = root.querySelector(".nuevo-mensaje");
  const $responderBanner = root.querySelector(".responder-banner");
  const $responderTexto = root.querySelector(".texto-cita");
  const $cancelarResponder = root.querySelector(".cancelar-responder");
  const $estadoTexto = root.querySelector(".estado-texto");
  const $guardada = root.querySelector(".guardada");
  const $abrirQr = root.querySelector(".abrir-qr");
  const $qrPanel = root.querySelector(".qr-panel");
  const $qrImg = root.querySelector(".qr-img");
  const $qrEstado = root.querySelector(".qr-estado");
  const $qrRegenerar = root.querySelector(".qr-regenerar");
  let temporizadorQr = null;
  let movidoAOtroDispositivo = false;

  // Maximizar ventana (solo laptop/tablet: el botón y el estilo se anulan con la clase "movil").
  const CLAVE_MAX = "sofi_panel_maximizado";
  const $maxBtn = root.querySelector(".maximizar");
  function aplicarMaximizado(on, guardar) {
    $panel.classList.toggle("maximizado", on);
    $maxBtn.setAttribute("aria-pressed", String(on));
    $maxBtn.setAttribute("aria-label", on ? "Restaurar tamaño" : "Maximizar ventana");
    $maxBtn.title = on ? "Restaurar tamaño" : "Maximizar ventana";
    if (guardar) { try { localStorage.setItem(CLAVE_MAX, on ? "1" : "0"); } catch (err) { /* no crítico */ } }
    if (typeof $cuerpo !== "undefined" && $cuerpo) setTimeout(() => { try { $cuerpo.scrollTop = $cuerpo.scrollHeight; } catch (err) {} }, 0);
  }
  $maxBtn.addEventListener("click", (e) => { e.stopPropagation(); aplicarMaximizado(!$panel.classList.contains("maximizado"), true); });
  let maxGuardado = false;
  try { maxGuardado = localStorage.getItem(CLAVE_MAX) === "1"; } catch (err) { /* no crítico */ }
  aplicarMaximizado(maxGuardado, false);
  // El botón del QR solo tiene sentido en computador (en un celular no se puede escanear a sí mismo).
  const esComputador = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches
    && window.innerWidth > 700;
  if (esComputador) $abrirQr.style.display = "";
  // Aviso "tu conversación queda guardada": se muestra la primera vez que se abre el chat,
  // se oculta solo a los 30 segundos (o antes con la ✕) y se puede volver a ver con el botón "i".
  // Control del tamaño del texto (solo se ve en celular): botón "Aa" -> panel con 3 niveles.
  const $tamBtn = root.querySelector(".tam-fuente");
  const $tamPanel = root.querySelector(".tam-panel");
  function cerrarTamPanel() {
    $tamPanel.classList.remove("visible");
    $tamBtn.classList.remove("activo");
    $tamBtn.setAttribute("aria-expanded", "false");
  }
  $tamBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const abrir = !$tamPanel.classList.contains("visible");
    $tamPanel.classList.toggle("visible", abrir);
    $tamBtn.classList.toggle("activo", abrir);
    $tamBtn.setAttribute("aria-expanded", String(abrir));
  });
  root.querySelectorAll(".tam-op").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    fsNivel = Number(b.dataset.nivel) || 0;
    try { localStorage.setItem(CLAVE_FUENTE, String(fsNivel)); } catch (err) { /* no crítico */ }
    aplicarFuente();
    setTimeout(cerrarTamPanel, 250);
  }));
  root.addEventListener("click", (e) => {
    if ($tamPanel.classList.contains("visible") && !e.target.closest(".tam-panel")) cerrarTamPanel();
  });
  aplicarFuente();
  const $infoGuardada = root.querySelector(".info-guardada");
  const GUARDADA_SEGUNDOS = 30;
  let temporizadorGuardada = null;
  let guardadaMostradaAlAbrir = false;
  function mostrarGuardada() {
    $guardada.classList.add("visible");
    $infoGuardada.classList.add("activo");
    clearTimeout(temporizadorGuardada);
    temporizadorGuardada = setTimeout(ocultarGuardada, GUARDADA_SEGUNDOS * 1000);
  }
  function ocultarGuardada() {
    clearTimeout(temporizadorGuardada);
    $guardada.classList.remove("visible");
    $infoGuardada.classList.remove("activo");
  }
  root.querySelector(".ocultar-guardada").onclick = ocultarGuardada;
  $infoGuardada.onclick = () => ($guardada.classList.contains("visible") ? ocultarGuardada() : mostrarGuardada());
  // Mientras el cliente tiene el cursor encima (p. ej. para tocar "Seguir en mi celular") no se oculta solo.
  $guardada.addEventListener("mouseenter", () => clearTimeout(temporizadorGuardada));

  function pedirQr() {
    $qrImg.style.display = "none";
    $qrRegenerar.style.display = "none";
    $qrEstado.textContent = "Generando código...";
    clearTimeout(temporizadorQr);
    enviar({ type: "generar_traspaso", payload: { url: window.location.origin + window.location.pathname } });
  }
  $abrirQr.onclick = () => { $qrPanel.classList.add("visible"); pedirQr(); };
  $qrRegenerar.onclick = pedirQr;

  // -----------------------------------------------------------------------
  // Avisos push en el sitio web (OPCIONAL). Solo se activa si el <script> del widget trae
  // data-push-sw="/sanitas-push-sw.js" y ese archivo está publicado en la raíz del sitio
  // (un service worker solo puede vivir en el mismo dominio que la página). Sin ese atributo
  // el widget se comporta exactamente igual que antes.
  // -----------------------------------------------------------------------
  const PUSH_SW = (script && script.dataset.pushSw) || "";
  const $activarAvisos = root.querySelector(".activar-avisos");
  (function avisosPush() {
    if (!PUSH_SW || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
    let dispId = null;
    try { dispId = localStorage.getItem("sofi_dispositivo_id"); } catch (e) { return; }
    if (!dispId) {
      dispId = (crypto.randomUUID ? crypto.randomUUID() : "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12));
      try { localStorage.setItem("sofi_dispositivo_id", dispId); } catch (e) { return; }
    }
    const token = () => { try { return localStorage.getItem("sanitas_webchat_sesion") || null; } catch (e) { return null; } };
    const urlAbrir = () => window.location.origin + window.location.pathname + "?ac=1";
    const post = (ruta, cuerpo) => fetch(API_BASE + ruta, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cuerpo) });
    const b64aU8 = (b64) => {
      const raw = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
    };
    async function registro() {
      return (await navigator.serviceWorker.getRegistration(PUSH_SW)) || (await navigator.serviceWorker.register(PUSH_SW));
    }
    async function suscribir() {
      const reg = await registro();
      await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        const k = await (await fetch(API_BASE + "/push/clave")).json();
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64aU8(k.clave) });
      }
      const r = await post("/push/suscribir", { dispositivo_id: dispId, sesion_token: token(), modo: "widget", suscripcion: sub.toJSON(), url_abrir: urlAbrir() });
      if (!r.ok) throw new Error("servidor");
    }
    async function revisar() {
      // Muestra el botón solo si el servidor ya tiene avisos habilitados y el cliente aún no los activó/bloqueó.
      if (Notification.permission === "denied") return;
      try {
        // Si el archivo del service worker aún no está publicado en el sitio, el botón no se muestra.
        const sw = await fetch(PUSH_SW, { method: "HEAD", cache: "no-store" });
        if (!sw.ok || !/javascript/i.test(sw.headers.get("content-type") || "")) return;
        if ((await fetch(API_BASE + "/push/clave")).ok === false) return;
        if (Notification.permission === "granted") {
          const reg = await navigator.serviceWorker.getRegistration(PUSH_SW);
          const sub = reg && (await reg.pushManager.getSubscription());
          if (sub) { suscribir().catch(() => {}); return; }   // ya activo: solo renueva y enlaza con la sesión
        }
        $activarAvisos.style.display = "";
      } catch (e) { /* sin red o sin avisos: el widget sigue normal */ }
    }
    $activarAvisos.onclick = async () => {
      $activarAvisos.disabled = true;
      try {
        const p = await Notification.requestPermission();
        if (p !== "granted") { post("/push/visita", { dispositivo_id: dispId, sesion_token: token(), modo: "widget", permiso: p }).catch(() => {}); $activarAvisos.style.display = "none"; return; }
        await suscribir();
        $activarAvisos.textContent = "🔔 Avisos activados ✓";
        setTimeout(() => { $activarAvisos.style.display = "none"; }, 2500);
      } catch (e) {
        $activarAvisos.textContent = "No se pudo activar";
        $activarAvisos.disabled = false;
      }
    };
    revisar();
  })();
  root.querySelector(".qr-cerrar").onclick = () => {
    $qrPanel.classList.remove("visible");
    clearTimeout(temporizadorQr);
  };
  const $textoInput = root.querySelector(".texto-input");
  const $enviarBtn = root.querySelector(".enviar");
  const $adjuntarBtn = root.querySelector(".adjuntar");
  const $adjuntarMenu = root.querySelector(".adjuntar-menu");
  const $archivoInputDocumento = root.querySelector(".archivo-input-documento");
  const $archivoInputFotos = root.querySelector(".archivo-input-fotos");
  const $archivoInputCamara = root.querySelector(".archivo-input-camara");
  const $pie = root.querySelector(".pie");
  const $grabacionIndicador = root.querySelector(".grabacion-indicador");
  const $grabacionCancelar = root.querySelector(".grabacion-cancelar");
  const $grabacionTiempo = root.querySelector(".grabacion-tiempo");
  const $lightboxOverlay = root.querySelector(".lightbox-overlay");
  const $lightboxImg = $lightboxOverlay.querySelector("img");
  const $lightboxCerrar = $lightboxOverlay.querySelector(".lightbox-cerrar");

  // ---- Emojis: selector en la caja de texto + reacciones rápidas por mensaje ----
  const EMOJIS_REACCION = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
  const EMOJIS_COMUNES = ["😀","😃","😄","😁","😆","😅","😂","🤣","😊","🙂","😉","😍","🥰","😘","😋","😎",
    "🤗","🤔","😐","😮","😯","😢","😭","😔","😞","😟","😕","🙁","😣","😖","😩","😤",
    "😡","🙏","👍","👎","👌","✌️","🤝","👏","🙌","💪","👋","🤞","❤️","💙","💚","💛",
    "🧡","💜","🖤","💔","💯","✅","❌","⚠️","❗","❓","⭐","✨","🎉","🎂","🎁","🔔",
    "📞","📱","📧","📄","📎","📅","⏰","🕐","🏥","💊","🩺","🌡️","😷","🤒","🤕","🚑",
    "🩹","💉","🏠","🚗","☀️","🌙","🙋","🤷","🙇","😴","🤩","🥳"];
  const $emojiBtn = root.querySelector(".emoji-btn");
  const $emojiPanel = root.querySelector(".emoji-panel");
  EMOJIS_COMUNES.forEach((em) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = em;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const ini = $textoInput.selectionStart != null ? $textoInput.selectionStart : $textoInput.value.length;
      const fin = $textoInput.selectionEnd != null ? $textoInput.selectionEnd : $textoInput.value.length;
      $textoInput.value = $textoInput.value.slice(0, ini) + em + $textoInput.value.slice(fin);
      const pos = ini + em.length;
      $textoInput.focus();
      $textoInput.setSelectionRange(pos, pos);
      // Para que el botón de enviar pase de micrófono a ➤ (escucha "input").
      $textoInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    $emojiPanel.appendChild(b);
  });
  $emojiBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    $emojiPanel.classList.toggle("abierto");
  });
  document.addEventListener("click", (e) => {
    if (!e.composedPath().includes($emojiPanel) && !e.composedPath().includes($emojiBtn)) {
      $emojiPanel.classList.remove("abierto");
    }
  });

  // Chips de reacciones debajo de la burbuja (reemplaza los que hubiera).
  function pintarReacciones(div, reacciones) {
    const previo = div.querySelector(".reacciones");
    if (previo) previo.remove();
    const usados = Object.entries(reacciones || {});
    if (!usados.length) return;
    const fila = document.createElement("div");
    fila.className = "reacciones";
    usados.forEach(([emoji, cantidad]) => {
      const chip = document.createElement("span");
      chip.className = "reaccion-chip";
      chip.textContent = cantidad > 1 ? `${emoji} ${cantidad}` : emoji;
      fila.appendChild(chip);
    });
    div.appendChild(fila);
    // El ⋯ y su menú siguen siendo lo último de la burbuja.
    const btn = div.querySelector(":scope > .msg-acciones");
    const menu = div.querySelector(":scope > .msg-menu");
    if (btn) div.appendChild(btn);
    if (menu) div.appendChild(menu);
  }

  function abrirLightbox(url) {
    lbReset();
    $lightboxImg.src = url;
    $lightboxOverlay.classList.add("abierto");
  }
  function cerrarLightbox() {
    $lightboxOverlay.classList.remove("abierto");
    $lightboxImg.src = "";
    lbReset();
  }

  // ---- Zoom de la imagen ampliada (igual que en la consola del asesor/supervisor) ----
  // Botones −/+ y 100%, rueda del mouse, pellizco con dos dedos, doble toque/clic, y arrastrar
  // para moverla cuando está ampliada.
  const LB_MIN = 0.5, LB_MAX = 6;
  let lbEsc = 1, lbX = 0, lbY = 0, lbMovio = false;
  const $lbPct = $lightboxOverlay.querySelector(".lb-pct");
  function lbAplicar() {
    $lightboxImg.style.transform = `translate(${lbX}px,${lbY}px) scale(${lbEsc})`;
    $lightboxImg.classList.toggle("zoom", lbEsc > 1);
    $lbPct.textContent = Math.round(lbEsc * 100) + "%";
  }
  function lbReset() { lbEsc = 1; lbX = 0; lbY = 0; lbAplicar(); }
  function lbZoom(nuevo) {
    lbEsc = Math.min(LB_MAX, Math.max(LB_MIN, nuevo));
    if (lbEsc <= 1) { lbX = 0; lbY = 0; }
    lbAplicar();
  }
  $lightboxOverlay.querySelector(".lb-mas").addEventListener("click", (e) => { e.stopPropagation(); lbZoom(lbEsc * 1.25); });
  $lightboxOverlay.querySelector(".lb-menos").addEventListener("click", (e) => { e.stopPropagation(); lbZoom(lbEsc / 1.25); });
  $lightboxOverlay.querySelector(".lb-reset").addEventListener("click", (e) => { e.stopPropagation(); lbReset(); });
  $lightboxOverlay.addEventListener("wheel", (e) => {
    if (!$lightboxOverlay.classList.contains("abierto")) return;
    e.preventDefault();
    lbZoom(lbEsc * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
  }, { passive: false });
  (function () {
    const punt = new Map();
    let ini = null, distIni = 0, escIni = 1, ultimoToque = 0;
    $lightboxImg.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      punt.set(e.pointerId, { x: e.clientX, y: e.clientY });
      lbMovio = false;
      try { $lightboxImg.setPointerCapture(e.pointerId); } catch (_) { /* nada */ }
      if (punt.size === 2) {
        const [a, b] = [...punt.values()];
        distIni = Math.hypot(a.x - b.x, a.y - b.y); escIni = lbEsc; ini = null;
      } else {
        ini = { x: e.clientX, y: e.clientY, bx: lbX, by: lbY };
      }
      if (lbEsc > 1) $lightboxImg.classList.add("arrastrando");
    });
    $lightboxImg.addEventListener("pointermove", (e) => {
      if (!punt.has(e.pointerId)) return;
      punt.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (punt.size === 2 && distIni) {
        const [a, b] = [...punt.values()];
        lbMovio = true;
        lbZoom(escIni * Math.hypot(a.x - b.x, a.y - b.y) / distIni);
      } else if (ini && lbEsc > 1) {
        const dx = e.clientX - ini.x, dy = e.clientY - ini.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) lbMovio = true;
        lbX = ini.bx + dx; lbY = ini.by + dy; lbAplicar();
      }
    });
    const fin = (e) => {
      const eraUnSoloDedo = punt.size === 1;
      punt.delete(e.pointerId);
      $lightboxImg.classList.remove("arrastrando");
      if (punt.size < 2) distIni = 0;
      if (punt.size === 0) ini = null;
      // Doble toque / doble clic: amplía al 250% o vuelve al 100% (hecho a mano: en iPhone el
      // evento dblclick no llega con touch-action: none).
      if (e.type === "pointerup" && eraUnSoloDedo && !lbMovio) {
        const ahora = Date.now();
        if (ahora - ultimoToque < 300) { ultimoToque = 0; if (lbEsc > 1) lbReset(); else lbZoom(2.5); }
        else ultimoToque = ahora;
      }
      setTimeout(() => { lbMovio = false; }, 0);
    };
    $lightboxImg.addEventListener("pointerup", fin);
    $lightboxImg.addEventListener("pointercancel", fin);
  })();
  document.addEventListener("keydown", (e) => {
    if (!$lightboxOverlay.classList.contains("abierto")) return;
    if (e.key === "Escape") cerrarLightbox();
    else if (e.key === "+" || e.key === "=") lbZoom(lbEsc * 1.25);
    else if (e.key === "-" || e.key === "_") lbZoom(lbEsc / 1.25);
    else if (e.key === "0") lbReset();
  });
  $lightboxCerrar.addEventListener("click", cerrarLightbox);
  $lightboxOverlay.addEventListener("click", (e) => { if (e.target === $lightboxOverlay && !lbMovio) cerrarLightbox(); });

  let ws = null;
  let noLeidos = 0;
  let panelAbierto = false;
  let sesionTokenActual = "";
  let conversacionIdActual = null;
  // El bot no puede procesar notas de voz (solo texto), así que el botón de
  // micrófono únicamente se habilita cuando la conversación ya está siendo
  // atendida por un asesor humano -- ver "asignado"/"en_cola"/"mensaje_bot"
  // /"conversacion_cerrada" en el switch del WebSocket, y actualizarIconoEnviar().
  let atendidoPorAsesor = false;

  function urlArchivoWidget(ruta, convId) {
    return `${API_BASE}/widget/archivo?conversacion_id=${encodeURIComponent(convId || conversacionIdActual)}`
      + `&ruta=${encodeURIComponent(ruta)}&sesion_token=${encodeURIComponent(sesionTokenActual)}`;
  }
  function esImagen(ruta) {
    return /\.(jpe?g|png|gif|webp|bmp)$/i.test(ruta || "");
  }
  function esAudio(ruta) {
    return /\.(webm|ogg|m4a|mp3|wav)$/i.test(ruta || "");
  }
  let intentosReconexion = 0;
  let reconectando = false;
  let cerradoManualmente = false;

  // Textos/sonido parametrizables desde /editor (configuracion_global) — se
  // usa un valor por defecto mientras llega la respuesta, o si el fetch falla.
  let textoEsperaSubida = "Espere un momento por favor !!!";
  let audioNotificacion = null; // se crea solo si hay una URL configurada
  fetch(`${API_BASE}/widget/textos`)
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) { console.warn("[webchat] /widget/textos no respondió OK"); return; }
      if (data.mensaje_subiendo_archivo) textoEsperaSubida = data.mensaje_subiendo_archivo;
      if (data.titulo_widget) {
        const $titulo = root.querySelector(".titulo");
        if ($titulo) $titulo.textContent = data.titulo_widget;
      }
      if (data.sonido_notificacion_widget) {
        const url = /^https?:\/\//i.test(data.sonido_notificacion_widget)
          ? data.sonido_notificacion_widget
          : `${API_BASE}${data.sonido_notificacion_widget}`;
        console.debug("[webchat] cargando sonido de notificación:", url);
        const audio = new Audio(url);
        audio.preload = "auto";
        // Si la URL configurada no carga (borrada, mal escrita, etc.), no
        // rompe nada — simplemente se sigue usando el tono sintetizado.
        audio.addEventListener("error", () => {
          console.warn("[webchat] no se pudo cargar el sonido configurado, usando el tono genérico:", url, audio.error);
          audioNotificacion = null;
        }, { once: true });
        audio.addEventListener("canplaythrough", () => console.debug("[webchat] sonido de notificación listo:", url), { once: true });
        audioNotificacion = audio;
      } else {
        console.debug("[webchat] sin sonido_notificacion_widget configurado, se usará el tono genérico");
      }
    })
    .catch((e) => console.warn("[webchat] fallo al pedir /widget/textos:", e));

  function siguienteEsperaReconexion() {
    // 1s, 2s, 4s, 8s, ... con tope de 20s
    return Math.min(1000 * Math.pow(2, intentosReconexion), 20000);
  }

  // ---------------------------------------------------------------------
  // Aviso de mensaje nuevo cuando la persona no está mirando la pestaña
  // (cambió de pantalla, minimizó, o está en otra pestaña): sonido +
  // parpadeo del título de la página, igual que WhatsApp Web.
  // ---------------------------------------------------------------------
  const tituloOriginalPagina = document.title;
  let intervaloParpadeoTitulo = null;
  let audioCtxWidget = null;
  let audioWidgetHabilitado = false;

  function inicializarAudioWidget() {
    try {
      if (!audioCtxWidget) audioCtxWidget = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtxWidget.state === "suspended") audioCtxWidget.resume();
      audioWidgetHabilitado = true;
    } catch (e) { /* navegador sin soporte de Web Audio — sin aviso sonoro */ }
  }
  // El navegador exige un gesto real del usuario para permitir sonido — se
  // engancha al primer clic en la burbuja o al primer envío de mensaje.
  $burbuja.addEventListener("click", inicializarAudioWidget, { once: true });
  $enviarBtn.addEventListener("click", inicializarAudioWidget, { once: true });

  function reproducirAvisoMensajeNuevo() {
    if (!audioWidgetHabilitado) return;

    // Si hay un audio configurado en /editor (ej. el "Sánitas" con voz), se
    // usa ese en vez del tono sintetizado.
    if (audioNotificacion) {
      try {
        audioNotificacion.currentTime = 0;
        audioNotificacion.play().catch((e) => console.warn("[webchat] play() del sonido configurado fue bloqueado/falló:", e));
        return;
      } catch (e) { console.warn("[webchat] error reproduciendo el sonido configurado, usando tono genérico:", e); }
    }

    if (!audioCtxWidget) return;
    try {
      if (audioCtxWidget.state === "suspended") audioCtxWidget.resume();
      const ahora = audioCtxWidget.currentTime;
      // Patrón "ding-dong" repetido dos veces, más fuerte y con un timbre
      // (triangle) más presente que el tono anterior (sine), para que no
      // pase desapercibido incluso con el volumen del sistema bajo.
      const notas = [
        [988, ahora],
        [740, ahora + 0.17],
        [988, ahora + 0.5],
        [740, ahora + 0.67],
      ];
      notas.forEach(([frecuencia, inicio]) => {
        const osc = audioCtxWidget.createOscillator();
        const gain = audioCtxWidget.createGain();
        osc.type = "triangle";
        osc.frequency.value = frecuencia;
        gain.gain.setValueAtTime(0, inicio);
        gain.gain.linearRampToValueAtTime(0.4, inicio + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, inicio + 0.32);
        osc.connect(gain).connect(audioCtxWidget.destination);
        osc.start(inicio);
        osc.stop(inicio + 0.34);
      });
    } catch (e) { /* si algo falla, no interrumpe el flujo normal */ }
  }

  function pestanaEnPrimerPlano() {
    return document.visibilityState === "visible" && document.hasFocus();
  }

  function iniciarParpadeoTitulo() {
    if (intervaloParpadeoTitulo) return;
    let mostrandoAviso = false;
    intervaloParpadeoTitulo = setInterval(() => {
      document.title = mostrandoAviso ? tituloOriginalPagina : "💬 Nuevo mensaje";
      mostrandoAviso = !mostrandoAviso;
    }, 1000);
  }

  function detenerParpadeoTitulo() {
    if (intervaloParpadeoTitulo) {
      clearInterval(intervaloParpadeoTitulo);
      intervaloParpadeoTitulo = null;
      document.title = tituloOriginalPagina;
    }
  }

  function avisarMensajeNuevoSiNoMiraPantalla() {
    if (pestanaEnPrimerPlano()) return;
    reproducirAvisoMensajeNuevo();
    iniciarParpadeoTitulo();
  }

  document.addEventListener("visibilitychange", () => { if (pestanaEnPrimerPlano()) detenerParpadeoTitulo(); });
  window.addEventListener("focus", () => { if (pestanaEnPrimerPlano()) detenerParpadeoTitulo(); });

  // ---------------------------------------------------------------------
  // Abrir / cerrar
  // ---------------------------------------------------------------------
  // De dónde abre el chat la persona (métrica del dashboard). Por defecto
  // es un clic en la burbuja; los enlaces de SMS lo cambian antes de abrir.
  let origenApertura = "burbuja";

  function abrirPanel() {
    panelAbierto = true;
    $panel.classList.add("abierto");
    $burbuja.classList.add("oculta");
    noLeidos = 0;
    actualizarBadge();
    if (!ws) conectar();
    else enviar({ type: "marcar_leido", payload: {} });
    scrollAlFinal();
    revisarHistorialPrevio(700);
  }
  function cerrarPanel() {
    panelAbierto = false;
    $panel.classList.remove("abierto");
    $burbuja.classList.remove("oculta");
  }
  $burbuja.addEventListener("click", () => (panelAbierto ? cerrarPanel() : abrirPanel()));
  $cerrar.addEventListener("click", cerrarPanel);

  // -----------------------------------------------------------------------
  // Enlace directo (SMS saliente que manda un asesor/supervisor, o la
  // invitación inicial tras una cita):
  //   ?wcs=<sesion_token>  -> "vuelve a tu conversación" (ya existe una
  //                           sesión) — la guardamos como sesión activa
  //                           (misma clave que usa conectar()).
  //   ?ac=1                -> invitación nueva, sin conversación previa —
  //                           solo abre el panel para que el paciente
  //                           empiece a escribir (conectar() crea la sesión
  //                           nueva como en cualquier visita normal).
  //                           ("ac" = "abrir chat"; nombre corto a propósito
  //                           porque este link va en un SMS, con poco espacio).
  // En ambos casos se abre el panel solo, sin que el usuario tenga que
  // buscar la burbuja, y se limpia el parámetro de la URL para que no quede
  // visible ni reusable desde el historial del navegador.
  // -----------------------------------------------------------------------
  try {
    const params = new URLSearchParams(window.location.search);
    const tokenEnlace = params.get("wcs");
    const abrirDesdeInvitacion = params.get("ac") === "1";
    if (abrirDesdeInvitacion) origenApertura = "invitacion_sms";
    else if (tokenEnlace) origenApertura = "enlace_sms";
    const codigoTraspaso = params.get("wct");
    const tokenArchivoSms = params.get("af");   // enlace único de un archivo que nos envió un asesor/supervisor por SMS
    if (tokenArchivoSms) origenApertura = "enlace_sms";
    const tokenDirecto = params.get("ad");      // enlace directo a una campaña/asesor, enviado por SMS desde el panel
    if (tokenDirecto) origenApertura = "enlace_directo";
    if (tokenEnlace) {
      localStorage.setItem("sanitas_webchat_sesion", tokenEnlace);
    }
    if (codigoTraspaso) {
      // Llegó escaneando el QR: canjea el código (un solo uso) por la sesión de la
      // conversación y abre el chat ya con ella.
      origenApertura = "traspaso_qr";
      fetch(`${API_BASE}/widget/traspaso/canjear`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codigo: codigoTraspaso }),
      }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { localStorage.setItem("sanitas_webchat_sesion", d.sesion_token); abrirPanel(); })
        .catch(() => {
          abrirPanel();
          setTimeout(() => agregarMensaje(
            "⚠️ Ese código QR venció o ya se usó. Genera uno nuevo desde tu computadora (📱 Seguir en mi celular).",
            "sistema"), 1500);
        });
    }
    if (tokenArchivoSms) {
      // Canjea el enlace (un solo uso) por la sesión del celular al que se envió el archivo y abre el chat:
      // el servidor pone el archivo en la conversación apenas se conecta.
      fetch(`${API_BASE}/widget/archivo-enlace/canjear`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tokenArchivoSms, sesion_token: localStorage.getItem("sanitas_webchat_sesion") || null }),
      }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { localStorage.setItem("sanitas_webchat_sesion", d.sesion_token); abrirPanel(); })
        .catch(() => {
          abrirPanel();
          setTimeout(() => agregarMensaje(
            "⚠️ Ese enlace venció o ya se usó. Escríbenos por aquí y te ayudamos a recibir el archivo.",
            "sistema"), 1500);
        });
    }
    if (tokenDirecto) {
      // Canjea el enlace (un solo uso) por la sesión del celular que lo recibió; al conectarse el chat el servidor
      // lo deriva a la campaña/asesor elegido (o le muestra el mensaje de fuera de horario).
      fetch(`${API_BASE}/widget/enlace-directo/canjear`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tokenDirecto, sesion_token: localStorage.getItem("sanitas_webchat_sesion") || null }),
      }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { localStorage.setItem("sanitas_webchat_sesion", d.sesion_token); abrirPanel(); })
        .catch(() => {
          abrirPanel();
          setTimeout(() => agregarMensaje(
            "⚠️ Ese enlace venció o ya se usó. Escríbenos por aquí y te ayudamos.",
            "sistema"), 1500);
        });
    }
    if (tokenEnlace || abrirDesdeInvitacion || codigoTraspaso || tokenArchivoSms || tokenDirecto) {
      params.delete("ad");
      params.delete("wcs");
      params.delete("wct");
      params.delete("ac");
      params.delete("af");
      const resto = params.toString();
      const nuevaUrl = window.location.pathname + (resto ? "?" + resto : "") + window.location.hash;
      window.history.replaceState({}, "", nuevaUrl);
      if (!codigoTraspaso && !tokenArchivoSms && !tokenDirecto) setTimeout(abrirPanel, 300);
    }
  } catch (e) {
    // localStorage bloqueado (modo privado estricto) o URL no soportada —
    // no rompe el widget normal, simplemente no hace el auto-open.
  }

  function actualizarBadge() {
    if (noLeidos > 0) {
      $badge.textContent = noLeidos > 9 ? "9+" : String(noLeidos);
      $badge.style.display = "flex";
    } else {
      $badge.style.display = "none";
    }
  }

  // ---------------------------------------------------------------------
  // Render de mensajes
  // ---------------------------------------------------------------------
  let scrollPendiente = false;
  let checksPendientesRecibido = []; // FIFO: el orden de llegada de las confirmaciones coincide con el de envío
  let divsPendientesId = []; // FIFO paralelo: le asigna a cada burbuja propia su "id" real de la base de datos en cuanto el servidor confirma, para poder editarla/responderla/eliminarla después
  function scrollAlFinal() {
    // Si ya hay uno programado, no dupliques — deja que se resuelva UNA sola vez,
    // en el siguiente frame, cuando todo lo que se agregó en esta tanda (mensaje
    // + botones, por ejemplo) ya esté en el DOM. Interrumpir un scroll suave a
    // medio camino con otro es lo que lo dejaba cortado antes de llegar al final.
    if (scrollPendiente) return;
    scrollPendiente = true;
    requestAnimationFrame(() => {
      scrollPendiente = false;
      $cuerpo.scrollTop = $cuerpo.scrollHeight;
    });
    ocultarIndicadorNuevoMensaje();
  }

  // Umbral de "está viendo el final del chat" — si el usuario se desplazó
  // más arriba que esto (revisando mensajes viejos), un mensaje nuevo no lo
  // debe arrastrar de un jalón hasta abajo; en cambio se le avisa con la
  // flechita "Mensaje nuevo ⬇️" para que decida cuándo bajar.
  function estaCercaDelFinal() {
    return $cuerpo.scrollHeight - $cuerpo.scrollTop - $cuerpo.clientHeight < 80;
  }
  function mostrarIndicadorNuevoMensaje() {
    $nuevoMensaje.classList.add("visible");
  }
  function ocultarIndicadorNuevoMensaje() {
    $nuevoMensaje.classList.remove("visible");
  }
  $cuerpo.addEventListener("scroll", () => {
    if (estaCercaDelFinal()) ocultarIndicadorNuevoMensaje();
    if ($cuerpo.scrollTop < 80) pedirHistorialPrevio();
  }, { passive: true });

  // ---------------------------------------------------------------------
  // Conversaciones pasadas (hasta 3 meses): al subir el scroll hasta el inicio se cargan
  // en bloques, de la más reciente hacia atrás. Solo si el celular está verificado
  // (el servidor decide); son de solo lectura.
  // ---------------------------------------------------------------------
  let previoCursor = null;     // id del mensaje más antiguo ya cargado
  let previoFin = false;       // no hay más (o no está disponible)
  let previoCargando = false;
  let previoCargados = 0;
  let previoAutoSeguidos = 0;
  let previoTimer = null;
  let sepPorConv = {};

  function resetPrevio() {
    previoCursor = null; previoFin = false; previoCargando = false; previoCargados = 0;
    previoAutoSeguidos = 0; sepPorConv = {};
    if (previoTimer) { clearTimeout(previoTimer); previoTimer = null; }
  }
  function pedirHistorialPrevio() {
    if (previoFin || previoCargando || !ws || ws.readyState !== WebSocket.OPEN) return;
    previoCargando = true;
    if (!$cuerpo.querySelector(".cargando-previo")) {
      const ind = document.createElement("div");
      ind.className = "cargando-previo";
      ind.textContent = "Cargando conversaciones anteriores…";
      $cuerpo.insertBefore(ind, $cuerpo.firstChild);
    }
    enviar({ type: "cargar_historial_previo", payload: { antes_de: previoCursor } });
  }
  // Si lo que hay en pantalla no alcanza para hacer scroll, se carga solo lo anterior.
  function revisarHistorialPrevio(ms) {
    if (previoTimer) clearTimeout(previoTimer);
    previoTimer = setTimeout(() => {
      previoTimer = null;
      if (panelAbierto && !previoFin && !previoCargando && $cuerpo.scrollHeight <= $cuerpo.clientHeight + 4) {
        pedirHistorialPrevio();
      }
    }, ms || 400);
  }
  function fechaCorta(iso) {
    if (!iso) return "";
    try { return new Date(iso).toLocaleDateString("es-PE", { day: "numeric", month: "short", year: "numeric" }); }
    catch (e) { return ""; }
  }
  function crearSeparadorConv(convId, info) {
    if (sepPorConv[convId]) sepPorConv[convId].remove();
    const sep = document.createElement("div");
    sep.className = "sep-hist";
    const t = document.createElement("span");
    const partes = ["Conversación anterior", fechaCorta(info && info.iniciado_en)];
    if (info && info.campana) partes.push(info.campana);
    t.textContent = partes.filter(Boolean).join(" · ");
    sep.appendChild(t);
    sepPorConv[convId] = sep;
    return sep;
  }
  function crearMensajePrevio(m) {
    const tipo = m.emisor_tipo === "usuario" ? "usuario" : m.emisor_tipo === "sistema" ? "sistema" : "bot";
    const div = document.createElement("div");
    div.className = "msg previo " + tipo;
    if (m.eliminado_en) {
      div.classList.add("eliminado");
      const sp = document.createElement("span");
      sp.className = "texto-mensaje";
      sp.textContent = "🚫 Este mensaje se eliminó";
      div.appendChild(sp);
    } else if (m.tipo_contenido === "archivo") {
      if (m.archivo_disponible) {
        const url = urlArchivoWidget(m.contenido, m.conversacion_id);
        if (esImagen(m.contenido)) {
          const img = document.createElement("img");
          img.src = url; img.className = "msg-imagen"; img.loading = "lazy";
          img.addEventListener("click", () => abrirLightbox(url));
          div.appendChild(img);
        } else if (esAudio(m.contenido)) {
          const audio = document.createElement("audio");
          audio.controls = true; audio.preload = "none"; audio.src = url;
          audio.style.cssText = "max-width:230px;width:100%;";
          div.appendChild(audio);
        } else {
          const a = document.createElement("a");
          a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
          a.textContent = "📎 Ver archivo enviado"; a.style.color = "inherit";
          div.appendChild(a);
        }
      } else {
        const sp = document.createElement("span");
        sp.className = "texto-mensaje archivo-no-disponible";
        sp.textContent = "📎 Aquí había un archivo, pero ya no está disponible.";
        div.appendChild(sp);
      }
    } else {
      const sp = document.createElement("span");
      sp.className = "texto-mensaje";
      sp.innerHTML = formatearTexto(m.contenido);
      div.appendChild(sp);
    }
    if (tipo !== "sistema") {
      if (m.editado_en && !m.eliminado_en) {
        const tag = document.createElement("span");
        tag.className = "editado-tag"; tag.textContent = "(editado)";
        div.appendChild(tag);
      }
      const hora = document.createElement("span");
      hora.className = "hora";
      hora.textContent = formatearHora(m.enviado_en);
      div.appendChild(hora);
    }
    return div;
  }
  function mostrarHistorialPrevio(p) {
    previoCargando = false;
    // Se mide ANTES de quitar el "Cargando…", para que ese hueco también cuente al conservar la posición.
    const estabaAbajo = estaCercaDelFinal();
    const alturaAntes = $cuerpo.scrollHeight, topAntes = $cuerpo.scrollTop;
    const ind = $cuerpo.querySelector(".cargando-previo");
    if (ind) ind.remove();
    const msgs = (p && p.mensajes) || [];
    if (!p || !p.disponible || !msgs.length) { previoFin = true; return; }

    const frag = document.createDocumentFragment();
    let convPrev = null;
    msgs.forEach((m) => {
      if (m.conversacion_id !== convPrev) {
        convPrev = m.conversacion_id;
        frag.appendChild(crearSeparadorConv(m.conversacion_id, (p.conversaciones || {})[String(m.conversacion_id)]));
      }
      frag.appendChild(crearMensajePrevio(m));
    });
    if (previoCargados === 0) {  // primer bloque: marca dónde empieza la conversación actual
      const act = document.createElement("div");
      act.className = "sep-actual";
      const t = document.createElement("span");
      t.textContent = "Conversación actual";
      act.appendChild(t);
      frag.appendChild(act);
    }
    const finViejo = $cuerpo.querySelector(".sep-fin");
    if (finViejo) finViejo.remove();
    if (!p.hay_mas) {
      const fin = document.createElement("div");
      fin.className = "sep-fin";
      fin.textContent = "Aquí empieza tu historial (últimos 3 meses)";
      frag.insertBefore(fin, frag.firstChild);
    }
    $cuerpo.insertBefore(frag, $cuerpo.firstChild);
    previoCursor = msgs[0].id;
    previoCargados += msgs.length;
    previoFin = !p.hay_mas;

    // Sin saltos: si la persona estaba abajo se queda abajo; si estaba arriba, se mantiene lo que miraba.
    $cuerpo.style.scrollBehavior = "auto";
    if (estabaAbajo) $cuerpo.scrollTop = $cuerpo.scrollHeight;
    else $cuerpo.scrollTop = topAntes + ($cuerpo.scrollHeight - alturaAntes);
    requestAnimationFrame(() => { $cuerpo.style.scrollBehavior = ""; });

    // Si todavía no hay con qué hacer scroll, sigue cargando (con tope).
    if (!previoFin && $cuerpo.scrollHeight <= $cuerpo.clientHeight + 4 && previoAutoSeguidos < 5) {
      previoAutoSeguidos++;
      revisarHistorialPrevio(60);
    }
  }
  $nuevoMensaje.addEventListener("click", () => {
    scrollAlFinal();
    if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
  });

  function formatearHora(fecha) {
    const d = fecha ? new Date(fecha) : new Date();
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function formatearTexto(texto) {
    // Escapa HTML primero (por seguridad, incluye lo que escriba el usuario)
    // y recién después interpreta *negrita*, _cursiva_ y ~tachado~ — igual
    // que WhatsApp — sin correr riesgo de inyección de código.
    const escapado = (texto || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    // Protege las URLs ANTES de aplicar *negrita*/_cursiva_/~tachado~: un
    // nombre de archivo con guiones bajos (ej. "SCTR_EMPRESA_20260930.xlsx",
    // como los que genera "Generar Excel") se confundía con la marca de
    // "_cursiva_" y el formateo se comía los guiones bajos, dejando un link
    // roto que apuntaba a un archivo que no existe. Se reemplaza cada URL
    // por un marcador temporal, se formatea el resto del texto con
    // tranquilidad, y al final se restauran las URLs ya como links reales.
    const urls = [];
    const conMarcadores = escapado.replace(/(https?:\/\/[^\s<]+)/g, (match) => {
      urls.push(match);
      return `\u0000${urls.length - 1}\u0000`;
    });

    const formateado = conMarcadores
      .replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
      .replace(/_([^_\n]+)_/g, "<i>$1</i>")
      .replace(/~([^~\n]+)~/g, "<s>$1</s>");

    return formateado.replace(/\u0000(\d+)\u0000/g, (_, i) => {
      const url = urls[Number(i)];
      // No mostrar nunca la URL cruda en la burbuja del chat (expone
      // nombres de bucket/rutas internas de otros servicios, como el Excel
      // que genera sofi-apis) — en su lugar, una etiqueta genérica.
      // Si es un link de descarga conocido (ej. ese mismo Excel), además lo
      // hacemos pasar por /widget/redirigir para que ni siquiera el href
      // visible (al pasar el mouse, o mirando el código fuente del mensaje)
      // muestre el dominio/ruta real de Google Cloud Storage — solo se ve
      // nuestro propio dominio hasta el momento en que arranca la descarga.
      const esDescargaConocida = /^https:\/\/storage\.googleapis\.com\/sanitas-recetas-archivos\//.test(url);
      if (esDescargaConocida) {
        const linkOculto = `${API_BASE}/widget/redirigir?u=${base64UrlEncode(url)}`;
        return `<a href="${linkOculto}" target="_blank" rel="noopener noreferrer">⬇️ Descargar archivo</a>`;
      }
      // Enlace al mapa (confirmación de Médico a domicilio): etiqueta propia, para que se entienda qué abre.
      if (/^https:\/\/www\.google\.com\/maps\?q=-?\d+(\.\d+)?,-?\d+(\.\d+)?$/.test(url)) {
        return `<a href="${url}" target="_blank" rel="noopener noreferrer">📍 Ver en el mapa</a>`;
      }
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">🔗 Abrir enlace</a>`;
    });
  }

  // Codifica un texto a base64 "URL-safe" (sin +, / ni = de relleno) para
  // pasarlo como query param sin que rompa la URL — usado por
  // formatearTexto() para armar el link de /widget/redirigir.
  function base64UrlEncode(str) {
    return btoa(unescape(encodeURIComponent(str)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  // Ventana de edición, igual que la que aplica el servidor (main.py,
  // VENTANA_EDICION_MINUTOS) — se usa solo para decidir si se muestra la
  // opción "Editar" en el menú; el servidor es quien de verdad la exige.
  const VENTANA_EDICION_MS = 2 * 60 * 1000;

  function etiquetaEmisor(tipo) {
    if (tipo === "usuario") return "Tú";
    if (tipo === "bot") return "Asistente";
    return "Asesor";
  }

  // ID del mensaje al que se está respondiendo (null si no hay ninguno
  // seleccionado) — se manda como "respondido_a_id" en el próximo envío.
  let respondiendoAId = null;
  let respondiendoAPreview = null;

  function iniciarRespuesta(mensajeId, emisorTipo, textoOriginal) {
    respondiendoAId = mensajeId;
    respondiendoAPreview = { emisor_tipo: emisorTipo, texto: (textoOriginal || "").slice(0, 160) };
    $responderTexto.textContent = `${etiquetaEmisor(emisorTipo)}: ${(textoOriginal || "").slice(0, 120)}`;
    $responderBanner.classList.add("abierto");
    $textoInput.focus();
  }
  function cancelarRespuesta() {
    respondiendoAId = null;
    respondiendoAPreview = null;
    $responderBanner.classList.remove("abierto");
  }
  $cancelarResponder.addEventListener("click", cancelarRespuesta);

  function iniciarEdicion(div, mensajeId, textoActual) {
    const span = div.querySelector(".texto-mensaje");
    if (!span) return;
    const original = textoActual;
    span.innerHTML = "";
    const input = document.createElement("input");
    input.type = "text";
    input.value = original;
    input.style.cssText = "width:100%;border:1px solid rgba(255,255,255,.6);border-radius:6px;padding:4px 6px;font:inherit;text-align:right;";
    span.appendChild(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    const confirmar = () => {
      const nuevo = input.value.trim();
      if (nuevo && nuevo !== original) {
        enviar({ type: "editar_mensaje", payload: { mensaje_id: mensajeId, texto: nuevo } });
      } else {
        span.innerHTML = formatearTexto(original);
      }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") confirmar();
      if (e.key === "Escape") span.innerHTML = formatearTexto(original);
    });
    input.addEventListener("blur", confirmar);
  }

  function confirmarEliminar(mensajeId) {
    if (!confirm("¿Eliminar este mensaje?")) return;
    enviar({ type: "eliminar_mensaje", payload: { mensaje_id: mensajeId } });
  }

  function cerrarTodosLosMenus() {
    $cuerpo.querySelectorAll(".msg-menu.abierto").forEach((m) => m.classList.remove("abierto"));
    $cuerpo.querySelectorAll(".msg-acciones.abierto").forEach((b) => b.classList.remove("abierto"));
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".msg-acciones") && !e.target.closest(".msg-menu")) cerrarTodosLosMenus();
  });

  function agregarBotonAcciones(div, { mensajeId, tipo, tipoContenido, texto, enviadoEnMs, esArchivo }) {
    const btn = document.createElement("button");
    btn.className = "msg-acciones";
    btn.type = "button";
    btn.textContent = "⋯";
    btn.setAttribute("aria-label", "Opciones del mensaje");
    const menu = document.createElement("div");
    menu.className = "msg-menu";

    const item = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", (e) => { e.stopPropagation(); cerrarTodosLosMenus(); fn(); });
      menu.appendChild(b);
    };

    // Se lee "div.dataset.id" al momento del clic (no un valor fijo al crear
    // el menú): recién enviado un mensaje propio todavía no tiene id — lo
    // recibe un instante después, cuando el servidor confirma — así que si
    // se leyera una sola vez al armar el menú, quedaría con "undefined".
    const barra = document.createElement("div");
    barra.className = "msg-reacciones-barra";
    EMOJIS_REACCION.forEach((em) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = em;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        cerrarTodosLosMenus();
        if (div.dataset.id) enviar({ type: "reaccionar_mensaje", payload: { mensaje_id: Number(div.dataset.id), emoji: em } });
      });
      barra.appendChild(b);
    });
    menu.appendChild(barra);
    item("↩️ Responder", () => { if (div.dataset.id) iniciarRespuesta(div.dataset.id, tipo, texto); });
    if (tipo === "usuario") {
      const dentroDeVentana = Date.now() - enviadoEnMs <= VENTANA_EDICION_MS;
      if (!esArchivo && dentroDeVentana) {
        item("✏️ Editar", () => { if (div.dataset.id) iniciarEdicion(div, div.dataset.id, texto); });
      }
      item("🗑️ Eliminar", () => { if (div.dataset.id) confirmarEliminar(div.dataset.id); });
    }

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const yaAbierto = menu.classList.contains("abierto");
      cerrarTodosLosMenus();
      if (!yaAbierto) {
        menu.classList.add("abierto");
        btn.classList.add("abierto");
      }
    });
    div.appendChild(btn);
    div.appendChild(menu);
  }

  // Indicador "escribiendo…" (tres puntos): el servidor lo manda cuando una respuesta tarda
  // (agente de IA). Desaparece solo al llegar cualquier mensaje, o a los 60 s por seguridad.
  let divEscribiendo = null, temporizadorEscribiendo = null;
  function quitarEscribiendo() {
    clearTimeout(temporizadorEscribiendo);
    if (divEscribiendo) { divEscribiendo.remove(); divEscribiendo = null; }
  }
  function mostrarEscribiendo() {
    quitarEscribiendo();
    const d = document.createElement("div");
    d.className = "msg bot escribiendo";
    d.setAttribute("role", "status");
    d.setAttribute("aria-label", "Sofi está escribiendo");
    d.innerHTML = '<span class="punto"></span><span class="punto"></span><span class="punto"></span>';
    $cuerpo.appendChild(d);
    divEscribiendo = d;
    temporizadorEscribiendo = setTimeout(quitarEscribiendo, 60000);
    scrollAlFinal();
  }

  function agregarMensaje(texto, tipo, imagenUrl, ruta, extra) {
    extra = extra || {};
    const cercaDelFinalAntes = estaCercaDelFinal();
    const div = document.createElement("div");
    div.className = "msg " + tipo;
    const enviadoEnMs = Date.now();
    div.dataset.enviado = String(enviadoEnMs);
    if (extra.mensajeId) div.dataset.id = String(extra.mensajeId);

    if (extra.respondidoA) {
      const cita = document.createElement("span");
      cita.className = "msg-cita";
      const ra = extra.respondidoA;
      cita.textContent = `${etiquetaEmisor(ra.emisor_tipo)}: ${(ra.texto || "").slice(0, 120)}`;
      div.appendChild(cita);
    }

    if (imagenUrl) {
      if (ruta && esAudio(ruta)) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "metadata";
        audio.src = imagenUrl;
        audio.style.cssText = "max-width:230px;width:100%;";
        div.appendChild(audio);
      } else {
        // Si el archivo es un PDF (p.ej. la carta de tarifas 2026, o un
        // documento que mandó el asesor) en vez de una imagen, no tiene
        // sentido meterlo en un <img> (se ve como ícono roto) — se muestra
        // como un link de descarga en su lugar.
        //
        // OJO: cuando el archivo viene de /widget/archivo (asesor/supervisor),
        // "imagenUrl" ya incluye ?conversacion_id=...&ruta=...&sesion_token=...
        // por delante, así que buscar ".pdf" al FINAL de esa URL nunca daba
        // con nada (el ".pdf" real queda en medio, dentro del query string) —
        // eso hacía que un PDF terminara metido en un <img> y se viera como
        // ícono roto. Por eso, si tenemos la "ruta" original (el nombre real
        // del archivo, antes de armar la URL), la usamos en su lugar.
        const esPdf = ruta ? !esImagen(ruta) : /\.pdf(\?|#|$)/i.test(imagenUrl);
        if (esPdf) {
          const link = document.createElement("a");
          link.href = imagenUrl;
          link.target = "_blank";
          link.rel = "noopener";
          link.className = "msg-archivo-link";
          link.textContent = "📄 Descargar documento";
          div.appendChild(link);
        } else {
          const img = document.createElement("img");
          img.src = imagenUrl;
          img.className = "msg-imagen";
          img.loading = "lazy";
          img.addEventListener("click", () => abrirLightbox(imagenUrl));
          div.appendChild(img);
        }
      }
    }
    const textoSpan = document.createElement("span");
    textoSpan.className = "texto-mensaje";
    textoSpan.innerHTML = formatearTexto(texto);
    div.appendChild(textoSpan);
    if (tipo !== "sistema") {
      const hora = document.createElement("span");
      hora.className = "hora";
      hora.textContent = formatearHora();
      if (tipo === "usuario") {
        const check = document.createElement("span");
        check.className = "check";
        check.textContent = " ✓";
        hora.appendChild(check);
        checksPendientesRecibido.push(check);
        divsPendientesId.push(div);
        ultimaBurbujaUsuario = div; ultimaBurbujaUsuarioTs = Date.now();
      }
      div.appendChild(hora);
      agregarBotonAcciones(div, {
        tipo, texto, enviadoEnMs, esArchivo: !!imagenUrl || !!extra.esArchivo,
      });
    }
    $cuerpo.appendChild(div);
    // Los mensajes propios (o cuando ya se estaba viendo el final del chat)
    // sí empujan el scroll hasta abajo, como siempre. Pero si la persona
    // estaba revisando mensajes viejos más arriba, un mensaje del bot o del
    // asesor NO la arrastra de un jalón — solo se enciende la flechita
    // "Mensaje nuevo ⬇️" para que decida cuándo bajar.
    if (tipo === "usuario" || cercaDelFinalAntes) {
      scrollAlFinal();
    } else {
      mostrarIndicadorNuevoMensaje();
    }
    if (!panelAbierto && tipo !== "usuario") {
      noLeidos++;
      actualizarBadge();
    }
    return div;
  }

  function marcarMensajeRecibido(mensajeId, recibido) {
    // El servidor confirmó que guardó/entregó el mensaje — pasa de ✓ a ✓✓
    // (gris) y le asigna a esa burbuja su id real (para poder editarla,
    // responderla o eliminarla más adelante).
    const check = checksPendientesRecibido.shift();
    if (check && recibido !== false && !check.classList.contains("leido")) check.textContent = " ✓✓";
    const div = divsPendientesId.shift();
    if (div && mensajeId) div.dataset.id = String(mensajeId);
  }

  function marcarMensajesLeidos() {
    $cuerpo.querySelectorAll(".msg.usuario .check").forEach((el) => {
      el.textContent = " ✓✓";
      el.classList.add("leido");
    });
    checksPendientesRecibido = [];
  }

  function agregarMensajeHistorial(msg, esUltimoMensaje) {
    if (msg.tipo_contenido === "formulario" && msg.emisor_tipo === "bot") {
      // Formulario del bot: solo se reconstruye como formulario vivo si es
      // el ÚLTIMO mensaje del historial (es decir, todavía nadie lo
      // respondió) — así no resucita formularios de flujos ya completados
      // hace rato, que también quedaron guardados con este mismo tipo.
      if (esUltimoMensaje) {
        try {
          agregarFormulario(JSON.parse(msg.contenido));
        } catch (e) { /* contenido corrupto/antiguo — se ignora, no rompe el historial */ }
      }
      return;
    }
    if (msg.tipo_contenido === "opciones" && msg.emisor_tipo === "bot") {
      // Botones de un menú del bot: solo se reconstruyen si es el ÚLTIMO
      // mensaje (todavía nadie eligió) — así no reaparecen menús viejos ya
      // respondidos. Sin esto, al reconectarse el widget (cortes de red,
      // pestaña en segundo plano, deploy) los botones del menú desaparecían.
      if (esUltimoMensaje) {
        try {
          const ops = JSON.parse(msg.contenido);
          if (Array.isArray(ops) && ops.length) agregarOpciones(ops);
        } catch (e) { /* contenido corrupto/antiguo — se ignora */ }
      }
      return;
    }
    if (msg.tipo_contenido !== "texto" && msg.tipo_contenido !== "archivo" && msg.tipo_contenido !== "boton_click") {
      return; // otros tipos (ej. formulario ya respondido por el usuario, opciones) no se reconstruyen
    }
    const tipo = msg.emisor_tipo === "usuario" ? "usuario" : msg.emisor_tipo === "sistema" ? "sistema" : "bot";
    const div = document.createElement("div");
    div.className = "msg " + tipo;
    if (msg.id) div.dataset.id = String(msg.id);
    const enviadoEnMs = msg.enviado_en ? new Date(msg.enviado_en).getTime() : 0;
    div.dataset.enviado = String(enviadoEnMs);

    if (msg.eliminado_en) {
      div.classList.add("eliminado");
      const span = document.createElement("span");
      span.className = "texto-mensaje";
      span.textContent = "🚫 Este mensaje se eliminó";
      div.appendChild(span);
      $cuerpo.appendChild(div);
      return;
    }

    if (msg.respondido_a) {
      const cita = document.createElement("span");
      cita.className = "msg-cita";
      cita.textContent = `${etiquetaEmisor(msg.respondido_a.emisor_tipo)}: ${(msg.respondido_a.texto || "").slice(0, 120)}`;
      div.appendChild(cita);
    }

    if (msg.tipo_contenido === "archivo") {
      // Propia foto, o una que le mandó el asesor/supervisor — ambas se ven
      // igual que cualquier imagen del chat: vista previa + clic para agrandar.
      const url = urlArchivoWidget(msg.contenido);
      if (esImagen(msg.contenido)) {
        const img = document.createElement("img");
        img.src = url;
        img.className = "msg-imagen";
        img.loading = "lazy";
        img.addEventListener("click", () => abrirLightbox(url));
        div.appendChild(img);
      } else if (esAudio(msg.contenido)) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = url;
        audio.style.cssText = "max-width:230px;width:100%;";
        div.appendChild(audio);
      } else {
        const a = document.createElement("a");
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "📎 Ver archivo enviado";
        a.style.color = "inherit";
        div.appendChild(a);
      }
    } else {
      const span = document.createElement("span");
      span.className = "texto-mensaje";
      span.innerHTML = formatearTexto(msg.contenido);
      div.appendChild(span);
    }

    if (tipo !== "sistema") {
      if (msg.editado_en) {
        const tag = document.createElement("span");
        tag.className = "editado-tag";
        tag.textContent = "(editado)";
        div.appendChild(tag);
      }
      const hora = document.createElement("span");
      hora.className = "hora";
      hora.textContent = formatearHora(msg.enviado_en);
      if (tipo === "usuario") {
        const check = document.createElement("span");
        check.className = "check" + (msg.leido ? " leido" : "");
        check.textContent = msg.leido ? " ✓✓" : " ✓";
        hora.appendChild(check);
      }
      div.appendChild(hora);
      pintarReacciones(div, msg.reacciones);
      if (msg.id && msg.tipo_contenido !== "boton_click") {
        agregarBotonAcciones(div, {
          tipo, texto: msg.contenido, enviadoEnMs, esArchivo: msg.tipo_contenido === "archivo",
        });
      }
    }
    $cuerpo.appendChild(div);
  }

  // "Hola de nuevo": si el navegador ya fue reconocido, una barra discreta sobre
  // el chat muestra el nombre y permite borrar los datos con "No soy yo".
  function mostrarNoSoyYo(nombre) {
    let barra = root.querySelector(".no-soy-yo");
    if (!nombre) {
      if (barra) barra.remove();
      return;
    }
    if (!barra) {
      barra = document.createElement("div");
      barra.className = "no-soy-yo";
      $cuerpo.parentNode.insertBefore(barra, $cuerpo);
    }
    barra.textContent = "";
    const quien = document.createElement("span");
    quien.textContent = "👤 " + nombre + " ·";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "No soy yo";
    btn.title = "Olvidar mis datos en este navegador";
    btn.onclick = () => {
      barra.remove();
      $cuerpo.innerHTML = "";
      enviar({ type: "reiniciar_datos", payload: {} });
    };
    barra.appendChild(quien);
    barra.appendChild(btn);
  }

  function agregarAccionesCierre() {
    const cont = document.createElement("div");
    cont.className = "acciones-cierre";

    const btnNueva = document.createElement("button");
    btnNueva.textContent = "🔄 Iniciar nueva conversación";
    btnNueva.onclick = () => {
      cont.remove();
      $cuerpo.innerHTML = "";
      enviar({ type: "iniciar_nueva_conversacion", payload: {} });
    };

    const btnCambiarDatos = document.createElement("button");
    btnCambiarDatos.className = "secundario";
    btnCambiarDatos.textContent = "🧹 Cambiar mis datos (nombre/celular)";
    btnCambiarDatos.onclick = () => {
      cont.remove();
      $cuerpo.innerHTML = "";
      enviar({ type: "reiniciar_datos", payload: {} });
    };

    cont.appendChild(btnNueva);
    cont.appendChild(btnCambiarDatos);
    $cuerpo.appendChild(cont);
    scrollAlFinal();
  }

  // Botón "📍 Compartir mi ubicación" (opción con accion:"ubicacion", ver bot_engine.py captura_ubicacion).
  // Pide la posición con alta precisión y se la manda al servidor; si falla (permiso denegado, sin señal,
  // tiempo agotado, navegador sin soporte) avisa el motivo y el servidor responde con un mensaje claro
  // y el botón de nuevo, para reintentar.
  function pedirUbicacion(btn, cont) {
    const enviarError = (motivo) => {
      enviar({ type: "ubicacion_error", payload: { motivo: motivo } });
      cont.remove();
    };
    if (!navigator.geolocation) { enviarError("no_soportado"); return; }
    const textoOriginal = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ Obteniendo tu ubicación…";
    // Por si el navegador nunca llama a ninguno de los dos callbacks (pasa con algunos permisos en espera).
    const respaldo = setTimeout(() => {
      btn.disabled = false;
      btn.textContent = textoOriginal;
    }, 30000);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        clearTimeout(respaldo);
        agregarMensaje("📍 Ubicación compartida", "usuario");
        enviar({ type: "ubicacion_compartida", payload: {
          lat: pos.coords.latitude, lng: pos.coords.longitude, precision: pos.coords.accuracy } });
        cont.remove();
      },
      (err) => {
        clearTimeout(respaldo);
        enviarError(err && err.code === 1 ? "denegado" : err && err.code === 3 ? "timeout" : "no_disponible");
      },
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 }
    );
  }

  // "🗺️ Elegir en el mapa": abre /mapa/mapa.html (Leaflet + OpenStreetMap, servido por la API) en un iframe que cubre
  // el panel. Leaflet necesita su CSS y el Shadow DOM del widget no lo recibiría; el iframe lo evita. La página devuelve
  // lat/lng por postMessage; solo se aceptan mensajes de ESE iframe y del origen de la API.
  let mapaAbierto = null;
  function abrirMapa(cont) {
    if (mapaAbierto) return;
    let origenApi;
    try { origenApi = new URL(API_BASE || window.location.origin, window.location.href).origin; }
    catch (e) { origenApi = window.location.origin; }
    const overlay = document.createElement("div");
    overlay.className = "mapa-overlay";
    const iframe = document.createElement("iframe");
    iframe.setAttribute("allow", "geolocation");
    iframe.setAttribute("title", "Elegir el lugar en el mapa");
    iframe.src = `${origenApi}/mapa/mapa.html`;
    overlay.appendChild(iframe);
    const cerrar = () => {
      window.removeEventListener("message", alMensaje);
      overlay.remove();
      mapaAbierto = null;
    };
    function alMensaje(e) {
      if (e.source !== iframe.contentWindow || e.origin !== origenApi) return;
      const d = e.data || {};
      if (d.tipo !== "sanitas_mapa") return;
      if (d.accion === "cancelar") { cerrar(); return; }
      if (d.accion === "confirmar") {
        const lat = Number(d.lat), lng = Number(d.lng);
        if (!isFinite(lat) || !isFinite(lng)) return;
        cerrar();
        agregarMensaje("🗺️ Lugar elegido en el mapa", "usuario");
        enviar({ type: "ubicacion_compartida", payload: { lat: lat, lng: lng, precision: null, origen: "mapa" } });
        cont.remove();
      }
    }
    window.addEventListener("message", alMensaje);
    mapaAbierto = overlay;
    $panel.appendChild(overlay);
  }

  function agregarOpciones(opciones) {
    const cont = document.createElement("div");
    cont.className = "opciones";
    (opciones || []).forEach((op) => {
      const btn = document.createElement("button");
      btn.textContent = op.label;
      if (op.accion === "ubicacion") {
        btn.onclick = () => pedirUbicacion(btn, cont);
        cont.appendChild(btn);
        return;
      }
      if (op.accion === "mapa") {
        btn.onclick = () => abrirMapa(cont);
        cont.appendChild(btn);
        return;
      }
      btn.onclick = () => {
        agregarMensaje(op.label, "usuario");
        enviar({ type: "click_opcion", payload: { opcion_codigo: op.id || op.label } });
        cont.remove();
      };
      cont.appendChild(btn);
    });
    $cuerpo.appendChild(cont);
    scrollAlFinal();
  }

  // Botones de "Reenviar código"/"Llamarme" durante la verificación de
  // celular (nodo webchat_valida_pide_codigo). No se quitan solos al hacer
  // clic (a diferencia de agregarOpciones) porque la persona puede querer
  // reenviar más de una vez; en vez de eso se reemplazan por completo cada
  // vez que llega un nuevo mensaje_bot, para no ir apilando botones viejos
  // cuando el flujo vuelve a pedir el código (ver ws.onmessage).
  let $otpAccionesActual = null;
  let otpTimerId = null;

  // Antes de que se pueda pedir un reenvío hay que esperar este tiempo —
  // a veces el SMS solo tarda unos segundos en llegar y reenviar de
  // inmediato es innecesario. Se muestra como cuenta regresiva en el botón.
  const OTP_ESPERA_REENVIO_SEGUNDOS = 30;

  // --- Verificación por WhatsApp (respaldo del SMS) --------------------------
  const WA_APARECE_SEGUNDOS = 30;   // tras este tiempo sin SMS se ofrece "Verificar con WhatsApp"
  const WA_SONDEO_MS = 3000;
  let waTimerAparece = null;
  let waSondeoId = null;
  let waExpiraId = null;
  let waCard = null;
  let waBotonVisible = false;
  let waCodigoActual = null;

  function waDetenerTimers() {
    if (waTimerAparece) { clearTimeout(waTimerAparece); waTimerAparece = null; }
    if (waSondeoId) { clearInterval(waSondeoId); waSondeoId = null; }
    if (waExpiraId) { clearTimeout(waExpiraId); waExpiraId = null; }
  }

  function waLimpiar() {
    waDetenerTimers();
    if (waCard) { waCard.remove(); waCard = null; }
    waBotonVisible = false;
    waCodigoActual = null;
  }

  function waPedirCodigo() {
    waDetenerTimers();
    waCodigoActual = null;
    if (waCard) waCard.remove();
    waCard = document.createElement("div");
    waCard.className = "wa-card";
    waCard.innerHTML = '<h4>Verificar con WhatsApp</h4><p>Preparando tu código…</p>';
    $cuerpo.appendChild(waCard);
    scrollAlFinal();
    enviar({ type: "iniciar_verificacion_whatsapp", payload: { con_qr: esComputador } });
  }

  function waMostrarCodigo(p) {
    if (!waCard) return;
    waCodigoActual = p.codigo;
    waCard.innerHTML = "";
    const h = document.createElement("h4");
    h.textContent = "💬 Verifica con WhatsApp";
    waCard.appendChild(h);

    const instr = document.createElement("p");
    if (esComputador && p.qr) {
      instr.textContent = "Escanea este QR con tu celular y envía el mensaje que aparece, sin cambiarlo. " +
        "Debe ser desde el celular que registraste.";
      waCard.appendChild(instr);
      const img = document.createElement("img");
      img.src = p.qr;
      img.alt = "Código QR para verificar por WhatsApp";
      waCard.appendChild(img);
    } else {
      instr.textContent = "Toca el botón, se abrirá WhatsApp con el mensaje listo. Envíalo sin cambiarlo, " +
        "desde el celular que registraste.";
      waCard.appendChild(instr);
      const a = document.createElement("a");
      a.className = "wa-abrir";
      a.href = p.enlace;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Abrir WhatsApp";
      waCard.appendChild(a);
    }
    const cod = document.createElement("div");
    cod.className = "wa-codigo";
    cod.textContent = p.texto;
    waCard.appendChild(cod);
    const est = document.createElement("div");
    est.className = "wa-estado";
    est.textContent = "⏳ Esperando confirmación…";
    waCard.appendChild(est);
    scrollAlFinal();

    waSondeoId = setInterval(() => {
      if (waCodigoActual) enviar({ type: "consultar_verificacion_whatsapp", payload: { codigo: waCodigoActual } });
    }, WA_SONDEO_MS);
    waExpiraId = setTimeout(() => waVencido(), (p.expira_seg || 300) * 1000);
  }

  function waVencido(texto) {
    if (!waCard) return;
    waDetenerTimers();
    waCodigoActual = null;
    waCard.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = texto || "El código venció. Puedes generar uno nuevo.";
    waCard.appendChild(p);
    const b = document.createElement("button");
    b.className = "wa-nuevo";
    b.textContent = "Generar código nuevo";
    b.onclick = () => waPedirCodigo();
    waCard.appendChild(b);
  }

  function waCompletado() {
    waDetenerTimers();
    waCodigoActual = null;
    if (waCard) {
      waCard.innerHTML = '<div class="wa-estado ok">✅ Celular verificado</div>';
      const c = waCard;
      setTimeout(() => { if (waCard === c) { c.remove(); waCard = null; } }, 4000);
    }
    waBotonVisible = false;
  }

  function quitarAccionesOtp() {
    if (waTimerAparece) { clearTimeout(waTimerAparece); waTimerAparece = null; }
    if (otpTimerId) {
      clearInterval(otpTimerId);
      otpTimerId = null;
    }
    if ($otpAccionesActual) {
      $otpAccionesActual.remove();
      $otpAccionesActual = null;
    }
  }

  function agregarAccionesOtp(reenvios, puedeLlamar) {
    quitarAccionesOtp();
    const cont = document.createElement("div");
    cont.className = "opciones";

    const btnReenviar = document.createElement("button");
    btnReenviar.disabled = true;
    btnReenviar.onclick = () => {
      btnReenviar.disabled = true;
      enviar({ type: "reenviar_otp", payload: {} });
    };
    cont.appendChild(btnReenviar);

    // "Token por llamada": siempre disponible (ya no hay que reenviar dos veces antes);
    // se habilita junto con "Reenviar" para darle tiempo al SMS de llegar.
    const btnLlamar = document.createElement("button");
    btnLlamar.disabled = true;
    btnLlamar.onclick = () => {
      btnLlamar.disabled = true;
      agregarMensaje("📞 Token por llamada", "usuario");
      enviar({ type: "solicitar_llamada_otp", payload: {} });
    };
    cont.appendChild(btnLlamar);

    let restante = OTP_ESPERA_REENVIO_SEGUNDOS;
    const actualizarCuentaRegresiva = () => {
      if (restante <= 0) {
        btnReenviar.textContent = "🔁 Reenviar código";
        btnReenviar.disabled = false;
        btnLlamar.textContent = "📞 Token por llamada";
        btnLlamar.disabled = false;
        clearInterval(otpTimerId);
        otpTimerId = null;
        return;
      }
      btnReenviar.textContent = `🔁 Reenviar código (${restante}s)`;
      btnLlamar.textContent = `📞 Token por llamada (${restante}s)`;
      restante--;
    };
    actualizarCuentaRegresiva();
    otpTimerId = setInterval(actualizarCuentaRegresiva, 1000);

    // "Verificar con WhatsApp": aparece a los WA_APARECE_SEGUNDOS sin que llegue el SMS
    // (o de inmediato si ya había aparecido antes en esta verificación).
    const btnWhatsapp = document.createElement("button");
    btnWhatsapp.textContent = "💬 Verificar con WhatsApp";
    btnWhatsapp.style.display = "none";
    btnWhatsapp.onclick = () => {
      agregarMensaje("💬 Verificar con WhatsApp", "usuario");
      waPedirCodigo();
    };
    cont.appendChild(btnWhatsapp);
    const mostrarBotonWa = () => { waBotonVisible = true; btnWhatsapp.style.display = ""; scrollAlFinal(); };
    if (waBotonVisible) mostrarBotonWa();
    else waTimerAparece = setTimeout(mostrarBotonWa, WA_APARECE_SEGUNDOS * 1000);

    // A diferencia de "Reenviar"/"Llamar", este botón está disponible desde
    // el primer intento (sin esperar ni contador) -- si la persona se dio
    // cuenta de que escribió mal su celular, no tiene sentido hacerla
    // esperar un reenvío que de todas formas va a fallar.
    const btnCambiarCelular = document.createElement("button");
    btnCambiarCelular.textContent = "✏️ Cambiar celular";
    btnCambiarCelular.onclick = () => {
      btnCambiarCelular.disabled = true;
      agregarMensaje("✏️ Cambiar celular", "usuario");
      enviar({ type: "modificar_celular", payload: {} });
    };
    cont.appendChild(btnCambiarCelular);

    $cuerpo.appendChild(cont);
    $otpAccionesActual = cont;
    scrollAlFinal();
  }

  // Borrador del formulario en curso — se guarda en localStorage (sobrevive a
  // reconexiones y recargas de página, aunque no se comparte entre
  // navegadores/dispositivos). Se identifica por la lista de variables del
  // formulario, para no restaurar datos de un formulario distinto.
  const CLAVE_BORRADOR_FORMULARIO = "sanitas_webchat_formulario_borrador";

  function guardarBorradorFormulario(campos, valores) {
    try {
      localStorage.setItem(CLAVE_BORRADOR_FORMULARIO, JSON.stringify({
        variables: (campos || []).map((c) => c.variable),
        valores,
      }));
    } catch (e) { /* localStorage lleno/bloqueado — no es crítico, se sigue sin borrador */ }
  }

  function leerBorradorFormulario(campos) {
    try {
      const guardado = JSON.parse(localStorage.getItem(CLAVE_BORRADOR_FORMULARIO) || "null");
      if (!guardado) return null;
      const variablesActuales = (campos || []).map((c) => c.variable).join(",");
      if (guardado.variables.join(",") !== variablesActuales) return null; // es de otro formulario
      return guardado.valores || null;
    } catch (e) {
      return null;
    }
  }

  function borrarBorradorFormulario() {
    try { localStorage.removeItem(CLAVE_BORRADOR_FORMULARIO); } catch (e) { /* no crítico */ }
  }

  function agregarFormulario(campos) {
    const form = document.createElement("form");
    form.className = "formulario";
    const borrador = leerBorradorFormulario(campos);

    function guardarBorradorActual() {
      const valores = {};
      (campos || []).forEach((c) => { valores[c.variable] = form.elements[c.variable].value; });
      guardarBorradorFormulario(campos, valores);
    }

    (campos || []).forEach((campo) => {
      const label = document.createElement("label");
      label.textContent = campo.label + (campo.obligatorio ? " *" : "");
      form.appendChild(label);

      if (campo.tipo === "horario_semanal") {
        // Compuesto (día desde/hasta + hora desde/hasta) en vez de texto
        // libre, para que quede siempre en un formato consistente
        // ("Lunes a Viernes de 07:00 a 17:00") sin que el usuario lo escriba
        // mal a mano.
        const DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"];
        const cont = document.createElement("div");
        cont.className = "campo-compuesto";
        cont.style.cssText = "display:flex;flex-wrap:wrap;gap:5px;align-items:center;";

        const selDesde = document.createElement("select");
        const selHasta = document.createElement("select");
        DIAS_SEMANA.forEach((d) => {
          const o1 = document.createElement("option"); o1.value = d; o1.textContent = d; selDesde.appendChild(o1);
          const o2 = document.createElement("option"); o2.value = d; o2.textContent = d; selHasta.appendChild(o2);
        });
        const horaDesde = document.createElement("input"); horaDesde.type = "time";
        const horaHasta = document.createElement("input"); horaHasta.type = "time";
        selDesde.value = "Lunes";
        selHasta.value = "Viernes";
        horaDesde.value = "07:00";
        horaHasta.value = "17:00";

        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = campo.variable;

        function recomponerHorario() {
          hidden.value = selDesde.value === selHasta.value
            ? `${selDesde.value} de ${horaDesde.value} a ${horaHasta.value}`
            : `${selDesde.value} a ${selHasta.value} de ${horaDesde.value} a ${horaHasta.value}`;
        }

        // Si había un borrador guardado, intenta reconstruir los selectores
        // a partir del texto compuesto guardado (ej. "Lunes a Viernes de
        // 07:00 a 17:00"); si no calza el formato, se queda con los valores
        // por defecto de arriba.
        if (borrador && borrador[campo.variable]) {
          const m = /^(.+?) de (\d{2}:\d{2}) a (\d{2}:\d{2})$/.exec(borrador[campo.variable]);
          if (m) {
            const rango = /^(.+?) a (.+)$/.exec(m[1]);
            if (rango && DIAS_SEMANA.includes(rango[1]) && DIAS_SEMANA.includes(rango[2])) {
              selDesde.value = rango[1];
              selHasta.value = rango[2];
            } else if (DIAS_SEMANA.includes(m[1])) {
              selDesde.value = m[1];
              selHasta.value = m[1];
            }
            horaDesde.value = m[2];
            horaHasta.value = m[3];
          }
        }

        [selDesde, selHasta, horaDesde, horaHasta].forEach((el) => {
          el.addEventListener("change", () => { recomponerHorario(); guardarBorradorActual(); });
        });
        recomponerHorario();

        cont.appendChild(selDesde);
        cont.appendChild(document.createTextNode(" a "));
        cont.appendChild(selHasta);
        cont.appendChild(document.createTextNode(" de "));
        cont.appendChild(horaDesde);
        cont.appendChild(document.createTextNode(" a "));
        cont.appendChild(horaHasta);
        cont.appendChild(hidden);
        form.appendChild(cont);
        return;
      }

      if (campo.tipo === "fecha_hora_programada") {
        // Día (hoy hasta +14) y hora en 24 h, SIEMPRE en hora de Lima (no la del dispositivo). Deja en un campo
        // oculto "AAAA-MM-DDTHH:MM", el formato que espera el servidor.
        const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
        const DIAS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
        const ahoraLima = () => {
          const p = {};
          new Intl.DateTimeFormat("en-GB", { timeZone: "America/Lima", hourCycle: "h23", year: "numeric", month: "2-digit",
            day: "2-digit", hour: "2-digit", minute: "2-digit" }).formatToParts(new Date()).forEach((x) => { p[x.type] = x.value; });
          return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour, mi: +p.minute };
        };
        const base = ahoraLima();
        const pad = (n) => String(n).padStart(2, "0");
        const cont = document.createElement("div");
        cont.className = "campo-compuesto";
        cont.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;";
        const selDia = document.createElement("select");
        for (let i = 0; i <= 14; i++) {
          const f = new Date(Date.UTC(base.y, base.m - 1, base.d + i));
          const o = document.createElement("option");
          o.value = `${f.getUTCFullYear()}-${pad(f.getUTCMonth() + 1)}-${pad(f.getUTCDate())}`;
          o.textContent = (i === 0 ? "Hoy" : i === 1 ? "Mañana" : DIAS[f.getUTCDay()]) + ` ${f.getUTCDate()} ${MESES[f.getUTCMonth()]}`;
          selDia.appendChild(o);
        }
        const selHora = document.createElement("select");
        const selMin = document.createElement("select");
        for (let h = 0; h < 24; h++) { const o = document.createElement("option"); o.value = pad(h); o.textContent = pad(h); selHora.appendChild(o); }
        ["00", "15", "30", "45"].forEach((m) => { const o = document.createElement("option"); o.value = m; o.textContent = m; selMin.appendChild(o); });
        // Sugerencia inicial: dentro de ~2 horas, redondeada al próximo cuarto de hora.
        let t = base.h * 60 + base.mi + 120;
        t = Math.ceil(t / 15) * 15;
        selDia.selectedIndex = Math.min(14, Math.floor(t / 1440));
        t = t % 1440;
        selHora.value = pad(Math.floor(t / 60));
        selMin.value = pad(t % 60);
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = campo.variable;
        const recomponer = () => { hidden.value = `${selDia.value}T${selHora.value}:${selMin.value}`; };
        if (borrador && borrador[campo.variable]) {
          const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})$/.exec(borrador[campo.variable]);
          if (m && [...selDia.options].some((o) => o.value === m[1]) && ["00", "15", "30", "45"].includes(m[3])) {
            selDia.value = m[1]; selHora.value = m[2]; selMin.value = m[3];
          }
        }
        [selDia, selHora, selMin].forEach((el) => el.addEventListener("change", () => { recomponer(); guardarBorradorActual(); }));
        recomponer();
        const dos = document.createElement("span");
        dos.textContent = ":";
        cont.appendChild(selDia);
        cont.appendChild(selHora);
        cont.appendChild(dos);
        cont.appendChild(selMin);
        cont.appendChild(hidden);
        form.appendChild(cont);
        const nota = document.createElement("small");
        nota.textContent = "Hora de Lima, formato 24 h.";
        nota.style.cssText = "display:block;color:#5b6b7b;margin-top:2px;";
        form.appendChild(nota);
        return;
      }

      let input;
      if (campo.tipo === "textarea") {
        input = document.createElement("textarea");
        input.rows = 3;
      } else if (campo.tipo === "opciones") {
        input = document.createElement("select");
        (campo.opciones || []).forEach((op) => {
          const opt = document.createElement("option");
          opt.value = op;
          opt.textContent = op;
          input.appendChild(opt);
        });
      } else if (campo.tipo === "fecha_hora") {
        input = document.createElement("input");
        input.type = "datetime-local";
      } else if (campo.tipo === "fecha") {
        input = document.createElement("input");
        input.type = "date";
      } else {
        input = document.createElement("input");
        input.type =
          campo.tipo === "numero" || campo.tipo === "telefono"
            ? "tel"
            : campo.tipo === "correo"
            ? "email"
            : "text";
      }
      input.name = campo.variable;
      input.required = !!campo.obligatorio;
      if (borrador && borrador[campo.variable] !== undefined) input.value = borrador[campo.variable];
      input.addEventListener("input", guardarBorradorActual);
      input.addEventListener("change", guardarBorradorActual);
      form.appendChild(input);
    });

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = "Enviar";
    form.appendChild(btn);

    form.onsubmit = (e) => {
      e.preventDefault();
      const valores = {};
      (campos || []).forEach((c) => {
        let v = form.elements[c.variable].value;
        if (c.tipo === "fecha_hora" && v) {
          // "2026-09-25T14:30" (formato nativo del input) -> "25/09/2026 14:30"
          const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(v);
          if (m) v = `${m[3]}/${m[2]}/${m[1]} ${m[4]}`;
        } else if (c.tipo === "fecha" && v) {
          // "2026-09-25" (formato nativo del input) -> "25/09/2026"
          const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
          if (m) v = `${m[3]}/${m[2]}/${m[1]}`;
        }
        valores[c.variable] = v;
      });
      agregarMensaje("📋 Formulario enviado", "usuario");
      enviar({ type: "formulario_completado", payload: { valores } });
      borrarBorradorFormulario();
      form.remove();
    };

    $cuerpo.appendChild(form);
    scrollAlFinal();
  }

  // ---------------------------------------------------------------------
  // WebSocket
  // ---------------------------------------------------------------------
  function conectar() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
    const sesionToken = localStorage.getItem("sanitas_webchat_sesion") || "";
    sesionTokenActual = sesionToken;
    const url = `${WS_BASE}/ws/widget${sesionToken ? "?sesion_token=" + encodeURIComponent(sesionToken) : ""}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      conexionLista = false;
      intentosReconexion = 0;
      reconectando = false;
      // Solo informa el origen en la primera conexión de esta carga de página
      // (el servidor lo guarda únicamente si la conversación todavía no lo tiene).
      try {
        ws.send(JSON.stringify({ type: "registrar_origen",
                                 payload: { origen: origenApertura, pagina: window.location.pathname } }));
      } catch (e) { /* es solo una métrica */ }
      $estadoTexto.textContent = "En línea";
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
      actualizarIconoEnviar();
    };

    // Latido: sin tráfico por un rato, los balanceadores/navegadores cortan la
    // conexión y el widget tiene que reconectarse. Un mensaje liviano cada 25 s
    // la mantiene abierta.
    const latido = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "ping", payload: {} })); } catch (e) { /* se reconecta solo */ }
      }
    }, 25000);

    ws.onclose = () => {
      clearInterval(latido);
      quitarEscribiendo();
      ws = null;
      conexionLista = false;
      if (cerradoManualmente || movidoAOtroDispositivo) {
        $textoInput.disabled = true;
        $enviarBtn.disabled = true;
        return;
      }
      // Sin red el campo sigue habilitado: lo que escriba queda en cola y se envía al reconectar.

      reconectando = true;
      $estadoTexto.textContent = outbox.length ? "Sin conexión: se enviará al reconectar" : "Reconectando...";
      const espera = siguienteEsperaReconexion();
      intentosReconexion++;
      setTimeout(() => {
        if (!cerradoManualmente) conectar();
      }, espera);
    };

    ws.onerror = () => {
      $estadoTexto.textContent = "Error de conexión";
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const p = data.payload || {};
      // Cualquier evento (menos la confirmación de recibido) significa que ya llegó la respuesta.
      if (data.type !== "escribiendo" && data.type !== "mensaje_recibido_servidor") quitarEscribiendo();
      switch (data.type) {
        case "escribiendo":
          mostrarEscribiendo();
          break;
        case "aviso_cola":
          agregarMensaje(p.texto, "sistema");
          break;
        case "traspaso_listo":
          $qrImg.src = p.qr;
          $qrImg.style.display = "";
          $qrEstado.textContent = `Válido por ${Math.round(p.expira_seg / 60)} minutos y de un solo uso.`;
          clearTimeout(temporizadorQr);
          temporizadorQr = setTimeout(() => {
            $qrImg.style.display = "none";
            $qrEstado.textContent = "El código venció.";
            $qrRegenerar.style.display = "";
          }, p.expira_seg * 1000);
          break;
        case "traspaso_error":
          $qrEstado.textContent = "No pudimos generar el código. Inténtalo de nuevo.";
          $qrRegenerar.style.display = "";
          break;
        case "sesion_en_otro_dispositivo": {
          movidoAOtroDispositivo = true;
          $qrPanel.classList.remove("visible");
          $textoInput.disabled = true;
          $enviarBtn.disabled = true;
          $estadoTexto.textContent = "Abierta en otro dispositivo";
          const aviso = document.createElement("div");
          aviso.className = "acciones-cierre";
          const texto = document.createElement("div");
          texto.className = "msg sistema";
          texto.textContent = "📱 Tu conversación continúa en otro dispositivo.";
          const btn = document.createElement("button");
          btn.textContent = "Continuar aquí";
          btn.onclick = () => { aviso.remove(); movidoAOtroDispositivo = false; conectar(); };
          aviso.appendChild(texto);
          aviso.appendChild(btn);
          $cuerpo.appendChild(aviso);
          scrollAlFinal();
          break;
        }
        case "sesion_iniciada":
          $infoGuardada.classList.add("visible");
          if (!guardadaMostradaAlAbrir) { guardadaMostradaAlAbrir = true; mostrarGuardada(); }
          localStorage.setItem("sanitas_webchat_sesion", p.sesion_token);
          sesionTokenActual = p.sesion_token;
          conversacionIdActual = p.conversacion_id;
          resetPrevio();
          revisarHistorialPrevio(1200);
          mostrarNoSoyYo(p.reconocido);
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          // Si es una reconexión, enseguida llega el "historial" (que redibuja todo y marca la conexión
          // lista). Si no llega, se da por lista y se envía lo pendiente.
          clearTimeout(esperaHistorial);
          esperaHistorial = setTimeout(() => marcarConexionLista(false), 800);
          break;
        case "otp_acciones":
          // Tras reconectar mientras esperaba el código SMS: repone los botones.
          agregarAccionesOtp(p.otp_reenvios || 0, !!p.otp_puede_llamar);
          scrollAlFinal();
          break;
        case "historial":
          // Reconstruye la pantalla tras una reconexión (deploy, corte de red, etc.)
          $cuerpo.innerHTML = "";
          resetPrevio();
          (p.mensajes || []).forEach((msg, i, arr) => agregarMensajeHistorial(msg, i === arr.length - 1));
          scrollAlFinal();
          revisarHistorialPrevio(500);
          marcarConexionLista(true);
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
          break;
        case "historial_previo":
          mostrarHistorialPrevio(p);
          break;
        case "mensaje_bot":
          agregarMensaje(p.texto, "bot", p.imagen_url);
          if (p.opciones) agregarOpciones(p.opciones);
          // "codigo_nodo" llega en TODO captura_dato (ver bot_engine.py);
          // acá solo nos importa cuando es el que pide el código OTP, para
          // mostrarle a la persona el botón de reenviar/llamar. El reenvío
          // y la llamada responden con otp_reenvios/otp_puede_llamar en vez
          // de codigo_nodo (no pasan por bot_engine), por eso se chequea
          // cualquiera de los dos. En cualquier otro mensaje (ya avanzó a
          // otro paso, cerró, lo derivó a un asesor) se quitan los botones.
          if (p.codigo_nodo === "webchat_valida_pide_codigo" || p.otp_reenvios !== undefined) {
            agregarAccionesOtp(p.otp_reenvios || 0, !!p.otp_puede_llamar);
          } else {
            quitarAccionesOtp();
            if (!waCard || !waCodigoActual) waLimpiar();
          }
          // OJO: aquí NO se apaga atendidoPorAsesor. Al asignar, el servidor manda
          // "asignado" y enseguida el mensaje de bienvenida del asesor como
          // "mensaje_bot"; apagarlo aquí hacía desaparecer el micrófono. Los
          // estados "en_cola", "conversacion_cerrada" y "sesion_iniciada" ya lo reinician.
          break;
        case "verificacion_whatsapp_lista":
          waMostrarCodigo(p);
          break;
        case "verificacion_whatsapp_ok":
          if (waCodigoActual) enviar({ type: "consultar_verificacion_whatsapp", payload: { codigo: waCodigoActual } });
          break;
        case "verificacion_whatsapp_completada":
          waCompletado();
          break;
        case "verificacion_whatsapp_error":
          if (p.motivo === "demasiados") waVencido("Generaste muchos códigos seguidos. Espera unos minutos o usa el código del SMS.");
          else if (p.motivo === "vencido") waVencido();
          else waVencido("No pudimos usar WhatsApp en este momento. Usa el código del SMS o inténtalo de nuevo.");
          break;
        case "mostrar_formulario":
          if (p.texto) agregarMensaje(p.texto, "bot");
          agregarFormulario(p.campos);
          break;
        case "mensaje_asesor":
          agregarMensaje(p.asesor_nombre ? `${p.asesor_nombre}: ${p.texto}` : p.texto, "bot", null, null,
                         { mensajeId: p.mensaje_id, respondidoA: p.respondido_a });
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
          avisarMensajeNuevoSiNoMiraPantalla();
          break;
        case "archivo_asesor":
          agregarMensaje("", "bot", urlArchivoWidget(p.ruta), p.ruta, { mensajeId: p.mensaje_id });
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
          avisarMensajeNuevoSiNoMiraPantalla();
          break;
        case "mensajes_leidos":
          marcarMensajesLeidos();
          break;
        case "mensaje_recibido_servidor":
          if (p.cid) confirmarEntrada(p);
          else marcarMensajeRecibido(p.mensaje_id, p.recibido);
          break;
        case "mensaje_editado": {
          const divEditado = $cuerpo.querySelector(`[data-id="${p.mensaje_id}"]`);
          if (divEditado) {
            const span = divEditado.querySelector(".texto-mensaje");
            if (span) span.innerHTML = formatearTexto(p.texto);
            if (!divEditado.querySelector(".editado-tag")) {
              const hora = divEditado.querySelector(".hora");
              const tag = document.createElement("span");
              tag.className = "editado-tag";
              tag.textContent = "(editado)";
              if (hora) hora.before(tag); else divEditado.appendChild(tag);
            }
          }
          break;
        }
        case "mensaje_reaccion": {
          const divReaccion = $cuerpo.querySelector(`[data-id="${p.mensaje_id}"]`);
          if (divReaccion) pintarReacciones(divReaccion, p.reacciones);
          break;
        }
        case "mensaje_eliminado": {
          const divEliminado = $cuerpo.querySelector(`[data-id="${p.mensaje_id}"]`);
          if (divEliminado) {
            divEliminado.classList.add("eliminado");
            const span = divEliminado.querySelector(".texto-mensaje");
            if (span) span.textContent = "🚫 Este mensaje se eliminó";
            divEliminado.querySelectorAll(".msg-imagen, .msg-archivo-link, .msg-cita").forEach((el) => el.remove());
            const menu = divEliminado.querySelector(".msg-menu");
            const acciones = divEliminado.querySelector(".msg-acciones");
            if (menu) menu.remove();
            if (acciones) acciones.remove();
          }
          break;
        }
        case "en_cola":
          $estadoTexto.textContent = "En cola";
          agregarMensaje("⏳ En breve te atenderá un asesor...", "sistema");
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          break;
        case "asignado":
          $estadoTexto.textContent = `Con ${p.asesor_nombre}`;
          agregarMensaje(`✅ Te está atendiendo ${p.asesor_nombre}`, "sistema");
          atendidoPorAsesor = true;
          actualizarIconoEnviar();
          break;
        case "conversacion_cerrada":
          ocultarGuardada();
          $infoGuardada.classList.remove("visible");
          agregarMensaje("— Conversación finalizada —", "sistema");
          agregarAccionesCierre();
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          break;
      }
    };
  }

  // ---------------------------------------------------------------------
  // Envío fiable (como WhatsApp): lo que la persona escribe sin red NO se pierde.
  // Se guarda en una cola (también en el navegador, por si recarga la página) y se
  // reenvía al reconectar. Cada envío lleva un "cid": si el servidor ya lo había
  // procesado (se cortó justo antes de confirmar), responde "dup" y no se duplica.
  // ---------------------------------------------------------------------
  const TIPOS_FIABLES = { mensaje_usuario: 1, click_opcion: 1, archivo_subido: 1, formulario_completado: 1 };
  const CLAVE_OUTBOX = "sanitas_webchat_outbox";
  const VIGENCIA_OUTBOX_MS = 6 * 60 * 60 * 1000;
  let outbox = [];                 // [{cid, obj, div, ts, enviadoEn, rerender}]
  let conexionLista = false;       // true cuando el servidor ya nos devolvió el estado tras (re)conectar
  let esperaHistorial = null;
  let ultimaBurbujaUsuario = null, ultimaBurbujaUsuarioTs = 0;

  function guardarOutbox() {
    try {
      localStorage.setItem(CLAVE_OUTBOX, JSON.stringify(outbox.map(e => ({ cid: e.cid, obj: e.obj, ts: e.ts }))));
    } catch (e) { /* sin almacenamiento: queda solo en memoria */ }
  }
  function cargarOutbox() {
    try {
      const guardado = JSON.parse(localStorage.getItem(CLAVE_OUTBOX) || "[]");
      const limite = Date.now() - VIGENCIA_OUTBOX_MS;
      outbox = (Array.isArray(guardado) ? guardado : [])
        .filter(e => e && e.cid && e.obj && e.ts > limite)
        .map(e => ({ cid: e.cid, obj: e.obj, div: null, ts: e.ts, enviadoEn: 0, rerender: false }));
    } catch (e) { outbox = []; }
  }
  function marcarCheck(div, texto) {
    const c = div && div.querySelector(".check");
    if (c && !c.classList.contains("leido") && c.textContent !== " ✓✓") c.textContent = texto;
  }
  function enviarEntrada(entrada) {
    if (!(ws && ws.readyState === WebSocket.OPEN)) return;
    try { ws.send(JSON.stringify(entrada.obj)); } catch (e) { return; }
    const marca = entrada.enviadoEn = Date.now();
    marcarCheck(entrada.div, " ✓");
    // Si en 10 s el servidor no confirmó, la conexión está muerta aunque parezca abierta
    // (pasa al perder la red en el celular): se cierra para que reconecte y reenvíe.
    setTimeout(() => {
      if (entrada.enviadoEn === marca && outbox.indexOf(entrada) !== -1 && ws && ws.readyState === WebSocket.OPEN) {
        try { ws.close(); } catch (e) { /* ya cerrada */ }
      }
    }, 10000);
  }
  function vaciarOutbox() {
    if (!conexionLista) return;
    outbox.slice().forEach(enviarEntrada);
  }
  function encolar(obj) {
    const cid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(16).slice(2);
    const completo = { type: obj.type, payload: Object.assign({}, obj.payload || {}, { cid }) };
    const reciente = ultimaBurbujaUsuario && (Date.now() - ultimaBurbujaUsuarioTs < 800);
    const entrada = { cid, obj: completo, div: reciente ? ultimaBurbujaUsuario : null, ts: Date.now(), enviadoEn: 0, rerender: false };
    outbox.push(entrada);
    guardarOutbox();
    if (conexionLista && ws && ws.readyState === WebSocket.OPEN) enviarEntrada(entrada);
    else marcarCheck(entrada.div, " 🕓");
  }
  // Tras reconstruir la pantalla con el historial del servidor, se vuelven a dibujar los mensajes
  // que todavía no se confirmaron, para que la persona los siga viendo.
  function dibujarPendientes() {
    outbox.forEach((e) => {
      e.div = null; e.rerender = true;
      if (e.obj.type === "mensaje_usuario" && e.obj.payload.texto) {
        agregarMensaje(e.obj.payload.texto, "usuario");
        e.div = ultimaBurbujaUsuario;
        marcarCheck(e.div, " 🕓");
      }
    });
  }
  function marcarConexionLista(desdeHistorial) {
    clearTimeout(esperaHistorial); esperaHistorial = null;
    conexionLista = true;
    if (desdeHistorial) { checksPendientesRecibido = []; divsPendientesId = []; dibujarPendientes(); scrollAlFinal(); }
    vaciarOutbox();
  }
  function confirmarEntrada(p) {
    const i = outbox.findIndex(e => e.cid === p.cid);
    if (i === -1) return true;     // ya confirmado antes
    const e = outbox.splice(i, 1)[0];
    guardarOutbox();
    if (p.dup) {
      // El servidor ya lo tenía (llegó antes del corte): la copia que redibujamos sobra.
      if (e.rerender && e.div && e.div.parentNode) e.div.remove();
      return true;
    }
    if (e.div) {
      const ic = checksPendientesRecibido.indexOf(e.div.querySelector(".check"));
      if (ic !== -1) checksPendientesRecibido.splice(ic, 1);
      const id = divsPendientesId.indexOf(e.div);
      if (id !== -1) divsPendientesId.splice(id, 1);
      if (p.recibido !== false) marcarCheck(e.div, " ✓✓");
      if (p.mensaje_id) e.div.dataset.id = String(p.mensaje_id);
    }
    return true;
  }

  function enviar(obj) {
    if (TIPOS_FIABLES[obj.type]) { encolar(obj); return; }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  cargarOutbox();
  // El navegador avisa cuando vuelve la red: se reconecta de inmediato (sin esperar el backoff) y,
  // si la conexión "abierta" estaba muerta, se fuerza a cerrarse para reenviar lo pendiente.
  window.addEventListener("offline", () => {
    conexionLista = false;
    if (!cerradoManualmente) $estadoTexto.textContent = "Sin conexión: se enviará al reconectar";
  });
  window.addEventListener("online", () => {
    if (cerradoManualmente || movidoAOtroDispositivo) return;
    if (ws && ws.readyState === WebSocket.OPEN) { try { ws.close(); } catch (e) { /* ya cerrada */ } }
    else if (!ws) { intentosReconexion = 0; conectar(); }
  });

  // ---------------------------------------------------------------------
  // Envío de texto
  // ---------------------------------------------------------------------
  function enviarTexto() {
    const texto = $textoInput.value.trim();
    if (!texto) return;
    const respondidoAId = respondiendoAId;
    agregarMensaje(texto, "usuario", null, null, { respondidoA: respondiendoAPreview });
    enviar({ type: "mensaje_usuario", payload: { texto, respondido_a_id: respondidoAId } });
    $textoInput.value = "";
    cancelarRespuesta();
    actualizarIconoEnviar();
  }

  // ---------------------------------------------------------------------
  // Notas de voz: el botón de la derecha hace de micrófono cuando la caja
  // de texto está vacía (igual que WhatsApp) — un clic empieza a grabar, y
  // mientras graba el mismo botón se pone rojo y sirve para detener y
  // mandar. Apenas la persona escribe algo, vuelve a ser la flechita ➤.
  // ---------------------------------------------------------------------
  const ICONO_ENVIAR = "➤";
  const ICONO_MIC = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
    <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
    <line x1="12" y1="19" x2="12" y2="23"/>
    <line x1="8" y1="23" x2="16" y2="23"/>
  </svg>`;

  function actualizarIconoEnviar() {
    if (grabandoAudio) return;
    const hayTexto = $textoInput.value.trim().length > 0;
    if (!hayTexto && !atendidoPorAsesor) {
      // Con el bot no hay nada que grabar/enviar -- se deja la flechita
      // apagada en vez de un micrófono que fallaría al tocarlo.
      $enviarBtn.innerHTML = ICONO_ENVIAR;
      $enviarBtn.disabled = true;
      $enviarBtn.setAttribute("aria-label", "Enviar mensaje");
      return;
    }
    $enviarBtn.disabled = false;
    $enviarBtn.innerHTML = hayTexto ? ICONO_ENVIAR : ICONO_MIC;
    $enviarBtn.setAttribute("aria-label", hayTexto ? "Enviar mensaje" : "Grabar nota de voz");
  }

  let grabandoAudio = false;
  let mediaRecorderWidget = null;
  let streamAudioWidget = null;
  let chunksAudioWidget = [];
  let inicioGrabacionMs = 0;
  let intervaloGrabacion = null;
  let timeoutGrabacionMax = null;
  let canceladoGrabacion = false;

  const VENTANA_GRABACION_MAX_MS = 2 * 60 * 1000;
  const MIME_CANDIDATOS_AUDIO = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg", "audio/mp4"];

  function elegirMimeAudio() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    for (const m of MIME_CANDIDATOS_AUDIO) {
      try { if (MediaRecorder.isTypeSupported(m)) return m; } catch (e) { /* ignorar */ }
    }
    return "";
  }

  function actualizarTiempoGrabacion() {
    const segundos = Math.max(0, Math.floor((Date.now() - inicioGrabacionMs) / 1000));
    const mm = Math.floor(segundos / 60);
    const ss = String(segundos % 60).padStart(2, "0");
    $grabacionTiempo.textContent = `${mm}:${ss}`;
  }

  async function iniciarGrabacionAudio() {
    if (grabandoAudio) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      agregarMensaje("⚠️ Este navegador no permite grabar audio.", "sistema");
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      agregarMensaje("⚠️ No se pudo acceder al micrófono. Revisa los permisos del navegador.", "sistema");
      return;
    }
    const mime = elegirMimeAudio();
    try {
      mediaRecorderWidget = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      agregarMensaje("⚠️ No se pudo iniciar la grabación de audio.", "sistema");
      return;
    }
    streamAudioWidget = stream;
    chunksAudioWidget = [];
    canceladoGrabacion = false;
    mediaRecorderWidget.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksAudioWidget.push(e.data); };
    mediaRecorderWidget.onstop = () => {
      streamAudioWidget.getTracks().forEach((t) => t.stop());
      const duracionMs = Date.now() - inicioGrabacionMs;
      if (!canceladoGrabacion && duracionMs >= 800 && chunksAudioWidget.length) {
        const blob = new Blob(chunksAudioWidget, { type: mediaRecorderWidget.mimeType || mime || "audio/webm" });
        subirAudio(blob, Math.round(duracionMs / 1000));
      }
      chunksAudioWidget = [];
    };
    mediaRecorderWidget.start();
    grabandoAudio = true;
    inicioGrabacionMs = Date.now();
    inicializarAudioWidget();
    $pie.classList.add("grabando");
    $enviarBtn.classList.add("grabando");
    $enviarBtn.innerHTML = ICONO_ENVIAR;
    $enviarBtn.setAttribute("aria-label", "Detener y enviar nota de voz");
    actualizarTiempoGrabacion();
    intervaloGrabacion = setInterval(actualizarTiempoGrabacion, 250);
    timeoutGrabacionMax = setTimeout(() => detenerGrabacionAudio(true), VENTANA_GRABACION_MAX_MS);
  }

  function detenerGrabacionAudio(enviarAudio) {
    if (!grabandoAudio) return;
    canceladoGrabacion = !enviarAudio;
    grabandoAudio = false;
    clearInterval(intervaloGrabacion);
    clearTimeout(timeoutGrabacionMax);
    $pie.classList.remove("grabando");
    $enviarBtn.classList.remove("grabando");
    actualizarIconoEnviar();
    if (mediaRecorderWidget && mediaRecorderWidget.state !== "inactive") {
      mediaRecorderWidget.stop();
    } else if (streamAudioWidget) {
      streamAudioWidget.getTracks().forEach((t) => t.stop());
    }
  }
  $grabacionCancelar.addEventListener("click", (e) => {
    e.stopPropagation();
    detenerGrabacionAudio(false);
  });

  async function subirAudio(blob, duracionSegundos) {
    const localUrl = URL.createObjectURL(blob);
    const respondidoAId = respondiendoAId;
    agregarMensaje("", "usuario", localUrl, "nota-de-voz.webm", { respondidoA: respondiendoAPreview });
    cancelarRespuesta();
    $textoInput.disabled = true;
    $enviarBtn.disabled = true;
    $adjuntarBtn.disabled = true;
    try {
      const tipo = (blob.type || "").split(";")[0];
      const extension = tipo === "audio/ogg" ? "ogg" : tipo === "audio/mp4" ? "m4a" : "webm";
      const formData = new FormData();
      formData.append("file", blob, `nota-de-voz-${Date.now()}.${extension}`);
      formData.append("conversacion_id", conversacionIdActual);
      formData.append("sesion_token", sesionTokenActual);
      const resp = await fetch(`${API_BASE}/upload`, { method: "POST", body: formData });
      if (!resp.ok) throw new Error("upload falló");
      const data = await resp.json();
      enviar({ type: "archivo_subido", payload: { url: data.url, ruta: data.ruta, respondido_a_id: respondidoAId } });
    } catch (e) {
      agregarMensaje("⚠️ No se pudo enviar la nota de voz. Intenta de nuevo.", "sistema");
    } finally {
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
      $adjuntarBtn.disabled = false;
      actualizarIconoEnviar();
    }
  }

  $enviarBtn.addEventListener("click", () => {
    if (grabandoAudio) { detenerGrabacionAudio(true); return; }
    if ($textoInput.value.trim().length > 0) { enviarTexto(); return; }
    if (!atendidoPorAsesor) return; // el bot no procesa notas de voz
    iniciarGrabacionAudio();
  });
  // La caja crece con el texto (salta de línea en vez de desplazar el texto a la derecha).
  const TEXTO_ALTO_MAX = 120;
  function ajustarAltoTexto() {
    $textoInput.style.height = "auto";
    const alto = $textoInput.scrollHeight;
    $textoInput.style.height = Math.max(40, Math.min(alto, TEXTO_ALTO_MAX)) + "px";
    $textoInput.style.overflowY = alto > TEXTO_ALTO_MAX ? "auto" : "hidden";
  }
  (function () { // también reacciona a cambios por código (.value = "")
    const desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
    Object.defineProperty($textoInput, "value", {
      get() { return desc.get.call($textoInput); },
      set(v) { desc.set.call($textoInput, v); ajustarAltoTexto(); },
      configurable: true,
    });
  })();
  $textoInput.addEventListener("input", () => { ajustarAltoTexto(); actualizarIconoEnviar(); });
  // Enter envía; Shift+Enter hace un salto de línea.
  $textoInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); enviarTexto(); }
  });
  actualizarIconoEnviar();

  // ---------------------------------------------------------------------
  // Adjuntos: click y drag&drop (con preventDefault para no perder la página)
  // ---------------------------------------------------------------------
  $adjuntarBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    $adjuntarMenu.classList.toggle("abierto");
  });
  root.querySelector('[data-tipo="documento"]').addEventListener("click", () => {
    $adjuntarMenu.classList.remove("abierto");
    $archivoInputDocumento.click();
  });
  // "Fotos": abre directo el selector de imágenes del celular (en Android, el selector de fotos con las más
  // recientes primero). El navegador no deja leer la galería por su cuenta, así que el usuario toca la foto.
  root.querySelector('[data-tipo="fotos"]').addEventListener("click", () => {
    $adjuntarMenu.classList.remove("abierto");
    $archivoInputFotos.click();
  });
  $archivoInputFotos.addEventListener("change", (e) => {
    if (e.target.files.length > 0) subirArchivo(e.target.files[0]);
    e.target.value = "";
  });
  root.querySelector('[data-tipo="camara"]').addEventListener("click", () => {
    $adjuntarMenu.classList.remove("abierto");
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      abrirCamara();
    } else {
      // Navegador muy antiguo sin soporte de cámara en vivo: cae de vuelta
      // al selector de archivos con "capture" (funciona en algunos celulares).
      $archivoInputCamara.click();
    }
  });
  document.addEventListener("click", (e) => {
    if (!e.composedPath().includes($adjuntarMenu) && !e.composedPath().includes($adjuntarBtn)) {
      $adjuntarMenu.classList.remove("abierto");
    }
  });
  $archivoInputDocumento.addEventListener("change", (e) => {
    if (e.target.files.length > 0) subirArchivo(e.target.files[0]);
    e.target.value = "";
  });
  $archivoInputCamara.addEventListener("change", (e) => {
    if (e.target.files.length > 0) subirArchivo(e.target.files[0]);
    e.target.value = "";
  });

  ["dragover", "drop"].forEach((evento) => {
    $cuerpo.addEventListener(evento, (e) => e.preventDefault());
  });
  $cuerpo.addEventListener("dragover", (e) => {
    e.preventDefault();
    $cuerpo.classList.add("arrastrando");
  });
  $cuerpo.addEventListener("dragleave", () => $cuerpo.classList.remove("arrastrando"));
  $cuerpo.addEventListener("drop", (e) => {
    e.preventDefault();
    $cuerpo.classList.remove("arrastrando");
    if (e.dataTransfer.files.length > 0) subirArchivo(e.dataTransfer.files[0]);
  });

  async function abrirCamara() {
    const overlay = document.createElement("div");
    overlay.className = "camara-overlay";
    overlay.innerHTML = `
      <div class="cam-vista">
        <video autoplay playsinline muted></video>
        <canvas class="cam-revision cam-oculto" aria-label="Foto tomada"></canvas>
      </div>
      <div class="cam-destello"></div>
      <div class="cam-arriba">
        <button type="button" class="cam-btn cam-cerrar" title="Cerrar (Esc)" aria-label="Cerrar cámara">✕</button>
        <button type="button" class="cam-btn cam-luz oculto" title="Luz" aria-label="Encender o apagar la luz">⚡</button>
      </div>
      <div class="cam-zoom">1.0x</div>
      <div class="cam-abajo cam-controles">
        <button type="button" class="cam-btn cam-galeria" title="Elegir de la galería" aria-label="Elegir una foto de la galería">🖼️</button>
        <button type="button" class="cam-disparo" title="Tomar foto (Espacio)" aria-label="Tomar foto"></button>
        <button type="button" class="cam-btn cam-girar oculto" title="Cambiar de cámara" aria-label="Cambiar entre cámara frontal y trasera">🔄</button>
      </div>
      <div class="cam-abajo cam-revision-botones cam-oculto">
        <button type="button" class="cam-repetir">↺ Repetir</button>
        <button type="button" class="cam-enviar">Enviar ➤</button>
      </div>
    `;
    root.appendChild(overlay);
    const $ = (sel) => overlay.querySelector(sel);
    const video = $("video");
    // El mismo canvas sirve de "foto congelada" en la revisión y de origen del JPG.
    const canvas = $(".cam-revision");
    const btnDisparo = $(".cam-disparo");
    const btnLuz = $(".cam-luz");
    const btnGirar = $(".cam-girar");
    const $zoom = $(".cam-zoom");
    const esMovil = host.classList.contains("movil");

    let stream = null;
    let track = null;
    let facing = "environment";
    let luzEncendida = false;
    let zoomCap = null;          // {min, max, step} si la cámara permite zoom
    let zoomActual = 1;
    let enRevision = false;
    let fotoPromesa = null;      // Promise<Blob|null> del JPG que se codifica mientras el cliente revisa
    let cerrada = false;

    async function iniciarStream(modo) {
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null; track = null;
      let nuevo;
      try {
        // Sin restricciones el navegador entrega 640x480. Se pide la máxima resolución que la cámara permita
        // (ideal 4K; el navegador baja a lo que soporte el dispositivo).
        nuevo = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: modo }, width: { ideal: 4096 }, height: { ideal: 2160 } },
        });
      } catch (e1) {
        nuevo = await navigator.mediaDevices.getUserMedia({ video: true });
      }
      if (cerrada) { nuevo.getTracks().forEach((t) => t.stop()); return; }
      stream = nuevo;
      track = stream.getVideoTracks()[0] || null;
      video.srcObject = stream;
      const ajustes = (track && track.getSettings) ? track.getSettings() : {};
      // Espejo como en WhatsApp: cámara frontal (o la webcam de la laptop, que no informa facingMode).
      const frontal = ajustes.facingMode ? ajustes.facingMode === "user" : (modo === "user" || !esMovil);
      overlay.classList.toggle("espejo", frontal);
      // Luz (linterna) y zoom: solo si el navegador/cámara lo permiten (Chrome en Android, sobre todo).
      const caps = (track && track.getCapabilities) ? track.getCapabilities() : {};
      luzEncendida = false;
      btnLuz.classList.remove("encendido");
      btnLuz.classList.toggle("oculto", !caps.torch);
      zoomCap = (caps.zoom && caps.zoom.max > caps.zoom.min) ? caps.zoom : null;
      zoomActual = zoomCap ? (ajustes.zoom || zoomCap.min) : 1;
      $zoom.classList.remove("visible");
    }

    try {
      await iniciarStream(facing);
    } catch (e) {
      overlay.remove();
      agregarMensaje("⚠️ No se pudo acceder a la cámara. Revisa los permisos del navegador.", "sistema");
      return;
    }
    // Botón para cambiar de cámara solo si hay más de una (los nombres se conocen recién con el permiso dado).
    try {
      const dispositivos = await navigator.mediaDevices.enumerateDevices();
      if (dispositivos.filter((d) => d.kind === "videoinput").length > 1) btnGirar.classList.remove("oculto");
    } catch (_) { /* sin botón de girar */ }

    function cerrarCamara() {
      cerrada = true;
      if (stream) stream.getTracks().forEach((t) => t.stop());
      fotoPromesa = null;
      document.removeEventListener("keydown", teclas, true);
      overlay.remove();
    }
    function modoCamara() {
      enRevision = false;
      fotoPromesa = null;
      canvas.classList.add("cam-oculto");
      video.classList.remove("cam-oculto");
      $(".cam-controles").classList.remove("cam-oculto");
      $(".cam-revision-botones").classList.add("cam-oculto");
      $(".cam-enviar").disabled = false;
      btnDisparo.disabled = false;
    }

    // Captura INSTANTÁNEA, como WhatsApp: se copia al canvas el cuadro que el video muestra en el momento
    // exacto del toque y se deja congelado en pantalla; el JPG se codifica por detrás mientras se revisa.
    // (Antes se usaba ImageCapture.takePhoto(): en Android reconfigura el sensor y reenfoca, tarda 1-2 s y
    // la foto salía de lo que la cámara veía DESPUÉS del toque.) El cuadro del video ya viene a la máxima
    // resolución que la cámara entrega en vivo (se pide 4K al abrirla).
    const MAX_BYTES_FOTO = 9.5 * 1024 * 1024; // el servidor acepta hasta 10 MB
    async function codificarJpg() {
      for (const calidad of [0.92, 0.85, 0.75]) {
        const b = await new Promise((r) => canvas.toBlob(r, "image/jpeg", calidad));
        if (b && b.size <= MAX_BYTES_FOTO) return b;
      }
      return null;
    }
    function tomarFoto() {
      if (btnDisparo.disabled || !track || !video.videoWidth) return;
      btnDisparo.disabled = true;
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);    // <- el instante del toque
      const destello = $(".cam-destello");
      destello.classList.add("activo");
      setTimeout(() => destello.classList.remove("activo"), 60);
      enRevision = true;
      canvas.classList.remove("cam-oculto");
      video.classList.add("cam-oculto");
      $(".cam-controles").classList.add("cam-oculto");
      $zoom.classList.remove("visible");
      $(".cam-revision-botones").classList.remove("cam-oculto");
      fotoPromesa = codificarJpg();
    }
    async function enviarFoto() {
      if (!enRevision || !fotoPromesa) return;
      const btnEnviar = $(".cam-enviar");
      btnEnviar.disabled = true;
      const promesa = fotoPromesa;
      const blob = await promesa;       // normalmente ya está lista: se codificó mientras se revisaba
      if (cerrada || promesa !== fotoPromesa) return;   // cerró o tocó "Repetir" mientras tanto
      if (!blob) {
        btnEnviar.disabled = false;
        agregarMensaje("⚠️ La foto es demasiado pesada. Intenta de nuevo.", "sistema");
        modoCamara();
        return;
      }
      cerrarCamara();
      subirArchivo(blob, `foto-${Date.now()}.jpg`);
    }

    $(".cam-cerrar").addEventListener("click", cerrarCamara);
    btnDisparo.addEventListener("click", tomarFoto);
    $(".cam-repetir").addEventListener("click", modoCamara);
    $(".cam-enviar").addEventListener("click", enviarFoto);
    $(".cam-galeria").addEventListener("click", () => { cerrarCamara(); $archivoInputFotos.click(); });
    btnGirar.addEventListener("click", async () => {
      btnGirar.disabled = true;
      const anterior = facing;
      facing = facing === "environment" ? "user" : "environment";
      try { await iniciarStream(facing); }
      catch (e) { facing = anterior; try { await iniciarStream(facing); } catch (_) { /* nada */ } }
      btnGirar.disabled = false;
    });
    btnLuz.addEventListener("click", async () => {
      if (!track) return;
      try {
        await track.applyConstraints({ advanced: [{ torch: !luzEncendida }] });
        luzEncendida = !luzEncendida;
        btnLuz.classList.toggle("encendido", luzEncendida);
      } catch (_) { btnLuz.classList.add("oculto"); }
    });

    // Zoom de la cámara (pellizco en el celular, rueda en la laptop) — solo si la cámara lo soporta.
    let ocultarZoomT = null;
    async function aplicarZoom(valor) {
      if (!zoomCap || !track) return;
      const v = Math.min(zoomCap.max, Math.max(zoomCap.min, valor));
      zoomActual = v;
      $zoom.textContent = (v / (zoomCap.min || 1)).toFixed(1) + "x";
      $zoom.classList.add("visible");
      clearTimeout(ocultarZoomT);
      ocultarZoomT = setTimeout(() => $zoom.classList.remove("visible"), 1500);
      try { await track.applyConstraints({ advanced: [{ zoom: v }] }); } catch (_) { /* nada */ }
    }
    const dedos = new Map();
    let distIni = 0, zoomIni = 1;
    const vista = $(".cam-vista");
    vista.addEventListener("pointerdown", (e) => {
      dedos.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (dedos.size === 2) {
        const [a, b] = [...dedos.values()];
        distIni = Math.hypot(a.x - b.x, a.y - b.y); zoomIni = zoomActual;
      }
    });
    vista.addEventListener("pointermove", (e) => {
      if (!dedos.has(e.pointerId)) return;
      dedos.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (dedos.size === 2 && distIni && !video.classList.contains("cam-oculto")) {
        const [a, b] = [...dedos.values()];
        aplicarZoom(zoomIni * Math.hypot(a.x - b.x, a.y - b.y) / distIni);
      }
    });
    const finDedo = (e) => { dedos.delete(e.pointerId); if (dedos.size < 2) distIni = 0; };
    vista.addEventListener("pointerup", finDedo);
    vista.addEventListener("pointercancel", finDedo);
    vista.addEventListener("wheel", (e) => {
      if (!zoomCap || video.classList.contains("cam-oculto")) return;
      e.preventDefault();
      aplicarZoom(zoomActual + (e.deltaY < 0 ? 1 : -1) * Math.max(zoomCap.step || 0.1, (zoomCap.max - zoomCap.min) / 20));
    }, { passive: false });

    // Teclado (laptop): Esc cierra (o vuelve desde la revisión), Espacio/Enter toma la foto o la envía.
    function teclas(e) {
      if (e.repeat) return;
      // Que un botón con foco no se "clickee" además con la misma tecla (doble acción).
      if (root.activeElement && overlay.contains(root.activeElement)) root.activeElement.blur();
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (enRevision) modoCamara(); else cerrarCamara();
      } else if (e.key === " " || e.key === "Enter") {
        e.preventDefault(); e.stopPropagation();
        if (enRevision) enviarFoto(); else tomarFoto();
      }
    }
    document.addEventListener("keydown", teclas, true);
  }

  // Extensiones de foto que se previsualizan como en WhatsApp (HEIC/AVIF/TIFF el servidor los pasa a JPG).
  const EXT_FOTO_LOCAL = /\.(jpe?g|jpe|jfif|png|gif|webp|bmp|heic|heif|avif|tiff?)$/i;

  async function subirArchivo(file, nombreForzado) {
    const nombre = nombreForzado || file.name || "archivo";
    const respondidoAId = respondiendoAId;
    // Como WhatsApp: si es una foto, la burbuja la muestra AL INSTANTE (copia local, semitransparente
    // mientras sube) y al terminar se cambia por la del servidor (ya en JPG). Documentos: nombre del archivo.
    const esFoto = (file.type || "").startsWith("image/") || EXT_FOTO_LOCAL.test(nombre);
    const div = agregarMensaje(esFoto ? "" : `📎 ${nombre}`, "usuario", null, null,
      { respondidoA: respondiendoAPreview, esArchivo: true });
    div.classList.add("subiendo");
    const spanTexto = div.querySelector(".texto-mensaje");
    const estadoSubida = document.createElement("span");
    estadoSubida.className = "estado-subida";
    estadoSubida.textContent = "⏳ Enviando...";
    spanTexto.after(estadoSubida);
    let localUrl = null;
    let img = null;
    if (esFoto) {
      img = document.createElement("img");
      img.className = "msg-imagen";
      try { localUrl = URL.createObjectURL(file); } catch (_) { localUrl = null; }
      // HEIC no se puede dibujar en Chrome/Android: queda un recuadro gris hasta que llegue el JPG del servidor.
      img.onerror = () => { img.onerror = null; img.removeAttribute("src"); img.classList.add("sin-previa"); };
      if (localUrl) img.src = localUrl; else img.classList.add("sin-previa");
      img.addEventListener("click", () => { const u = img.dataset.urlServidor || localUrl; if (u) abrirLightbox(u); });
      spanTexto.before(img);
      spanTexto.style.display = "none";
      scrollAlFinal();
    }
    const liberarLocal = () => { if (localUrl) { try { URL.revokeObjectURL(localUrl); } catch (_) { /* nada */ } localUrl = null; } };
    agregarMensaje(textoEsperaSubida, "sistema");
    cancelarRespuesta();
    $textoInput.disabled = true;
    $enviarBtn.disabled = true;
    $adjuntarBtn.disabled = true;
    try {
      const formData = new FormData();
      formData.append("file", file, nombre);
      formData.append("conversacion_id", conversacionIdActual);
      formData.append("sesion_token", sesionTokenActual);
      const resp = await fetch(`${API_BASE}/upload`, { method: "POST", body: formData });
      if (!resp.ok) throw new Error("upload falló");
      const data = await resp.json();
      enviar({ type: "archivo_subido", payload: { url: data.url, ruta: data.ruta, respondido_a_id: respondidoAId } });
      div.classList.remove("subiendo");
      estadoSubida.remove();
      const urlServidor = urlArchivoWidget(data.ruta);
      if (img && esImagen(data.ruta)) {
        img.dataset.urlServidor = urlServidor;
        const definitiva = new Image();
        definitiva.onload = () => { img.onerror = null; img.classList.remove("sin-previa"); img.src = urlServidor; liberarLocal(); };
        definitiva.onerror = () => { /* se queda la copia local; al recargar se verá la del servidor */ };
        definitiva.src = urlServidor;
      } else {
        // Documento (o una foto que el servidor no pudo convertir): enlace para abrirlo.
        if (img) { img.remove(); liberarLocal(); }
        spanTexto.style.display = "";
        spanTexto.textContent = "";
        const a = document.createElement("a");
        a.href = urlServidor; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.className = "msg-archivo-link"; a.style.color = "inherit";
        a.textContent = `📄 ${nombre}`;
        spanTexto.appendChild(a);
      }
    } catch (e) {
      // No llegó al servidor: la burbuja queda marcada y se saca de la cola de ✓ para que el
      // siguiente mensaje no reciba el id de esta.
      div.classList.remove("subiendo");
      estadoSubida.textContent = "⚠️ No se envió";
      const check = div.querySelector(".check");
      if (check) {
        const i = checksPendientesRecibido.indexOf(check);
        if (i >= 0) checksPendientesRecibido.splice(i, 1);
        check.remove();
      }
      const j = divsPendientesId.indexOf(div);
      if (j >= 0) divsPendientesId.splice(j, 1);
      agregarMensaje("⚠️ No se pudo subir el archivo. Intenta de nuevo.", "sistema");
    } finally {
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
      $adjuntarBtn.disabled = false;
      actualizarIconoEnviar();
    }
  }
})();
