// #1303: the Llama server card's prompt-cache reuse and draft-acceptance tiles
// show a state word (Not reported / Idle / Reset / Paused) instead of a fake 0%.
import { describe, test, expect, beforeEach } from 'vitest';
import { srcFile, blockSrc, evalGlobal } from './helpers/harness.js';

const charts = srcFile('js/charts.js');
const indexHtml = srcFile('index.html');

evalGlobal(blockSrc(charts, 'const _LLAMA_WINDOW_STATE_TEXT = {', '// Mirrors backend clean_display_model()', { includeEnd: false }));
const fmt = (block) => window._fmtLlamaWindowStat(block, 'Prompt cache reuse', 'cached', 'processed');

describe('_fmtLlamaWindowStat', () => {
  test('ok ratio renders as a percent with the window in the label', () => {
    const t = fmt({ state: 'ok', processed: 10, cached: 8200, ratio: 0.9988 });
    expect(t.text).toBe('99.9%');
    expect(t.label).toBe('Prompt cache reuse · 60 s');
    expect(t.title).toContain('cached 8,200');
    expect(t.title).toContain('processed 10');
  });

  test('speculative decoding not configured reads "Not in use"', () => {
    const t = window._fmtLlamaWindowStat({ state: 'off', drafted: 0, accepted: 0, drafts: 0, ratio: null, accepted_per_draft: null },
      'Draft tokens accepted', 'accepted', 'drafted');
    expect(t).toEqual({ text: 'Not in use', label: 'Draft tokens accepted', title: '' });
  });

  test('unsupported build says so and never shows 0%', () => {
    const t = fmt({ state: 'unsupported', processed: null, cached: null, ratio: null });
    expect(t.text).toBe('Not reported');
    expect(t.label).toBe('Prompt cache reuse');
    expect(t.title).toBe('');
  });

  test('idle, reset and paused windows are labelled by state', () => {
    expect(fmt({ state: 'idle', processed: 0, cached: 0, ratio: null }).text).toBe('Idle');
    expect(fmt({ state: 'reset', processed: 0, cached: 0, ratio: null }).text).toBe('Reset');
    expect(fmt({ state: 'reset', processed: 30, cached: 30, ratio: 0.5 }).text).toBe('50.0% · Reset');
    expect(fmt({ state: 'paused', processed: 0, cached: 0, ratio: null }).text).toBe('Paused');
  });

  test('missing block (sleeping / old agent) renders a dash', () => {
    expect(fmt(null)).toEqual({ text: '—', label: 'Prompt cache reuse', title: '' });
    expect(fmt(undefined).text).toBe('—');
  });
});

describe('_setLlamaWindowTile', () => {
  beforeEach(() => {
    document.body.innerHTML = blockSrc(indexHtml, '<div class="llama-grid">', '</div>\n    <div class="legend-row"', { includeEnd: false });
  });

  test('card markup has both tiles and the writer fills value, label and title', () => {
    expect(document.getElementById('llamaCacheReuse')).not.toBeNull();
    expect(document.getElementById('llamaSpecAccept')).not.toBeNull();
    window._setLlamaWindowTile('llamaSpecAccept', window._fmtLlamaWindowStat(
      { state: 'ok', drafted: 93, accepted: 68, drafts: 30, ratio: 0.7312, accepted_per_draft: 2.27 },
      'Draft tokens accepted', 'accepted', 'drafted'));
    expect(document.getElementById('llamaSpecAccept').textContent).toBe('73.1%');
    expect(document.getElementById('llamaSpecAccept-lbl').textContent).toBe('Draft tokens accepted · 60 s');
    expect(document.getElementById('llamaSpecAccept').title).toContain('accepted 68 · drafted 93');
  });
});
