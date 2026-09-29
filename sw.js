/*
  Service worker for the advocate site.
  What it does:
  - Saves a copy of the page and its icons the first time someone visits, so the
    page still opens (showing that saved copy) if they later open it with no
    signal.
  - Legal updates (updates.json) are never saved. That request always goes to
    the network, so the list is never shown out of date; if there is no
    signal, the page's own script already shows a friendly message for that.
  - When online, the page itself is always fetched fresh from the network;
    the saved copy is only a fallback for when the network request fails.

  If you edit index.html, change CACHE_NAME below (e.g. to "dvr-shell-v2") so
  visitors pick up the new copy instead of an old saved one.
*/

const CACHE_NAME = 'dvr-shell-v1';
const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png',
  './apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES))
      .catch(() => { /* offline or a file is missing; the site still works, just without a saved copy yet */ })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') { return; }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) { return; }  // leave fonts and other outside requests to the browser

  if (url.pathname.endsWith('/updates.json')) {
    event.respondWith(fetch(request));  // legal updates: always the network, never the saved copy
    return;
  }

  if (request.mode === 'navigate') {
    // Page loads: try the network first so visitors see the latest content;
    // fall back to the saved copy only when there is no signal.
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('./index.html', copy));
          return response;
        })
        .catch(() => caches.match('./index.html'))
    );
    return;
  }

  // Icons, the manifest, and other saved files: the saved copy first, network as a backup.
  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request))
  );
});
