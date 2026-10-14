// #1217/#1204: enrollment secret chip, Enrollment block, reveal timer and install command.
import { describe, it, expect, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { blockSrc } from './helpers/harness.js';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const agentsSrc = readFileSync(join(here, '..', 'js', 'admin-agents.js'), 'utf8');
const indexSrc = readFileSync(join(here, '..', 'index.html'), 'utf8');

// The real panel markup, lifted from index.html so ids stay in lockstep.
const STAMP = indexSrc.slice(indexSrc.indexOf('<button type="button" class="hc-stamp"'), indexSrc.indexOf('</button>', indexSrc.indexOf('id="adminRefreshStamp"')) + 9);
const PANEL = STAMP + blockSrc(indexSrc, '<div id="admin-agents"', '<!-- Gateway sub-tab', { includeEnd: false });

function harness(bootstrap, body = PANEL) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${body}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(agentsSrc);
  if (bootstrap) inject(bootstrap);
  return dom.window;
}

describe('enrollment secret UI (#1217/#1204)', () => {
  it('chip text reflects whether a secret is required', () => {
    const v = harness('').AgentsView;
    expect(v.secretChipText(true)).toEqual({ html: 'Secret <b>required</b>', cls: 'ok' });
    expect(v.secretChipText(false)).toEqual({ html: '<b>No</b> secret', cls: 'warn' });
  });

  it('install command carries url, fingerprint and secret', () => {
    const v = harness('').AgentsView;
    const c = { managerHttpsUrl: 'https://10.0.0.1:5443', caFingerprint: 'sha256:' + 'a'.repeat(64), pageOrigin: 'http://10.0.0.1:5000' };
    const cmd = v.installCommand(c, 'S3CRET');
    expect(cmd).toBe('sudo bash install.sh --mode 5 -- --manager-url https://10.0.0.1:5443 --ca-fingerprint ' + 'a'.repeat(64) + ' --enrollment-secret S3CRET');
  });

  it('install command falls back to the page origin and omits missing parts', () => {
    const v = harness('').AgentsView;
    expect(v.installCommand({ managerHttpsUrl: '', caFingerprint: '', pageOrigin: 'http://10.0.0.1:5000' }, ''))
      .toBe('sudo bash install.sh --mode 5 -- --manager-url http://10.0.0.1:5000');
  });
});

describe('enrollment block render and reveal timer', () => {
  const FP = 'b'.repeat(64);

  it('header shows the secret state and the copy-fingerprint button', () => {
    const w = harness('_adminEnrollSecretRequired = true; _adminCaFingerprint = "sha256:' + FP + '";');
    w.AgentsView.render();
    const d = w.document;
    expect(d.getElementById('agSecretChip').textContent).toBe('Secret required');
    expect(d.getElementById('agSecretState').textContent).toContain('set');
    expect(d.getElementById('agCaFp')).toBeNull();
    expect(d.getElementById('agCaCopy').hidden).toBe(false);
  });

  it('header with no secret and no fingerprint', () => {
    const w = harness('_adminEnrollSecretRequired = false; _adminCaFingerprint = "";');
    w.AgentsView.render();
    const d = w.document;
    expect(d.getElementById('agSecretChip').textContent).toBe('No secret');
    expect(d.getElementById('agCaCopy').hidden).toBe(true);
  });

  it('a second reveal restarts the 60 s timer', async () => {
    vi.useFakeTimers();
    try {
      const w = harness('');
      w.fetch = async () => ({ ok: true, json: async () => ({ secret: 'S3CRET' }) });
      await w.adminEnrollmentSecretReveal();
      vi.advanceTimersByTime(30000);
      await w.adminEnrollmentSecretReveal();
      vi.advanceTimersByTime(59000);
      expect(w.document.getElementById('agSecretRow').hidden).toBe(false);
      expect(w.document.getElementById('agSecretVal').textContent).toBe('S3CRET');
      vi.advanceTimersByTime(2000);
      expect(w.document.getElementById('agSecretRow').hidden).toBe(true);
      expect(w.document.getElementById('agSecretVal').textContent).toBe('');
    } finally {
      vi.useRealTimers();
    }
  });

  it('install command is copied, never rendered; a missing clipboard gives a plain toast', async () => {
    const w = harness('_adminManagerHttpsUrl = "https://10.0.0.1:5443"; _adminCaFingerprint = "sha256:' + FP + '";');
    Object.defineProperty(w.navigator, 'clipboard', { value: undefined, configurable: true });
    w.fetch = async () => ({ ok: true, json: async () => ({ secret: 'S3CRET' }) });
    const errs = [];
    w._toastErr = (m) => errs.push(m);
    const built = [];
    w.AgentsView.installCommand = (c, s) => { built.push(s); return 'CMD'; };
    await w.adminCopyInstallCommand();
    expect(built).toEqual(['S3CRET']);
    expect(w.document.getElementById('agInstallRow')).toBeNull();
    expect(w.document.body.textContent).not.toContain('CMD');
    expect(errs.join(' ')).not.toContain('Could not build');
    expect(errs.join(' ')).toContain('HTTPS');
  });

  it('install command without a secret drops the flag and the sha256 prefix', () => {
    const v = harness('').AgentsView;
    const cmd = v.installCommand({ managerHttpsUrl: 'https://h:5443', caFingerprint: 'sha256:' + FP, pageOrigin: '' }, '');
    expect(cmd).toBe('sudo bash install.sh --mode 5 -- --manager-url https://h:5443 --ca-fingerprint ' + FP);
  });
});
