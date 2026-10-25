// Demo site Pages worker: serves the static site and reports each page visit to the telemetry receiver.
// The page gets a landing id that the demo's analytics send with the visit's first event.
const RECEIVER = 'https://telemetry-receiver.llmsyscore.workers.dev/v1/demo/landing';
// Dashboard links with no demo page (log out, log in, server log window), sent to the landing page.
const TO_LANDING = ['/logout', '/login', '/llm/log'];
const TAGS = ['ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
const TAG_COOKIE = 'lsm_device';
const TAG_MAX_AGE_S = 400 * 86400;
const CF = { country: 'country', region: 'region', city: 'city', timezone: 'timezone', asn: 'asn', as_org: 'asOrganization',
  colo: 'colo', bot_category: 'verifiedBotCategory' };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (TO_LANDING.includes(url.pathname)) return Response.redirect(new URL('/', request.url).href, 302);
    const tag = await tagValue(env);
    if (url.pathname === '/internal' && tag && url.searchParams.get('key') === env.INTERNAL_TAG_KEY) return tagDevice(url, tag);
    const res = noindex(request, await env.ASSETS.fetch(request));
    try { return visit(request, res, env, ctx, !!tag && cookie(request, TAG_COOKIE) === tag); } catch { return res; }
  },
};

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

// /internal?key=<INTERNAL_TAG_KEY> marks this browser as the operator's with a cookie; &remove=1 clears it.
// No referrer leaves the page, so the key never reaches the next page's analytics.
export function tagDevice(url, tag) {
  const remove = url.searchParams.get('remove') === '1';
  const res = new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Device tag</title><meta name="robots" content="noindex"><body style="font:16px/1.5 system-ui,sans-serif;background:#0a0e1f;color:#e8ecf8;padding:24px">
<p>${remove ? 'This browser is no longer marked as yours.' : 'This browser is now marked as yours. Its visits to the demo count as internal.'}</p>
<p><a style="color:#8ea4ff" href="/">Demo</a></p></body>`, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' } });
  res.headers.set('Set-Cookie', `${TAG_COOKIE}=${remove ? '' : tag}; Max-Age=${remove ? 0 : TAG_MAX_AGE_S}; Path=/; Secure; HttpOnly; SameSite=Lax`);
  return res;
}

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

// Sends the visit (own: from a tagged browser) to the receiver after the response and adds the landing id to the page.
// Any failure leaves the page as is.
export function visit(req, res, env, ctx, own = false) {
  if (!env.LANDING_SECRET || !isVisit(req, res)) return res;
  const id = hex16();
  ctx.waitUntil(fetch(env.LANDING_URL || RECEIVER, {
    method: 'POST', body: JSON.stringify(report(id, req, res, own)),
    headers: { Authorization: `Bearer ${env.LANDING_SECRET}`, 'Content-Type': 'application/json' },
  }).catch(() => {}));
  if (req.method !== 'GET' || res.status !== 200 || typeof HTMLRewriter !== 'function') return res;
  return new HTMLRewriter().on('head', {
    element(e) { e.prepend(`<meta name="lsm-landing" content="${id}">`, { html: true }); },
  }).transform(res);
}
