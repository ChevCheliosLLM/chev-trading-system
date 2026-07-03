const CACHE = 'chev-monitor-v1';
const SHELL = ['/monitor.html', '/chevlogo.png', '/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // Always fetch live: Firebase, Binance, Forex APIs
  if (url.includes('firebase') || url.includes('binance') || url.includes('freeforex') || url.includes('/api/')) {
    e.respondWith(fetch(e.request));
    return;
  }
  // App shell: network first, cache fallback
  e.respondWith(
    fetch(e.request)
      .then(r => { caches.open(CACHE).then(c => c.put(e.request, r.clone())); return r; })
      .catch(() => caches.match(e.request))
  );
});
