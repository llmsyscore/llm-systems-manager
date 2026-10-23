import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

// Loads the page scripts with mocked timers and a recording stand-in for the browser's WebSocket.
function page() {
  const made = [];
  function RealWs(url, protocols) { this.url = url; this.protocols = protocols; made.push(this); }
  RealWs.OPEN = 1;
  const self = load(['handlers/alarms.js', 'demo-page.js'], {
    WebSocket: RealWs, location: new URL('https://demo.example/dashboard'),
  });
  return { self, made, RealWs };
}

function frames(ws) {
  const got = [];
  ws.onmessage = (e) => got.push(JSON.parse(e.data));
  return got;
}

test('demo URLs get the stand-in: opens asynchronously; first update at 90-120 s, then every 45-90 s, toast every second one', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { self, made } = page();
  const ws = new self.WebSocket('wss://demo.invalid/ws');
  assert.equal(made.length, 0);
  assert.equal(ws.readyState, 0);
  let openState = null;
  ws.onopen = () => { openState = ws.readyState; ws.send(JSON.stringify({ action: 'subscribe', channels: ['alerts', 'metrics', 'rules'] })); };
  const got = frames(ws);
  t.mock.timers.tick(0);
  assert.equal(openState, 1);
  assert.equal(ws.readyState, self.WebSocket.OPEN);
  t.mock.timers.tick(1000);
  assert.equal(got.length, 0, 'subscribe gets no reply');
  const at = [], batches = [];
  for (let ms = 1000; ms < 600000; ms += 1000) {
    const n = got.length;
    t.mock.timers.tick(1000);
    if (got.length > n) { at.push(ms + 1000); batches.push(got.slice(n)); }
  }
  assert.ok(at.length >= 5, `updates at ${at}`);
  assert.ok(at[0] >= 90000 && at[0] <= 121000, `first at ${at[0]}`);
  for (let i = 1; i < at.length; i++) {
    const gap = at[i] - at[i - 1];
    assert.ok(gap >= 45000 && gap <= 91000, `gap ${gap}`);
  }
  batches.forEach((b, i) => {
    assert.deepEqual(b.map((f) => f.event), i % 2 ? ['alert_created', 'notification'] : ['alert_created'], `update ${i}`);
  });
  const [first] = batches[0];
  assert.ok(typeof first.ts === 'string' && !Number.isNaN(Date.parse(first.ts)));
  const open = self.DemoAlarms.openAlerts(Date.now()).map((a) => a.alert_id);
  assert.ok(open.includes(first.data.alert_id));
  assert.equal(first.data.source_host, 'forge.example');
  assert.equal(first.data.incident_size, 1);
  const [created, toast] = batches[1];
  assert.equal(created.data.alert_id, first.data.alert_id);
  assert.equal(toast.data.action, 'toast');
  assert.equal(toast.data.alert_id, created.data.alert_id);
  assert.match(toast.data.body, /^forge\.example · system · gpu_temperature_c = /);
  assert.equal(batches[2][0].data.severity, 'info');
  assert.equal(batches[3][1].data.severity, 'info');
});

test('ping gets a pong, bad input an error, close fires once and stops updates', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { self } = page();
  const ws = new self.WebSocket('ws://demo.example/ws/alarm?ticket=abc');
  const got = frames(ws);
  let closes = 0;
  ws.addEventListener('close', (e) => { closes++; assert.equal(e.code, 1000); });
  t.mock.timers.tick(0);
  ws.send(JSON.stringify({ action: 'ping' }));
  ws.send('not json');
  ws.send(JSON.stringify({ action: 'dance' }));
  t.mock.timers.tick(0);
  assert.deepEqual(got.map((f) => f.event), ['pong', 'error', 'error']);
  assert.equal(got[1].data.message, 'Invalid JSON');
  assert.equal(got[2].data.message, 'Unknown action: dance');
  ws.close();
  ws.close();
  t.mock.timers.tick(0);
  assert.equal(ws.readyState, 3);
  assert.equal(closes, 1);
  t.mock.timers.tick(200000);
  assert.equal(got.length, 3);
});

test('other URLs go to the real WebSocket', () => {
  const { self, made, RealWs } = page();
  const ws = new self.WebSocket('wss://elsewhere.example/socket', ['p1']);
  assert.equal(made.length, 1);
  assert.equal(ws, made[0]);
  assert.ok(ws instanceof RealWs);
  assert.equal(ws.url, 'wss://elsewhere.example/socket');
  assert.deepEqual(ws.protocols, ['p1']);
  assert.equal(self.DemoWs.isDemoUrl('wss://demo.invalid/ws'), true);
  assert.equal(self.DemoWs.isDemoUrl('/ws/alarm'), true);
  assert.equal(self.DemoWs.isDemoUrl('wss://elsewhere.example/ws'), false);
});

test('loading the page script twice keeps one wrapper', () => {
  const { self, RealWs } = page();
  const first = self.WebSocket;
  self.importScripts('/demo/demo-page.js');
  assert.equal(self.WebSocket, first);
  assert.equal(self.DemoWs.RealWebSocket, RealWs);
});
