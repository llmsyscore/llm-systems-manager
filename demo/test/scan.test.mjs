import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scanSource, pageScripts, DEFAULT_DIRS } from '../scan.mjs';

const SRC = `
const API_BASE = '/api/alarm';
fetch('/api/agents');
const r = await jfetch(\`/api/agents/\${encodeURIComponent(id)}/info?x=1\`);
fetch('/api/models/' + id, { method: 'DELETE' });
fetch(base + '/api/terminal/create', { method: 'POST', body: '{}' });
new EventSource('/api/stream/' + name);
towerAct('pin', '/api/tower/model', { method: 'PUT' });
fetch(tq('/api/llm/status'));
ApiClient._request(\`/rules/\${ruleId}\`);
fetch(someVariable);
// fetch('/api/commented')
fetch('https://example.invalid/x');
fetch('/health');
const hu = cond ? '/alarm/health' : '/v1/models';
fetch(hu);
fetch(\`\${origin}/tools-ping\`);
`;

test('scanSource finds literal, template, method, base and wrapper forms', () => {
  const got = scanSource(SRC, 'f.js').map((e) => `${e.method} ${e.pattern}`);
  assert.deepEqual(got, [
    'GET /api/agents',
    'GET /api/agents/:p/info',
    'DELETE /api/models/:p',
    'POST /api/terminal/create',
    'GET /api/stream/:p',
    'PUT /api/tower/model',
    'GET /api/llm/status',
    'GET /api/alarm/rules/:p',
    'GET null',
    'GET /health',
    'GET /alarm/health',
    'GET /v1/models',
  ]);
  assert.ok(scanSource(SRC, 'f.js').every((e) => !('stream' in e)));
});

test('pageScripts takes local script tags only; defaults follow the index pages and the phone app', (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'scan-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeFileSync(join(dir, 'p.html'), '<script src="/static/js/a.js?v=1"></script><script src="js/b.js"></script>' +
    '<script src="/static/vendor/v.js"></script><script src="https://x.invalid/c.js"></script><script>1</script>');
  assert.deepEqual(pageScripts(join(dir, 'p.html'), '/root'), ['/root/js/a.js', join(dir, 'js/b.js')]);
  const names = DEFAULT_DIRS.map((p) => p.split('/').slice(-1)[0]);
  assert.ok(names.includes('boot.js') && names.includes('api.js'));
  assert.ok(names.includes('companion.html') && names.includes('companion.js') && names.includes('companion-tower.js'));
});
