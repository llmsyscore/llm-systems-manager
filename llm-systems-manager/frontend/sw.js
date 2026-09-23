// Service worker for the PWA companion (#522). Network-first with cache
// fallback; cache name keys off the manager version stamped at serve time.
const VERSION = '__MGR_VERSION__';
const CACHE = `lsm-companion-${VERSION}`;
const SHELL = [
  '/companion',
  '/static/css/base.css?v=__MGR_VERSION__',
  '/static/css/companion.css?v=__MGR_VERSION__',
  '/static/js/lib/settingsdrawer.js?v=__MGR_VERSION__',
  '/static/js/lib/pushutil.js?v=__MGR_VERSION__',
  '/static/js/lib/energy.js?v=__MGR_VERSION__',
  '/static/js/lib/companion-spark.js?v=__MGR_VERSION__',
  '/static/js/lib/companion-view.js?v=__MGR_VERSION__',
  '/static/js/lib/sseguard.js?v=__MGR_VERSION__',
  '/static/js/lib/tower-view.js?v=__MGR_VERSION__',
  '/static/js/companion-tower.js?v=__MGR_VERSION__',
  '/static/js/companion.js?v=__MGR_VERSION__',
  '/static/icons/icon-192.png',
];

// SHELL paths without their ?v= query; the fetch handler matches on pathname
// and caches the versioned request, so the precache and runtime keys agree.
const SHELL_PATHS = new Set(SHELL.map((p) => new URL(p, self.location.origin).pathname));

// Cacheable = 200, same-request URL (a /login redirect must never be stored
// under an asset's cache key).
const cacheable = (resp) => resp && resp.ok && !resp.redirected;

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(SHELL.map(async (path) => {
      try {
        const resp = await fetch(path);
        if (cacheable(resp)) await c.put(path, resp);
      } catch (_) { /* offline install — fetch-time puts backfill later */ }
    }));
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys())
      if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

// Network-first for SHELL paths only: live responses win and refresh the
// cache; the cache answers when the network fails. Everything else — API
// calls included — passes through untouched.
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (!SHELL_PATHS.has(url.pathname)) return;
  e.respondWith((async () => {
    try {
      const resp = await fetch(e.request);
      if (cacheable(resp))
        (await caches.open(CACHE)).put(e.request, resp.clone());
      return resp;
    } catch (err) {
      // Offline: the exact versioned key first, then any cached version of the path.
      const hit = await caches.match(e.request) || await caches.match(e.request, { ignoreSearch: true });
      if (hit) return hit;
      throw err;
    }
  })());
});

self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; }
  catch (_) { data = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(
    data.title || 'LLM Systems Manager', {
      body: data.body || '',
      tag: data.tag || undefined,
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      data: { url: data.url || '/companion' },
    }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/companion';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll(
      { type: 'window', includeUncontrolled: true });
    // Only reuse a window already on the companion — never navigate away
    // from an open dashboard tab.
    for (const w of wins) {
      if (new URL(w.url).pathname.startsWith('/companion') && 'focus' in w) {
        await w.focus();
        // Already on the companion: ask it to switch tabs rather than reload.
        try { w.postMessage({ type: 'lsm-open', url }); return; } catch (_) { /* navigate */ }
        if ('navigate' in w) await w.navigate(url);
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
