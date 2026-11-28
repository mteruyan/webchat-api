/* Menú «Formatos» de las consolas de asesor y supervisor.
 *  - 📄 Formatos por correo: envía un formato para firmar SOLO por correo (el cliente no tiene conversación abierta).
 *  - 🗂️ Formatos firmados: busca los formatos firmados de un cliente por correo, celular o DNI/documento.
 * Usa lo que ya definen las dos consolas: tokenGuardado, urlBaseGuardada, urlArchivo() y escapeHtml(). */
(function () {
  'use strict';

  var CAMPO = 'display:block;width:100%;margin:4px 0 8px;padding:9px 11px;border:1px solid #C9D6E4;border-radius:8px;' +
              'font-size:14px;box-sizing:border-box;font-family:inherit;background:#fff';
  var ETIQ = 'font-size:12.5px;font-weight:600';
  var FORMATOS = [
    ['SINIESTRALIDAD', 'Autorización de siniestralidad', 48,
      'El cliente sube la foto de su DNI, completa sus datos (y los de sus dependientes menores, si quiere) y firma.'],
    ['LATENCIA', 'Declaración jurada de latencia', 48,
      'El cliente sube la foto de su DNI, indica su domicilio, su último empleador, la fecha de cese y sus aportes a EsSalud, y firma.'],
    ['RENOVACION', 'Constancia de renovación de contrato', 48,
      'Tú indicas el periodo y el % de incremento; el cliente sube la foto de su DNI, revisa sus datos y firma la constancia de aceptación.'],
    ['DESAFILIACION', 'Solicitud de desafiliación', 48,
      'El cliente sube la foto de su DNI, indica los nombres y documentos de los usuarios a desafiliar (hasta 5) y firma.'],
    ['REEMBOLSO', 'Solicitud de reembolso', 72,
      'El cliente sube la foto de su DNI, completa los datos del titular y del paciente y la forma de pago, adjunta la hoja 2 llenada por el médico y sus comprobantes, y firma.']
  ];
  var ESTADOS = {
    enviado: ['Enviado', '#4A5B70', '#EEF2F6'], abierto: ['Abierto', '#8A5A00', '#FFF4DE'],
    firmado: ['Firmado', '#197A54', '#E3F5EC'], vencido: ['Vencido', '#8A97A6', '#F1F3F5'],
    anulado: ['Anulado', '#8A97A6', '#F1F3F5']
  };
  var RE_CORREO = /^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/;

  function esc(v) { return (typeof escapeHtml === 'function') ? escapeHtml(v) : String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
  function $(id) { return document.getElementById(id); }

  function api(metodo, ruta, cuerpo) {
    var opciones = { method: metodo, headers: { Authorization: 'Bearer ' + tokenGuardado } };
    if (cuerpo !== undefined) { opciones.headers['Content-Type'] = 'application/json'; opciones.body = JSON.stringify(cuerpo); }
    return fetch(urlBaseGuardada + ruta, opciones).then(function (r) {
      if (r.status === 401) throw new Error('Tu sesión venció. Vuelve a ingresar.');
      if (!r.ok) throw new Error('No se pudo completar la operación (' + r.status + ').');
      return r.json();
    });
  }

  function crearModal(id, ancho, html) {
    var caja = document.createElement('div');
    caja.id = id;
    caja.style.cssText = 'position:fixed;inset:0;background:rgba(10,30,50,.45);z-index:9000;display:none;align-items:center;justify-content:center;padding:16px;';
    caja.innerHTML = '<div style="background:#fff;border-radius:14px;max-width:' + ancho + 'px;width:100%;max-height:92vh;overflow-y:auto;padding:18px 18px 16px;position:relative;box-shadow:0 20px 50px rgba(10,30,50,.3)">' +
      '<button type="button" data-cerrar aria-label="Cerrar" style="position:absolute;top:8px;right:10px;border:0;background:none;font-size:20px;cursor:pointer;color:#4A5B70">×</button>' + html + '</div>';
    document.body.appendChild(caja);
    var cerrar = function () { caja.style.display = 'none'; };
    caja.addEventListener('click', function (e) { if (e.target === caja) cerrar(); });
    caja.querySelector('[data-cerrar]').onclick = cerrar;
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && caja.style.display !== 'none') cerrar(); });
    return caja;
  }

  // ---------------------------------------------------------------- Enviar formato por correo
  var envio = null;

  function pintarEnviados(lista) {
    var cont = $('fmEnviados');
    if (!lista || !lista.length) { cont.textContent = 'Todavía no has enviado formatos desde este menú.'; return; }
    cont.innerHTML = lista.map(function (s) {
      var e = ESTADOS[s.estado] || [s.estado, '#4A5B70', '#EEF2F6'];
      var chip = '<span style="font-size:11px;font-weight:700;color:' + e[1] + ';background:' + e[2] + ';border-radius:10px;padding:2px 8px">' + esc(e[0]) + '</span>';
      var detalle = s.estado === 'firmado' ? 'Firmado ' + esc(s.firmado)
        : (s.estado === 'abierto' ? 'Abierto ' + esc(s.abierto) + ' · vence ' + esc(s.vence)
        : (s.estado === 'enviado' ? 'Enviado ' + esc(s.creado) + ' · vence ' + esc(s.vence) : 'Enviado ' + esc(s.creado)));
      var falla = s.correo_ok === false ? ' <span style="color:#C0392B">⚠️ el correo falló</span>' : '';
      var accion = s.estado === 'firmado' && s.ruta_pdf
        ? '<a href="' + urlArchivo(s.ruta_pdf) + '" target="_blank" rel="noopener" style="color:#0A4C8C;font-weight:700;text-decoration:none">📄 Ver PDF</a>'
        : ((s.estado === 'enviado' || s.estado === 'abierto')
          ? '<button type="button" data-anular="' + s.id + '" style="border:0;background:none;color:#C0392B;cursor:pointer;font-size:12px;padding:0">Anular</button>' : '');
      return '<div style="display:flex;gap:8px;align-items:flex-start;justify-content:space-between;padding:7px 0;border-top:1px solid #EEF2F6">' +
        '<div style="min-width:0"><div style="font-weight:600;color:#1F2D3D">' + esc(s.nombre || s.correo) + ' ' + chip + '</div>' +
        '<div><b>' + esc(s.formato_nombre) + '</b>' + (s.contrato ? ' · Contrato ' + esc(s.contrato) : '') + ' · ' + esc(s.correo) + falla + '</div><div>' + detalle + '</div></div>' +
        '<div style="flex-shrink:0;padding-top:2px">' + accion + '</div></div>';
    }).join('');
    Array.prototype.forEach.call(cont.querySelectorAll('[data-anular]'), function (b) {
      b.onclick = function () {
        b.disabled = true;
        api('POST', '/formatos/anular', { id: Number(b.dataset.anular) }).then(function (r) {
          if (!r.ok) { $('fmEstado').style.color = '#C0392B'; $('fmEstado').textContent = '⚠️ ' + (r.motivo || 'No se pudo anular.'); }
          pintarEnviados(r.enviados);
        }).catch(function (err) { $('fmEstado').style.color = '#C0392B'; $('fmEstado').textContent = '⚠️ ' + err.message; b.disabled = false; });
      };
    });
  }

  function actualizarFormato() {
    var cod = $('fmFormato').value;
    var f = FORMATOS.filter(function (x) { return x[0] === cod; })[0];
    $('fmAyuda').textContent = f[3];
    $('fmVigencia').textContent = f[2];
    $('fmCajaContrato').style.display = (cod === 'SINIESTRALIDAD' || cod === 'DESAFILIACION' || cod === 'RENOVACION') ? '' : 'none';
    $('fmContratoObl').style.display = cod === 'RENOVACION' ? 'none' : '';
    $('fmContratoOpc').style.display = cod === 'RENOVACION' ? '' : 'none';
    $('fmCajaRenovacion').style.display = cod === 'RENOVACION' ? 'flex' : 'none';
  }

  function enviarFormato() {
    var estado = $('fmEstado');
    var cod = $('fmFormato').value;
    var contrato = (cod === 'REEMBOLSO' || cod === 'LATENCIA') ? '' : $('fmContrato').value.trim();
    var periodo = $('fmPeriodo').value.trim();
    var incremento = $('fmIncremento').value.trim().replace(',', '.');
    var correo = $('fmCorreo').value.trim();
    var celular = $('fmCelular').value.replace(/\D/g, '');
    var error = function (m) { estado.style.color = '#C0392B'; estado.textContent = m; };
    if (!RE_CORREO.test(correo)) return error('Escribe un correo válido.');
    if ((cod === 'SINIESTRALIDAD' || cod === 'DESAFILIACION') && !contrato) return error('Escribe el N° de contrato.');
    if (cod === 'RENOVACION') {
      if (!/^\d{4}$/.test(periodo)) return error('Indica el periodo (año de 4 dígitos, por ejemplo 2027).');
      if (!(Number(incremento) > 0 && Number(incremento) <= 100)) return error('Indica el % de incremento (mayor que 0, por ejemplo 7.5).');
    }
    if (celular && !/^(51)?9\d{8}$/.test(celular)) return error('El celular debe tener 9 dígitos y empezar con 9.');
    var btn = $('fmEnviar');
    btn.disabled = true; btn.textContent = 'Enviando...';
    estado.style.color = '#4A5B70'; estado.textContent = '';
    api('POST', '/formatos/enviar', {
      formato: cod, contrato: contrato, correo: correo, periodo: periodo, incremento: incremento, celular: celular,
      nombre: $('fmNombre').value.trim(), tipo_documento: $('fmTipo').value, n_documento: $('fmNumero').value.trim()
    }).then(function (r) {
      btn.disabled = false; btn.textContent = 'Enviar enlace por correo';
      if (!r.ok) return error('⚠️ ' + (r.motivo || 'No se pudo enviar.'));
      estado.style.color = r.aviso ? '#8A5A00' : '#197A54';
      estado.textContent = (r.correo_ok ? '✅ Enlace enviado por correo a ' + r.correo + '. Vence el ' + r.vence + '.' : '') +
        (r.aviso ? '\n⚠️ ' + r.aviso : '');
      if (!r.correo_ok && !r.aviso) estado.textContent = '⚠️ El correo no se pudo enviar. Revisa la dirección e inténtalo de nuevo.';
      if (!r.correo_ok) estado.textContent += '\nEnlace del formato (si necesitas entregarlo por otro medio): ' + r.url;
      pintarEnviados(r.enviados);
    }).catch(function (err) { btn.disabled = false; btn.textContent = 'Enviar enlace por correo'; error('⚠️ ' + err.message); });
  }

  function abrirEnvio() {
    if (!envio) {
      envio = crearModal('modalFormatosCorreo', 520,
        '<div style="font-weight:700;font-size:15px;margin-bottom:4px">📄 Enviar formato por correo</div>' +
        '<div style="font-size:12.5px;color:#4A5B70;margin-bottom:10px">Para clientes que no tienen una conversación abierta. El enlace se envía solo por correo; cuando el cliente firme, el PDF se encuentra en «Formatos firmados».</div>' +
        '<label style="' + ETIQ + '">Formato</label><select id="fmFormato" style="' + CAMPO + '">' +
        FORMATOS.map(function (f) { return '<option value="' + f[0] + '">' + f[1] + '</option>'; }).join('') + '</select>' +
        '<div style="font-size:12.5px;color:#4A5B70;margin-bottom:12px"><span id="fmAyuda"></span> El enlace vence en <span id="fmVigencia">48</span> h.</div>' +
        '<div id="fmCajaContrato"><label style="' + ETIQ + '">N° de contrato del plan de salud <span id="fmContratoOpc" style="font-weight:400;color:#6B7A8A;display:none">(opcional)</span><span id="fmContratoObl"> *</span></label>' +
        '<input id="fmContrato" maxlength="30" autocomplete="off" style="' + CAMPO + '"></div>' +
        '<div id="fmCajaRenovacion" style="display:none;gap:8px">' +
          '<div style="flex:1"><label style="' + ETIQ + '">Periodo (año) *</label><input id="fmPeriodo" type="number" min="2026" max="2100" inputmode="numeric" placeholder="2027" style="' + CAMPO + '"></div>' +
          '<div style="flex:1"><label style="' + ETIQ + '">Incremento (%) *</label><input id="fmIncremento" type="number" min="0.01" max="100" step="0.01" inputmode="decimal" placeholder="7.5" style="' + CAMPO + '"></div></div>' +
        '<label style="' + ETIQ + '">Correo del cliente *</label>' +
        '<input id="fmCorreo" type="email" maxlength="120" autocomplete="off" placeholder="cliente@correo.com" style="' + CAMPO + '">' +
        '<label style="' + ETIQ + '">Nombres y apellidos <span style="font-weight:400;color:#6B7A8A">(opcional, el cliente los confirma)</span></label>' +
        '<input id="fmNombre" maxlength="120" autocomplete="off" style="' + CAMPO + '">' +
        '<div style="display:flex;gap:8px"><div style="flex:0 0 38%"><label style="' + ETIQ + '">Documento</label>' +
          '<select id="fmTipo" style="' + CAMPO + '"><option value="DNI">DNI</option><option value="CE">CE</option><option value="OTRO">Otro</option></select></div>' +
          '<div style="flex:1"><label style="' + ETIQ + '">Número <span style="font-weight:400;color:#6B7A8A">(opcional)</span></label>' +
          '<input id="fmNumero" maxlength="20" autocomplete="off" style="' + CAMPO + '"></div></div>' +
        '<label style="' + ETIQ + '">Celular del cliente <span style="font-weight:400;color:#6B7A8A">(opcional; sirve para encontrar el formato firmado después)</span></label>' +
        '<input id="fmCelular" inputmode="numeric" maxlength="15" autocomplete="off" placeholder="987654321" style="' + CAMPO + '">' +
        '<div id="fmNotaCorreo" style="font-size:12px;color:#8A5A00;display:none;margin-bottom:8px">El envío por correo aún no está configurado en el servidor.</div>' +
        '<div id="fmEstado" style="font-size:12.5px;min-height:18px;margin-bottom:8px;white-space:pre-line"></div>' +
        '<button type="button" id="fmEnviar" style="width:100%;border:0;border-radius:9px;background:#0A4C8C;color:#fff;font-weight:700;padding:10px;font-size:14px;cursor:pointer">Enviar enlace por correo</button>' +
        '<div style="font-weight:700;font-size:13px;margin:16px 0 6px">Mis envíos por correo (últimos 15 días)</div>' +
        '<div id="fmEnviados" style="font-size:12.5px;color:#4A5B70">Cargando…</div>');
      $('fmFormato').onchange = actualizarFormato;
      $('fmEnviar').onclick = enviarFormato;
    }
    ['fmContrato', 'fmIncremento', 'fmCorreo', 'fmNombre', 'fmNumero', 'fmCelular'].forEach(function (i) { $(i).value = ''; });
    $('fmPeriodo').value = String(new Date().getFullYear() + 1);
    $('fmTipo').value = 'DNI';
    $('fmEstado').textContent = '';
    $('fmEnviar').disabled = false;
    $('fmEnviar').textContent = 'Enviar enlace por correo';
    $('fmEnviados').textContent = 'Cargando…';
    actualizarFormato();
    envio.style.display = 'flex';
    setTimeout(function () { $('fmCorreo').focus(); }, 50);
    api('GET', '/formatos/opciones').then(function (r) {
      $('fmEnviar').disabled = !r.correo_configurado;
      $('fmNotaCorreo').style.display = r.correo_configurado ? 'none' : 'block';
      pintarEnviados(r.enviados);
    }).catch(function (err) { $('fmEnviados').textContent = err.message; });
  }

  // ---------------------------------------------------------------- Buscar formatos firmados
  var busqueda = null;

  function pintarResultados(r) {
    var cont = $('ffResultados');
    var aviso = $('ffAviso');
    cont.innerHTML = '';
    if (!r.ok) { aviso.style.color = '#C0392B'; aviso.textContent = r.motivo || 'No se pudo buscar.'; return; }
    var nombreCriterio = { correo: 'correo', celular: 'celular', documento: 'documento' }[r.criterio] || r.criterio;
    if (!r.resultados.length) {
      aviso.style.color = '#4A5B70';
      aviso.textContent = 'No hay formatos firmados para ese ' + nombreCriterio + ' (' + r.valor + ').';
      return;
    }
    aviso.style.color = '#4A5B70';
    aviso.textContent = r.resultados.length + ' formato(s) firmado(s) para el ' + nombreCriterio + ' ' + r.valor + (r.resultados.length >= 50 ? ' (se muestran los 50 más recientes)' : '') + '.';
    cont.innerHTML = r.resultados.map(function (x) {
      var datos = [x.n_documento ? 'Doc. ' + esc(x.n_documento) : '', x.contrato ? 'Contrato ' + esc(x.contrato) : '',
                   x.correo ? esc(x.correo) : '', x.celular ? 'Cel. ' + esc(x.celular) : ''].filter(Boolean).join(' · ');
      return '<div style="display:flex;gap:8px;align-items:flex-start;justify-content:space-between;padding:9px 0;border-top:1px solid #EEF2F6">' +
        '<div style="min-width:0"><div style="font-weight:700;color:#1F2D3D">' + esc(x.formato_nombre) + '</div>' +
        '<div style="font-weight:600">' + esc(x.nombre || '—') + '</div>' +
        '<div style="color:#4A5B70">' + datos + '</div>' +
        '<div style="color:#6B7A8A">Firmado ' + esc(x.firmado) + (x.enviado_por ? ' · enviado por ' + esc(x.enviado_por) : '') + '</div></div>' +
        '<div style="flex-shrink:0;padding-top:2px">' + (x.ruta_pdf
          ? '<a href="' + urlArchivo(x.ruta_pdf) + '" target="_blank" rel="noopener" style="color:#0A4C8C;font-weight:700;text-decoration:none">📄 Ver PDF</a>'
          : '<span style="color:#8A97A6">Sin PDF</span>') + '</div></div>';
    }).join('');
  }

  function buscarFirmados() {
    var q = $('ffTexto').value.trim();
    var aviso = $('ffAviso');
    if (!q) { aviso.style.color = '#C0392B'; aviso.textContent = 'Escribe un correo, un celular o un DNI.'; return; }
    aviso.style.color = '#4A5B70'; aviso.textContent = 'Buscando…';
    $('ffResultados').innerHTML = '';
    api('GET', '/formatos/firmados?q=' + encodeURIComponent(q)).then(pintarResultados)
      .catch(function (err) { aviso.style.color = '#C0392B'; aviso.textContent = err.message; });
  }

  function abrirBusqueda() {
    if (!busqueda) {
      busqueda = crearModal('modalFormatosFirmados', 560,
        '<div style="font-weight:700;font-size:15px;margin-bottom:4px">🗂️ Buscar formatos firmados</div>' +
        '<div style="font-size:12.5px;color:#4A5B70;margin-bottom:10px">Busca por el <b>correo</b>, el <b>celular</b> (9 dígitos) o el <b>DNI</b> del cliente. Trae todos sus formatos firmados (siniestralidad, latencia, renovación, desafiliación y reembolso).</div>' +
        '<div style="display:flex;gap:8px"><input id="ffTexto" autocomplete="off" placeholder="correo@dominio.com, 987654321 o 44556677" style="' + CAMPO + ';margin:0;flex:1">' +
        '<button type="button" id="ffBuscar" style="border:0;border-radius:8px;background:#0A4C8C;color:#fff;font-weight:700;padding:0 16px;font-size:14px;cursor:pointer">Buscar</button></div>' +
        '<div id="ffAviso" style="font-size:12.5px;min-height:18px;margin:8px 0"></div>' +
        '<div id="ffResultados" style="font-size:12.5px"></div>');
      $('ffBuscar').onclick = buscarFirmados;
      $('ffTexto').addEventListener('keydown', function (e) { if (e.key === 'Enter') buscarFirmados(); });
    }
    $('ffTexto').value = '';
    $('ffResultados').innerHTML = '';
    $('ffAviso').style.color = '#4A5B70';
    $('ffAviso').textContent = 'Escribe el dato del cliente y presiona Buscar.';
    busqueda.style.display = 'flex';
    setTimeout(function () { $('ffTexto').focus(); }, 50);
  }

  function iniciar() {
    var b1 = $('btnFormatosEnviar'), b2 = $('btnFormatosBuscar');
    if (b1) b1.onclick = abrirEnvio;
    if (b2) b2.onclick = abrirBusqueda;
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar); else iniciar();
})();
