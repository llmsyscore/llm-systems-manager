// #514: executes events-toasts.js in jsdom and asserts the bridge dial
// carries a freshly-fetched ticket, including across reconnects.
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'events-toasts.js'), 'utf8');

let dialled;      // every URL handed to `new WebSocket`
let sockets;      // the fake socket instances, so tests can fire onclose
let fetchCalls;

function installStubs({ wsUrl, ticketOk = true, ticket = '9999999999.abc' } = {}) {
  dialled = [];
  sockets = [];
  fetchCalls = [];

  if (wsUrl === undefined) delete window.__AE_WS_URL__;
  else window.__AE_WS_URL__ = wsUrl;

  globalThis.fetch = vi.fn(async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (!ticketOk) return { ok: false, status: 401, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ ticket, ttl_s: 300 }) };
  });

  class FakeWS {
    constructor(url) {
      dialled.push(url);
      this.url = url;
      sockets.push(this);
    }
    close() { if (this.onclose) this.onclose(); }
  }
  globalThis.WebSocket = FakeWS;
  window.WebSocket = FakeWS;
}

// events-toasts.js is a classic-script IIFE, not a module — run it the same
// way the browser would, in the jsdom global scope.
async function runScript() {
  // eslint-disable-next-line no-new-func
  new Function(src)();
  await vi.advanceTimersByTimeAsync(1600);   // clears the 1500ms deferred connect
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('bridge dial (#514)', () => {
  test('fetches a ticket and appends it to the bridge URL', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm' });
    await runScript();

    expect(fetchCalls).toHaveLength(1);
    expect(fetchCalls[0].url).toBe('/api/alarm-ws-ticket');
    expect(fetchCalls[0].opts).toMatchObject({ credentials: 'same-origin' });
    expect(dialled).toEqual(['ws://host:5444/ws/alarm?ticket=9999999999.abc']);
  });

  test('never dials before the ticket resolves', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm' });
    await runScript();
    // A dial without a ticket query would mean the await was skipped.
    expect(dialled.every(u => u.includes('ticket='))).toBe(true);
  });

  test('url-encodes the ticket', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm', ticket: '1+2/3=4' });
    await runScript();
    expect(dialled[0]).toContain('ticket=1%2B2%2F3%3D4');
  });

  test('uses & when the bridge URL already carries a query', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm?x=1' });
    await runScript();
    expect(dialled[0]).toBe('ws://host:5444/ws/alarm?x=1&ticket=9999999999.abc');
  });
});

describe('direct AE dial (no bridge)', () => {
  test('does not request a ticket when not going through /ws/alarm', async () => {
    installStubs({ wsUrl: 'ws://ae-host:8081/ws' });
    await runScript();
    expect(fetchCalls).toHaveLength(0);
    expect(dialled).toEqual(['ws://ae-host:8081/ws']);
  });

  test('the injected-URL-absent fallback still dials the AE directly', async () => {
    installStubs({ wsUrl: undefined });
    await runScript();
    expect(fetchCalls).toHaveLength(0);
    expect(dialled[0]).toMatch(/:8081\/ws$/);
  });

  test('an injected empty URL disables the stream entirely (#519)', async () => {
    // "" means the AE read bearer is set and the bridge is off — a direct
    // dial can only 1008-loop, so nothing must be dialled at all.
    installStubs({ wsUrl: '' });
    await runScript();
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetchCalls).toHaveLength(0);
    expect(dialled).toHaveLength(0);
  });
});

describe('ticket failure handling', () => {
  test('a rejected ticket does not dial, and backs off instead of hammering', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm', ticketOk: false });
    await runScript();

    expect(dialled).toHaveLength(0);
    const afterFirst = fetchCalls.length;
    expect(afterFirst).toBe(1);

    // Initial delay is 3000ms and grows 1.5x; at 3000ms elapsed the retry
    // must not have fired yet (it is scheduled at 4500ms).
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchCalls.length).toBe(afterFirst);

    await vi.advanceTimersByTimeAsync(1600);
    expect(fetchCalls.length).toBe(afterFirst + 1);
  });
});

describe('reconnect', () => {
  test('fetches a fresh ticket on every reconnect', async () => {
    installStubs({ wsUrl: 'ws://host:5444/ws/alarm' });
    await runScript();
    expect(fetchCalls).toHaveLength(1);

    sockets[0].close();                       // triggers onclose -> backoff
    await vi.advanceTimersByTimeAsync(5000);

    expect(fetchCalls.length).toBeGreaterThanOrEqual(2);
    expect(dialled.length).toBeGreaterThanOrEqual(2);
    expect(dialled.every(u => u.includes('ticket='))).toBe(true);
  });
});
