// Demo site Pages worker: serves the static site and reports each page visit to the telemetry receiver.
// The page gets a landing id that the demo's analytics send with the visit's first event.
const RECEIVER = 'https://telemetry-receiver.llmsyscore.workers.dev/v1/demo/landing';
// Dashboard links with no demo page (log out, log in, server log window), sent to the landing page.
const TO_LANDING = ['/logout', '/login', '/llm/log'];
const TAGS = ['ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
const CF = { country: 'country', region: 'region', city: 'city', timezone: 'timezone', asn: 'asn', as_org: 'asOrganization',
  colo: 'colo', bot_category: 'verifiedBotCategory' };

export default {
  async fetch(request, env, ctx) {
    if (TO_LANDING.includes(new URL(request.url).pathname)) return Response.redirect(new URL('/', request.url).href, 302);
    const res = noindex(request, await env.ASSETS.fetch(request));
    try { return visit(request, res, env, ctx); } catch { return res; }
  },
};

// Adds X-Robots-Tag: noindex to responses served on the pages.dev address.
export function noindex(req, res) {
  if (!new URL(req.url).hostname.endsWith('.pages.dev')) return res;
  const out = new Response(res.body, res);
  out.headers.set('X-Robots-Tag', 'noindex');
  return out;
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
export function report(id, req, res) {
  const url = new URL(req.url), cf = req.cf || {}, h = (k, n) => (req.headers.get(k) || '').slice(0, n) || null;
  const out = { id, site: url.host, method: req.method, path: url.pathname.slice(0, 200), status: res.status,
    ua: h('User-Agent', 300), referer: h('Referer', 500), lang: h('Accept-Language', 100) };
  for (const k of TAGS) out[k] = (url.searchParams.get(k) || '').slice(0, 100) || null;
  for (const [k, c] of Object.entries(CF)) out[k] = cf[c] ?? null;
  return out;
}

// Sends the visit to the receiver after the response and adds the landing id to the page; any failure leaves the page as is.
export function visit(req, res, env, ctx) {
  if (!env.LANDING_SECRET || !isVisit(req, res)) return res;
  const id = hex16();
  ctx.waitUntil(fetch(env.LANDING_URL || RECEIVER, {
    method: 'POST', body: JSON.stringify(report(id, req, res)),
    headers: { Authorization: `Bearer ${env.LANDING_SECRET}`, 'Content-Type': 'application/json' },
  }).catch(() => {}));
  if (req.method !== 'GET' || res.status !== 200 || typeof HTMLRewriter !== 'function') return res;
  return new HTMLRewriter().on('head', {
    element(e) { e.prepend(`<meta name="lsm-landing" content="${id}">`, { html: true }); },
  }).transform(res);
}
