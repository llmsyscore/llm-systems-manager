// Demo service worker: answers every dashboard request in the browser from the demo runtime.
(function (self) {
  var RELEASE = self.DEMO_RELEASE || 'dev';
  var CACHE = 'lsm-demo-' + RELEASE;
  var stamp = function (p) { return p + '?v=' + encodeURIComponent(RELEASE); };
  self.importScripts.apply(self, ['/demo/demo-router.js', '/demo/demo-sse.js',
    '/demo/demo-fleet.js', '/demo/demo-series.js'].concat(self.DEMO_HANDLERS || []).map(stamp));

  var ASSET_RE = /(?<![\w-])(?:src|href)=(["'])([^"'#]+)\1/g;

  // Lists same-origin /static and /alarm asset URLs referenced by a cached page.
  function assetUrls(html, base) {
    var out = [];
    var m;
    ASSET_RE.lastIndex = 0;
    while ((m = ASSET_RE.exec(html))) {
      var u = new URL(m[2], base);
      if (u.origin !== new URL(base).origin) continue;
      if (u.pathname.indexOf('/static/') === 0 || u.pathname.indexOf('/alarm/') === 0) out.push(u.href);
    }
    return out;
  }

  // Copies a fetched response so a redirected one can still answer a navigation.
  function clean(res) {
    return res.redirected ? new Response(res.body, { status: res.status, statusText: res.statusText, headers: res.headers }) : res;
  }

  function precache() {
    var origin = self.location.origin;
    return self.caches.open(CACHE).then(function (cache) {
      var pages = ['/dashboard', '/alarm/'].map(function (p) { return new URL(p, origin).href; });
      return Promise.all(pages.map(function (url) {
        return fetch(new Request(url, { cache: 'reload' })).then(clean).then(function (res) {
          if (!res.ok) throw new Error('demo: precache ' + url + ' ' + res.status);
          return res.clone().text().then(function (html) {
            return cache.put(url, res).then(function () { return assetUrls(html, url); });
          });
        });
      })).then(function (lists) {
        var urls = [].concat.apply([], lists).filter(function (u, i, a) { return a.indexOf(u) === i; });
        return Promise.allSettled(urls.map(function (url) {
          return fetch(new Request(url, { cache: 'reload' })).then(clean).then(function (res) {
            if (res.ok) return cache.put(url, res);
          });
        }));
      });
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

  self.DemoSw = { assetUrls: assetUrls };

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
    } else if (req.mode === 'navigate' && (p === '/dashboard' || p === '/dashboard/')) {
      event.respondWith(self.caches.open(CACHE).then(function (cache) {
        return cache.match(new URL('/dashboard', url).href);
      }).then(function (hit) { return hit || fetch('/dashboard').then(clean); }));
    } else if (p.indexOf('/static/') === 0 || p.indexOf('/alarm/') === 0 || p.indexOf('/demo/') === 0) {
      event.respondWith(cacheThenNetwork(event, req));
    }
  });
})(self);
