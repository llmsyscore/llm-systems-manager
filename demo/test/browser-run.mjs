// Playwright run over the built demo: node test/browser-run.mjs --dist <dir> [--quick] [--shots <dir>].
import { createServer } from 'node:http';
import { readFile, stat, mkdir } from 'node:fs/promises';
import { join, resolve, extname, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.woff2': 'font/woff2',
  '.woff': 'font/woff', '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8', '.map': 'application/json' };

function arg(name) {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : undefined;
}

// Static server that mimics Cloudflare Pages: pretty URLs, files as-is, 404 otherwise.
export function serve(root) {
  return new Promise((ok) => {
    const srv = createServer(async (req, res) => {
      const u = new URL(req.url, 'http://x');
      let p = decodeURIComponent(u.pathname);
      // Redirects /x/index.html to /x/ and /x.html to /x with a 308, as Pages does.
      const clean = p.endsWith('/index.html') ? p.slice(0, -'index.html'.length) : p.endsWith('.html') ? p.slice(0, -'.html'.length) : null;
      if (clean !== null) {
        res.writeHead(308, { location: encodeURI(clean) + u.search });
        res.end();
        return;
      }
      const cands = p.endsWith('/') ? [p + 'index.html'] : [p, p + '.html', p + '/index.html'];
      for (const c of cands) {
        const f = resolve(join(root, c));
        if (f !== root && !f.startsWith(root + sep)) continue;
        try {
          if (!(await stat(f)).isFile()) continue;
          const body = await readFile(f);
          res.writeHead(200, { 'content-type': MIME[extname(f)] || 'application/octet-stream', 'cache-control': 'no-cache' });
          res.end(body);
          return;
        } catch { /* try next */ }
      }
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('not found');
    });
    srv.listen(0, '127.0.0.1', () => ok(srv));
  });
}

