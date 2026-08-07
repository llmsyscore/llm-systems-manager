// Companion shell logic (#522): SW registration, theme, push opt-in/test.
// IIFE — classic scripts share one global scope with the dashboard bundle.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g,
    (c) => `&#${c.charCodeAt(0)};`);
  const enc = (cls, glyph, word) =>
    `<span class="${cls}">${glyph} ${esc(word)}</span>`;
  const OK = (w) => enc('cmp-ok', '✓', w);
  const WARN = (w) => enc('cmp-warn', '▲', w);
  const CRIT = (w) => enc('cmp-crit', '!', w);
  const DIM = (w) => enc('cmp-dim', '·', w);

  async function jfetch(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts));
    if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
    return r.json();
  }

  async function applyTheme() {
    try {
      const layout = await jfetch('/api/layout');
      if (layout && layout.theme)
        document.documentElement.setAttribute('data-theme', layout.theme);
    } catch (_) { /* default theme */ }
  }

  const standalone = () =>
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
    || window.navigator.standalone === true;

  async function registration() {
    if (!('serviceWorker' in navigator)) return null;
    try { return await navigator.serviceWorker.register('/sw.js'); }
    catch (_) { return null; }
  }

  async function subscription(reg) {
    if (!reg || !reg.pushManager) return null;
    try { return await reg.pushManager.getSubscription(); }
    catch (_) { return null; }
  }

  async function paint(reg) {
    $('stInstall').innerHTML = standalone() ? OK('INSTALLED') : WARN('BROWSER TAB');
    $('installHint').hidden = standalone();
    $('stSw').innerHTML = reg ? OK('ACTIVE') : CRIT('UNAVAILABLE');
    const perm = ('Notification' in window) ? Notification.permission : 'unsupported';
    $('stPerm').innerHTML =
      perm === 'granted' ? OK('GRANTED')
        : perm === 'denied' ? CRIT('DENIED') : DIM(perm.toUpperCase());
    const sub = await subscription(reg);
    $('stSub').innerHTML = sub ? OK('SUBSCRIBED') : DIM('NOT SUBSCRIBED');
    $('btnEnable').disabled = !!sub || !reg;
    $('btnTest').disabled = !sub;
    $('btnRemove').hidden = !sub;
    try {
      const subs = await jfetch('/api/companion/push/subscriptions');
      $('stCount').textContent = subs.count;
    } catch (_) { $('stCount').textContent = '?'; }
    return sub;
  }

  function say(html) { $('msg').innerHTML = html; }

  async function enable(reg) {
    if (!('Notification' in window) || !reg || !reg.pushManager) {
      say(CRIT('PUSH UNSUPPORTED') + ' — install to Home Screen first (iOS 16.4+)');
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { await paint(reg); return; }
    try {
      const key = (await jfetch('/api/companion/push/public-key')).key;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: window.PushUtil.urlB64ToUint8Array(key),
      });
      const res = await jfetch('/api/companion/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
      });
      say(res.ok ? OK('SUBSCRIBED') : CRIT((res.error || 'subscribe failed').toUpperCase()));
    } catch (err) {
      say(CRIT('SUBSCRIBE FAILED') + ` — ${esc(err && err.message || err)}`);
    }
    await paint(reg);
  }

  async function testPush() {
    say(DIM('SENDING…'));
    try {
      const res = await jfetch('/api/companion/push/test', { method: 'POST' });
      say(res.ok ? OK(`SENT ${res.sent}`)
        : CRIT((res.error || `failed ${res.failed}`).toUpperCase()));
    } catch (err) {
      say(CRIT('SEND FAILED') + ` — ${esc(err && err.message || err)}`);
    }
  }

  async function disable(reg) {
    const sub = await subscription(reg);
    if (sub) {
      try {
        await jfetch('/api/companion/push/unsubscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      } catch (_) { /* repaint reflects the actual state */ }
    }
    say('');
    await paint(reg);
  }

  document.addEventListener('DOMContentLoaded', async () => {
    applyTheme();
    const reg = await registration();
    await paint(reg);
    $('btnEnable').addEventListener('click', () => enable(reg));
    $('btnTest').addEventListener('click', testPush);
    $('btnRemove').addEventListener('click', () => disable(reg));
  });
})();
