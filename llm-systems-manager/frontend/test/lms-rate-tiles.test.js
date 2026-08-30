// #736: LMS Server card Gen/Prompt tokens/s tiles — live while active, the
// 60-min active-sample mean otherwise, the 60-min peak always. Real source in jsdom.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fnSrc, srcFile, blockSrc } from './helpers/harness.js';
import LMPeaks from '../js/lib/peaks.js';

const src = srcFile('js/lmstudio.js');
const fn = (name) => {
  const m = fnSrc(src, name);
  expect(m, `${name} not found`).toBeTruthy();
  return m;
};
const T0 = Date.UTC(2026, 7, 29, 20, 0, 0);
const MIN = 60000;
const $ = (id) => document.getElementById(id);

beforeEach(() => {
  document.body.innerHTML = '<div class="val" id="lms-pps">—</div><div class="lbl" id="lms-pps-lbl">Prompt tokens/s</div>'
    + '<div class="val" id="lms-tps">—</div><div class="lbl" id="lms-tps-lbl">Gen tokens/s</div>';
  window.LMPeaks = LMPeaks;
  window._peakSpan = (v, t) => `(peak ${v} @${t})`;
  window._setLivePeak = (id, html) => { $(id).innerHTML = html; };
  const consts = blockSrc(src, 'const _LMS_TILE_WINDOW_MS', 'let _lmsTilesLastTs = null;')
    .replace(/^(const|let) /gm, 'window.');
  (0, eval)([consts, fn('_setEl'), fn('_lmsTilesReset'), fn('_lmsSeedTileRow'), fn('fmtLmsRateTile'),
             fn('_renderLmsRateTiles'), 'window._render = _renderLmsRateTiles; window._seed = _lmsSeedTileRow;',
             'window._reset = _lmsTilesReset;'].join('\n'));
  window._reset();
});

afterEach(() => vi.restoreAllMocks());

const gr = (ts, gen, prompt) => ({ ts: new Date(ts).toISOString(), gen_tps: gen, prompt_tps: prompt });

describe('LMS rate tiles (#736)', () => {
  it('shows the live rate with its label while traffic flows', () => {
    window._render(gr(T0, 42.5, 900), T0);
    expect($('lms-tps').innerHTML).toBe(`42.5 (peak 42.5 @${T0})`);
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · live');
    expect($('lms-pps-lbl').textContent).toBe('Prompt tokens/s · live');
  });
  it('switches to the 60-min average of active samples when the rate drops to 0', () => {
    window._render(gr(T0, 10, 100), T0);
    window._render(gr(T0 + 15000, 30, 300), T0 + 15000);
    window._render(gr(T0 + 30000, 0, 0), T0 + 30000);
    expect($('lms-tps').innerHTML).toBe(`20.0 (peak 30.0 @${T0 + 15000})`);
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · 60\u2011min avg');
    expect($('lms-pps').innerHTML).toBe(`200.0 (peak 300.0 @${T0 + 15000})`);
  });
  it('tiles are independent: gen idle while prompt is live', () => {
    window._render(gr(T0, 0, 500), T0);
    expect($('lms-tps').innerHTML).toBe('—');
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · 60\u2011min avg');
    expect($('lms-pps-lbl').textContent).toBe('Prompt tokens/s · live');
  });
  it('re-polls of the same gateway sample enter the trackers once', () => {
    window._render(gr(T0, 10, 10), T0);
    window._render(gr(T0, 10, 10), T0 + 5000);
    window._render(gr(T0 + 15000, 30, 30), T0 + 15000);
    window._render(gr(T0 + 30000, 0, 0), T0 + 30000);
    expect($('lms-tps').innerHTML.startsWith('20.0 ')).toBe(true);   // not (10+10+30)/3
  });
  it('samples age out after 60 minutes; no data at all reads —', () => {
    window._render(gr(T0, 50, 50), T0);
    window._render(gr(T0 + 61 * MIN, 0, 0), T0 + 61 * MIN);
    expect($('lms-tps').innerHTML).toBe('—');
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · 60\u2011min avg');
    window._render(null, T0 + 62 * MIN);
    expect($('lms-pps').innerHTML).toBe('—');
  });
  it('no gateway sample at all falls back to the window average', () => {
    window._render(gr(T0, 40, 400), T0);
    window._render(null, T0 + 15000);
    expect($('lms-tps').innerHTML).toBe(`40.0 (peak 40.0 @${T0})`);
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · 60\u2011min avg');
  });
  it('an offline agent is never live: its last rates read as the window average', () => {
    window._render(gr(T0, 40, 400), T0);
    window._render(gr(T0 + 15000, 40, 400), T0 + 15000, false);
    expect($('lms-tps').innerHTML).toBe(`40.0 (peak 40.0 @${T0})`);   // sample not re-counted
    expect($('lms-tps-lbl').textContent).toBe('Gen tokens/s · 60\u2011min avg');
  });
  it('history rows seed the window through the row clock and reset clears it', () => {
    vi.spyOn(Date, 'now').mockReturnValue(T0);
    const clock = (ts) => new Date(ts).getTime() + 1000;
    window._seed({ ts: new Date(T0 - 90 * MIN).toISOString(), lms_tps: 999, lms_pps: 999 }, clock);  // outside the window
    window._seed({ ts: new Date(T0 - 10 * MIN).toISOString(), lms_tps: 80, lms_pps: 0 }, clock);
    window._seed({ ts: new Date(T0 - 5 * MIN).toISOString(), lms_tps: 0, lms_pps: 640 }, clock);
    window._render(gr(T0, 0, 0), T0);
    expect($('lms-tps').innerHTML).toBe(`80.0 (peak 80.0 @${T0 - 10 * MIN + 1000})`);
    expect($('lms-pps').innerHTML).toBe(`640.0 (peak 640.0 @${T0 - 5 * MIN + 1000})`);
    window._reset();
    window._render(gr(T0 + 15000, 0, 0), T0 + 15000);
    expect($('lms-tps').innerHTML).toBe('—');
  });
});
