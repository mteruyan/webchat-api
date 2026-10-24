// Service worker mínimo de la PWA de Sofi (experimento).
// Solo guarda el "cascarón" (pantalla de inicio, íconos) para que el app abra
// instantáneo y muestre un aviso sin internet. NUNCA toca la API, el WebSocket,
// /upload ni /widget.js: el chat siempre va directo a la red.
const VERSION = "sofi-app-v3";
const CASCARON = ["/app", "/app/manifest.webmanifest", "/app/icon-192.png", "/app/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(CASCARON)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app")) return;
  // Red primero; si no hay internet, el cascarón guardado.
  e.respondWith(fetch(req).then((r) => {
    if (r.ok) { const copia = r.clone(); caches.open(VERSION).then((c) => c.put(req, copia)); }
    return r;
  }).catch(() => caches.match(req).then((r) => r || caches.match("/app"))));
});

// ---- Avisos push -----------------------------------------------------------
// El servidor manda solo texto genérico (se ve en la pantalla de bloqueo): nunca datos de salud.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {}; }
  const titulo = d.titulo || "Sanitas";
  e.waitUntil(self.registration.showNotification(titulo, {
    body: d.cuerpo || "Tienes una novedad en Sanitas.",
    icon: "/app/icon-notif-192.png",   // isotipo de Sanitas (mariposa) sobre azul
    badge: "/app/badge-96.png",        // silueta blanca para la barra de estado de Android
    tag: d.tag || "sanitas",           // un aviso nuevo reemplaza al anterior del mismo tipo
    renotify: true,
    timestamp: Date.now(),
    vibrate: [120, 60, 120],
    actions: [{ action: "abrir", title: "Abrir chat" }],
    data: { url: d.url || "/app?ac=1" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const destino = new URL((e.notification.data && e.notification.data.url) || "/app?ac=1", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((ventanas) => {
    // Si el app ya está abierta, se trae al frente en vez de abrir otra.
    for (const v of ventanas) {
      if (new URL(v.url).origin === new URL(destino).origin && "focus" in v) {
        // Se trae al frente y se lleva al chat (?ac=1 abre el panel solo).
        return v.focus().then((f) => (f && "navigate" in f && f.url !== destino ? f.navigate(destino) : f));
      }
    }
    return self.clients.openWindow(destino);
  }));
});

// Si el navegador renueva la suscripción, el app la vuelve a registrar al abrirse.
self.addEventListener("pushsubscriptionchange", () => {});
