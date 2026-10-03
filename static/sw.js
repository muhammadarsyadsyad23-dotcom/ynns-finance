// Service Worker — DEVELOPMENT (no cache)
self.addEventListener("install", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", e => {
  // Selalu ambil dari network — no cache
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
