import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/alarms.js', 'handlers/tower.js']);
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function raw(method, path, body = null, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find(method, url);
  assert.ok(found, `no route for ${method} ${path}`);
  const ctx = { now, params: found.params, query: url.searchParams, body, fleet: self.DemoFleet };
  return found.route.handler(ctx);
}
async function call(path, now = NOW) { return JSON.parse(JSON.stringify(await raw('GET', path, null, now))); }

async function streamEvents(runId) {
  const res = await raw('GET', `/api/tower/runs/${runId}/stream`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const events = [];
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value);
    for (const part of buf.split('\n\n').slice(0, -1)) {
      assert.ok(part.startsWith('data: '), 'frames carry data only');
      events.push(JSON.parse(part.slice(6)));
    }
    buf = buf.split('\n\n').pop();
    if (events.length && events[events.length - 1].event === 'done') { await reader.cancel(); break; }
  }
  return events;
}
async function answerFor(text) {
  const out = await raw('POST', '/api/tower/threads/demo-t1/messages', { text });
  assert.equal(out.ok, true);
  const ev = await streamEvents(out.run_id);
  assert.equal(ev[0].event, 'status');
  assert.equal(ev[ev.length - 1].event, 'done');
  return ev.filter((e) => e.event === 'delta').map((e) => e.text).join('');
}

test('templated tower routes keep the captured shape', async () => {
  const file = join(ROOT, 'templates', 'GET__api_tower_state.json');
  assert.ok(existsSync(file));
  assertCompatible(await call('/api/tower/state'), JSON.parse(readFileSync(file, 'utf8')).body);
});

test('state has a model loaded, a finished capability check and a past latest insight', async () => {
  const st = await call('/api/tower/state');
  assert.equal(st.enabled, true);
  assert.ok(st.model && st.hosts.length === 1);
  assert.notEqual(st.check.grade, 'pending');
  assert.ok(st.latest_insight.created * 1000 < NOW);
});

test('insights: three canned rows tied to the sample alerts', async () => {
  const d = await call('/api/tower/insights');
  assert.equal(d.insights.length, 3);
  assert.equal(d.insights[0].host, 'forge.example');
  assert.equal(d.insights[0].severity, 'warning');
  assert.ok(d.insights[0].snapshot.points.length > 2);
  const ids = new Set(self.DemoAlarms.alerts(NOW).map((a) => a.alert_id));
  assert.ok(d.insights.every((i) => ids.has(i.alert_id)));
});

test('thread list and history hold one earlier conversation', async () => {
  const list = await call('/api/tower/threads');
  assert.equal(list.threads.length, 1);
  const t = await call(`/api/tower/threads/${list.threads[0].id}`);
  assert.deepEqual(t.messages.map((m) => m.role), ['user', 'tool', 'assistant']);
  assert.equal((await raw('GET', '/api/tower/threads/nope')).status, 404);
  const made = await raw('POST', '/api/tower/threads', { page: {} });
  assert.equal(made.ok, true);
  assert.equal((await call(`/api/tower/threads/${made.thread.id}`)).messages.length, 0);
});

test('keywords pick the canned answer; anything else gets the fallback', async () => {
  assert.match(await answerFor('Why is forge so slow?'), /forge\.example is the slow one/);
  assert.match(await answerFor('Any alarms open?'), /two open alarms/);
  assert.match(await answerFor('What does it cost?'), /costs the most/);
  assert.match(await answerFor('Which model is loaded?'), /Llama-3\.1-8B/);
  assert.equal(await answerFor('Tell me a joke'), self.DemoTower.FALLBACK);
});

