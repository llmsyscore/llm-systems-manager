// #760/#761: co-loads the real admin-settings.js in jsdom and drives it with a
// stubbed GET /api/admin/settings, then asserts on the rendered DOM.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = f => readFileSync(join(here, '..', 'js', f), 'utf8');

const GATEWAY_KEYS = {
  path: 'manager.gateway.api_keys', label: 'Gateway API keys',
  help: 'Bearer keys for external clients.', group: 'gateway',
  service: 'manager', type: 'list', secret: true,
};
const AE_INTERVAL = {
  path: 'alarm_engine.evaluation_interval', label: 'Evaluation interval',
  help: 'seconds', group: 'ae', service: 'alarm_engine', type: 'int',
};

function payload(over = {}) {
  return {
    ok: true,
    groups: [{ key: 'gateway', title: 'Inference Gateway' }, { key: 'ae', title: 'Alarm Engine' }],
    entries: [GATEWAY_KEYS, AE_INTERVAL],
    values: {}, secrets: { 'manager.gateway.api_keys': 'set' },
    drift: {}, restart_pending: [], ae_sync_pending: [], ae_sync_retry_s: 30,
    topology: { split: false, ae_config_reachable: true },
    ...over,
  };
}

async function boot(data) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="adminTab"><div id="adminSettingsRoot"></div></div></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  dom.window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.body.appendChild(s);
  };
  inject(src('foundation.js'));
  inject(src('admin-settings.js'));
  await dom.window.adminSettingsLoad();
  return dom.window.document;
}

describe('secret row layout (#760)', () => {
  test('the multi-line secret keeps its chip and Clear on one control row', async () => {
    const doc = await boot(payload());
    const row = doc.querySelector('.settings-row[data-path="manager.gateway.api_keys"]');
    const head = row.querySelector('.st-secret-head');
    expect(head).toBeTruthy();
    // chip and Clear are siblings in the head row, not siblings of the textarea
    expect(head.querySelector('.status')).toBeTruthy();
    expect(head.querySelector('[data-clear]')).toBeTruthy();
    expect(head.querySelector('textarea')).toBeNull();
    expect(row.querySelector('.st-secret > textarea.st-input')).toBeTruthy();
  });

  test('Clear uses the compact button variant so it matches the chip', async () => {
    const doc = await boot(payload());
    const btn = doc.querySelector('[data-clear="manager.gateway.api_keys"]');
    expect(btn.className.split(/\s+/)).toContain('sm');
    // no ad-hoc inline offset left over from the old wrapping layout
    expect(btn.getAttribute('style')).toBeNull();
  });

  test('an unset secret renders no Clear button', async () => {
    const doc = await boot(payload({ secrets: { 'manager.gateway.api_keys': 'unset' } }));
    expect(doc.querySelector('[data-clear="manager.gateway.api_keys"]')).toBeNull();
    expect(doc.querySelector('.st-secret-head .status')).toBeTruthy();
  });
});

describe('alarm-engine config banner (#761)', () => {
  const split = over => payload({
    topology: { split: true, ae_config_reachable: false, ...over },
  });

  test('no banner when the AE config API is reachable', async () => {
    const doc = await boot(payload());
    expect(doc.getElementById('adminSettingsAeConfigBanner')).toBeNull();
  });

  test('a rejected token names the token and the remedy, not "unreachable"', async () => {
    const doc = await boot(split({
      ae_config_error: {
        kind: 'unauthorized', status: 401, detail: 'HTTP 401 from https://ae/api/alarm/admin/config',
        remedy: 'set the same management_token in both hosts',
      },
    }));
    const bar = doc.getElementById('adminSettingsAeConfigBanner');
    expect(bar.textContent).toContain('rejected');
    expect(bar.textContent).toContain('management_token');
    expect(bar.textContent).toContain('HTTP 401');
    expect(bar.textContent).not.toContain('unreachable');
  });

  test('an old engine is reported as missing the API', async () => {
    const doc = await boot(split({
      ae_config_error: { kind: 'unsupported', status: 404, detail: 'HTTP 404', remedy: 'upgrade it' },
    }));
    expect(doc.getElementById('adminSettingsAeConfigBanner').textContent)
      .toContain('no settings API');
  });

  test('AE rows stay locked and point at the banner', async () => {
    const doc = await boot(split({
      ae_config_error: { kind: 'unreachable', status: null, detail: 'OSError', remedy: 'check the URL' },
    }));
    const row = doc.querySelector('.settings-row[data-path="alarm_engine.evaluation_interval"]');
    expect(row.textContent).toContain('🔒');
    expect(row.querySelector('input,select,textarea')).toBeNull();
    expect(row.textContent).toContain('notice above');
  });

  test('the banner still renders with no error detail from the server', async () => {
    const doc = await boot(split());
    expect(doc.getElementById('adminSettingsAeConfigBanner').textContent)
      .toContain('Alarm-engine settings unavailable');
  });
});
