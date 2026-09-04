// #782: the shared SSE reconnect guard — transient drops, CLOSED re-open with
// backoff and resume, keepalive restores the label, bounded give-up.
import { describe, it, expect, beforeEach } from 'vitest';
import SG from '../js/lib/sseguard.js';

let streams, timers;

function FakeES(url) {
  this.url = url;
  this.readyState = 0;
  streams.push(this);
}
FakeES.CONNECTING = 0; FakeES.OPEN = 1; FakeES.CLOSED = 2;
FakeES.prototype.close = function () { this.readyState = 2; this.closedByClient = true; };

function last() { return streams[streams.length - 1]; }
function msg(es, m, id) { es.onmessage({ data: JSON.stringify(m), lastEventId: id || '' }); }

function guard(extra = {}) {
  const log = { events: [], reconnecting: [], restored: 0, lost: [] };
  const g = SG.open({
    url: extra.url || '/api/x/stream', ES: FakeES, maxDrops: extra.maxDrops,
    onEvent: (m) => log.events.push(m),
    onReconnecting: (n) => log.reconnecting.push(n),
    onRestored: () => { log.restored += 1; },
    onLost: (rs) => log.lost.push(rs),
  });
  return { g, log };
}

beforeEach(() => {
  streams = []; timers = [];
  SG.timers.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  SG.timers.clearTimeout = (id) => { timers[id - 1] = null; };
});

describe('withLastId', () => {
  it('appends the resume id with the right separator', () => {
    expect(SG.withLastId('/a', '')).toBe('/a');
    expect(SG.withLastId('/a', 'r:1')).toBe('/a?last_event_id=r%3A1');
    expect(SG.withLastId('/a?agent=x', 'r:1')).toBe('/a?agent=x&last_event_id=r%3A1');
  });
});

describe('transient drop (CONNECTING)', () => {
  it('reports reconnecting once and restores on the next frame', () => {
    const { log } = guard();
    const es = last();
    es.readyState = 0; es.onerror(); es.onerror();
    expect(log.reconnecting).toEqual([1, 2]);
    expect(timers).toEqual([]);            // browser retries on its own
    es.readyState = 1;
    msg(es, { type: 'line' }, 'r:3');
    expect(log.restored).toBe(1);
    expect(log.events).toEqual([{ type: 'line' }]);
  });

  it('a keepalive is enough to restore the label but is not delivered', () => {
    const { log } = guard();
    const es = last();
    es.readyState = 0; es.onerror();
    es.readyState = 1;
    msg(es, { type: 'keepalive' });
    expect(log.restored).toBe(1);
    expect(log.events).toEqual([]);
  });

  it('gives up after maxDrops with no frame in between', () => {
    const { g, log } = guard({ maxDrops: 3 });
    const es = last();
    for (let i = 0; i < 4; i++) { es.readyState = 0; es.onerror(); }
    expect(log.lost).toEqual([0]);
    expect(es.closedByClient).toBe(true);
    expect(g.readyState).toBe(2);
  });
});

describe('closed source (non-2xx such as a pool 503)', () => {
  it('re-opens after a backoff, resuming from the last seen id', () => {
    const { g, log } = guard({ url: '/api/x/stream?agent=a1' });
    const es1 = last();
    msg(es1, { type: 'line' }, 'run1:4');
    es1.readyState = 2; es1.onerror();
    expect(log.reconnecting).toEqual([1]);
    expect(timers.length).toBe(1);
    expect(timers[0].ms).toBe(SG.BACKOFF_MS[0]);
    timers[0].fn();
    const es2 = last();
    expect(es2).not.toBe(es1);
    expect(es2.url).toBe('/api/x/stream?agent=a1&last_event_id=run1%3A4');
    expect(g.reopens).toBe(1);
    es2.readyState = 1;
    msg(es2, { type: 'line' }, 'run1:5');
    expect(log.restored).toBe(1);
    expect(log.lost).toEqual([]);
  });

  it('backs off further on repeated closes and caps the delay', () => {
    guard();
    for (let i = 0; i < SG.BACKOFF_MS.length + 2; i++) {
      const es = last();
      es.readyState = 2; es.onerror();
      timers[timers.length - 1].fn();
    }
    const delays = timers.map(t => t.ms);
    expect(delays.slice(0, SG.BACKOFF_MS.length)).toEqual(SG.BACKOFF_MS);
    expect(delays[delays.length - 1]).toBe(SG.BACKOFF_MS[SG.BACKOFF_MS.length - 1]);
  });

  it('ignores callbacks from a superseded source', () => {
    const { log } = guard();
    const es1 = last();
    es1.readyState = 2; es1.onerror();
    timers[0].fn();
    const es2 = last();
    msg(es1, { type: 'line', stale: true });
    es1.onerror();
    expect(log.events).toEqual([]);
    expect(log.reconnecting).toEqual([1]);
    msg(es2, { type: 'line' });
    expect(log.events).toEqual([{ type: 'line' }]);
  });

  it('close() cancels a pending re-open and stops all callbacks', () => {
    const { g, log } = guard();
    const es1 = last();
    es1.readyState = 2; es1.onerror();
    g.close();
    expect(timers[0]).toBeNull();
    es1.onerror();
    expect(log.lost).toEqual([]);
    expect(streams.length).toBe(1);
  });

  it('gives up after maxDrops re-opens', () => {
    const { log } = guard({ maxDrops: 2 });
    for (let i = 0; i < 3; i++) {
      const es = last();
      es.readyState = 2; es.onerror();
      const t = timers[timers.length - 1];
      if (t && i < 2) t.fn();
    }
    expect(log.lost).toEqual([2]);
    expect(streams.length).toBe(3);
  });
});

describe('LivePause gate (#822)', () => {
  it('drops frames while paused but keeps the stream and its resume id', () => {
    const { g, log } = guard();
    const orig = SG.paused;
    let paused = true;
    SG.paused = () => paused;
    try {
      msg(last(), { type: 'x', n: 1 }, 'r:1');
      expect(log.events).toEqual([]);
      expect(g.lastId).toBe('r:1');
      expect(last().closedByClient).toBeUndefined();
      paused = false;
      msg(last(), { type: 'x', n: 2 }, 'r:2');
      expect(log.events).toEqual([{ type: 'x', n: 2 }]);
    } finally {
      SG.paused = orig;
    }
  });
});
