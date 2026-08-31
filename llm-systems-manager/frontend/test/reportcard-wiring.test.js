// #468: the Report Card sub-tab must match the llama/LMS/vLLM panel UX.
// Markup/CSS are driven through real jsdom DOM + CSSOM APIs, not regexed text.
import { describe, it, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, runHarness } from './helpers/harness.js';

const html = srcFile('index.html');
const css = srcFile('css/base.css');
const rcCss = srcFile('css/reportcard.css');
const rcJs = srcFile('js/report-card.js');

const start = html.indexOf('id="llm-tools"');
const tagStart = html.lastIndexOf('<div', start);
const after = html.indexOf('id="llm-', start + 1);
const panelHtml = html.slice(tagStart, after === -1 ? html.indexOf('end llmTab') : after);

const fullDoc = new JSDOM(html).window.document;

// Mounts the real panel markup in a scripts-enabled jsdom window so its
// onclick="" attributes really fire when clicked.
function mountPanel() {
  return runHarness({ bodyHtml: panelHtml });
}

// Same, plus the real report-card.js executed so its functions are callable.
function mountPanelWithReportCard() {
  return runHarness({ sources: [rcJs], bodyHtml: panelHtml });
}

const baseRules = [...new JSDOM(`<!doctype html><head><style>${css}</style></head><body></body></html>`)
  .window.document.styleSheets[0].cssRules];

// Finds the first rule with a comma-separated selector alternative that
// contains every token, returning that alternative alongside its rule.
function findAlt(rules, tokens) {
  for (const rule of rules) {
    if (!rule.selectorText) continue;
    const alt = rule.selectorText.split(',').map(s => s.trim())
      .find(s => tokens.every(t => s.includes(t)));
    if (alt) return { rule, alt };
  }
  return null;
}

