// Reconnect guard for tool SSE streams (#782): a CONNECTING drop rides the
// browser's own retry, a CLOSED source is re-opened with backoff and resumed
// via ?last_event_id=, and any frame (keepalive included) restores the label.
// Dual-mode lib: classic <script> global (window.SG) and vitest-importable.
(function () {
  const BACKOFF_MS = [1000, 2000, 4000, 8000, 10000];
  const DEFAULT_MAX_DROPS = 20;
  const timers = {
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };

  function withLastId(url, id) {
    if (!id) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&')
      + 'last_event_id=' + encodeURIComponent(id);
  }

  function backoffMs(drop) {
    return BACKOFF_MS[Math.min(Math.max(drop, 1), BACKOFF_MS.length) - 1];
  }

  // opts: url, onEvent(msg, ev), onReconnecting(drop), onRestored(),
  // onLost(readyState), maxDrops, ES (EventSource constructor override).
  function open(opts) {
    const ES = opts.ES || (typeof EventSource !== 'undefined' ? EventSource : null);
    const maxDrops = opts.maxDrops || DEFAULT_MAX_DROPS;
    const g = { es: null, lastId: '', drops: 0, reconnecting: false,
                closed: false, retry: null, reopens: 0 };

    function connect() {
      const es = new ES(withLastId(opts.url, g.lastId));
      g.es = es;
      es.onmessage = (ev) => {
        if (g.closed || es !== g.es) return;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (ev.lastEventId) g.lastId = ev.lastEventId;
        g.drops = 0;
        if (g.reconnecting) {
          g.reconnecting = false;
          if (opts.onRestored) opts.onRestored();
        }
        if (msg && msg.type === 'keepalive') return;
        opts.onEvent(msg, ev);
      };
      es.onerror = () => {
        if (g.closed || es !== g.es) return;
        const rs = es.readyState;
        if (++g.drops <= maxDrops) {
          if (rs === ES.CLOSED) {
            g.reopens += 1;
            g.retry = timers.setTimeout(() => {
              g.retry = null;
              if (!g.closed) connect();
            }, backoffMs(g.drops));
          }
          if (!g.reconnecting) g.reconnecting = true;
          if (opts.onReconnecting) opts.onReconnecting(g.drops);
          return;
        }
        g.reconnecting = false;
        close();
        if (opts.onLost) opts.onLost(rs);
      };
    }

    function close() {
      g.closed = true;
      if (g.retry) { timers.clearTimeout(g.retry); g.retry = null; }
      if (g.es) { try { g.es.close(); } catch (_) { /* already closed */ } g.es = null; }
    }

    connect();
    return {
      close,
      get readyState() { return g.es ? g.es.readyState : (ES ? ES.CLOSED : 2); },
      get source() { return g.es; },
      get reconnecting() { return g.reconnecting; },
      get drops() { return g.drops; },
      get reopens() { return g.reopens; },
      get lastId() { return g.lastId; },
    };
  }

  const _SG_API = { open, withLastId, backoffMs, timers, BACKOFF_MS, DEFAULT_MAX_DROPS };
  if (typeof window !== 'undefined') window.SG = _SG_API;
  if (typeof module !== 'undefined' && module.exports) module.exports = _SG_API;
})();
