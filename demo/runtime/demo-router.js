// DemoRouter: route table, matching, read/refusal rules and miss tracking for the demo worker.
(function (self) {
  var routes = [];
  var JSON_HEADERS = { 'Content-Type': 'application/json' };
  var REFUSAL = { ok: false, error: 'Not available in the demo' };
  var MISS = { ok: false, error: 'demo: no answer' };
  var FAILED = { ok: false, error: 'demo: handler error' };

  function compile(pattern) {
    var names = [];
    var src = pattern.split('/').map(function (seg) {
      if (seg.charAt(0) === ':') { names.push(seg.slice(1)); return '([^/]+)'; }
      return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('/');
    return { re: new RegExp('^' + src + '/?$'), names: names };
  }

  function json(obj, status) {
    return new Response(JSON.stringify(obj), { status: status || 200, headers: JSON_HEADERS });
  }

  function notify(clientId, msg) {
    if (!clientId || !self.clients || !self.clients.get) return Promise.resolve();
    return Promise.resolve(self.clients.get(clientId)).then(function (c) {
      if (c) c.postMessage(msg);
    }, function () {});
  }

  function readBody(request) {
    if (request.method === 'GET' || request.method === 'HEAD') return Promise.resolve(null);
    return request.text().then(function (t) {
      if (!t) return null;
      try { return JSON.parse(t); } catch (e) { return t; }
    }, function () { return null; });
  }

  var DemoRouter = {
    misses: [],
    route: function (key, handler, opts) {
      var sp = key.indexOf(' ');
      var method = key.slice(0, sp).toUpperCase();
      var c = compile(key.slice(sp + 1));
      routes.push({ key: key, method: method, re: c.re, names: c.names,
        handler: handler, read: !!(opts && opts.read), fallback: !!(opts && opts.fallback) });
    },
    // First matching route in registration order; { fallback: true } routes are tried after all others.
    find: function (method, url) {
      method = method.toUpperCase();
      var ordered = routes.filter(function (r) { return !r.fallback; })
        .concat(routes.filter(function (r) { return r.fallback; }));
      for (var i = 0; i < ordered.length; i++) {
        var r = ordered[i];
        if (r.method !== method) continue;
        var m = r.re.exec(url.pathname);
        if (!m) continue;
        var params = {};
        r.names.forEach(function (n, j) { params[n] = decodeURIComponent(m[j + 1]); });
        return { route: r, params: params };
      }
      return null;
    },
    match: function (method, url) {
      var f = DemoRouter.find(method, url);
      return f ? { key: f.route.key, params: f.params } : null;
    },
    // Answers one request; a throwing handler or a malformed path gives a 500 and a demo-error message.
    respond: function (request, now, clientId) {
      var url = new URL(request.url);
      var method = request.method.toUpperCase();
      return Promise.resolve().then(function () {
        return DemoRouter.answer(request, url, method, now, clientId);
      }).catch(function (e) {
        if (self.console) self.console.error('demo handler error', method, url.pathname, e && e.message);
        return notify(clientId, { type: 'demo-error', method: method, path: url.pathname })
          .then(function () { return json(FAILED, 500); });
      });
    },
    answer: function (request, url, method, now, clientId) {
      var f = DemoRouter.find(method, url);
      var write = method !== 'GET' && method !== 'HEAD';
      if (!f && write) {
        return notify(clientId, { type: 'demo-refused', key: method + ' ' + url.pathname })
          .then(function () { return json(REFUSAL, 403); });
      }
      if (!f) {
        var miss = method + ' ' + url.pathname;
        if (DemoRouter.misses.indexOf(miss) < 0) DemoRouter.misses.push(miss);
        return notify(clientId, { type: 'demo-miss', method: method, path: url.pathname })
          .then(function () { return json(MISS, 404); });
      }
      if (write && !f.route.read) {
        return notify(clientId, { type: 'demo-refused', key: f.route.key })
          .then(function () { return json(REFUSAL, 403); });
      }
      return readBody(request).then(function (body) {
        var ctx = { now: now, params: f.params, query: url.searchParams, body: body, fleet: self.DemoFleet };
        return f.route.handler(ctx);
      }).then(function (out) {
        if (out instanceof Response) return out;
        return json(out === undefined ? null : out, 200);
      });
    },
    keys: function () {
      return routes.map(function (r) { return r.key; });
    },
  };

  self.DemoRouter = DemoRouter;
})(self);
