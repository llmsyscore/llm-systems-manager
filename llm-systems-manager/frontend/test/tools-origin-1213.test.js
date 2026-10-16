// #1213: the Tools tab loads each tool in a sandboxed frame from the tool origin
// after a reachability probe, and explains an unreachable tool port.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const bootSrc = srcFile('js/boot.js');
const indexSrc = srcFile('index.html');
const TOOLS = ['openclaw', 'llmchat', 'imggen'];

function panels() {
  const at = indexSrc.indexOf('<div id="toolsTab"');
  return indexSrc.slice(at, indexSrc.indexOf('<!-- Admin tab', at));
}

function mockFetch(responses) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts) => {
    calls.push(String(url));
    const r = responses(String(url));
    if (r instanceof Error) throw r;
    return { ok: r.status === 200, status: r.status, json: async () => r.body || {} };
  });
  return calls;
}

describe('Tools frames (#1213)', () => {
  beforeEach(() => {
    document.body.innerHTML = panels();
    const fn = fnSrc(bootSrc, '_toolsLoadFrame');
    expect(fn, '_toolsLoadFrame not found').toBeTruthy();
    evalGlobal(fn + '\nwindow._toolsLoadFrame = _toolsLoadFrame;');
  });

  it('every tool frame is sandboxed without top navigation and has no src', () => {
    for (const t of TOOLS) {
      const f = document.querySelector(`#tools-${t} iframe[data-tool="${t}"]`);
      expect(f, t).toBeTruthy();
      const sb = f.getAttribute('sandbox');
      expect(sb).toContain('allow-scripts');
      expect(sb).toContain('allow-same-origin');
      expect(sb).not.toContain('allow-top-navigation');
      expect(f.hasAttribute('src')).toBe(false);
      expect(document.querySelector(`#tools-${t} .tools-unreachable`)).toBeTruthy();
    }
  });

  it('loads the frame from the ticket url after a successful probe', async () => {
    const url = 'http://mgr.example:5447/proxy/openclaw/?tool_ticket=abc';
    const calls = mockFetch(u => u.startsWith('/api/tool-ticket') ? { status: 200, body: { url, port: 5447 } }
                                : { status: 200 });
    await window._toolsLoadFrame('openclaw');
    expect(calls).toEqual(['/api/tool-ticket?tool=openclaw', 'http://mgr.example:5447/tools-ping']);
    expect(document.querySelector('#tools-openclaw iframe').src).toBe(url);
    expect(document.querySelector('#tools-openclaw .tools-unreachable').hidden).toBe(true);
  });

  it('shows the unreachable notice and leaves the frame empty when the probe fails', async () => {
    mockFetch(u => u.startsWith('/api/tool-ticket')
      ? { status: 200, body: { url: 'http://mgr.example:5447/proxy/llmchat/?tool_ticket=x', port: 5447 } }
      : new TypeError('Failed to fetch'));
    await window._toolsLoadFrame('llmchat');
    const note = document.querySelector('#tools-llmchat .tools-unreachable');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe('The tools port 5447 is not reachable from your browser. Open it on the firewall, or set [manager].tools_port to 0 to show the tools on the dashboard port.');
    expect(document.querySelector('#tools-llmchat iframe').hasAttribute('src')).toBe(false);
  });

  it('names the certificate step in the unreachable notice for an https tool port', async () => {
    mockFetch(u => u.startsWith('/api/tool-ticket')
      ? { status: 200, body: { url: 'https://mgr.example:5448/proxy/llmchat/?tool_ticket=x', port: 5448 } }
      : new TypeError('Failed to fetch'));
    await window._toolsLoadFrame('llmchat');
    expect(document.querySelector('#tools-llmchat .tools-unreachable').textContent).toBe(
      'The tools port 5448 is not reachable from your browser. Open it on the firewall, or open https://mgr.example:5448/tools-ping once and accept the certificate, or set [manager].tools_port to 0 to show the tools on the dashboard port.');
  });

  it('shows a notice when the ticket request fails with another status (401)', async () => {
    mockFetch(() => ({ status: 401, body: { error: 'authentication required' } }));
    await window._toolsLoadFrame('openclaw');
    const note = document.querySelector('#tools-openclaw .tools-unreachable');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe('The tool page could not be opened (HTTP 401). Sign in again or reload the dashboard.');
    expect(document.querySelector('#tools-openclaw iframe').hasAttribute('src')).toBe(false);
  });

  it('asks for the https tool port when the dashboard is on https', async () => {
    const fn = fnSrc(bootSrc, '_toolsLoadFrame');
    // Shadows the global location so the function reads an https page scheme.
    evalGlobal(`(function (location) {\n${fn}\nwindow._toolsLoadFrame = _toolsLoadFrame;\n})({ protocol: 'https:' });`);
    const url = 'https://mgr.example:5448/proxy/openclaw/?tool_ticket=abc';
    const calls = mockFetch(u => u.startsWith('/api/tool-ticket') ? { status: 200, body: { url, port: 5448 } }
                                : { status: 200 });
    await window._toolsLoadFrame('openclaw');
    expect(calls[0]).toBe('/api/tool-ticket?tool=openclaw&https=1');
  });

  it('falls back to the same-origin path when the tool origin is disabled (409)', async () => {
    mockFetch(() => ({ status: 409, body: { error: 'tool origin disabled' } }));
    await window._toolsLoadFrame('imggen');
    expect(document.querySelector('#tools-imggen iframe').getAttribute('src')).toBe('/proxy/imggen/');
  });

  it('does not reload a frame that is already showing the tool', async () => {
    const url = 'http://mgr.example:5447/proxy/openclaw/?tool_ticket=abc';
    const calls = mockFetch(u => u.startsWith('/api/tool-ticket') ? { status: 200, body: { url, port: 5447 } }
                                : { status: 200 });
    await window._toolsLoadFrame('openclaw');
    await window._toolsLoadFrame('openclaw');
    expect(calls.filter(c => c.startsWith('/api/tool-ticket')).length).toBe(1);
  });
});
