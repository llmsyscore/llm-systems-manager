// Account → Change my password: two-field dialog with inline validation
// (#819), inline wrong-current-password error (#820), notice refresh (#821).
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const foundationSrc = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');

function harness(responder) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  w.calls = [];
  w.fetch = async (url, opts) => {
    w.calls.push({ url, body: JSON.parse(opts.body) });
    const [status, body] = await responder(w.calls.length);
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  const inject = (code) => {
    const s = w.document.createElement('script');
    s.textContent = code;
    w.document.head.appendChild(s);
  };
  inject(foundationSrc);
  inject(adminSrc);
  inject('window.authLoads = 0; window.adminAuthLoad = () => { window.authLoads++; };'
    + 'window.setTab = (t) => { _activeTab = t; };');
  return w;
}

const tick = () => new Promise(r => setTimeout(r, 5));
const type = (w, el, value) => { el.value = value; el.dispatchEvent(new w.Event('input', { bubbles: true })); };
const enter = (w, el) => el.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
const open = (w) => {
  const p = w._accountPasswordDialog();
  const q = (id) => w.document.getElementById(id);
  return { p, dlg: () => q('accountPwDialog'), cur: q('apCur'), np: q('apNew'), save: q('apSave'),
    hint: q('apHint'), err: q('apCurErr') };
};

describe('default-password notice (#821)', () => {
  test('the hidden attribute wins over the Admin notice display rule', () => {
    const css = readFileSync(join(here, '..', 'css', 'admin-tabs.css'), 'utf8');
    const dom = new JSDOM(`<!doctype html><html><head><style>${css}</style></head><body>
      <div id="adminTab"><div class="notice" id="n" hidden>Default password in use</div></div></body></html>`);
    const w = dom.window;
    const n = w.document.getElementById('n');
    expect(w.getComputedStyle(n).display).toBe('none');
    n.hidden = false;
    expect(w.getComputedStyle(n).display).toBe('flex');
  });
});

describe('change-password dialog', () => {
  test('Save stays disabled and the hint counts down until the minimum is met', async () => {
    const w = harness(async () => [200, { ok: true }]);
    const d = open(w);
    expect(d.save.disabled).toBe(true);
    type(w, d.cur, 'oldpass');
    type(w, d.np, 'abc');
    expect(d.hint.textContent).toBe('5 more characters required.');
    expect(d.save.disabled).toBe(true);
    type(w, d.np, 'abcdefg');
    expect(d.hint.textContent).toBe('1 more character required.');
    enter(w, d.np);
    await tick();
    expect(d.dlg()).not.toBeNull();
    expect(w.calls.length).toBe(0);
    type(w, d.np, 'abcdefgh');
    expect(d.hint.textContent).toBe('');
    expect(d.save.disabled).toBe(false);
  });

  test('a wrong current password is reported inline and the new password is kept', async () => {
    const w = harness(async (n) => n === 1
      ? [403, { ok: false, error: 'current password is incorrect', field: 'current_password' }]
      : [200, { ok: true }]);
    const d = open(w);
    type(w, d.cur, 'wrong');
    type(w, d.np, 'brand-new-pw');
    d.save.click();
    await tick();
    expect(d.dlg()).not.toBeNull();
    expect(d.err.textContent).toBe('current password is incorrect');
    expect(d.np.value).toBe('brand-new-pw');
    expect(d.save.disabled).toBe(false);
    type(w, d.cur, 'right');
    expect(d.err.textContent).toBe('');
    d.save.click();
    await tick();
    expect(d.dlg()).toBeNull();
    expect(await d.p).toBe(true);
    expect(w.calls[1].body).toEqual({ current_password: 'right', new_password: 'brand-new-pw' });
  });

  test('a server failure keeps the dialog open and raises a sticky toast', async () => {
    const w = harness(async () => [500, { ok: false, error: 'store unavailable' }]);
    const d = open(w);
    type(w, d.cur, 'oldpass');
    type(w, d.np, 'brand-new-pw');
    d.save.click();
    await tick();
    expect(d.dlg()).not.toBeNull();
    const toast = w.document.querySelector('.themed-toast.sticky');
    expect(toast).not.toBeNull();
    expect(toast.textContent).toContain('store unavailable');
    toast.click();
    expect(w.document.querySelector('.themed-toast.sticky')).toBeNull();
  });

  test('Enter on the Cancel button does not submit, and Escape is ignored while a save is in flight', async () => {
    let release;
    const w = harness(() => new Promise(r => { release = () => r([200, { ok: true }]); }));
    const d = open(w);
    type(w, d.cur, 'oldpass');
    type(w, d.np, 'brand-new-pw');
    const cancelBtn = w.document.getElementById('apCancel');
    cancelBtn.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await tick();
    expect(w.calls.length).toBe(0);
    d.np.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await tick();
    expect(w.calls.length).toBe(1);
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(d.dlg()).not.toBeNull();
    release();
    expect(await d.p).toBe(true);
  });

  test('a repeated sticky toast replaces the previous one', () => {
    const w = harness(async () => [200, { ok: true }]);
    w._themedToast('first', { kind: 'err', sticky: true });
    w._themedToast('second', { kind: 'err', sticky: true });
    const toasts = w.document.querySelectorAll('.themed-toast.sticky');
    expect(toasts.length).toBe(1);
    expect(toasts[0].textContent).toContain('second');
  });

  test('Escape cancels without a request', async () => {
    const w = harness(async () => [200, { ok: true }]);
    const d = open(w);
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(await d.p).toBe(false);
    expect(w.calls.length).toBe(0);
  });

  test('a successful change refreshes the Access Control notice while on Admin (#821)', async () => {
    const w = harness(async () => [200, { ok: true }]);
    w.setTab('admin');
    const p = w._accountChangePassword();
    await tick();
    type(w, w.document.getElementById('apCur'), 'oldpass');
    type(w, w.document.getElementById('apNew'), 'brand-new-pw');
    w.document.getElementById('apSave').click();
    await p;
    expect(w.authLoads).toBe(1);
  });
});
