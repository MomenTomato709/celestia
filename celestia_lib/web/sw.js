/* Service worker de la app de Celestia.

   Hace tres cosas y nada más:
   1. Saca los avisos push aunque la app esté cerrada (Chrome lo despierta).
   2. Al tocar un aviso, trae la app delante (o la abre si no lo estaba).
   3. Si Celestia está apagada, la app abre igual con la última carcasa que
      vio, en vez de la página de error del navegador. La carcasa no lleva
      datos: el propio chat ya dice «no la alcanzo — ¿está encendida?».

   Nada de cachear respuestas, hilos ni imágenes: todo eso es de Celestia y
   tiene que llegar fresco. */

const CACHE = "celestia-carcasa-v1";
const CARCASA = "/chat";

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.add(new Request(CARCASA, {cache: "no-store"})))
      .catch(() => {})            // sin Celestia al instalar: ya se guardará luego
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Sólo la navegación al chat: primero la red (siempre la versión nueva) y, si
   no contesta, la copia guardada. Todo lo demás pasa sin tocarlo. */
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.mode !== "navigate") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/chat")) return;
  e.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp.ok) {
          const copia = resp.clone();
          caches.open(CACHE).then((c) => c.put(CARCASA, copia)).catch(() => {});
        }
        return resp;
      })
      .catch(() => caches.match(CARCASA).then((r) => r || Response.error()))
  );
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {cuerpo: e.data && e.data.text()}; }
  // Parte de lo que pasa se le cuenta a Celestia (`/push/recibido`): sin él,
  // un aviso que no sale no deja rastro en ningún sitio que se pueda leer
  // desde fuera del móvil.
  const contar = (resultado) => fetch("/push/recibido", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({resultado, permiso: (self.Notification && Notification.permission) || "?"}),
  }).catch(() => {});
  e.waitUntil(
    self.registration.showNotification(d.titulo || "Celestia", {
      body: d.cuerpo || "Te ha dejado algo",
      icon: "/app/icono-192.png",
      badge: "/app/insignia.png",
      tag: "celestia",            // uno solo: que no se apilen veinte
      renotify: true,             // …pero que el nuevo sí suene
      data: {url: d.url || "/chat"},
    })
      .then(() => self.registration.getNotifications({tag: "celestia"}))
      .then((ns) => contar("enseñado (" + ns.length + " visibles)"))
      .catch((err) => contar("error: " + err))
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const destino = new URL((e.notification.data && e.notification.data.url) || "/chat",
                          self.location.origin).href;
  e.waitUntil(
    self.clients.matchAll({type: "window", includeUncontrolled: true}).then((ventanas) => {
      for (const v of ventanas) {
        if (new URL(v.url).pathname.startsWith("/chat") && "focus" in v) return v.focus();
      }
      return self.clients.openWindow(destino);
    })
  );
});
