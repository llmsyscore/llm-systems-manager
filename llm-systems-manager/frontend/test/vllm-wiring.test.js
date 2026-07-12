// #125: vLLM frontend wiring — source-level assertions that every
// hardcoded provider list gained the vllm entry (same style as tabswitch.test.js).
import { describe, test, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(join(here, '..', f), 'utf8');

describe('foundation.js', () => {
  const foundation = src('js/foundation.js');
  test('routes /api/vllm/* to the vllm provider for ?agent= injection', () => {
    expect(foundation).toMatch(/\[\/\^\\\/api\\\/vllm\\\/\/,\s*'vllm'\]/);
  });
  test('picker containers and provider state include vllm', () => {
    expect(foundation).toContain("agentPickerDashVllm");
    expect(foundation).toContain("agentPickerCtrlVllm");
    expect(foundation).toMatch(/_agentsByProvider.*vllm/);
    expect(foundation).toContain('CARD_LABELS_VLLM');
  });
});

describe('boot.js', () => {
  const boot = src('js/boot.js');
  test('dashboard + llm sub-tab maps include vllm', () => {
    const dash = boot.match(/dashboard:\s*\{[^}]*subs:\s*\[([^\]]*)\]/);
    const llm = boot.match(/llm:\s*\{[^}]*subs:\s*\[([^\]]*)\]/);
    expect(dash[1]).toContain("'vllm'");
    expect(llm[1]).toContain("'vllm'");
  });
  test('boot IIFE kicks off the vllm poller', () => {
    expect(boot).toContain('fetchVllmMetrics');
  });
});

describe('overall.js', () => {
  const overall = src('js/overall.js');
  test('fetches the vllm fleet aggregate and paints the tile', () => {
    expect(overall).toContain('/api/fleet/vllm/aggregate');
    expect(overall).toContain('updateOverallVllmFleet');
  });
});

describe('charts.js', () => {
  const charts = src('js/charts.js');
  test('checkConfig gates the vllm sub-tab buttons on vllm_present', () => {
    expect(charts).toContain('vllm_present');
    expect(charts).toContain('subTabBtnDashVllm');
    expect(charts).toContain('subTabBtnLlmVllm');
  });
  // #366: _activeTabLayoutKeys must resolve the vllm sub-tab to the vllm grid.
  test('_activeTabLayoutKeys handles the vllm sub-tab', () => {
    const fn = charts.slice(charts.indexOf('function _activeTabLayoutKeys'));
    const body = fn.slice(0, fn.indexOf('\n}'));
    expect(body).toContain("sub === 'vllm'");
    expect(body).toContain('vllmCardGrid');
    expect(body).toContain('vllmCols');
  });
});

describe('admin.js', () => {
  const admin = src('js/admin.js');
  test('capability chip order includes vllm', () => {
    expect(admin).toMatch(/'llama',\s*'lms',\s*'vllm'/);
  });
  // #370: the Data Flow panel must render a vLLM push row like llama/LMS.
  test('Data Flow renders a primary vLLM push row', () => {
    expect(admin).toContain('primary_vllm_push');
    expect(admin).toContain('Primary vLLM push');
  });
});

describe('index.html', () => {
  const html = src('index.html');
  test('has the two vllm sub-tab panels and nav buttons', () => {
    expect(html).toContain('id="dash-vllm"');
    expect(html).toContain('id="llm-vllm"');
    expect(html).toContain('id="subTabBtnDashVllm"');
    expect(html).toContain('id="subTabBtnLlmVllm"');
  });
  test('loads js/vllm.js before boot.js', () => {
    const vllmIdx = html.indexOf('/static/js/vllm.js');
    const bootIdx = html.indexOf('/static/js/boot.js');
    expect(vllmIdx).toBeGreaterThan(-1);
    expect(vllmIdx).toBeLessThan(bootIdx);
  });
  // #364: bare canvases inside a flex .card with maintainAspectRatio:false
  // grow unbounded. Each vllm chart canvas must sit in a height-constrained
  // .chart-wrap like every other dashboard chart.
  test.each(['vllmKvChart', 'vllmTpsChart'])('%s canvas is wrapped in .chart-wrap', (id) => {
    const m = html.match(new RegExp(`<div class="chart-wrap"[^>]*>\\s*<canvas id="${id}"`));
    expect(m).not.toBeNull();
  });

  // #366: the vllm control panel must match the llama/LMS toolbar pattern.
  describe('vllm control panel matches llama/LMS UX', () => {
    const panel = html.slice(html.indexOf('id="llm-vllm"'), html.indexOf('end llmTab'));
    test('server-control buttons sit in a .llm-toolbar', () => {
      expect(panel).toMatch(/<div class="llm-toolbar"[^>]*>\s*<button[^>]*vllmBtnStart/);
    });
    test('uses the muted button palette, not bright green/red/amber', () => {
      expect(panel).not.toContain('btn-green-muted-gradient');
      expect(panel).not.toContain('btn-red-muted-gradient');
      expect(panel).not.toContain('btn-amber-muted-gradient');
    });
    test('section titles use the canonical llm-collapse-icon', () => {
      expect(panel).toContain('llm-collapse-icon');
      expect(panel).not.toContain('llm-section-arrow');
    });
    // #368: full parity with the llama/LMS Server Control.
    test('has Terminal, Server Log, and Server Config buttons', () => {
      expect(panel).toContain('onclick="toggleVllmTerminal()"');
      expect(panel).toContain('>☰ Server Log</button>');
      expect(panel).toContain("openServerConfig('vllm')");
    });
    test('terminal panel + mount with the vllm fit key', () => {
      expect(panel).toContain('id="vllmTerminalPanel"');
      expect(panel).toContain('id="vllmTerminalMount"');
      expect(panel).toContain('data-fit-xterm="vllm"');
      expect(panel).toContain('onclick="reconnectVllmTerminal()"');
    });
    test('log panel has pop-out / fullscreen / refresh toolbar', () => {
      expect(panel).toContain('onclick="popOutVllmLog()"');
      expect(panel).toContain('onclick="fullscreenVllmLog()"');
      expect(panel).toContain('onclick="fetchVllmLog()"');
    });
    test('control badge is seeded as a status pill', () => {
      expect(panel).toMatch(/<span class="status status--crit" id="vllmCtrlBadge"[^>]*>/);
      expect(panel).toContain('<span class="status__dot"></span>');
    });
  });
});

describe('#368 vllm control parity wiring', () => {
  test('base.css button override includes #llm-vllm', () => {
    const css = src('css/base.css');
    expect(css).toContain('#llm-vllm .btn:not([data-act]):not([data-lmsact]):not(.btn-log)');
    expect(css).toContain('#llm-vllm .btn:not([data-act]):not([data-lmsact]):not(.btn-log):hover');
  });
  test('terminal.js wires the vllm terminal to /api/vllm/terminal/create', () => {
    const term = src('js/terminal.js');
    expect(term).toContain('function toggleVllmTerminal()');
    expect(term).toContain("fetch('/api/vllm/terminal/create'");
    expect(term).toContain('function popOutVllmTerminal()');
  });
  test('llmcontrol.js _fitXterm handles the vllm key', () => {
    expect(src('js/llmcontrol.js')).toContain("key === 'vllm'");
  });
  test('backend registers POST /api/vllm/terminal/create', () => {
    const bt = readFileSync(join(here, '..', '..', 'backend', 'terminal.py'), 'utf8');
    expect(bt).toContain('/api/vllm/terminal/create');
    expect(bt).toContain('_proxy_create("vllm"');
  });
});
