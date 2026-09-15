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
