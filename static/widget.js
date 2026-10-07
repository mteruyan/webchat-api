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

  const style = document.createElement("style");
  style.textContent = `
    :host, * { box-sizing: border-box; }
    .burbuja {
      /* Antes era un círculo de 78px -- se agrandó para que la imagen de
         Sofi (con el globo "Escribe a Sofi, tu asistente virtual") se vea
         grande y legible. Dejó de ser un círculo a propósito: con
         border-radius:50% el recorte circular siempre se come las esquinas
         del cuadrado sin importar el tamaño (es geometría, no resolución),
         y ahí vive justo el globo de texto -- por eso ahora es un cuadrado
         con esquinas suavizadas, para respetar el 100% de la imagen.
         Se triplicó de 170px a 510px, y se quitaron el fondo y la sombra
         (background/box-shadow) para que el cuadrado que contiene la
         imagen sea invisible -- solo se percibe la ilustración de Sofi,
         no el contenedor. bottom/right en 0 para que arranque pegada
         justo al borde inferior derecho del navegador (al lado de la
         barra de tareas), sin el margen de 24px que antes dejaba un
         espacio vacío ahí. */
      position: fixed; bottom: 0; right: 0; width: 420px; height: 420px;
      border-radius: 20px; background: transparent; color: white; border: none;
      padding: 0; cursor: pointer; box-shadow: none;
      display: flex; align-items: center; justify-content: center;
      z-index: 999999; transition: transform .15s ease; overflow: visible;
    }
    .burbuja img { width: 100%; height: 100%; display: block; border-radius: 20px; object-fit: contain; }
    .burbuja:hover { transform: scale(1.04); }
    .burbuja .badge {
      position: absolute; top: -8px; right: -8px; background: #E0483E;
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
    .cuerpo { flex: 1; overflow-y: auto; padding: 16px; background: ${TOKENS.fondo}; scroll-behavior: smooth; }
    .cuerpo.arrastrando { outline: 2px dashed ${TOKENS.azul}; outline-offset: -6px; }
    .msg {
      position: relative; max-width: 82%; margin: 6px 0; padding: 10px 13px; border-radius: 14px;
      font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word;
    }
    .msg.bot { background: ${TOKENS.burbujaBot}; color: ${TOKENS.texto}; margin-right: auto; border-bottom-left-radius: 4px; }
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
    .msg .msg-archivo-link { display: inline-flex; align-items: center; gap: 6px; background: rgba(0,0,0,.06); border-radius: 8px; padding: 8px 12px; margin-bottom: 6px; text-decoration: none; color: inherit; font-weight: 600; font-size: 13.5px; }
    .msg.bot .msg-archivo-link { background: #EAF1F9; color: var(--azul, #1a73e8); }
    .lightbox-overlay {
      position: fixed; inset: 0; background: rgba(10,20,30,.85); z-index: 1000000;
      display: none; align-items: center; justify-content: center; padding: 24px;
    }
    .lightbox-overlay.abierto { display: flex; }
    .lightbox-overlay img { max-width: 100%; max-height: 100%; border-radius: 8px; box-shadow: 0 8px 32px rgba(0,0,0,.4); }
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
      border-radius: 999px; padding: 7px 14px; font-size: 13px; cursor: pointer;
      transition: background .12s;
    }
    .opciones button:hover { background: ${TOKENS.burbujaBot}; }
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
    .formulario label { display: block; font-size: 12.5px; margin-top: 10px; color: ${TOKENS.textoSuave}; font-weight: 600; }
    .formulario input, .formulario select, .formulario textarea {
      width: 100%; padding: 8px 10px; border: 1px solid ${TOKENS.borde}; border-radius: 8px;
      margin-top: 4px; font-size: 14px; font-family: inherit; color: ${TOKENS.texto};
    }
    .formulario button[type="submit"] {
      margin-top: 14px; width: 100%; background: ${TOKENS.azul}; color: white; border: none;
      border-radius: 8px; padding: 10px; font-size: 14px; font-weight: 600; cursor: pointer;
    }
    .pie { border-top: 1px solid ${TOKENS.borde}; padding: 10px; display: flex; gap: 8px; flex-shrink: 0; background: white; position: relative; }
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
    .pie input[type="text"] {
      flex: 1; border: 1px solid ${TOKENS.borde}; border-radius: 999px; padding: 10px 16px;
      font-size: 14px; font-family: inherit; outline: none;
    }
    .pie input[type="text"]:focus { border-color: ${TOKENS.azul}; }
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
    .camara-overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 1000000;
      display: flex; align-items: center; justify-content: center;
    }
    .camara-caja { display: flex; flex-direction: column; align-items: center; gap: 14px; width: 100%; max-width: 420px; padding: 20px; }
    .camara-caja video { width: 100%; border-radius: 12px; background: #000; max-height: 70vh; }
    .camara-botones { display: flex; gap: 10px; width: 100%; }
    .camara-botones button {
      flex: 1; padding: 12px; border-radius: 999px; border: none; font-size: 14px;
      font-weight: 600; cursor: pointer; font-family: inherit;
    }
    .camara-cancelar { background: rgba(255,255,255,.15); color: white; }
    .camara-tomar { background: ${TOKENS.azul}; color: white; }
    @media (max-width: 480px) {
      .panel {
        bottom: 0; right: 0; width: 100vw; height: 100vh; height: 100dvh;
        max-height: 100vh; max-height: 100dvh; border-radius: 0;
      }
      .cabecera { padding-top: max(16px, env(safe-area-inset-top)); }
      .pie { padding-bottom: max(10px, env(safe-area-inset-bottom)); }
      .burbuja.oculta { display: none; }
    }
  `;
  root.appendChild(style);

  root.innerHTML += `
    <button class="burbuja" aria-label="Abrir chat"><img src="${API_BASE}/static/imagenes/sofi-icono.png" alt="Sofi" /><span class="badge" style="display:none;"></span></button>
    <div class="panel">
      <div class="cabecera">
        ${LOGO_URL ? `<img src="${LOGO_URL}" alt="Sanitas" />` : ""}
        <div class="info">
          <div class="titulo">Sofi, tu asistente virtual</div>
          <div class="estado"><span class="dot"></span><span class="estado-texto">Conectando...</span></div>
        </div>
        <button class="cerrar" aria-label="Cerrar chat">✕</button>
      </div>
      <div class="cuerpo"></div>
      <button class="nuevo-mensaje" type="button">Mensaje nuevo ⬇️</button>
      <div class="responder-banner">
        <span class="texto-cita"></span>
        <button class="cancelar-responder" type="button" aria-label="Cancelar respuesta">✕</button>
      </div>
      <div class="pie">
        <input type="file" class="archivo-input-documento" accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.csv" style="display:none" />
        <input type="file" class="archivo-input-camara" accept="image/*" capture="environment" style="display:none" />
        <div class="adjuntar-wrap">
          <div class="adjuntar-menu">
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
        <input type="text" class="texto-input" placeholder="Escribe un mensaje..." disabled />
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
  const $textoInput = root.querySelector(".texto-input");
  const $enviarBtn = root.querySelector(".enviar");
  const $adjuntarBtn = root.querySelector(".adjuntar");
  const $adjuntarMenu = root.querySelector(".adjuntar-menu");
  const $archivoInputDocumento = root.querySelector(".archivo-input-documento");
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
    $lightboxImg.src = url;
    $lightboxOverlay.classList.add("abierto");
  }
  function cerrarLightbox() {
    $lightboxOverlay.classList.remove("abierto");
    $lightboxImg.src = "";
  }
  $lightboxCerrar.addEventListener("click", cerrarLightbox);
  $lightboxOverlay.addEventListener("click", (e) => { if (e.target === $lightboxOverlay) cerrarLightbox(); });

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

  function urlArchivoWidget(ruta) {
    return `${API_BASE}/widget/archivo?conversacion_id=${encodeURIComponent(conversacionIdActual)}`
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
    if (tokenEnlace) {
      localStorage.setItem("sanitas_webchat_sesion", tokenEnlace);
    }
    if (tokenEnlace || abrirDesdeInvitacion) {
      params.delete("wcs");
      params.delete("ac");
      const resto = params.toString();
      const nuevaUrl = window.location.pathname + (resto ? "?" + resto : "") + window.location.hash;
      window.history.replaceState({}, "", nuevaUrl);
      setTimeout(abrirPanel, 300);
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
  });
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
      }
      div.appendChild(hora);
      agregarBotonAcciones(div, {
        tipo, texto, enviadoEnMs, esArchivo: !!imagenUrl,
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

  function agregarOpciones(opciones) {
    const cont = document.createElement("div");
    cont.className = "opciones";
    (opciones || []).forEach((op) => {
      const btn = document.createElement("button");
      btn.textContent = op.label;
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
  const OTP_ESPERA_REENVIO_SEGUNDOS = 60;

  function quitarAccionesOtp() {
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

    let restante = OTP_ESPERA_REENVIO_SEGUNDOS;
    const actualizarCuentaRegresiva = () => {
      if (restante <= 0) {
        btnReenviar.textContent = "🔁 Reenviar código";
        btnReenviar.disabled = false;
        clearInterval(otpTimerId);
        otpTimerId = null;
        return;
      }
      btnReenviar.textContent = `🔁 Reenviar código (${restante}s)`;
      restante--;
    };
    actualizarCuentaRegresiva();
    otpTimerId = setInterval(actualizarCuentaRegresiva, 1000);

    if (puedeLlamar) {
      const btnLlamar = document.createElement("button");
      btnLlamar.textContent = "📞 Prefiero que me llamen";
      btnLlamar.onclick = () => {
        btnLlamar.disabled = true;
        agregarMensaje("📞 Prefiero que me llamen", "usuario");
        enviar({ type: "solicitar_llamada_otp", payload: {} });
      };
      cont.appendChild(btnLlamar);
    }

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
    const sesionToken = localStorage.getItem("sanitas_webchat_sesion") || "";
    sesionTokenActual = sesionToken;
    const url = `${WS_BASE}/ws/widget${sesionToken ? "?sesion_token=" + encodeURIComponent(sesionToken) : ""}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
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
      ws = null;
      $textoInput.disabled = true;
      $enviarBtn.disabled = true;
      if (cerradoManualmente) return;

      reconectando = true;
      $estadoTexto.textContent = "Reconectando...";
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
      switch (data.type) {
        case "sesion_iniciada":
          localStorage.setItem("sanitas_webchat_sesion", p.sesion_token);
          sesionTokenActual = p.sesion_token;
          conversacionIdActual = p.conversacion_id;
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          break;
        case "otp_acciones":
          // Tras reconectar mientras esperaba el código SMS: repone los botones.
          agregarAccionesOtp(p.otp_reenvios || 0, !!p.otp_puede_llamar);
          scrollAlFinal();
          break;
        case "historial":
          // Reconstruye la pantalla tras una reconexión (deploy, corte de red, etc.)
          $cuerpo.innerHTML = "";
          (p.mensajes || []).forEach((msg, i, arr) => agregarMensajeHistorial(msg, i === arr.length - 1));
          scrollAlFinal();
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
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
          }
          // Cualquier mensaje del bot significa que todavía no hay un
          // asesor humano en la conversación -- notas de voz deshabilitadas.
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          break;
        case "mostrar_formulario":
          if (p.texto) agregarMensaje(p.texto, "bot");
          agregarFormulario(p.campos);
          break;
        case "mensaje_asesor":
          agregarMensaje(`${p.asesor_nombre}: ${p.texto}`, "bot", null, null,
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
          marcarMensajeRecibido(p.mensaje_id, p.recibido);
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
          agregarMensaje("— Conversación finalizada —", "sistema");
          agregarAccionesCierre();
          atendidoPorAsesor = false;
          actualizarIconoEnviar();
          break;
      }
    };
  }

  function enviar(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

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
  $textoInput.addEventListener("input", actualizarIconoEnviar);
  $textoInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") enviarTexto();
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
      <div class="camara-caja">
        <video autoplay playsinline muted></video>
        <canvas style="display:none;"></canvas>
        <div class="camara-botones">
          <button type="button" class="camara-cancelar">✕ Cancelar</button>
          <button type="button" class="camara-tomar">📸 Tomar foto</button>
        </div>
      </div>
    `;
    root.appendChild(overlay);
    const video = overlay.querySelector("video");
    const canvas = overlay.querySelector("canvas");

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    } catch (e1) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: true });
      } catch (e2) {
        overlay.remove();
        agregarMensaje("⚠️ No se pudo acceder a la cámara. Revisa los permisos del navegador.", "sistema");
        return;
      }
    }
    video.srcObject = stream;

    function cerrarCamara() {
      stream.getTracks().forEach((t) => t.stop());
      overlay.remove();
    }

    overlay.querySelector(".camara-cancelar").addEventListener("click", cerrarCamara);
    overlay.querySelector(".camara-tomar").addEventListener("click", () => {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      canvas.toBlob((blob) => {
        cerrarCamara();
        if (blob) subirArchivo(blob, `foto-${Date.now()}.jpg`);
      }, "image/jpeg", 0.9);
    });
  }

  async function subirArchivo(file, nombreForzado) {
    const nombre = nombreForzado || file.name || "archivo";
    const respondidoAId = respondiendoAId;
    agregarMensaje(`📎 Subiendo ${nombre}...`, "usuario", null, null, { respondidoA: respondiendoAPreview });
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
    } catch (e) {
      agregarMensaje("⚠️ No se pudo subir el archivo. Intenta de nuevo.", "sistema");
    } finally {
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
      $adjuntarBtn.disabled = false;
      actualizarIconoEnviar();
    }
  }
})();
