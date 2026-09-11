// #904: toasts classify on the alarm state the engine sends, not on the
// message text, and only actionable toasts carry the Ack/Close controls.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'events-toasts.js'), 'utf8');

function harness() {
  const dom = new JSDOM(
    '<!doctype html><html><head></head><body><div id="alarmToastContainer"></div></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.__AE_WS_URL__ = 'ws://localhost/ws';
  // jsdom ships no CSS object; the incident-coalescing path needs CSS.escape.
  w.CSS = { escape: (s) => String(s).replace(/[^\w-]/g, (c) => '\\' + c) };
  const sockets = [];
  w.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); }
    close() {}
  };
  // The module defers connect() by 1500 ms; run that one immediately.
  const realTimeout = w.setTimeout;
  w.setTimeout = (fn, ms) => (ms === 1500 ? (fn(), 0) : realTimeout(fn, ms));
  const s = w.document.createElement('script');
  s.textContent = src;
  w.document.head.appendChild(s);
  w.setTimeout = realTimeout;
  return { w, sockets };
}

async function deliver(payload) {
  const { w, sockets } = harness();
  // connect() awaits dialUrl() before the socket exists.
  await new Promise((r) => setTimeout(r, 0));
  const ws = sockets[0];
  ws.onmessage({ data: JSON.stringify({ event: 'notification', data: { action: 'toast', ...payload } }) });
  const el = w.document.querySelector('#alarmToastContainer .ae-toast');
  return { w, el };
}

const base = { title: 'GPU hot', body: 'llama-box · gpu = 91', severity: 'critical', alert_id: 'a1' };

describe('#904 toast categorisation', () => {
  test('an actionable alert keeps the Ack/Close controls', async () => {
    const { el } = await deliver({ ...base, category: 'alert', alert_status: 'active' });
    expect(el.className).toContain('ae-toast-critical');
    expect(el.querySelector('.ae-toast-actions')).not.toBeNull();
  });

  test('an acknowledged alert drops the controls even when the text says nothing', async () => {
    const { el } = await deliver({ ...base, category: 'ack', alert_status: 'acknowledged' });
    expect(el.className).toContain('ae-toast-ack');
    expect(el.querySelector('.ae-toast-actions')).toBeNull();
  });

  test('alarm state classifies when the engine sent no category', async () => {
    const { el } = await deliver({ ...base, alert_status: 'acknowledged' });
    expect(el.className).toContain('ae-toast-ack');
    expect(el.querySelector('.ae-toast-actions')).toBeNull();
  });

  test('a closed alert reads as cleared regardless of wording', async () => {
    const { el } = await deliver({ ...base, title: 'GPU hot', alert_status: 'closed' });
    expect(el.className).toContain('ae-toast-clear');
    expect(el.querySelector('.ae-toast-actions')).toBeNull();
  });

  test('a firing alert whose text happens to contain "clear" stays actionable', async () => {
    const { el } = await deliver({ ...base, title: 'Disk not clear', alert_status: 'active' });
    expect(el.className).toContain('ae-toast-critical');
    expect(el.querySelector('.ae-toast-actions')).not.toBeNull();
  });

  test('text inference still applies when the engine sends no state at all', async () => {
    const { el } = await deliver({ ...base, title: 'Cleared: GPU hot' });
    expect(el.className).toContain('ae-toast-clear');
    expect(el.querySelector('.ae-toast-actions')).toBeNull();
  });

  test('an in-place incident update to a cleared toast removes the controls', async () => {
    const { w, sockets } = harness();
    await new Promise((r) => setTimeout(r, 0));
    const ws = sockets[0];
    const send = (p) => ws.onmessage({ data: JSON.stringify({ event: 'notification', data: { action: 'toast', ...p } }) });
    send({ ...base, alert_status: 'active', incident_id: 'inc1' });
    expect(w.document.querySelector('.ae-toast .ae-toast-actions')).not.toBeNull();
    send({ ...base, title: 'Cleared: GPU hot', alert_status: 'closed', incident_id: 'inc1' });
    const el = w.document.querySelector('#alarmToastContainer .ae-toast');
    expect(el.className).toContain('ae-toast-clear');
    expect(el.querySelector('.ae-toast-actions')).toBeNull();
  });

  test('an incident that re-fires after clearing gets its controls back', async () => {
    const { w, sockets } = harness();
    await new Promise((r) => setTimeout(r, 0));
    const ws = sockets[0];
    const send = (p) => ws.onmessage({ data: JSON.stringify({ event: 'notification', data: { action: 'toast', ...p } }) });
    send({ ...base, alert_status: 'active', incident_id: 'inc1', sticky: true });
    send({ ...base, title: 'Cleared: GPU hot', alert_status: 'closed', incident_id: 'inc1', sticky: true });
    expect(w.document.querySelector('.ae-toast .ae-toast-actions')).toBeNull();
    send({ ...base, alert_status: 'active', incident_id: 'inc1', sticky: true });
    const toasts = w.document.querySelectorAll('#alarmToastContainer .ae-toast');
    expect(toasts.length).toBe(1);
    expect(toasts[0].className).toContain('ae-toast-critical');
    expect(toasts[0].querySelector('.ae-toast-actions')).not.toBeNull();
  });
});
