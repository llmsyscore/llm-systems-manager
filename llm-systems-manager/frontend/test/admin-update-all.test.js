// #637/#1108: Admin → Agents "Update all" — sequential runs, verify gate stops the run, other failures carry on, one download retry.
// Co-loads the real admin.js in jsdom and drives adminUpdateAll() with the stream/verify/confirm seams stubbed.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');

const A1 = 'a'.repeat(32);
const A2 = 'b'.repeat(32);

function harness(bootstrap) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(bootstrap);
  return dom.window;
}

// Stubs the seams and starts adminUpdateAll(); scenario knobs:
// streams: {aid: {ok, noRestart}}, verify: {aid: bool}, confirm: bool.
function boot({ agents, streams, verify, confirm = true }) {
  return `
    _adminAgentsCache = ${JSON.stringify(agents)};
    _latestAgentVersion = 'v2';
    window.__calls = { stream: [], verify: [], toasts: [], reloads: 0 };
    _themedConfirm = async () => ${JSON.stringify(confirm)};
    _themedToast = (m) => { window.__calls.toasts.push(m); };
    adminLoadAgents = async () => { window.__calls.reloads++; };
    const __streams = ${JSON.stringify(streams || {})};
    _adminStreamUpdate = async (aid) => {
      window.__calls.stream.push(aid);
      const s = __streams[aid];
      const one = Array.isArray(s) ? s.shift() : s;
      return { transport: false, noRestart: false, ...one };
    };
    _adminAwaitAgentVersion = async (aid, v) => {
      window.__calls.verify.push([aid, v]);
      return (${JSON.stringify(verify || {})})[aid] !== false;
    };
    window.__P = adminUpdateAll();
  `;
}

const agent = (aid, host, upd = true) =>
  ({ agent_id: aid, hostname: host, version: 'v1', update_available: upd });

function panelText(win) {
  const el = win.document.getElementById('adminUpdateLog');
  return el ? el.textContent : '';
}

describe('#637 adminUpdateAll', () => {
  test('updates agents sequentially and verifies each version', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      streams: { [A1]: { ok: true }, [A2]: { ok: true } },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1, A2]);
    expect(win.__calls.verify).toEqual([[A1, 'v2'], [A2, 'v2']]);
    const text = panelText(win);
    expect(text).toContain('[1/2] h1: v1 → v2');
    expect(text).toContain('[2/2] h2: v1 → v2');
    expect(text).toContain('✓ h1 is back on v2');
    expect(text).toContain('Update all finished: 2 updated');
    expect(win.__calls.reloads).toBe(1);
  });

  test('a stream failure is reported and the rest still update (#1108)', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      streams: { [A1]: { ok: false, msg: 'install.sh failed' }, [A2]: { ok: true } },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1, A2]);
    const text = panelText(win);
    expect(text).toContain('Update all finished: 1 updated, failed: h1');
    expect(text).not.toContain('retrying');
  });

  test('a failed download is retried once, then the host counts as updated (#1108)', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      streams: { [A1]: [{ ok: false, retryable: true, msg: 'tarball download failed: connection broken after 131072 of 437120 bytes (3 tries)' }, { ok: true }],
                 [A2]: { ok: true } },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1, A1, A2]);
    const text = panelText(win);
    expect(text).toContain('retrying h1 once — the download failed');
    expect(text).toContain('Update all finished: 2 updated');
  });

  test('a download that fails twice is reported and not retried again', async () => {
    const bad = { ok: false, retryable: true, msg: 'tarball download failed: connection broken after 1 of 9 bytes (3 tries)' };
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      streams: { [A1]: [bad, bad, { ok: true }], [A2]: { ok: true } },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1, A1, A2]);
    expect(panelText(win)).toContain('failed: h1');
  });

  test('a non-retryable download failure is not retried', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      streams: { [A1]: [{ ok: false, retryable: false, msg: 'tarball download failed: HTTP 401 from the manager' }, { ok: true }] },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1]);
  });

  test('a verify timeout stops the run and skips the rest (bad build guard)', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      streams: { [A1]: { ok: true }, [A2]: { ok: true } },
      verify: { [A1]: false },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1]);
    const text = panelText(win);
    expect(text).toContain('✗ h1 did not report v2');
    expect(text).toContain('stopping: h1 did not come back on v2');
    expect(text).toContain('0 updated, failed: h1, skipped: h2');
  });

  test('no-restart success skips the verify wait', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      streams: { [A1]: { ok: true, noRestart: true } },
    }));
    await win.__P;
    expect(win.__calls.verify).toEqual([]);
    expect(panelText(win)).toContain('h1 already up to date');
  });

  test('only update_available agents are included', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2', false)],
      streams: { [A1]: { ok: true } },
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([A1]);
  });

  test('nothing to do → toast, no panel', async () => {
    const win = harness(boot({ agents: [agent(A1, 'h1', false)] }));
    await win.__P;
    expect(win.__calls.toasts.length).toBe(1);
    expect(win.document.getElementById('adminUpdatePanel')).toBeNull();
  });

  test('declined confirm runs nothing', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')], streams: { [A1]: { ok: true } },
      confirm: false,
    }));
    await win.__P;
    expect(win.__calls.stream).toEqual([]);
    expect(win.document.getElementById('adminUpdatePanel')).toBeNull();
  });
});
