const CACHE = "become-shell-v9";
const SHELL = ["/", "/styles.css", "/app.js", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
});

self.addEventListener("message", event => {
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith("become-shell-") && key !== CACHE).map(key => caches.delete(key)));
    await self.clients.claim();
    for (const client of await self.clients.matchAll({type: "window"})) client.postMessage({type: "SHELL_UPDATED"});
  })());
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request, {cache: "no-cache"});
      if (response.ok) (await caches.open(CACHE)).put(event.request, response.clone());
      return response;
    } catch {
      return (await caches.match(event.request)) || (await caches.match("/"));
    }
  })());
});