test('thread and message POSTs are answered as reads; other Tower writes are refused', async () => {
  const post = (path, body) => self.DemoRouter.respond(new self.Request('http://demo.example' + path,
    { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }), NOW, null);
  assert.equal((await post('/api/tower/threads', {})).status, 200);
  assert.equal((await post('/api/tower/threads/demo-t1/messages', { text: 'hi' })).status, 200);
  assert.equal((await post('/api/tower/threads/demo-t1/messages', { text: ' ' })).status, 400);
  assert.equal((await post('/api/tower/actions/x/approve', {})).status, 403);
  const seen = await post('/api/tower/insights/seen', {});
  assert.equal(seen.status, 200);
  assert.equal((await seen.json()).ok, true);
  const park = await post('/api/tower/runs/demo-run-slow-1/park', {});
  assert.equal(park.status, 200);
  assert.deepEqual(await park.json(), { ok: true });
  assert.equal((await post('/api/tower/timers/x/cancel', {})).status, 403);
});

test('eval and models pages are populated', async () => {
  const e = await call('/api/tower/eval');
  assert.equal(e.results[0].passed, e.results[0].total);
  const m = await call('/api/tower/models');
  assert.ok(m.models.length >= 3 && m.hosts.length === 1);
  assert.deepEqual((await call('/api/tower/timers')).timers, []);
});

test('who makes it and how to get help have their own answers, with no tool call', async () => {
  const K = self.DemoTower.keyFor;
  for (const q of ['Who develops LLM Systems Manager?', 'Who makes this?', 'who builds it', 'Who maintains the project?', 'Who is behind LLM Systems Manager?']) {
    assert.equal(K(q), 'about', q);
  }
  for (const q of ['How do I get help?', 'Where is support?', 'How can I contact you?', 'How do I report a bug?']) {
    assert.equal(K(q), 'help', q);
  }
  assert.equal(K('Which machine is slow?'), 'slow');
  assert.equal(K('Is the alarm engine healthy?'), 'engine');
  assert.equal(K('What models are loaded everywhere?'), 'model');
  const about = await answerFor('Who develops LLM Systems Manager?');
  assert.match(about, /LLM SysCore/);
  assert.match(about, /AGPL-3\.0/);
  assert.match(about, /github\.com\/llmsyscore\/llm-systems-manager/);
  assert.match(about, /own machines/);
  const help = await answerFor('How do I get help?');
  for (const re of [/https:\/\/www\.llmsyscore\.com\/docs\.html/, /support@llmsyscore\.com/,
    /github\.com\/llmsyscore\/llm-systems-manager\/issues/, /security/i]) assert.match(help, re);
  for (const key of ['about', 'help']) {
    const out = await raw('POST', '/api/tower/threads/demo-t1/messages', { text: key === 'about' ? 'Who makes it?' : 'help' });
    assert.match(out.run_id, /^demo-run-([a-z]+)-/);
    assert.equal(/^demo-run-([a-z]+)-/.exec(out.run_id)[1], key, 'the analytics read the answer key from the run id');
    const ev = await streamEvents(out.run_id);
    assert.ok(!ev.some((e) => e.event === 'tool'), `${key} has no tool tick`);
  }
});

