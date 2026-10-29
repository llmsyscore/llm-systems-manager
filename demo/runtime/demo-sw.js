// Demo service worker: answers every dashboard request in the browser from the demo runtime.
(function (self) {
  // On an old demo address: moves open tabs to the demo's domain, then removes its caches and itself.
  var MOVED = ['llmsyscore-demo.pages.dev', 'demo.llmsyscore.com'], HOME = 'https://demo.llmsysmanager.com';
  if (MOVED.indexOf(self.location.hostname) >= 0) {
    self.addEventListener('install', function () { self.skipWaiting(); });
    self.addEventListener('activate', function (event) {
      event.waitUntil(self.clients.claim().then(function () { return self.clients.matchAll({ type: 'window' }); }).then(function (tabs) {
        return Promise.all(tabs.map(function (t) { var u = new URL(t.url); return t.navigate(HOME + u.pathname + u.search); }));
      }).then(function () { return self.caches.keys(); }).then(function (names) {
        return Promise.all(names.map(function (n) { return self.caches.delete(n); }));
      }).then(function () { return self.registration.unregister(); }));
    });
    return;
  }
  var RELEASE = self.DEMO_RELEASE || 'dev';
  var CACHE = 'lsm-demo-' + RELEASE;
  var stamp = function (p) { return p + '?v=' + encodeURIComponent(RELEASE); };
  self.importScripts.apply(self, ['/demo/demo-router.js', '/demo/demo-sse.js',
    '/demo/demo-fleet.js', '/demo/demo-series.js', '/demo/demo-state.js'].concat(self.DEMO_HANDLERS || []).map(stamp));

  // Demo pages answered from the cache when the visitor opens them.
  var PAGES = ['/dashboard', '/phone'];

  // Copies a fetched response so a redirected one can still answer a navigation.
  function clean(res) {
    return res.redirected ? new Response(res.body, { status: res.status, statusText: res.statusText, headers: res.headers }) : res;
  }

  // Stores the demo pages; their scripts and styles are cached on first use, so the demo opens without waiting for them.
  function precache() {
    var origin = self.location.origin;
    return self.caches.open(CACHE).then(function (cache) {
      var pages = ['/dashboard', '/phone', '/alarm/'].map(function (p) { return new URL(p, origin).href; });
      return Promise.all(pages.map(function (url) {
        return fetch(new Request(url, { cache: 'reload' })).then(clean).then(function (res) {
          if (!res.ok) throw new Error('demo: precache ' + url + ' ' + res.status);
          return cache.put(url, res);
        });
      }));
    });
  }

  // Answers from this release's cache, else fetches and stores a copy while the event stays alive.
  function cacheThenNetwork(event, request) {
    return self.caches.open(CACHE).then(function (cache) {
      return cache.match(request).then(function (hit) {
        if (hit) return hit;
        return fetch(request).then(clean).then(function (res) {
          if (res.ok && request.method === 'GET') event.waitUntil(cache.put(request, res.clone()));
          return res;
        });
      });
    });
  }

  self.addEventListener('install', function (event) {
    self.skipWaiting();
    event.waitUntil(precache());
  });

  self.addEventListener('activate', function (event) {
    event.waitUntil(self.caches.keys().then(function (names) {
      return Promise.all(names.filter(function (n) {
        return n.indexOf('lsm-demo-') === 0 && n !== CACHE;
      }).map(function (n) { return self.caches.delete(n); }));
    }).then(function () { return self.clients.claim(); }));
  });

  self.addEventListener('fetch', function (event) {
    var req = event.request;
    var url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    var p = url.pathname;
    var client = event.clientId || event.resultingClientId;
    if (p.indexOf('/api/') === 0 || p.indexOf('/alarm/api/') === 0 || p === '/alarm/health'
        || p === '/tools-ping') {
      event.respondWith(self.DemoRouter.respond(req, Date.now(), client));
    } else if (p.indexOf('/proxy/') === 0) {
      event.respondWith(cacheThenNetwork(event, new Request(new URL('/demo/placeholder', url).href)));
    } else if (req.mode === 'navigate' && PAGES.indexOf(p.replace(/\/$/, '')) >= 0) {
      var page = p.replace(/\/$/, '');
      event.respondWith(self.caches.open(CACHE).then(function (cache) {
        return cache.match(new URL(page, url).href);
      }).then(function (hit) { return hit || fetch(page).then(clean); }));
    } else if (p.indexOf('/static/') === 0 || p.indexOf('/alarm/') === 0 || p.indexOf('/demo/') === 0) {
      event.respondWith(cacheThenNetwork(event, req));
    }
  });
})(self);
