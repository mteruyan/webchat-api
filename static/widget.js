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
      position: fixed; bottom: 24px; right: 24px; width: 60px; height: 60px;
      border-radius: 50%; background: ${TOKENS.azul}; color: white; border: none;
      cursor: pointer; box-shadow: 0 4px 16px rgba(10,76,140,.35);
      display: flex; align-items: center; justify-content: center;
      font-size: 26px; z-index: 999999; transition: transform .15s ease;
    }
    .burbuja:hover { transform: scale(1.06); }
    .burbuja .badge {
      position: absolute; top: -4px; right: -4px; background: #E0483E;
      color: white; border-radius: 999px; font-size: 11px; font-weight: 600;
      min-width: 18px; height: 18px; display: flex; align-items: center;
      justify-content: center; padding: 0 4px;
    }
    .panel {
      position: fixed; bottom: 96px; right: 24px; width: 380px; max-width: calc(100vw - 32px);
      height: 560px; max-height: calc(100vh - 140px); background: white;
      border-radius: 16px; box-shadow: 0 12px 40px rgba(10,30,50,.22);
      display: none; flex-direction: column; overflow: hidden; z-index: 999999;
      font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: ${TOKENS.texto};
    }
    .panel.abierto { display: flex; }
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
      max-width: 82%; margin: 6px 0; padding: 10px 13px; border-radius: 14px;
      font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word;
    }
    .msg.bot { background: ${TOKENS.burbujaBot}; color: ${TOKENS.texto}; margin-right: auto; border-bottom-left-radius: 4px; }
    .msg.usuario { background: ${TOKENS.burbujaUsuario}; color: ${TOKENS.texto}; margin-left: auto; border-bottom-right-radius: 4px; text-align: right; }
    .msg.sistema { background: transparent; color: ${TOKENS.textoSuave}; font-size: 12px; text-align: center; margin: 10px auto; max-width: 100%; }
    .msg .hora { display: block; font-size: 10.5px; margin-top: 3px; font-weight: 400; }
    .msg .msg-imagen { display: block; width: 100%; max-width: 260px; border-radius: 10px; margin-bottom: 6px; }
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
    .pie { border-top: 1px solid ${TOKENS.borde}; padding: 10px; display: flex; gap: 8px; flex-shrink: 0; background: white; }
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
    <button class="burbuja" aria-label="Abrir chat">💬<span class="badge" style="display:none;"></span></button>
    <div class="panel">
      <div class="cabecera">
        ${LOGO_URL ? `<img src="${LOGO_URL}" alt="Sanitas" />` : ""}
        <div class="info">
          <div class="titulo">Sanitas — Asistente virtual</div>
          <div class="estado"><span class="dot"></span><span class="estado-texto">Conectando...</span></div>
        </div>
        <button class="cerrar" aria-label="Cerrar chat">✕</button>
      </div>
      <div class="cuerpo"></div>
      <div class="pie">
        <input type="file" class="archivo-input-documento" accept="image/*,.pdf,.doc,.docx" style="display:none" />
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
        <input type="text" class="texto-input" placeholder="Escribe un mensaje..." disabled />
        <button class="enviar" disabled>➤</button>
      </div>
    </div>
  `;

  const $burbuja = root.querySelector(".burbuja");
  const $badge = root.querySelector(".badge");
  const $panel = root.querySelector(".panel");
  const $cerrar = root.querySelector(".cerrar");
  const $cuerpo = root.querySelector(".cuerpo");
  const $estadoTexto = root.querySelector(".estado-texto");
  const $textoInput = root.querySelector(".texto-input");
  const $enviarBtn = root.querySelector(".enviar");
  const $adjuntarBtn = root.querySelector(".adjuntar");
  const $adjuntarMenu = root.querySelector(".adjuntar-menu");
  const $archivoInputDocumento = root.querySelector(".archivo-input-documento");
  const $archivoInputCamara = root.querySelector(".archivo-input-camara");

  let ws = null;
  let noLeidos = 0;
  let panelAbierto = false;
  let intentosReconexion = 0;
  let reconectando = false;
  let cerradoManualmente = false;

  function siguienteEsperaReconexion() {
    // 1s, 2s, 4s, 8s, ... con tope de 20s
    return Math.min(1000 * Math.pow(2, intentosReconexion), 20000);
  }

  // ---------------------------------------------------------------------
  // Abrir / cerrar
  // ---------------------------------------------------------------------
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
  }

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
    return escapado
      .replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
      .replace(/_([^_\n]+)_/g, "<i>$1</i>")
      .replace(/~([^~\n]+)~/g, "<s>$1</s>");
  }

  function agregarMensaje(texto, tipo, imagenUrl) {
    const div = document.createElement("div");
    div.className = "msg " + tipo;
    if (imagenUrl) {
      const img = document.createElement("img");
      img.src = imagenUrl;
      img.className = "msg-imagen";
      img.loading = "lazy";
      div.appendChild(img);
    }
    const textoSpan = document.createElement("span");
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
      }
      div.appendChild(hora);
    }
    $cuerpo.appendChild(div);
    scrollAlFinal();
    if (!panelAbierto && tipo !== "usuario") {
      noLeidos++;
      actualizarBadge();
    }
  }

  function marcarMensajeRecibido() {
    // El servidor confirmó que guardó/entregó el mensaje — pasa de ✓ a ✓✓ (gris).
    const check = checksPendientesRecibido.shift();
    if (check && !check.classList.contains("leido")) check.textContent = " ✓✓";
  }

  function marcarMensajesLeidos() {
    $cuerpo.querySelectorAll(".msg.usuario .check").forEach((el) => {
      el.textContent = " ✓✓";
      el.classList.add("leido");
    });
    checksPendientesRecibido = [];
  }

  function agregarMensajeHistorial(msg) {
    // Los archivos que el cliente mismo envió se muestran como un indicador
    // genérico — la URL real es privada, solo la ve el asesor/supervisor.
    if (msg.tipo_contenido === "archivo" && msg.emisor_tipo === "usuario") {
      msg = { ...msg, tipo_contenido: "texto", contenido: "📎 Archivo enviado" };
    } else if (msg.tipo_contenido !== "texto") {
      return; // formularios/opciones/archivos de otros lados no se reconstruyen
    }
    const tipo = msg.emisor_tipo === "usuario" ? "usuario" : msg.emisor_tipo === "sistema" ? "sistema" : "bot";
    const div = document.createElement("div");
    div.className = "msg " + tipo;
    div.innerHTML = formatearTexto(msg.contenido);
    if (tipo !== "sistema") {
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

  function agregarFormulario(campos) {
    const form = document.createElement("form");
    form.className = "formulario";
    (campos || []).forEach((campo) => {
      const label = document.createElement("label");
      label.textContent = campo.label + (campo.obligatorio ? " *" : "");
      form.appendChild(label);

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
      form.appendChild(input);
    });

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = "Enviar";
    form.appendChild(btn);

    form.onsubmit = (e) => {
      e.preventDefault();
      const valores = {};
      (campos || []).forEach((c) => (valores[c.variable] = form.elements[c.variable].value));
      agregarMensaje("📋 Formulario enviado", "usuario");
      enviar({ type: "formulario_completado", payload: { valores } });
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
    const url = `${WS_BASE}/ws/widget${sesionToken ? "?sesion_token=" + encodeURIComponent(sesionToken) : ""}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      intentosReconexion = 0;
      reconectando = false;
      $estadoTexto.textContent = "En línea";
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
    };

    ws.onclose = () => {
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
          break;
        case "historial":
          // Reconstruye la pantalla tras una reconexión (deploy, corte de red, etc.)
          $cuerpo.innerHTML = "";
          (p.mensajes || []).forEach(agregarMensajeHistorial);
          scrollAlFinal();
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
          break;
        case "mensaje_bot":
          agregarMensaje(p.texto, "bot", p.imagen_url);
          if (p.opciones) agregarOpciones(p.opciones);
          break;
        case "mostrar_formulario":
          if (p.texto) agregarMensaje(p.texto, "bot");
          agregarFormulario(p.campos);
          break;
        case "mensaje_asesor":
          agregarMensaje(`${p.asesor_nombre}: ${p.texto}`, "bot");
          if (panelAbierto) enviar({ type: "marcar_leido", payload: {} });
          break;
        case "mensajes_leidos":
          marcarMensajesLeidos();
          break;
        case "mensaje_recibido_servidor":
          marcarMensajeRecibido();
          break;
        case "en_cola":
          $estadoTexto.textContent = "En cola";
          agregarMensaje("⏳ En breve te atenderá un asesor...", "sistema");
          break;
        case "asignado":
          $estadoTexto.textContent = `Con ${p.asesor_nombre}`;
          agregarMensaje(`✅ Te está atendiendo ${p.asesor_nombre}`, "sistema");
          break;
        case "conversacion_cerrada":
          agregarMensaje("— Conversación finalizada —", "sistema");
          agregarAccionesCierre();
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
    agregarMensaje(texto, "usuario");
    enviar({ type: "mensaje_usuario", payload: { texto } });
    $textoInput.value = "";
  }
  $enviarBtn.addEventListener("click", enviarTexto);
  $textoInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") enviarTexto();
  });

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
    agregarMensaje(`📎 Subiendo ${nombre}...`, "usuario");
    $textoInput.disabled = true;
    $enviarBtn.disabled = true;
    $adjuntarBtn.disabled = true;
    try {
      const formData = new FormData();
      formData.append("file", file, nombre);
      const resp = await fetch(`${API_BASE}/upload`, { method: "POST", body: formData });
      if (!resp.ok) throw new Error("upload falló");
      const data = await resp.json();
      enviar({ type: "archivo_subido", payload: { url: data.url } });
    } catch (e) {
      agregarMensaje("⚠️ No se pudo subir el archivo. Intenta de nuevo.", "sistema");
    } finally {
      $textoInput.disabled = false;
      $enviarBtn.disabled = false;
      $adjuntarBtn.disabled = false;
    }
  }
})();
