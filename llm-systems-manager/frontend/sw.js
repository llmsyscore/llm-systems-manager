// Service worker for the PWA companion (#522). Network-first with cache
// fallback; cache name keys off the manager version stamped at serve time.
const VERSION = '__MGR_VERSION__';
const CACHE = `lsm-companion-${VERSION}`;
const SHELL = [
  '/companion',
  '/static/css/base.css',
  '/static/css/companion.css',
  '/static/js/lib/pushutil.js',
  '/static/js/companion.js',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys())
      if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

// Network-first: live responses win and refresh the shell cache; the cache
// only answers when the network fails. API calls are never cached.
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith((async () => {
    try {
      const resp = await fetch(e.request);
      if (resp.ok && SHELL.includes(url.pathname))
        (await caches.open(CACHE)).put(e.request, resp.clone());
      return resp;
    } catch (err) {
      const hit = await caches.match(e.request);
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
    for (const w of wins) {
      if ('focus' in w) {
        await w.focus();
        if ('navigate' in w) await w.navigate(url);
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
