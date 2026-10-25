import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { build, checkOutDir, parseArgs, stampStaticUrls } from '../build.mjs';

const DEMO = resolve(dirname(fileURLToPath(import.meta.url)), '..');

test('stampStaticUrls hashes known files and keeps missing ones', async (t) => {
  const dir = await mkdtemp(join(tmpdir(), 'stamp-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await mkdir(join(dir, 'js')); await mkdir(join(dir, 'css'));
  await writeFile(join(dir, 'js', 'a.js'), 'aaa');
  await writeFile(join(dir, 'css', 'b.css'), 'bbb');
  const h = (s) => createHash('sha256').update(s).digest('hex').slice(0, 10);
  const html = '<script src="/static/js/a.js"></script><link href="/static/css/b.css?v=old">'
    + '<script src="/static/js/gone.js"></script>';
  const out = stampStaticUrls(html, dir);
  assert.ok(out.includes(`src="/static/js/a.js?v=${h('aaa')}"`));
  assert.ok(out.includes(`href="/static/css/b.css?v=${h('bbb')}"`));
  assert.ok(out.includes('src="/static/js/gone.js"'));
});

test('build produces the stamped, injected site', async (t) => {
  const out = await mkdtemp(join(tmpdir(), 'dist-'));
  t.after(() => rm(out, { recursive: true, force: true }));
  await build({ outDir: out, release: 'v9.9.9' });
  const dash = await readFile(join(out, 'dashboard.html'), 'utf8');
  assert.ok(dash.includes('window.__MGR_AGENT="demo-mgr"'));
  assert.ok(dash.includes('window.__AE_WS_URL__="wss://demo.invalid/ws"'));
  const page = dash.indexOf('<script src="/demo/demo-page.js');
  assert.ok(page >= 0 && page < dash.indexOf('/static/js/'));
  const ae = await readFile(join(out, 'alarm', 'index.html'), 'utf8');
  assert.ok(ae.includes('window.ALARM_WS_URL="wss://demo.invalid/ws"'));
  const srcs = (html) => [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((m) => m[1]);
  const aeSrcs = srcs(ae);
  const firstAeScript = aeSrcs.findIndex((u) => u.startsWith('js/'));
  assert.ok(firstAeScript > 0 && aeSrcs.indexOf('/demo/demo-page.js?v=v9.9.9') >= 0
    && aeSrcs.indexOf('/demo/demo-page.js?v=v9.9.9') < firstAeScript);
  for (const html of [dash, ae]) {
    const list = srcs(html);
    const order = ['demo-fleet.js', 'demo-series.js', 'handlers/alarms.js', 'demo-page.js']
      .map((f) => list.indexOf(`/demo/${f}?v=v9.9.9`));
    assert.ok(order.every((n, i) => n >= 0 && (i === 0 || n > order[i - 1])), order.join(','));
  }
  const tourCss = dash.indexOf('<link rel="stylesheet" href="/demo/demo-tour.css?v=v9.9.9">');
  const tourJs = dash.indexOf('<script src="/demo/demo-tour.js?v=v9.9.9"></script>');
  assert.ok(tourCss > dash.indexOf('/static/js/boot.js') && tourJs > tourCss && tourJs < dash.indexOf('</body>'));
  assert.ok(!ae.includes('demo-tour'));
  for (const f of ['demo-tour.js', 'demo-tour.css', 'placeholder.html']) {
    assert.ok((await readFile(join(out, 'demo', f), 'utf8')).length > 0, f);
  }
  const sw = await readFile(join(out, 'demo-sw.js'), 'utf8');
  assert.equal(sw.split('\n')[0], '// release v9.9.9');
  const files = (await readdir(join(DEMO, 'runtime', 'handlers'))).filter((f) => f.endsWith('.js')).sort();
  assert.ok(files.length >= 2);
  const handlers = JSON.parse(/^self\.DEMO_HANDLERS=(\[.*\]);$/m.exec(sw)[1]);
  assert.deepEqual(handlers, files.map((f) => `/demo/handlers/${f}`));
  const imported = [];
  const ctx = { addEventListener() {}, location: new URL('https://demo.example/demo-sw.js'),
    importScripts: (...u) => imported.push(...u) };
  ctx.self = ctx;
  vm.runInNewContext(sw, ctx);
  assert.deepEqual(imported.slice(-files.length), files.map((f) => `/demo/handlers/${f}?v=v9.9.9`));
  await assert.rejects(readFile(join(out, 'demo', 'demo-sw.js')));
  for (const f of ['package.json', 'package-lock.json', 'vitest.config.mjs', 'sw.js', 'companion.html']) {
    await assert.rejects(readFile(join(out, 'static', f)), f);
  }
  const idx = await readFile(join(out, 'index.html'), 'utf8');
  assert.ok(idx.includes('/demo-sw.js'));
  assert.ok(idx.includes('<link rel="canonical" href="https://demo.llmsyscore.com/">') && idx.includes('og:image'));
  for (const html of [dash, ae]) assert.ok(html.includes('<meta name="robots" content="noindex">'));
  assert.ok(!idx.includes('noindex'));
  assert.ok(idx.includes('<h1>LLM Systems Manager demo</h1>'));
  assert.match(await readFile(join(out, 'robots.txt'), 'utf8'), /^User-agent: \*$/m);
  assert.ok((await readFile(join(out, 'sitemap.xml'), 'utf8')).includes('<loc>https://demo.llmsyscore.com/</loc>'));
  assert.ok((await readFile(join(out, '404.html'), 'utf8')).includes('noindex'));
  const built = await readdir(out, { recursive: true, withFileTypes: true });
  for (const f of built) {
    if (!f.isFile() || !/\.(html|js|css|json|webmanifest)$/.test(f.name)) continue;
    const t = await readFile(join(f.parentPath, f.name), 'utf8');
    assert.ok(!t.includes('__MGR_VERSION__'), f.name);
  }
  assert.ok(!built.some((f) => f.parentPath.includes('node_modules') || f.name === 'companion-dom.test.js'));
  const routes = JSON.parse(await readFile(join(out, '_routes.json'), 'utf8'));
  assert.equal(routes.version, 1);
  assert.deepEqual(routes.include, ['/*']);
  const excluded = (path) => routes.exclude.some((r) => (r.endsWith('/*') ? path.startsWith(r.slice(0, -1)) : path === r));
  for (const f of built) {
    if (!f.isFile()) continue;
    const path = '/' + relative(out, join(f.parentPath, f.name)).split(sep).join('/');
    if (path === '/_worker.js' || path === '/_routes.json') continue;
    assert.equal(excluded(path), !['/index.html', '/dashboard.html', '/phone.html'].includes(path), path);
  }
  assert.ok((await readFile(join(out, '_worker.js'), 'utf8')).includes('/v1/demo/landing'));
});

test('build makes the phone app page with the demo layer and the body-end scripts on both pages', async (t) => {
  const out = await mkdtemp(join(tmpdir(), 'dist-'));
  t.after(() => rm(out, { recursive: true, force: true }));
  await build({ outDir: out, release: 'v9.9.9' });
  const phone = await readFile(join(out, 'phone.html'), 'utf8');
  const dash = await readFile(join(out, 'dashboard.html'), 'utf8');
  const srcs = (html) => [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((m) => m[1]);
  const list = srcs(phone);
  const first = list.findIndex((u) => u.startsWith('/static/js/'));
  const head = ['demo-analytics.js', 'demo-fleet.js', 'demo-series.js', 'handlers/alarms.js', 'demo-page.js', 'demo-phone.js']
    .map((f) => list.indexOf(`/demo/${f}?v=v9.9.9`));
  assert.ok(head.every((n, i) => n >= 0 && n < first && (i === 0 || n > head[i - 1])), head.join(','));
  for (const html of [phone, dash]) {
    const l = srcs(html);
    const tail = ['demo-tour.js', 'demo-starters.js', 'demo-links.js'].map((f) => l.indexOf(`/demo/${f}?v=v9.9.9`));
    const lastStatic = l.map((u) => u.startsWith('/static/')).lastIndexOf(true);
    assert.ok(tail.every((n, i) => n > lastStatic && (i === 0 || n > tail[i - 1])), tail.join(','));
    assert.ok(html.includes('<link rel="stylesheet" href="/demo/demo-tour.css?v=v9.9.9">'));
  }
  assert.ok(!dash.includes('demo-phone.js'));
  assert.ok(phone.includes('<meta name="robots" content="noindex">'));
  assert.ok(phone.includes('window.__AE_WS_URL__="wss://demo.invalid/ws"') && phone.includes('--brand-accent'));
  assert.ok(!phone.includes('rel="manifest"') && !phone.includes('__MGR_VERSION__'));
  assert.match(phone, /src="\/static\/js\/companion\.js\?v=[0-9a-f]{10}"/);
  assert.match(phone, /href="\/static\/css\/companion\.css\?v=[0-9a-f]{10}"/);
  await assert.rejects(readFile(join(out, 'static', 'companion.html')));
  assert.ok((await readFile(join(out, 'demo', 'demo-phone.js'), 'utf8')).length > 0);
});

test('build refuses unsafe output directories and flags without values', async () => {
  for (const bad of ['', '  ', '/', DEMO, resolve(DEMO, '..'), homedir()]) {
    assert.throws(() => checkOutDir(bad), /build:/, JSON.stringify(bad));
    await assert.rejects(build({ outDir: bad, release: 'v1' }), /build:/);
  }
  assert.equal(checkOutDir(join(DEMO, 'dist')), join(DEMO, 'dist'));
  assert.deepEqual({ ...parseArgs(['--release', 'v1.2.3', '--out', 'x']) }, { release: 'v1.2.3', out: 'x' });
  assert.throws(() => parseArgs(['--release', '--out', 'x']), /--release needs a value/);
  assert.throws(() => parseArgs(['--release', 'v1', '--out']), /--out needs a value/);
  assert.throws(() => parseArgs(['--release', '']), /needs a value/);
  assert.throws(() => parseArgs(['--out', 'x']), /--release is required/);
  assert.throws(() => parseArgs(['--release', 'v1', 'stray']), /unknown argument/);
});
