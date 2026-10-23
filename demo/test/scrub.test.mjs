import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { scrub, isDocIp, DUMMY_FINGERPRINT } from '../scrub.mjs';
import { routeKey, fileName } from '../capture.mjs';

const TEMPLATES = join(dirname(fileURLToPath(import.meta.url)), '..', 'templates');
const IPV4 = /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g;
const IPV4_ONLY = /^\d{1,3}(\.\d{1,3}){3}$/;

// Returns the list of problems found in a template: bad IPs, real names, hostnames in host fields and URLs, fingerprints.
export function problems(text) {
  const bad = [];
  for (const ip of text.match(IPV4) || []) if (!isDocIp(ip)) bad.push(`ip ${ip}`);
  const words = ['.lan', '192.168.', ...(process.env.DEMO_SCRUB_NAMES || '').split(',').map((n) => n.trim().toLowerCase()).filter(Boolean)];
  for (const w of words) if (text.toLowerCase().includes(w)) bad.push(`word ${w}`);
  for (const f of text.match(/\b[0-9a-f]{64}\b/gi) || []) if (f !== DUMMY_FINGERPRINT) bad.push(`fingerprint ${f.slice(0, 8)}`);
  for (const m of text.matchAll(/[a-z][a-z0-9+.-]*:\/\/(?:[^\/\s@"']*@)?([^\/\s:?#"'\\]+)/gi)) {
    if (!/(^localhost$|\.example$)/i.test(m[1]) && !IPV4_ONLY.test(m[1])) bad.push(`url host ${m[1]}`);
  }
  const walk = (v, key) => {
    if (typeof v === 'string' && /^(hostname|host|host_name|fqdn|server|machine|node)$/i.test(key || '')) {
      const h = v.replace(/:\d+$/, '');
      if (h && !/(\.example$|^localhost$)/i.test(h) && !IPV4_ONLY.test(h)) bad.push(`host ${h}`);
    } else if (Array.isArray(v)) v.forEach((x) => walk(x, key));
    else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) walk(x, k);
  };
  try { walk(JSON.parse(text)); } catch { /* not JSON */ }
  return bad;
}

test('scrub replaces hostnames, IPs, usernames and paths', () => {
  process.env.DEMO_SCRUB_NAMES = 'qzuser,qzowner';
  const out = scrub({
    hostname: 'tower-01.lan', host: '10.0.0.5:8080', user: 'qzuser', agent_id: '11111111-2222-3333-4444-555555555555',
    ip: '192.168.0.20', url: 'http://box9.lan:5000/x', model: '/home/qzuser/models/Llama-3.1-8B.gguf',
    fp: 'sha256:' + 'ab12'.repeat(16),
    nested: [{ name: 'a', addr: '172.16.4.2 and tower-01.lan', cfg: '/opt/qzowner/data/x.json' }],
    'alarm_engine.host': 'x', file: 'backup.json', mail: 'a@b.com',
  });
  assert.deepEqual(problems(JSON.stringify(out)), []);
  assert.equal(out.hostname, 'atlas.example');
  assert.equal(out.agent_id, 'atlas');
  assert.equal(out.user, 'alex');
  assert.equal(out.model, '/opt/models/Llama-3.1-8B.gguf');
  assert.equal(out.fp, 'sha256:' + DUMMY_FINGERPRINT);
  assert.equal(out['alarm_engine.host'], 'x');
  assert.equal(out.file, 'backup.json');
  assert.equal(out.nested[0].cfg, '/opt/models/x.json');
  assert.ok(out.nested[0].addr.includes('atlas.example'));
  assert.equal(JSON.stringify(scrub(out)), JSON.stringify(out));
});

test('scrub leaves model names, versions and documentation IPs alone', () => {
  const v = { m: 'Qwen2.5-7B-Instruct.gguf', ver: '1.2.3', ip: '198.51.100.7', h: 'forge.example' };
  assert.deepEqual(scrub(v), v);
});

test('routeKey maps to scanned patterns and falls back to literal paths', () => {
  const scanned = ['GET /api/agents/:p/info', 'GET /api/agents'];
  assert.equal(routeKey('get', '/api/agents/atlas/info', scanned), 'GET /api/agents/:p/info');
  assert.equal(routeKey('GET', '/api/other/12345/x', scanned), 'GET /api/other/:p/x');
  assert.equal(fileName('GET /api/agents/:p'), 'GET__api_agents__p.json');
});

test('no captured template carries real hostnames, IPs or names', () => {
  const files = existsSync(TEMPLATES) ? readdirSync(TEMPLATES).filter((f) => f.endsWith('.json')) : [];
  for (const f of files) assert.deepEqual(problems(readFileSync(join(TEMPLATES, f), 'utf8')), [], f);
});

test('scrub replaces hostnames inside hyphenated file names', () => {
  const out = scrub({ hostname: 'dt-box', file: 'lsm-auto-manager-dt-box-20261023-054233.lsmenc',
    nested: { hostname: 'tower.lan', file: 'lsm-auto-ae-tower-20261023.lsmenc' } });
  assert.equal(out.file, 'lsm-auto-manager-atlas.example-20261023-054233.lsmenc');
  assert.equal(out.nested.file, 'lsm-auto-ae-forge.example-20261023.lsmenc');
  assert.equal(JSON.stringify(scrub(out)), JSON.stringify(out));
});

test('agent ids in /api/agents/<id> paths and agent/target fields map to one sample id everywhere', () => {
  const id = 'd9f59a4d-a4f8-452f-ba81-470bf10c9cc2', other = '0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0';
  const out = scrub({ entries: [
    { detail: { agent: id }, path: `/api/agents/${id}`, target: id, label: 'Deleted an agent' },
    { path: `/api/agents/${other}/approve`, target: 'forge.example', note: `agent ${id} removed` },
    { agent_id: id, hostname: 'x' },
  ] });
  const text = JSON.stringify(out);
  assert.ok(!text.includes(id) && !text.includes(other), text);
  const [a, b, c] = out.entries;
  assert.equal(a.detail.agent, a.target);
  assert.equal(a.path, `/api/agents/${a.target}`);
  assert.equal(c.agent_id, a.target);
  assert.equal(b.note, `agent ${a.target} removed`);
  assert.match(b.path, /^\/api\/agents\/[a-z0-9]+\/approve$/);
  assert.notEqual(b.path, `/api/agents/${a.target}/approve`);
  assert.equal(JSON.stringify(scrub(out)), text);
});

test('the audit log template carries no preview agent id', () => {
  const t = readFileSync(join(TEMPLATES, 'GET__api_admin_audit_log.json'), 'utf8');
  assert.ok(!/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(t));
});
