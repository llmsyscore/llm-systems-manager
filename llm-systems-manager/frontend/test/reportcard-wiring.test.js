// #468: the Report Card sub-tab must match the llama/LMS/vLLM panel UX —
// same section chrome, same muted button palette, same restyle coverage.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(resolve(ROOT, 'index.html'), 'utf8');
const css = readFileSync(resolve(ROOT, 'css/base.css'), 'utf8');

const start = html.indexOf('id="llm-reportcard"');
const after = html.indexOf('id="llm-', start + 1);
const panel = html.slice(start, after === -1 ? html.indexOf('end llmTab') : after);

describe('report card panel', () => {
  it('exists as a sub-tab panel with the sibling padding', () => {
    expect(start).toBeGreaterThan(-1);
    expect(panel).toContain('class="sub-tab-panel"');
    expect(panel).toContain('padding:8px 20px 20px;');
  });

  it('uses the canonical collapsible section chrome', () => {
    expect(panel).toContain('llm-section-title');
    expect(panel).toContain('llm-collapse-icon');
    expect(panel).toContain("toggleSection('rcSection')");
  });

  it('uses the muted button palette, not bright green/red/amber', () => {
    expect(panel).not.toContain('btn-green-muted-gradient');
    expect(panel).not.toContain('btn-red-muted-gradient');
    expect(panel).not.toContain('btn-amber-muted-gradient');
  });

  it('every button carries the shared .btn class', () => {
    const buttons = panel.match(/<button[^>]*>/g) || [];
    expect(buttons.length).toBeGreaterThan(4);
    buttons.forEach(b => expect(b).toMatch(/class="btn /));
  });

  // Without #llm-reportcard in this rule the buttons keep their raw gradient
  // and visibly differ from every sibling sub-tab.
  it('is covered by the per-sub-tab .btn restyle', () => {
    const rule = css.slice(css.indexOf('#llm-llamacpp .btn:not([data-act])'));
    const firstBlock = rule.slice(0, rule.indexOf('}'));
    expect(firstBlock).toContain('#llm-reportcard .btn');
  });

  // A reportcard selector without :hover inside the hover block applies the
  // hover colors unconditionally and silently beats the base restyle.
  it('appears in the hover block only with :hover attached', () => {
    const blocks = css.split('}');
    blocks.forEach(block => {
      block.split(',').forEach(sel => {
        if (!sel.includes('#llm-reportcard .btn')) return;
        const siblings = block.split(',')
          .filter(s => s.includes('#llm-llamacpp .btn'));
        if (!siblings.length) return;
        // Whatever the llama.cpp selector does in this block, ours must match.
        const sibHover = siblings.some(s => s.includes(':hover'));
        expect(sel.includes(':hover')).toBe(sibHover);
      });
    });
  });

  // #509: leaderboard submission is inert until the service ships.
  describe('leaderboard submit button (#509)', () => {
    const js = readFileSync(resolve(ROOT, 'js/report-card.js'), 'utf8');
    const btn = panel.match(/<button[^>]*id="rcSubmitBtn"[^>]*>/)[0];

    it('carries the disabled attribute so it cannot be clicked', () => {
      expect(btn).toMatch(/\sdisabled\b/);
      expect(btn).toContain('aria-disabled="true"');
    });

    // Title on both: browsers differ on whether a disabled control shows its
    // own title or inherits the ancestor's, so carry it in both places.
    it('carries the coming-soon tooltip on the button and its wrapper', () => {
      const wrap = panel.match(/<span[^>]*id="rcSubmitWrap"[^>]*>/)[0];
      expect(wrap).toMatch(/title="[^"]*coming soon[^"]*"/i);
      expect(btn).toMatch(/title="[^"]*coming soon[^"]*"/i);
      expect(panel.indexOf('id="rcSubmitWrap"')).toBeLessThan(panel.indexOf('id="rcSubmitBtn"'));
    });

    it('no longer wires a click handler that opens the submit URL', () => {
      expect(js).not.toMatch(/rcSubmitBtn'\)[\s\S]{0,200}onclick/);
      expect(js).not.toContain('window.open(url');
    });

    it('still gates visibility on card eligibility via the wrapper', () => {
      expect(js).toContain('rcSubmitWrap');
      expect(js).toContain('RC.submitUrl(card)');
    });
  });

  it('exposes cancel and download-confirm controls', () => {
    expect(panel).toContain('id="rcCancelBtn"');
    expect(panel).toContain('rcCancelRun()');
    expect(panel).toContain('id="rcDownload"');
    expect(panel).toContain('rcDownloadProceed()');
  });

  it('has a live status line for run progress', () => {
    expect(panel).toContain('id="rcStatus"');
  });

  it('loads its scripts and stylesheet', () => {
    expect(html).toContain('js/lib/reportcard.js');
    expect(html).toContain('js/report-card.js');
    expect(html).toContain('css/reportcard.css');
  });
});
