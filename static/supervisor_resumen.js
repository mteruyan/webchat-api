/* Resumen de asesores para la consola de supervisor.
   Se muestra donde dice 'Haz clic en "👂 Escuchar"...' (cuando no se está escuchando ninguna conversación).
   Se alimenta de los mismos mensajes que ya recibe la consola por WebSocket (estado_global y
   asesor_estado_cambio), que el servidor ya filtra por las campañas del supervisor. No hace llamadas extra.
   Se carga con UNA línea en <head> de supervisor.html, ANTES del script de la página:
       <script src="/static/supervisor_resumen.js"></script> */
(function () {
  "use strict";
  const asesores = {};          // asesor_id -> datos del último mensaje
  let recibidoEn = Date.now();  // cuándo llegó cada dato (para que los contadores avancen solos)
  const recibidoPor = {};
  let filtroCampana = "";

  // --- escucha pasiva del WebSocket de supervisor -------------------------------------------------
  const WS_ORIGINAL = window.WebSocket;
  function WSEnvuelto(url, protocolos) {
    const ws = protocolos === undefined ? new WS_ORIGINAL(url) : new WS_ORIGINAL(url, protocolos);
    if (String(url).indexOf("/ws/supervisor") !== -1) {
      ws.addEventListener("message", (ev) => {
        try {
          const m = JSON.parse(ev.data);
          const p = m.payload || {};
          if (m.type === "estado_global") {
            Object.keys(asesores).forEach((k) => delete asesores[k]);
            (p.asesores || []).forEach(registrar);
            pintar();
          } else if (m.type === "asesor_estado_cambio") {
            registrar(p);
            pintar();
          }
        } catch (e) { /* mensaje que no es JSON: se ignora */ }
      });
    }
    return ws;
  }
  WSEnvuelto.prototype = WS_ORIGINAL.prototype;
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach((k) => { WSEnvuelto[k] = WS_ORIGINAL[k]; });
  window.WebSocket = WSEnvuelto;

  function registrar(a) {
    if (!a || a.asesor_id == null) return;
    asesores[a.asesor_id] = a;
    recibidoPor[a.asesor_id] = Date.now();
  }

  // --- helpers ---------------------------------------------------------------------------------------
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function hms(seg) {
    seg = Math.max(0, Math.floor(seg || 0));
    const h = Math.floor(seg / 3600), m = Math.floor((seg % 3600) / 60), s = seg % 60;
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }
  function estadoDe(a) {
    if (!a.conectado) return { clave: "off", texto: "Desconectado" };
    return a.en_pausa ? { clave: "pausa", texto: "Pausado" } : { clave: "on", texto: "En línea" };
  }
  function conteos(a) {
    const conv = a.conversaciones || [];
    const pausadas = conv.filter((c) => c.pausada).length;
    return { principal: conv.length - pausadas, pausadas };
  }
  // Segundos "de hoy" avanzando en vivo desde que llegó el dato.
  function segundosVivos(a, campo, avanza) {
    const base = a[campo] || 0;
    return avanza ? base + (Date.now() - (recibidoPor[a.asesor_id] || Date.now())) / 1000 : base;
  }

  // --- DOM -------------------------------------------------------------------------------------------
  let cont = null;
  function asegurarContenedor() {
    if (cont && document.body.contains(cont)) return cont;
    const vacio = document.getElementById("panelVacio");
    if (!vacio || !vacio.parentNode) return null;
    const css = document.createElement("style");
    css.textContent = `
      #panelVacio { display: none !important; }
      #resumenAsesores { width: 100%; padding: 16px 18px; overflow: auto; align-self: stretch; }
      #resumenAsesores .ra-hint { color: var(--texto-suave, #5B6B7C); font-size: 13px; margin-bottom: 12px; text-align: center; }
      #resumenAsesores .ra-barra { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
      #resumenAsesores .ra-barra h3 { margin: 0; font-size: 15px; color: var(--texto, #1A2B3C); flex: 1; }
      #resumenAsesores select { padding: 6px 10px; border: 1px solid var(--borde, #E1E8EF); border-radius: 8px; font: inherit; font-size: 13px; background: #fff; }
      #resumenAsesores table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--borde, #E1E8EF); border-radius: 10px; overflow: hidden; font-size: 13px; }
      #resumenAsesores th { background: var(--fondo, #F7F9FB); color: var(--texto-suave, #5B6B7C); font-weight: 600; text-align: left; padding: 9px 12px; white-space: nowrap; border-bottom: 1px solid var(--borde, #E1E8EF); }
      #resumenAsesores td { padding: 9px 12px; border-bottom: 1px solid var(--borde, #E1E8EF); color: var(--texto, #1A2B3C); }
      #resumenAsesores td.num, #resumenAsesores th.num { text-align: center; font-variant-numeric: tabular-nums; }
      #resumenAsesores tr:last-child td { border-bottom: none; }
      #resumenAsesores .ra-badge { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
      #resumenAsesores .ra-on { background: #E6F7F1; color: #1B7F5F; }
      #resumenAsesores .ra-pausa { background: #FDF1E0; color: #A8650F; }
      #resumenAsesores .ra-off { background: #EEF1F4; color: #5B6B7C; }
      #resumenAsesores .ra-vacio { text-align: center; color: var(--texto-suave, #5B6B7C); padding: 24px; }
      #resumenAsesores .ra-total td { font-weight: 700; background: var(--fondo, #F7F9FB); }`;
    document.head.appendChild(css);
    cont = document.createElement("div");
    cont.id = "resumenAsesores";
    vacio.parentNode.insertBefore(cont, vacio);
    cont.addEventListener("change", (e) => {
      if (e.target && e.target.id === "raCampana") { filtroCampana = e.target.value; pintar(); }
    });
    return cont;
  }

  function hayConversacionEscuchada() {
    const vacio = document.getElementById("panelVacio");
    return !vacio || vacio.style.display === "none";   // la página lo oculta mientras escucha
  }

  function pintar() {
    const c = asegurarContenedor();
    if (!c) return;
    if (hayConversacionEscuchada()) { c.style.display = "none"; return; }
    c.style.display = "block";

    const todos = Object.values(asesores);
    const campanas = Array.from(new Set(todos.flatMap((a) => a.campanas || []))).sort();
    if (filtroCampana && campanas.indexOf(filtroCampana) === -1) filtroCampana = "";
    const lista = todos
      .filter((a) => !filtroCampana || (a.campanas || []).indexOf(filtroCampana) !== -1)
      .sort((x, y) => {
        const o = { on: 0, pausa: 1, off: 2 };
        return o[estadoDe(x).clave] - o[estadoDe(y).clave] || String(x.nombre_display || "").localeCompare(String(y.nombre_display || ""));
      });

    let totPrin = 0, totPaus = 0, totAten = 0;
    const filas = lista.map((a) => {
      const e = estadoDe(a), n = conteos(a);
      totPrin += n.principal; totPaus += n.pausadas; totAten += a.atendidas_hoy || 0;
      return `<tr data-asesor="${esc(a.asesor_id)}">
        <td>${esc(a.nombre_display || "Asesor " + a.asesor_id)}</td>
        <td><span class="ra-badge ra-${e.clave}">${e.clave === "on" ? "🟢" : e.clave === "pausa" ? "⏸️" : "⚪"} ${e.texto}</span></td>
        <td class="num" data-t="conectado">${hms(segundosVivos(a, "segundos_conectado_hoy", a.conectado))}</td>
        <td class="num" data-t="pausado">${hms(segundosVivos(a, "segundos_pausado_hoy", a.conectado && a.en_pausa))}</td>
        <td class="num">${n.principal}</td>
        <td class="num">${n.pausadas}</td>
        <td class="num">${a.atendidas_hoy || 0}</td></tr>`;
    }).join("");

    c.innerHTML = `
      <div class="ra-hint">Haz clic en "👂 Escuchar" sobre cualquier conversación del tablero para empezar a supervisarla.</div>
      <div class="ra-barra">
        <h3>👥 Resumen de asesores (hoy)</h3>
        <label style="font-size:13px;color:var(--texto-suave,#5B6B7C)">Campaña
          <select id="raCampana"><option value="">Todas</option>${campanas.map((k) => `<option value="${esc(k)}"${k === filtroCampana ? " selected" : ""}>${esc(k)}</option>`).join("")}</select>
        </label>
      </div>
      <table>
        <thead><tr>
          <th>Asesor</th><th>Estado</th><th class="num">Tiempo en línea</th><th class="num">Tiempo pausado</th>
          <th class="num">Bandeja principal</th><th class="num">Bandeja pausados</th><th class="num">Atendidos</th>
        </tr></thead>
        <tbody>${filas || '<tr><td colspan="7" class="ra-vacio">No hay asesores para mostrar.</td></tr>'}
          ${lista.length ? `<tr class="ra-total"><td colspan="4">Total</td><td class="num">${totPrin}</td><td class="num">${totPaus}</td><td class="num">${totAten}</td></tr>` : ""}
        </tbody>
      </table>`;
  }

  // Los contadores avanzan cada segundo sin volver a dibujar toda la tabla.
  setInterval(() => {
    if (!cont || cont.style.display === "none") return;
    cont.querySelectorAll("tr[data-asesor]").forEach((tr) => {
      const a = asesores[tr.getAttribute("data-asesor")];
      if (!a) return;
      const tc = tr.querySelector('[data-t="conectado"]'), tp = tr.querySelector('[data-t="pausado"]');
      if (tc) tc.textContent = hms(segundosVivos(a, "segundos_conectado_hoy", a.conectado));
      if (tp) tp.textContent = hms(segundosVivos(a, "segundos_pausado_hoy", a.conectado && a.en_pausa));
    });
  }, 1000);

  // Se vuelve a mostrar cuando la página deja de escuchar una conversación (muestra otra vez #panelVacio).
  function vigilar() {
    const vacio = document.getElementById("panelVacio");
    if (!vacio) { setTimeout(vigilar, 500); return; }
    new MutationObserver(pintar).observe(vacio, { attributes: true, attributeFilter: ["style"] });
    pintar();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", vigilar);
  else vigilar();
})();