test('the dashboard\'s own help suggestions get the new answers', async () => {
  const src = readFileSync(join(ROOT, '..', 'llm-systems-manager', 'frontend', 'js', 'lib', 'tower-view.js'), 'utf8');
  const list = JSON.parse(/const HELP_SUGS = (\[[^\]]*\]);/.exec(src)[1].replace(/'/g, '"'));
  assert.deepEqual(list.map((q) => self.DemoTower.keyFor(q)), ['about', 'help']);
});

test('every suggestion and Ask Tower prompt the demo can show has its own scripted answer', async () => {
  const require = createRequire(import.meta.url);
  const lib = join(ROOT, '..', 'llm-systems-manager', 'frontend', 'js', 'lib');
  const TW = require(join(lib, 'tower-view.js'));
  const FC = require(join(lib, 'forecast.js'));
  const caps = (await call('/api/tower/state')).capabilities;
  const pages = [{}, { tab: 'events' }, { tab: 'dashboard', sub: 'energy' }, { tab: 'llm' }, { tab: 'admin' }, { tab: 'tools' }]
    .concat(self.DemoFleet.machines.map((m) => ({ tab: 'dashboard', host: m.hostname })));
  const asks = new Set(pages.flatMap((p) => TW.suggestions(p, caps)));
  const ov = load(['demo-fleet.js', 'demo-series.js', 'demo-router.js', 'demo-state.js', 'handlers/overall.js']);
  const url = new URL('http://demo.example/api/forecast');
  const fc = ov.DemoRouter.find('GET', url);
  const view = JSON.parse(JSON.stringify(await fc.route.handler({ now: NOW, params: fc.params, query: url.searchParams, body: null, fleet: ov.DemoFleet })));
  assert.equal(view.findings.length, 2);
  asks.add(FC.askRunText(view));
  view.findings.forEach((f) => asks.add(FC.askText(f)));
  for (const a of self.DemoAlarms.alerts(NOW).filter((x) => x.status !== 'closed')) {
    asks.add(TW.troubleshootPrompt({ rule: a.rule_name, host: a.source_host, alert_id: a.alert_id }));
  }
  const missing = [...asks].filter((q) => !self.DemoTower.keyFor(q));
  assert.deepEqual(missing, [], 'add a scripted answer in demo/runtime/handlers/tower.js');
  assert.match(await answerFor('Explain slot pressure'), /4 slots/);
  assert.match(await answerFor(FC.askRunText(view)), /forge\.example first/);
});

async function toolEvent(text) {
  const out = await raw('POST', '/api/tower/threads/demo-t1/messages', { text });
  const ev = await streamEvents(out.run_id);
  return { run: out.run_id, tool: ev.find((e) => e.event === 'tool'), deltas: ev.filter((e) => e.event === 'delta').map((e) => e.text).join('') };
}
function assertChart(c) {
  assert.ok(Array.isArray(c.points) && c.points.length >= 2);
  c.points.forEach((p, i) => {
    assert.ok(Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (i) assert.ok(p[0] > c.points[i - 1][0], 'ascending time');
  });
}

test('the fallback is a formatted list of working example questions with the support contacts', () => {
  const fb = self.DemoTower.FALLBACK;
  assert.match(fb, /support@llmsyscore\.com/);
  assert.match(fb, /https:\/\/www\.llmsyscore\.com/);
  const ex = fb.split('\n').filter((l) => l.startsWith('- ')).map((l) => l.slice(2));
  assert.ok(ex.length >= 4);
  ex.forEach((q) => assert.ok(self.DemoTower.keyFor(q), q));
});

test('every earlier question keeps its answer key', () => {
  const K = self.DemoTower.keyFor;
  const was = [
    ['Which machine is slow?', 'slow'],
    ['Any open alarms?', 'alarm'],
    ['What does each machine cost to run?', 'cost'],
    ['Show GPU temperature on forge.example over the last day', 'trendtemp'],
    ['Chart power use over time', 'trendpower'],
    ['How do I get help?', 'help'],
    ['Which machine is slow right now?', 'slow'],
    ['Chart answer speed over the last day', 'trendspeed'],
    ['Watch the forge GPU temperature', 'watch'],
    ['What alarms are open right now?', 'alarm'],
    ['What does the forecast warn about?', 'forecast'],
    ['Who develops LLM Systems Manager?', 'about'],
    ['Who makes this?', 'about'],
    ['who builds it', 'about'],
    ['Who maintains the project?', 'about'],
    ['Who is behind LLM Systems Manager?', 'about'],
    ['Where is support?', 'help'],
    ['How can I contact you?', 'help'],
    ['How do I report a bug?', 'help'],
    ['Why is forge so slow?', 'slow'],
    ['Is the alarm engine healthy?', 'engine'],
    ['What models are loaded everywhere?', 'model'],
    ['Is forge getting hot? Show a graph', 'trendtemp'],
    ['Chart tokens per second over time', 'trendspeed'],
    ['Watch forge for me', 'watch'],
    ['Keep an eye on the GPU', 'watch'],
    ['Which graphics card uses the most power?', 'power'],
    ['Is the alarm engine monitoring?', 'engine'],
    ['Draw a graph of power use', 'trendpower'],
    ['Explain slot pressure', 'slots'],
    ['Tell me a joke', null],
    ['Activity summary for today', 'activity'],
    ['Alarm summary for today', 'alarm'],
    ['Why is a host red?', 'red'],
    ['What used the most power today?', 'power'],
    ['Top alarm offenders this month', 'offenders'],
    ['What hardware does each host have?', 'hardware'],
    ['Unload a model nobody is using', 'unused'],
    ['Summarise active alarms', 'alarm'],
    ['Which alert needs me first?', 'alarm'],
    ['Top 10 alarm rules over 30 days', 'offenders'],
    ['Alarms per day this week as a chart', 'offenders'],
    ['Which host alarms the most?', 'offenders'],
    ['What changed in the last hour?', 'recent'],
    ['Acknowledge the oldest active alert', 'alarm'],
    ['What is my $/Mtok today?', 'mtok'],
    ['Energy per host this week', 'power'],
    ['Idle vs active energy today', 'idle'],
    ['Which host is idle the most?', 'idle'],
    ['Which model is fastest here?', 'fastest'],
    ['Is anything loaded that nobody uses?', 'unused'],
    ['What CPU and GPU does each host have?', 'hardware'],
    ['Wake llama-server', null],
    ['What version is each agent on?', 'versions'],
    ['Are any hosts offline?', 'offline'],
    ['What was the last benchmark run?', 'bench'],
    ['Which host is fastest for a 27B model?', 'big'],
    ['Any autotune runs this week?', 'bench'],
    ['Why is forge.example red?', 'red'],
    ['What hardware does forge.example have?', 'hardware']
  ];
  was.forEach(([q, key]) => assert.equal(K(q), key, q));
});

const PRODUCT_ASKS = {
  whatis: ['What is LLM Systems Manager?', 'What can this do?', 'What can LLM Systems Manager do?', 'What does it do?'],
  whofor: ['Who is this for?', 'Who should use it?'],
  how: ['How does it work?', 'How does this work?', 'How does LLM Systems Manager work?'],
  servers: ['Which model servers does it support?', 'Does it support vLLM?', 'Does it work with LM Studio?', 'Does this support NVIDIA GPUs?'],
  needs: ['What do I need to run it?', 'What are the system requirements?', 'Does it run on Windows?'],
  alarms: ['How do alarms work?', 'How do I get notified?', 'What is an alert?'],
  autotune: ['How does autotune work?', 'What is Autotune?'],
  whatforecast: ['What is Forecast?', 'How does Forecast work?'],
  benchmarks: ['What is a Report Card?', 'How do benchmarks work?', 'What is a benchmark?'],
  energy: ['How is energy tracked?', 'Does it measure electricity cost?'],
  tower: ['What can Tower do?', 'What is Tower?', 'Which model does Tower use?'],
  phone: ['Is there a phone app?', 'Can I use it on my phone?'],
  addhost: ['How do I add a machine?', 'How can I add another host?'],
  install: ['How do I install it?', 'How do I get started?'],
  free: ['Is it free?', 'What license is it under?', 'Is it open source?'],
  privacy: ['What data leaves my machines?', 'Does it collect telemetry?'],
  update: ['How do I update it?', 'How do I upgrade the agents?'],
  routing: ['Can it route requests between machines?', 'Is there an OpenAI-compatible API?'],
  users: ['Can several users have access?', 'What user roles are there?']
};

test('product questions pick their own answers', () => {
  const A = self.DemoTower.ANSWERS;
  Object.keys(PRODUCT_ASKS).forEach((key) => {
    assert.ok(A[key], key);
    assert.equal(A[key].tool, undefined, key);
    PRODUCT_ASKS[key].forEach((q) => assert.equal(self.DemoTower.keyFor(q), key, q));
  });
  assert.ok(Object.keys(PRODUCT_ASKS).length >= 15);
});

test('a product answer streams with no tool tick and its key in the run id', async () => {
  const { run, tool, deltas } = await toolEvent('How do alarms work?');
  assert.equal(/^demo-run-([a-z]+)-/.exec(run)[1], 'alarms');
  assert.equal(tool, undefined);
  assert.equal(deltas, self.DemoTower.ANSWERS.alarms.text);
  assert.match(deltas, /Discord/);
});

test('trend questions pick the chart answers and stream a chart the dashboard can draw', async () => {
  const K = self.DemoTower.keyFor;
  assert.equal(K('Show GPU temperature on forge.example over the last day'), 'trendtemp');
  assert.equal(K('Is forge getting hot? Show a graph'), 'trendtemp');
  assert.equal(K('Chart tokens per second over time'), 'trendspeed');
  assert.equal(K('Chart power use over time'), 'trendpower');
  assert.equal(K('Watch forge for me'), 'watch');
  assert.equal(K('Keep an eye on the GPU'), 'watch');
  assert.equal(K('Which graphics card uses the most power?'), 'power');
  assert.equal(K('Is the alarm engine monitoring?'), 'engine');
  assert.equal(K('Draw a graph of power use'), 'trendpower');
  const nowS = Math.floor(NOW / 1000);
  for (const [q, hosts] of [['Show GPU temperature on forge.example over the last day', 1],
    ['Chart tokens per second over time', 2], ['Chart power use over time', 2]]) {
    const { run, tool } = await toolEvent(q);
    assert.match(run, /^demo-run-[a-z]+-/);
    assert.equal(tool.name, 'host_history');
    assert.equal(tool.result.hosts.length, hosts);
    tool.result.hosts.forEach((h) => { assertChart(h._chart); assert.equal(h._chart.points[h._chart.points.length - 1][0], nowS); });
  }
  const t = (await toolEvent('Show GPU temperature on forge.example over the last day')).tool.result.hosts[0]._chart;
  assert.equal(t.points.length, 48);
  assert.equal(t.threshold, 80);
  assert.ok(t.points[t.points.length - 1][1] > 80);
});

test('watch streams a timer series', async () => {
  const { tool } = await toolEvent('Watch the forge GPU temperature');
  assert.equal(tool.name, 'timer');
  const sr = tool.result.series;
  assert.equal(sr[sr.length - 1][0] - sr[0][0], 20 * 60);
  assertChart({ points: sr });
  assert.ok(sr.every((p) => p[1] >= 80 && p[1] <= 82));
});

test('every answer is formatted and streams back as exactly its text', async () => {
  const A = self.DemoTower.ANSWERS;
  Object.keys(A).forEach((k) => {
    assert.ok(A[k].text.includes('\n\n'), k);
    assert.ok(!A[k].text.includes('undefined'), k);
  });
  assert.ok(self.DemoTower.FALLBACK.includes('\n'));
  for (const [q, key] of [['Why is forge so slow?', 'slow'], ['Watch forge', 'watch'], ['Tell me a joke', null]]) {
    const { deltas } = await toolEvent(q);
    assert.equal(deltas, key ? A[key].text : self.DemoTower.FALLBACK);
  }
});

test('fleet questions with product-sounding words keep fleet or fallback answers', () => {
  const K = self.DemoTower.keyFor;
  const product = ['needs', 'free', 'addhost', 'install', 'phone', 'whofor', 'users'];
  for (const q of ['What model runs on forge.example?', 'Does forge have a free slot?', 'What is the electricity price?',
    'What is the address of each host?', 'How do I set up an alarm rule?', 'Which app sends the most requests?',
    'Who is waiting for approval?']) assert.ok(!product.includes(K(q)), q + ' -> ' + K(q));
});
