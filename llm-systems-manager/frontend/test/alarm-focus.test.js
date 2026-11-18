// #962: focusAlarmAlert deep-links one alert into the embedded alarm console.
import { describe, test, expect, beforeEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const src = srcFile('js/foundation.js');
const fn = fnSrc(src, 'focusAlarmAlert');

beforeEach(() => {
  expect(fn, 'focusAlarmAlert not found').toBeTruthy();
  evalGlobal(fn + '\nwindow.focusAlarmAlert = focusAlarmAlert;');
  document.documentElement.dataset.theme = 'slate';
  document.body.innerHTML = '<div id="eventsTab"><iframe id="alarmEngineIframe" data-src="/alarm/"></iframe></div>';
  window.__tabs = [];
  window.switchTab = t => window.__tabs.push(t);
  window._ensureAlarmIframeLoaded = () => {};
  vi.useFakeTimers();
});

describe('focusAlarmAlert', () => {
  test('a console that has not loaded boots straight to the alert via ?alert= and no message is posted', () => {
    const iframe = document.getElementById('alarmEngineIframe');
    const post = vi.fn();
    Object.defineProperty(iframe, 'contentWindow', { value: { postMessage: post }, configurable: true });
    focusAlarmAlert('al-42');
    expect(iframe.getAttribute('src')).toBe('/alarm/?theme=slate&alert=al-42');
    iframe.removeAttribute('src');
    document.documentElement.dataset.theme = '"><script>';
    focusAlarmAlert('a&b');
    expect(iframe.getAttribute('src')).toBe('/alarm/?alert=a%26b');
    expect(window.__tabs).toEqual(['events', 'events']);
    expect(post).not.toHaveBeenCalled();
  });
  test('a booted console gets an open_alert message now and again on its next load', () => {
    const iframe = document.getElementById('alarmEngineIframe');
    iframe.setAttribute('src', '/alarm/?theme=slate');
    const post = vi.fn();
    Object.defineProperty(iframe, 'contentWindow', { value: { postMessage: post }, configurable: true });
    focusAlarmAlert(7);
    expect(iframe.getAttribute('src')).toBe('/alarm/?theme=slate');
    expect(post).toHaveBeenCalledWith({ type: 'open_alert', id: '7' }, 'http://localhost:3000');
    iframe.dispatchEvent(new Event('load'));
    expect(post).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(16000);
    iframe.dispatchEvent(new Event('load'));
    expect(post).toHaveBeenCalledTimes(2);
  });
  test('an empty id does nothing', () => {
    focusAlarmAlert('');
    focusAlarmAlert(null);
    expect(window.__tabs).toEqual([]);
  });
});

describe('focusAlarmMetric', () => {
  const mfn = fnSrc(src, 'focusAlarmMetric');
  beforeEach(() => {
    expect(mfn, 'focusAlarmMetric not found').toBeTruthy();
    evalGlobal(mfn + '\nwindow.focusAlarmMetric = focusAlarmMetric;');
  });
  test('a console that has not loaded boots straight to Metrics via ?metric= with the host and metric', () => {
    const iframe = document.getElementById('alarmEngineIframe');
    const post = vi.fn();
    Object.defineProperty(iframe, 'contentWindow', { value: { postMessage: post }, configurable: true });
    focusAlarmMetric('llm-a', 'system', 'cpu_total');
    expect(iframe.getAttribute('src')).toBe('/alarm/?theme=slate&metric=llm-a%7Csystem%2Fcpu_total');
    expect(window.__tabs).toEqual(['events']);
    expect(post).not.toHaveBeenCalled();
  });
  test('a booted console gets an open_metric message now and again on its next load', () => {
    const iframe = document.getElementById('alarmEngineIframe');
    iframe.setAttribute('src', '/alarm/?theme=slate');
    const post = vi.fn();
    Object.defineProperty(iframe, 'contentWindow', { value: { postMessage: post }, configurable: true });
    focusAlarmMetric('llm-a', 'llama', 'tokens_per_second');
    expect(post).toHaveBeenCalledWith({ type: 'open_metric', host: 'llm-a', source: 'llama', name: 'tokens_per_second' }, 'http://localhost:3000');
    iframe.dispatchEvent(new Event('load'));
    expect(post).toHaveBeenCalledTimes(2);
  });
  test('a missing source or name does nothing', () => {
    focusAlarmMetric('llm-a', '', 'x');
    focusAlarmMetric('llm-a', 'system', '');
    expect(window.__tabs).toEqual([]);
  });
});

describe('alarm console open_metric', () => {
  const AE_MAIN = srcFile('../../llm-systems-alarm-engine/frontend/js/main.js');
  test('the console opens Metrics for an open_metric message from its own origin only', () => {
    const start = AE_MAIN.indexOf("window.addEventListener('message'");
    const end = AE_MAIN.indexOf('\n});', start) + 4;
    expect(start).toBeGreaterThan(-1);
    const shown = [];
    window.MetricsView = { openLive: (...a) => shown.push(a) };
    window.AlertsView = { openFor: () => {} };
    window.LEGACY_THEMES = {}; window.THEMES = new Set();
    const before = window.addEventListener;
    let handler = null;
    window.addEventListener = (t, h) => { if (t === 'message') handler = h; };
    try { evalGlobal(AE_MAIN.slice(start, end)); } finally { window.addEventListener = before; }
    handler({ origin: window.location.origin, data: { type: 'open_metric', host: 'llm-a', source: 'system', name: 'ram_percent' } });
    handler({ origin: 'http://evil.example', data: { type: 'open_metric', host: 'x', source: 'system', name: 'cpu_total' } });
    handler({ origin: window.location.origin, data: { type: 'open_metric', host: 'x', source: '', name: 'cpu_total' } });
    expect(shown).toEqual([['llm-a', 'system', 'ram_percent']]);
  });
  const AE_METRICS = srcFile('../../llm-systems-alarm-engine/frontend/js/metrics.js');
  const method = (name) => {
    const m = new RegExp('\\n    ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n    \\},').exec(AE_METRICS);
    expect(m, name).toBeTruthy();
    return m[0].trim().replace(/,$/, '');
  };
  test('the console boots to the live window of the metric in ?metric=', () => {
    const start = AE_MAIN.indexOf('    const q = new URLSearchParams(window.location.search);');
    const end = AE_MAIN.indexOf("else TabManager.switchTab(m ? m[1] : 'console', false);", start);
    expect(start).toBeGreaterThan(-1);
    const boot = AE_MAIN.slice(start, end) + 'else tabs.push(1);';
    const run = (search) => {
      window.history.replaceState(null, '', '/alarm/' + search);
      const opened = [], tabs = [];
      const MetricsView = eval('({' + method('parseKey') + ', openLive: (...a) => opened.push(a) })');
      new Function('MetricsView', 'AlertsView', 'tabs', boot)(MetricsView, { openFor: () => {} }, tabs);
      return { opened, tabs };
    };
    expect(run('?metric=llm-a%7Csystem%2Fcpu_total').opened).toEqual([['llm-a', 'system', 'cpu_total']]);
    expect(run('?metric=*%7Cllama%2Ftokens_per_second').opened).toEqual([['', 'llama', 'tokens_per_second']]);
    expect(run('?metric=junk').tabs).toEqual([1]);
    window.history.replaceState(null, '', '/');
  });
  test('openLive shows the current window, clears an alert mark and asks for a note when the metric is missing', () => {
    const shown = [];
    window.AppState = { filters: { metrics: { offset: 3, mark: { key: 'x' }, want: null } } };
    const MV = eval('({' + method('openLive') + ', _liveTag() {}, showFor: (...a) => shown.push(a) })');
    MV.openLive('llm-a', 'system', 'ram_percent');
    expect(window.AppState.filters.metrics).toEqual({ offset: 0, mark: null, want: 'llm-a|system/ram_percent' });
    expect(shown).toEqual([['llm-a', 'system', 'ram_percent']]);
    delete window.AppState;
  });
});