describe('report card panel', () => {
  it('exists as a sub-tab panel with the sibling padding', () => {
    const doc = mountPanel().document;
    const panel = doc.getElementById('llm-tools');
    expect(panel).toBeTruthy();
    expect(panel.classList.contains('sub-tab-panel')).toBe(true);
    expect(panel.style.padding).toBe('8px 20px 20px');
  });

  // #769: the launcher home and the report-card module shell coexist in the
  // panel — home visible, module hidden until a tool is opened.
  it('hosts the launcher home and a hidden report-card module shell', () => {
    const win = mountPanel();
    const doc = win.document;
    expect(doc.getElementById('toolsHome')).toBeTruthy();
    expect(doc.getElementById('toolsLauncher')).toBeTruthy();
    expect(doc.getElementById('toolsLedgerSec')).toBeTruthy();
    const mod = doc.getElementById('toolsMod');
    expect(mod).toBeTruthy();
    expect(mod.style.display).toBe('none');
    const crumb = mod.querySelector('.tools-crumb');
    expect(crumb).toBeTruthy();
    let calls = 0;
    win.toolsCloseModule = () => { calls++; };
    crumb.click();
    expect(calls).toBe(1);
  });

  it('offers the three launcher views in the segmented switcher', () => {
    const seg = mountPanel().document.getElementById('toolsViewSeg');
    expect(seg).toBeTruthy();
    const views = [...seg.querySelectorAll('button[data-view]')].map(b => b.dataset.view);
    expect(views).toEqual(['card', 'list', 'compact']);
  });

  it('uses the muted button palette, not bright green/red/amber', () => {
    const doc = mountPanel().document;
    const buttons = [...doc.querySelectorAll('#llm-tools .rc-panel button')];
    expect(buttons.length).toBeGreaterThan(4);
    const forbidden = ['btn-green-muted-gradient', 'btn-red-muted-gradient', 'btn-amber-muted-gradient'];
    buttons.forEach(b => forbidden.forEach(cls => expect(b.classList.contains(cls)).toBe(false)));
  });

  it('every report-card button carries the shared .btn class', () => {
    const doc = mountPanel().document;
    const buttons = [...doc.querySelectorAll('#llm-tools .rc-panel button')];
    expect(buttons.length).toBeGreaterThan(4);
    buttons.forEach(b => expect(b.classList.contains('btn')).toBe(true));
  });

  it('is covered by the per-sub-tab .btn restyle (real cascade, not just llamacpp)', () => {
    const tabs = ['llm-llamacpp', 'llm-lmstudio', 'llm-tools', 'dash-energy', 'llm-vllm'];
    const markup = tabs.map(id => `<div id="${id}"><button class="btn" id="btn-${id}"></button></div>`).join('')
      + '<div id="llm-bogus"><button class="btn" id="btn-bogus"></button></div>';
    const dom = new JSDOM(`<!doctype html><head><style>${css}</style></head><body>${markup}</body></html>`);
    const w = dom.window, doc = w.document;
    const props = (id) => {
      const cs = w.getComputedStyle(doc.getElementById(id));
      return { padding: cs.padding, borderRadius: cs.borderRadius, fontSize: cs.fontSize, lineHeight: cs.lineHeight };
    };
    const llama = props('btn-llm-llamacpp');
    tabs.forEach(id => expect(props(`btn-${id}`)).toEqual(llama));
    // Sanity control: an id outside the restyle list must NOT pick up these
    // values, proving the equality checks above actually discriminate.
    expect(props('btn-bogus')).not.toEqual(llama);
  });

  it('appears in the hover block only with :hover attached', () => {
    baseRules.forEach(rule => {
      if (!rule.selectorText) return;
      const alts = rule.selectorText.split(',').map(s => s.trim());
      const rc = alts.find(s => s.includes('#llm-tools .btn'));
      if (!rc) return;
      const llamaAlts = alts.filter(s => s.includes('#llm-llamacpp .btn'));
      if (!llamaAlts.length) return;
      const sibHover = llamaAlts.some(s => s.includes(':hover'));
      expect(rc.includes(':hover')).toBe(sibHover);
    });
  });

  // #509: leaderboard submission is inert until the service ships.
  describe('leaderboard submit button (#509)', () => {
    it('carries the disabled attribute so it cannot be clicked', () => {
      const btn = mountPanel().document.getElementById('rcSubmitBtn');
      expect(btn.disabled).toBe(true);
      expect(btn.getAttribute('aria-disabled')).toBe('true');
    });

    // Safari renders no native tooltip for a disabled control, so the text is
    // drawn by CSS from the wrapper's data-tip; title stays for AT only.
    it('supplies the coming-soon text via the wrapper data-tip', () => {
      const doc = mountPanel().document;
      const wrap = doc.getElementById('rcSubmitWrap');
      const btn = doc.getElementById('rcSubmitBtn');
      expect(wrap.dataset.tip.toLowerCase()).toContain('coming soon');
      expect(wrap.classList.contains('rc-soon')).toBe(true);
      expect(btn.getAttribute('title').toLowerCase()).toContain('coming soon');
      // The wrapper genuinely wraps the button (not just precedes it in
      // markup) — that's what lets pointer-events fall through to it below.
      expect(wrap.contains(btn)).toBe(true);
    });

    it('draws the tooltip in CSS on hover', () => {
      const dom = new JSDOM(`<!doctype html><head><style>${rcCss}</style></head><body>`
        + '<span class="rc-soon" data-tip="hi"><button class="btn" id="inner"></button></span></body></html>');
      const w = dom.window, doc = w.document;
      // Pointer-events genuinely fall through the inert button to the
      // wrapper, otherwise the wrapper never enters :hover.
      expect(w.getComputedStyle(doc.getElementById('inner')).pointerEvents).toBe('none');

      const rules = [...doc.styleSheets[0].cssRules];
      const afterRule = rules.find(r => r.selectorText === '.rc-soon::after');
      expect(afterRule, '.rc-soon::after rule not found').toBeTruthy();
      expect(afterRule.style.getPropertyValue('opacity')).toBe('0');
      const hoverRule = rules.find(r => r.selectorText
        && r.selectorText.split(',').map(s => s.trim()).includes('.rc-soon:hover::after'));
      expect(hoverRule, '.rc-soon:hover::after rule not found').toBeTruthy();
      expect(hoverRule.style.getPropertyValue('opacity')).toBe('1');

      // wiring (unexecutable): jsdom's CSSOM drops `content: attr(...)`.
      expect(rcCss).toMatch(/\.rc-soon::after\s*\{[\s\S]*?content:\s*attr\(data-tip\)/);
    });

    it('a click never reaches a handler, even with the disabled guard lifted', () => {
      const win = mountPanelWithReportCard();
      const btn = win.document.getElementById('rcSubmitBtn');
      btn.disabled = false; // simulate the guard being lifted by a future edit
      let opened = false;
      win.open = () => { opened = true; };
      win.fetch = () => { throw new Error('should not fetch on submit click'); };
      btn.click();
      expect(opened).toBe(false);
    });

    // wiring (unexecutable): proves no handler is assigned anywhere in the module source.
    it('does not assign an onclick handler anywhere in the module source', () => {
      expect(rcJs).not.toMatch(/rcSubmitBtn'\)[\s\S]{0,200}onclick/);
      expect(rcJs).not.toContain('window.open(url');
    });

    it('still gates visibility on card eligibility via the wrapper', () => {
      const win = mountPanelWithReportCard();
      win.RC = {
        buildCard: () => win.document.createDocumentFragment(),
        submitUrl: (card) => card.eligible ? 'https://example/submit' : '',
      };
      win.rcRenderCard({ result: {}, provider: 'llama', ts: 1, mode: 'standard', eligible: true });
      expect(win.document.getElementById('rcSubmitWrap').style.display).toBe('');
      win.rcRenderCard({ result: {}, provider: 'llama', ts: 1, mode: 'custom', eligible: false });
      expect(win.document.getElementById('rcSubmitWrap').style.display).toBe('none');
    });
  });

  // The id-scoped hover restyles outspecify .btn:disabled, so without an
  // explicit exclusion a disabled button brightens to opacity 1 on hover.
  describe('disabled buttons keep their greyed state on hover (#509)', () => {
    it.each([
      '#llm-llamacpp', '#llm-lmstudio', '#llm-tools', '#dash-energy', '#llm-vllm',
    ])('%s hover restyle excludes :disabled', (tab) => {
      const found = findAlt(baseRules, [tab, '.btn', ':not(:disabled)', ':hover']);
      expect(found, `${tab} hover-exclusion rule not found`).toBeTruthy();
      // jsdom never simulates :hover, so drop it and check the rest of the
      // real selector — including :not(:disabled) — with real elements.
      const stripped = found.alt.replace(':hover', '');
      const doc = new JSDOM(`<!doctype html><body><div id="${tab.slice(1)}">`
        + '<button class="btn" id="on"></button><button class="btn" id="off" disabled></button>'
        + '</div></body></html>').window.document;
      expect(doc.getElementById('on').matches(stripped)).toBe(true);
      expect(doc.getElementById('off').matches(stripped)).toBe(false);
    });

    it('the generic .btn:hover opacity bump excludes :disabled', () => {
      const rule = baseRules.find(r => r.selectorText
        && r.selectorText.split(',').map(s => s.trim()).includes('.btn:not(:disabled):hover'));
      expect(rule, '.btn:not(:disabled):hover rule not found').toBeTruthy();
      expect(rule.style.getPropertyValue('opacity')).toBe('0.8');
      const bare = baseRules.some(r => r.selectorText
        && r.selectorText.split(',').map(s => s.trim()).includes('.btn:hover'));
      expect(bare).toBe(false);
      const doc = new JSDOM('<!doctype html><body><button class="btn" id="on"></button>'
        + '<button class="btn" id="off" disabled></button></body></html>').window.document;
      expect(doc.getElementById('on').matches('.btn:not(:disabled)')).toBe(true);
      expect(doc.getElementById('off').matches('.btn:not(:disabled)')).toBe(false);
    });
  });

  it('wires the cancel and download-confirm controls to their handlers', () => {
    const win = mountPanel();
    const doc = win.document;
    const cancelBtn = doc.getElementById('rcCancelBtn');
    expect(cancelBtn).toBeTruthy();
    let cancelCalls = 0;
    win.rcCancelRun = () => { cancelCalls++; };
    cancelBtn.click();
    expect(cancelCalls).toBe(1);

    expect(doc.getElementById('rcDownload')).toBeTruthy();
    const downloadBtn = doc.querySelector('#rcDownload button');
    expect(downloadBtn).toBeTruthy();
    let proceedCalls = 0;
    win.rcDownloadProceed = () => { proceedCalls++; };
    downloadBtn.click();
    expect(proceedCalls).toBe(1);
  });

  it('has a live status line for run progress', () => {
    expect(mountPanel().document.getElementById('rcStatus')).toBeTruthy();
  });

  it('loads its scripts and stylesheet', () => {
    const hasScript = (frag) => [...fullDoc.scripts].some(s => (s.getAttribute('src') || '').includes(frag));
    expect(hasScript('js/lib/reportcard.js')).toBe(true);
    expect(hasScript('js/report-card.js')).toBe(true);
    expect(fullDoc.querySelector('link[rel="stylesheet"][href*="css/reportcard.css"]')).toBeTruthy();
  });
});
