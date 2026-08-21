/* Service worker for the Ward Monitor.
 *
 * It caches the application shell — stylesheet, icons, the offline page — and
 * NOTHING ELSE. Patient data is never written to the device cache, and no page
 * that contains it is ever served from cache.
 *
 * That is a deliberate clinical decision, not an oversight. A nurse acting on a
 * cached NEWS2 score from three hours ago is worse off than a nurse who is told
 * plainly that the device is offline. Stale observations, an outdated allergy
 * list or a superseded drug chart are exactly the things that cause harm, and
 * a cached copy on a shared ward tablet is also patient data sitting on a device
 * outside the server's control.
 */
const VERSION = "ward-monitor-v1";
const SHELL = [
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/offline",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // The shell may be served from cache; it holds no patient information.
  const isShell = url.pathname.startsWith("/static/");
  if (isShell) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(VERSION).then((cache) => cache.put(request, copy));
        return response;
      }))
    );
    return;
  }

  // Everything else is network-only. Offline means offline, not "here is
  // yesterday's chart".
  event.respondWith(
    fetch(request).catch(() =>
      request.mode === "navigate" ? caches.match("/offline") : Response.error()
    )
  );
});
