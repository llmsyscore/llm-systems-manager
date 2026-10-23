// Captures scrubbed response templates from a running dashboard: node capture.mjs --base <url> --out <dir>.
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { scanEndpoints, covers, DEFAULT_DIRS } from './scan.mjs';
import { makeScrubber } from './scrub.mjs';
import { load } from './test/load.mjs';

const ID_SEG = /^(\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;

// Template file name for a key: non-alphanumerics become '_'.
export function fileName(key) {
  return key.replace(/[^A-Za-z0-9]/g, '_') + '.json';
}

// Maps a request to the most specific scanned pattern key, or a literal-path key with id segments as ':p'.
export function routeKey(method, pathname, scanned) {
  const m = method.toUpperCase();
  const hits = scanned.filter((k) => covers([k], m, pathname));
  if (hits.length) {
    hits.sort((a, b) => (a.split(':').length - b.split(':').length));
    return hits[0];
  }
  const segs = pathname.split('/').map((s) => (ID_SEG.test(s) ? ':p' : s));
  return `${m} ${segs.join('/')}`;
}

function arg(name) {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : undefined;
}

async function main() {
  const base = arg('--base');
  const out = resolve(arg('--out') || 'templates');
  const user = process.env.DEMO_CAPTURE_USER;
  const pass = process.env.DEMO_CAPTURE_PASS;
  if (!base || !user || !pass) throw new Error('usage: DEMO_CAPTURE_USER=.. DEMO_CAPTURE_PASS=.. node capture.mjs --base <url> --out <dir>');
  const { chromium } = await import('playwright');
  const fleet = load(['demo-fleet.js']).DemoFleet.machines.map((m) => m.hostname);
  const scrub = makeScrubber(fleet);
  const scanned = [...new Set(scanEndpoints(DEFAULT_DIRS).filter((e) => e.pattern).map((e) => `${e.method} ${e.pattern}`))];
  mkdirSync(out, { recursive: true });

  const bodies = new Map();
  const frames = new Map();
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1920, height: 1080 } });
  const page = await ctx.newPage();

  page.on('response', async (res) => {
    try {
      const url = new URL(res.url());
      if (!/^\/(api|alarm\/api)\//.test(url.pathname)) return;
      if (!(res.headers()['content-type'] || '').includes('json')) return;
      const key = routeKey(res.request().method(), url.pathname, scanned);
      if (bodies.has(key)) return;
      bodies.set(key, { status: res.status(), body: JSON.parse(await res.text()) });
    } catch { /* body unavailable */ }
  });
  page.on('websocket', (ws) => {
    const where = ws.url().split('?')[0];
    console.error(`websocket opened: ${where}`);
    ws.on('close', () => console.error(`websocket closed: ${where}`));
    ws.on('socketerror', (e) => console.error(`websocket error: ${where} ${e}`));
    ws.on('framereceived', ({ payload }) => {
      try {
        const msg = JSON.parse(typeof payload === 'string' ? payload : payload.toString());
        const type = String(msg && (msg.type || msg.event || (Array.isArray(msg) ? 'array' : Object.keys(msg).sort().join('_'))));
        const list = frames.get(type) || [];
        if (list.length < 3) list.push(msg);
        frames.set(type, list);
      } catch { /* not JSON */ }
    });
  });

  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.fill('#lf-user', user);
  await page.fill('#lf-pass', pass);
  await page.press('#lf-pass', 'Enter');
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(3000);

  const tabs = await page.$$eval('[id^="tabBtn"]', (els) => els.map((e) => e.id));
  for (const id of tabs) {
    await page.click(`#${id}`, { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(3000);
    const subs = await page.$$eval('[id^="subTabBtn"]', (els) => els.filter((e) => e.offsetParent).map((e) => e.id));
    for (const sid of subs) {
      await page.click(`#${sid}`, { timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(3000);
    }
  }
  await page.click('#tabBtnEvents', { timeout: 5000 }).catch(() => {});
  await page.waitForSelector('#alarmEngineIframe', { state: 'attached', timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(20000);
  await browser.close();

  for (const [key, v] of bodies) {
    writeFileSync(join(out, fileName(key)), JSON.stringify(scrub({ key, status: v.status, body: v.body }), null, 2) + '\n');
  }
  for (const [type, list] of frames) {
    const safe = type.replace(/[^A-Za-z0-9]/g, '_');
    writeFileSync(join(out, `ws_${safe}.json`), JSON.stringify(scrub({ type, frames: list }), null, 2) + '\n');
  }
  console.log(`captured ${bodies.size} routes, ${frames.size} websocket message types`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((e) => { console.error(e.message); process.exit(1); });
}
