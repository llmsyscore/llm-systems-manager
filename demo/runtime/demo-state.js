// DemoState: the visitor's saved demo changes, kept in this browser's Cache Storage (in memory when that fails).
(function (self) {
  var CACHE = 'lsmdemo-state', KEY = '/demo-state/all';
  var mem = {};
  var queue;

  function store() {
    return self.caches && self.caches.open ? self.caches.open(CACHE) : Promise.reject(new Error('no caches'));
  }
  function copy(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }

  var RELEASE = self.DEMO_RELEASE || 'dev';
  var WAIT_MS = 1500;

  // Reads this release's saved changes once (another release's are dropped); a key set first keeps its value.
  var load = store().then(function (c) { return c.match(KEY); })
    .then(function (r) { return r ? r.json() : null; })
    .then(function (saved) {
      var data = saved && saved.release === RELEASE && saved.data && typeof saved.data === 'object' ? saved.data : {};
      Object.keys(data).forEach(function (k) { if (!(k in mem)) mem[k] = data[k]; });
    }, function () {});
  // Reads and writes give up after WAIT_MS so a storage that never replies cannot hold answers back.
  function bounded(p) { return Promise.race([p, new Promise(function (res) { setTimeout(res, WAIT_MS); })]); }
  var ready = bounded(load);
  queue = ready;

  function write() {
    var body = JSON.stringify({ release: RELEASE, data: mem });
    return bounded(store().then(function (c) {
      return c.put(KEY, new Response(body, { headers: { 'Content-Type': 'application/json' } }));
    })).then(function () {}, function () {});
  }

  self.DemoState = {
    ready: ready,
    get: function (key, dflt) { return key in mem ? copy(mem[key]) : dflt; },
    // Saves one key; the returned promise settles once the change is written (never rejects).
    set: function (key, value) {
      mem[key] = copy(value);
      queue = queue.then(write);
      return queue;
    }
  };
})(self);