const problems = [];
let refusedLogs = 0;
const isApiRefusal = (t, url) => /Failed to load resource.*\b403\b/.test(t) && /\/api\//.test(url || '');
const bad = (m) => { if (!problems.includes(m)) problems.push(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const REFUSAL = 'Not available in the demo';

async function main() {
  const dist = resolve(arg('--dist') || 'dist');
  const shots = resolve(arg('--shots') || join(dist, 'shots'));
  const quick = process.argv.includes('--quick');
  await mkdir(shots, { recursive: true });
  const srv = await serve(dist);
  const origin = `http://127.0.0.1:${srv.address().port}`;
  const { chromium } = await import('playwright');
  const browser = await chromium.launch();

  // Reads the visible error toasts (fixed-position divs with the critical accent border).
  const errToasts = (page) => page.evaluate(() => [...document.body.children]
    .filter((e) => e.tagName === 'DIV' && /position:\s*fixed/.test(e.getAttribute('style') || '') && /var\(--crit\)/.test(e.getAttribute('style') || '')
      && getComputedStyle(e).opacity !== '0')
    .map((e) => e.textContent.trim()));

  function watch(page, label) {
    page.on('console', (m) => {
      const t = m.text();
      if (m.type() === 'warning' && t.startsWith('demo miss')) bad(`miss: ${t}`);
      else if (m.type() === 'error' && t.startsWith('demo error')) bad(`handler error: ${t}`);
      else if (m.type() === 'error' && isApiRefusal(t, m.location().url)) refusedLogs++;
      else if (m.type() === 'error' && t.includes(REFUSAL)) { /* the refusal text logged by the alarm page */ }
      else if (m.type() === 'error') bad(`console error [${label}]: ${t} ${m.location().url || ''}`);
    });
    page.on('pageerror', (e) => bad(`page error [${label}]: ${e.message}`));
  }

  // Analytics go to a local collector on its own port, so the run never writes to the live receiver.
  const beacons = [];
  const collector = await new Promise((ok) => {
    const c = createServer((req, res) => {
      let body = '';
      req.on('data', (d) => { body += d; });
      req.on('end', () => {
        if (req.method === 'POST') { try { beacons.push(JSON.parse(body)); } catch { bad('analytics batch is not JSON'); } }
        res.writeHead(204, { 'access-control-allow-origin': origin });
        res.end();
      });
    });
    c.listen(0, '127.0.0.1', () => ok(c));
  });
  const ANALYTICS = `http://127.0.0.1:${collector.address().port}/v1/demo`;

  async function newCtx(seen) {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, serviceWorkers: 'allow' });
    if (seen) await ctx.addInitScript(() => { try { localStorage.setItem('lsmDemoTourSeen', '1'); } catch (e) { /* none */ } });
    await ctx.addInitScript((u) => { self.DEMO_ANALYTICS_URL = u; }, ANALYTICS);
    await ctx.route('https://telemetry-receiver.llmsyscore.workers.dev/**', (route) => {
      bad('analytics went to the live receiver');
      return route.fulfill({ status: 204, body: '' });
    });
    // Context-level listener also sees requests made by the service worker.
    ctx.on('request', (r) => {
      const u = r.url();
      if (/^(data|blob|about):/.test(u) || u.startsWith(ANALYTICS)) return;
      if (new URL(u).origin !== origin) bad(`left origin: ${u}`);
    });
    const page = await ctx.newPage();
    watch(page, seen ? 'main' : 'tour');
    return { ctx, page };
  }

  // ---- Main crawl ----
  const { ctx, page } = await newCtx(true);
  await page.goto(origin + '/', { waitUntil: 'domcontentloaded' });
  try { await page.waitForURL('**/dashboard', { timeout: 30000 }); } catch { bad('cold load did not reach /dashboard'); }
  await page.waitForSelector('#tabBtnOverall', { timeout: 30000 }).catch(() => bad('dashboard did not render'));
  await sleep(3000);

  const empty = async (sel, name) => {
    const info = await page.evaluate((s) => {
      const e = document.querySelector(s);
      if (!e) return { missing: true };
      const r = e.getBoundingClientRect();
      return { w: r.width, h: r.height, text: (e.innerText || '').trim().length };
    }, sel);
    if (info.missing || info.h < 50 || (info.text < 5 && !info.canvas)) {
      const hasMedia = await page.evaluate((s) => !!document.querySelector(s + ' canvas, ' + s + ' svg, ' + s + ' iframe'), sel);
      if (!hasMedia) bad(`empty panel: ${name}`);
    }
  };

  const tabs = [
    ['tabBtnOverall', '#overallTab', 'overall'], ['tabBtnDashboard', '#dashboardTab', 'dashboards'],
    ['tabBtnLlmControl', '#llmTab', 'llm-control'], ['tabBtnTools', '#toolsTab', 'tools'],
    ['tabBtnEvents', '#eventsTab', 'events'], ['tabBtnAdmin', '#adminTab', 'admin'],
  ];
  const mainIds = await page.$$eval('[id^="tabBtn"]', (els) => els.map((e) => e.id));
  const subIds = await page.$$eval('[id^="subTabBtn"]', (els) => els.map((e) => e.id));

  // Buttons that had to be clicked from script (hidden, covered or not clickable), with the count per id.
  const jsClicks = new Map();
  const jsClick = (id) => {
    jsClicks.set(id, (jsClicks.get(id) || 0) + 1);
    return page.evaluate((i) => { const b = document.getElementById(i); if (b) b.click(); }, id);
  };
  async function click(id) {
    const vis = await page.evaluate((i) => { const b = document.getElementById(i); return !!b && b.offsetParent !== null; }, id);
    if (!vis) await jsClick(id);
    else await page.click('#' + id, { timeout: 5000 }).catch(() => jsClick(id));
    await sleep(900);
    const t = await errToasts(page);
    t.filter((x) => !x.includes(REFUSAL)).forEach((x) => bad(`error toast after ${id}: ${x}`));
  }

  for (const id of mainIds) {
    await click(id);
    const t = tabs.find((x) => x[0] === id);
    if (t) {
      const name = t[2];
      await page.screenshot({ path: join(shots, `${name}.png`) }).catch(() => {});
      await empty(t[1], `${name} (${t[1]})`);
    }
  }
  for (const id of subIds) await click(id);

  // OpenClaw dashboard and the System Health card, with a shot of each.
  await click('tabBtnDashboard');
  await click('subTabBtnOpenclaw');
  await sleep(1500);
  await page.screenshot({ path: join(shots, 'dashboards-openclaw.png'), fullPage: true }).catch(() => {});
  const oc = await page.evaluate(() => ({
    sessions: (document.getElementById('opcaTotSessions') || {}).textContent || '',
    agents: document.querySelectorAll('#opcaAgentTbody tr').length,
    flows: document.querySelectorAll('#opcaFlowRecentWrap tbody tr').length,
  }));
  if (!(Number(oc.sessions) > 0) || oc.agents < 2 || oc.flows < 2) bad(`openclaw: panels not filled (${JSON.stringify(oc)})`);
  await click('tabBtnAdmin');
  await sleep(1500);
  const hc = await page.$('#adminHealthCard');
  if (hc) await hc.screenshot({ path: join(shots, 'admin-system-health.png') }).catch(() => {});
  const jobsNone = await page.evaluate(() => {
    const c = document.getElementById('adminHealthCard');
    return !c || c.querySelectorAll('.hj.live').length < 2;
  });
  if (jobsNone) bad('system health: Jobs card shows no jobs');
  const subFallbacks = subIds.filter((id) => jsClicks.has(id)).length;
  for (const id of mainIds) if (jsClicks.has(id)) bad(`main tab button ${id} could not be clicked by a visitor (needed a script click)`);
  console.log(`script-click fallbacks: ${mainIds.filter((id) => jsClicks.has(id)).length} main tabs, ${subFallbacks} sub-tabs`);

  // Panels under the header (banner must not cover them).
  for (const [btn, sel] of [['tabBtnEvents', '#eventsTab'], ['tabBtnTools', '#toolsTab'], ['tabBtnAdmin', '#adminTab']]) {
    await page.evaluate((b) => document.getElementById(b).click(), btn);
    await sleep(600);
    const r = await page.evaluate((s) => {
      const p = document.querySelector(s), n = document.querySelector('.tab-nav');
      if (!p || !n) return null;
      const bn = document.getElementById('demoBanner');
      const bh = bn ? bn.getBoundingClientRect().height : 0;
      const withBanner = p.getBoundingClientRect().top - n.getBoundingClientRect().bottom;
      if (bn) bn.style.display = 'none';
      const navBare = n.getBoundingClientRect().bottom;
      if (bn) bn.style.display = '';
      // Without the banner the panel's own top is the current top minus the banner height.
      const without = (p.getBoundingClientRect().top - bh) - navBare;
      return { withBanner, without };
    }, sel);
    if (!r) bad(`panel layout: ${sel} or .tab-nav missing`);
    else if (r.withBanner < r.without - 1) bad(`panel covered: ${sel} sits ${r.without - r.withBanner}px higher against the tab bar than the original layout`);
  }

  // Alarm frame: open Events and confirm the iframe loaded the alarm engine page.
  await page.evaluate(() => document.getElementById('tabBtnEvents').click());
  await sleep(2500);
  const fr = page.frames().find((f) => f.url().includes('/alarm'));
  if (!fr) bad('alarm frame did not load');
  else {
    const subs = await fr.$$eval('button, [role="tab"], .tab', (els) => els.length).catch(() => 0);
    if (!subs) bad('alarm frame has no controls');
    await page.screenshot({ path: join(shots, 'events-alarm.png') }).catch(() => {});
  }
  (await errToasts(page)).filter((x) => !x.includes(REFUSAL)).forEach((x) => bad(`error toast (alarm): ${x}`));

  // ---- Change buttons ----
  const expectRefusal = async (label, fn) => {
    await page.evaluate(() => { document.querySelectorAll('body > div').forEach((e) => { if (/var\(--crit\)/.test(e.getAttribute('style') || '')) e.remove(); }); });
    await sleep(2100);
    const before = refusedLogs;
    try { await fn(); } catch (e) { bad(`${label}: could not trigger (${e.message.split('\n')[0]})`); return; }
    await sleep(900);
    const t = await errToasts(page);
    if (refusedLogs <= before) bad(`${label}: no refused (403) API request seen`);
    if (!t.some((x) => x.includes(REFUSAL))) bad(`${label}: no "${REFUSAL}" toast (saw ${JSON.stringify(t)})`);
  };

  await page.evaluate(() => document.getElementById('tabBtnAdmin').click());
  await sleep(600);
  await page.click('#subTabBtnAdminSettings');
  await sleep(1500);
  await expectRefusal('save settings', async () => {
    const btn = page.locator('#adminTab button:visible', { hasText: /^(On|Off)$/ }).first();
    await btn.click({ timeout: 5000 });
    await page.click('#adminSettingsSaveBtn', { timeout: 5000 });
  });
  await page.click('#subTabBtnAdminAgents');
  await sleep(1200);
  await expectRefusal('admin restart', async () => {
    await page.locator('[data-restart-svc="alarm_engine"]:visible').first().click({ timeout: 5000 });
    await page.locator('button:visible', { hasText: /^Restart$/ }).last().click({ timeout: 5000 });
  });
  await page.evaluate(() => document.getElementById('tabBtnEvents').click());
  await sleep(1500);
  // Acknowledging an alarm is a saved change: no refusal, and the alert reads back as acknowledged.
  {
    await page.evaluate(() => { document.querySelectorAll('body > div').forEach((e) => { if (/var\(--crit\)/.test(e.getAttribute('style') || '')) e.remove(); }); });
    await sleep(2100);
    const f = page.frames().find((x) => x.url().includes('/alarm'));
    const before = refusedLogs;
    try {
      if (!f) throw new Error('no alarm frame');
      await f.locator('[data-act="ack"]').first().click({ timeout: 5000 });
      await sleep(1500);
      if (refusedLogs > before) bad('alarm acknowledge: refused, expected a saved change');
      if ((await errToasts(page)).some((x) => x.includes(REFUSAL))) bad('alarm acknowledge: showed the refusal toast');
      const acked = await page.evaluate(() => fetch('/api/alarm/alerts?status=active').then((r) => r.json())
        .then((l) => l.filter((a) => a.acknowledged_by).length));
      if (!acked) bad('alarm acknowledge: no alert reads back as acknowledged');
    } catch (e) { bad(`alarm acknowledge: could not trigger (${e.message.split('\n')[0]})`); }
  }

  // Hard reload ends back on the dashboard.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#tabBtnOverall', { timeout: 30000 }).catch(() => bad('hard reload: dashboard did not render'));
  if (!new URL(page.url()).pathname.endsWith('/dashboard')) bad(`hard reload ended on ${page.url()}`);

  // Overall chart keeps moving.
  if (!quick) {
    await page.evaluate(() => document.getElementById('tabBtnOverall').click());
    await sleep(1500);
    // Snapshot of the Overall hero chart: every dataset's length and last point, plus the Updated stamp.
    const snap = () => page.evaluate(() => {
      const c = typeof ovHeroChart !== 'undefined' ? ovHeroChart : null;
      if (!c) return null;
      return JSON.stringify(c.data.datasets.map((d) => [d.data.length, d.data.length ? d.data[d.data.length - 1] : null]));
    });
    const a = await snap();
    await sleep(90000);
    let b = await snap();
    // The last point keeps the bucket's highest value, so a quiet spell may only show at the next bucket.
    if (a && a === b) {
      const wait = await page.evaluate(() => {
        const ms = typeof ovHeroBucketMs === 'function' ? ovHeroBucketMs() : 300000;
        return Math.min(ms - (Date.now() % ms) + 20000, 330000);
      });
      await sleep(wait);
      b = await snap();
    }
    if (!a) bad('overall: hero chart not found');
    else if (a === b) bad('overall: hero chart did not change within one chart bucket');
  }
  await page.evaluate(() => self.DemoAnalytics && self.DemoAnalytics.flush(false));
  await sleep(500);
  {
    const types = new Set(beacons.flatMap((b) => (b.events || []).map((e) => e.type)));
    for (const t of ['start', 'tab', 'subtab', 'refused']) if (!types.has(t)) bad(`analytics: no ${t} event sent (got ${[...types].join(",")}; ${beacons.length} batches)`);
    if (beacons.some((b) => !/^[0-9a-f]{16}$/.test(b.session || ''))) bad('analytics: bad session id');
  }
  await ctx.close();

  // ---- Tour on a fresh profile ----
  {
    const { ctx: c2, page: p2 } = await newCtx(false);
    await p2.goto(origin + '/', { waitUntil: 'domcontentloaded' });
    await p2.waitForURL('**/dashboard', { timeout: 30000 }).catch(() => bad('tour: did not reach /dashboard'));
    await p2.waitForSelector('.demo-tour', { timeout: 15000 }).catch(() => bad('tour did not appear on a fresh profile'));
    // Walks every step: the ring must frame a real box (at least 40 x 40 px); one shot per step.
    const nSteps = await p2.evaluate(() => (self.DemoTour ? self.DemoTour.steps.length : 0));
    for (let i = 0; i < nSteps; i++) {
      await sleep(1600);
      const r = await p2.evaluate(() => {
        const ring = document.querySelector('.demo-tour-ring');
        if (!ring || ring.style.display === 'none') return null;
        const b = ring.getBoundingClientRect();
        const inside = b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight;
        return { w: Math.round(b.width), h: Math.round(b.height), inside, title: (document.querySelector('.demo-tour-title') || {}).textContent };
      });
      if (!r || r.w < 40 || r.h < 40 || !r.inside) bad(`tour step ${i + 1}: ring too small or off screen (${JSON.stringify(r)})`);
      await p2.screenshot({ path: join(shots, `tour-${i + 1}.png`) }).catch(() => {});
      if (!(await p2.$('.demo-tour-close'))) bad(`tour step ${i + 1}: no Close button`);
      if (i < nSteps - 1) await p2.click('.demo-tour-next', { timeout: 5000 }).catch(() => bad(`tour step ${i + 1}: Next not clickable`));
    }
    await p2.click('.demo-tour-close', { timeout: 5000 }).catch(() => bad('tour Close button not clickable'));
    await sleep(300);
    if (await p2.$('.demo-tour')) bad('tour still present after Close');
    const seen = await p2.evaluate(() => { try { return localStorage.getItem('lsmDemoTourSeen'); } catch (e) { return null; } });
    if (!seen) bad('tour Close did not set lsmDemoTourSeen');
    await c2.close();
  }

  await browser.close();
  srv.close();
  collector.close();
  if (problems.length) {
    problems.forEach((p) => console.log('PROBLEM ' + p));
    process.exit(1);
  }
  console.log('browser run clean');
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((e) => { console.log('PROBLEM run crashed: ' + (e && e.stack || e)); process.exit(1); });
