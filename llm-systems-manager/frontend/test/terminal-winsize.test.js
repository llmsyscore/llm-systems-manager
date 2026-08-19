// #573: the terminal sends its real fitted size (create body + an immediate
// resize) instead of leaving the PTY at 80x24. Real sources run in jsdom.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'terminal.js'), 'utf8');

function fnSrc(name) {
  const m = src.match(new RegExp(
    `(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found in terminal.js`).toBeTruthy();
  return m[0];
}

let fetchCalls;

beforeEach(() => {
  document.body.innerHTML = '<div id="terminalMount"></div>';
  fetchCalls = [];
  window.fetch = vi.fn((url, opts = {}) => {
    fetchCalls.push({ url, opts });
    return Promise.resolve({ ok: true });
  });
  window._jsonOrThrow = async () => ({ ok: true, sid: 'sid1' });
  window.EventSource = class {
    constructor() { this.onmessage = null; this.onerror = null; }
    close() {}
  };
  // xterm stubs: the fitted terminal reports a real (wide) size.
  window.Terminal = class {
    constructor() { this.cols = 187; this.rows = 42; }
    loadAddon() {} open() {} write() {} dispose() {}
    onData() {} onResize() {}
  };
  window.FitAddon = { FitAddon: class { fit() {} } };
  window._term = null; window._termFit = null; window._termSid = null;
  window._termEvt = null;
  (0, eval)([
    fnSrc('_termPostSize'), fnSrc('_termMkXterm'), fnSrc('_termCloseSession'),
    fnSrc('_termStart'),
    'window._termStart = _termStart;',
  ].join('\n'));
});

describe('embedded terminal winsize (#573)', () => {
  it('sends the fitted rows/cols in the create request body', async () => {
    await window._termStart(document.getElementById('terminalMount'));
    const create = fetchCalls.find(c => c.url === '/api/terminal/create');
    expect(create).toBeTruthy();
    expect(JSON.parse(create.opts.body)).toMatchObject({ rows: 42, cols: 187 });
  });

  it('syncs the size right after connecting (covers older agents)', async () => {
    await window._termStart(document.getElementById('terminalMount'));
    const resize = fetchCalls.find(c => c.url === '/api/terminal/resize/sid1');
    expect(resize).toBeTruthy();
    expect(JSON.parse(resize.opts.body)).toEqual({ rows: 42, cols: 187 });
  });
});
