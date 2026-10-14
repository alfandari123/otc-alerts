// Makes the trading screen installable as a phone app. Always loads fresh data from the network;
// the page itself is kept so the app still opens (with the last view) when the phone is offline.
const CACHE = "otc-screen-v1";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["./", "./index.html", "./manifest.webmanifest", "./icon-192.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;   // data / Schwab: never cached
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request).then(r => r || caches.match("./"))));
});
