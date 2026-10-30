// Demo site Pages worker: serves the static site and reports each page visit to the telemetry queue or the db-store Worker.
// The page gets a landing id that the demo's analytics send with the visit's first event.
// Dashboard links with no demo page (log out, log in, server log window), sent to the landing page.
const TO_LANDING = ['/logout', '/login', '/llm/log'];
const TAGS = ['ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
const TAG_COOKIE = 'lsm_device';
// Old demo addresses, sent to the demo's own domain with the path and query.
const MOVED = ['llmsyscore-demo.pages.dev', 'demo.llmsyscore.com'];
const HOME = 'demo.llmsysmanager.com';
const CF = { country: 'country', region: 'region', city: 'city', timezone: 'timezone', asn: 'asn', as_org: 'asOrganization',
  colo: 'colo', bot_category: 'verifiedBotCategory' };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const tag = await tagValue(env);
    if (MOVED.includes(url.hostname)) return moved(request, url, env, ctx, !!tag && cookie(request, TAG_COOKIE) === tag);
    if (TO_LANDING.includes(url.pathname)) return Response.redirect(new URL('/', request.url).href, 302);
    if (url.pathname === '/s') return settings();
    const res = noindex(request, await env.ASSETS.fetch(request));
    try { return visit(request, res, env, ctx, !!tag && cookie(request, TAG_COOKIE) === tag); } catch { return res; }
  },
};

// A 301 to the same page on the demo's domain; a page navigation is reported with status 301.
export function moved(req, url, env, ctx, own = false) {
  const res = Response.redirect(`https://${HOME}${url.pathname}${url.search}`, 301), mode = req.headers.get('Sec-Fetch-Mode');
  if ((env.WRITES || env.STORE) && (!mode || mode === 'navigate')) ctx.waitUntil(sendLanding(env, report(hex16(), req, res, own)));
  return res;
}

// Adds X-Robots-Tag: noindex to responses served on the pages.dev address.
export function noindex(req, res) {
  if (!new URL(req.url).hostname.endsWith('.pages.dev')) return res;
  const out = new Response(res.body, res);
  out.headers.set('X-Robots-Tag', 'noindex');
  return out;
}

// SHA-256 hex of INTERNAL_TAG_KEY (the device cookie's value), or null when the key is unset.
async function tagValue(env) {
  if (!env.INTERNAL_TAG_KEY) return null;
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(env.INTERNAL_TAG_KEY));
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, '0')).join('');
}

// One cookie's value from the request, or null.
function cookie(req, name) {
  for (const part of (req.headers.get('Cookie') || '').split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === name) return v.join('=');
  }
  return null;
}

// Serves the storage page; the response is never cached and sends no referrer.
export function settings() {
  return new Response(PAGE, { headers: { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Type': 'text/html; charset=utf-8' } });
}

// Stores the value from the URL fragment (or clears it) in this browser, then moves on to the website's page.
const PAGE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings</title><meta name="robots" content="noindex"><body style="font:16px/1.5 system-ui,sans-serif;background:#0a0e1f;color:#e8ecf8;padding:24px">
<p>One moment.</p><script>
var h = location.hash, m = /^#v=([0-9a-f]{64})$/.exec(h);
try { if (m) localStorage.setItem('lsmPref', m[1]); else if (h === '#v=0') localStorage.removeItem('lsmPref'); } catch (e) {}
location.replace('https://www.llmsyscore.com/s.html' + (m || h === '#v=0' ? h : ''));
</script></body>`;

function hex16() {
  const a = new Uint8Array(8);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

// A page load from outside the site: a navigation (or a client that sends no fetch mode) answered with HTML.
export function isVisit(req, res) {
  const mode = req.headers.get('Sec-Fetch-Mode');
  let own = false;
  try { own = new URL(req.headers.get('Referer') || '').host === new URL(req.url).host; } catch { /* no referrer */ }
  return !own && (!mode || mode === 'navigate') && /text\/html/i.test(res.headers.get('Content-Type') || '');
}

// The receiver's record of one page visit: request line, headers it classifies by, and the visitor's location.
export function report(id, req, res, own = false) {
  const url = new URL(req.url), cf = req.cf || {}, h = (k, n) => (req.headers.get(k) || '').slice(0, n) || null;
  const out = { id, site: url.host, method: req.method, path: url.pathname.slice(0, 200), status: res.status,
    ua: h('User-Agent', 300), referer: h('Referer', 500), lang: h('Accept-Language', 100), own: own ? 1 : 0 };
  for (const k of TAGS) out[k] = (url.searchParams.get(k) || '').slice(0, 100) || null;
  for (const [k, c] of Object.entries(CF)) out[k] = cf[c] ?? null;
  return out;
}

// Queues the report on WRITES (two tries), else writes it through the STORE binding; failures are dropped.
async function sendLanding(env, body) {
  const msg = { kind: 'landing_report', at: new Date().toISOString(), report: body };
  if (env.WRITES) {
    for (let i = 0; i < 2; i++) {
      try { await env.WRITES.send(msg); return; } catch { /* try again, then store */ }
    }
  }
  if (env.STORE) try { await env.STORE.write(msg); } catch (e) { console.log(JSON.stringify({ store_rpc_error: msg.kind, error: String(e).slice(0, 200) })); }
}

// Reports the visit (own: from a tagged browser) after the response and adds the landing id to the page.
// Any failure leaves the page as is.
export function visit(req, res, env, ctx, own = false) {
  if (!(env.WRITES || env.STORE) || !isVisit(req, res)) return res;
  const id = hex16();
  ctx.waitUntil(sendLanding(env, report(id, req, res, own)));
  if (req.method !== 'GET' || res.status !== 200 || typeof HTMLRewriter !== 'function') return res;
  return new HTMLRewriter().on('head', {
    element(e) { e.prepend(`<meta name="lsm-landing" content="${id}">`, { html: true }); },
  }).transform(res);
}
