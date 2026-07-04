const CACHE_NAME = 'demandas-v3';
const urlsToCache = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/manifest.json',
  '/offline.html'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(urlsToCache).catch(() => {
        // Alguns URLs podem falhar, é ok
        return Promise.resolve();
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Estratégia: Network first, then cache
  if (event.request.method === 'GET') {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          // Clona a resposta
          const responseClone = response.clone();
          
          // Armazena em cache
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseClone);
          });
          
          return response;
        })
        .catch(() => {
          // Se falhar, tenta cache
          return caches.match(event.request).then((cachedResponse) => {
            return cachedResponse || new Response(
              'Offline - recurso não disponível',
              { status: 503, statusText: 'Service Unavailable' }
            );
          });
        })
    );
  } else {
    // POST, PUT, DELETE - apenas tenta network
    event.respondWith(fetch(event.request));
  }
});
