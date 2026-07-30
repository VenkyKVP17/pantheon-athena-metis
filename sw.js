const CACHE_NAME = 'metis-anki-v7';
const ASSETS_TO_CACHE = [
  './metis_dashboard.html',
  './athena_dashboard.html',
  './manifest.json',
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((key) => (key !== CACHE_NAME ? caches.delete(key) : null)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache the API: login/session state, live decks/cards/reviews must
  // always hit the server. This is the fix for progress silently failing to
  // save/sync — caching a review POST (or serving a stale GET) here would be
  // exactly that bug again.
  if (url.pathname.startsWith('/api/') || url.pathname === '/session' || url.pathname === '/metrics') {
    event.respondWith(fetch(event.request));
    return;
  }

  if (event.request.method !== 'GET') {
    event.respondWith(fetch(event.request));
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).catch(() => {
        const accept = event.request.headers.get('accept') || '';
        if (accept.includes('text/html')) {
          return caches.match('./metis_dashboard.html');
        }
      });
    })
  );
});
