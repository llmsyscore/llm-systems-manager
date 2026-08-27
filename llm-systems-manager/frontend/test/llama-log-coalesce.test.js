// #672: llama log SSE lines are coalesced into one DOM write per frame and
// the box is capped at _LOG_BOX_MAX_LINES.
import { describe, it, expect, beforeEach } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const src = srcFile('js/terminal.js');

function stateSrc(name) {
  const m = src.match(new RegExp(`^(?:const|let) ${name} = [^;]*;`, 'm'));
  expect(m, `${name} not found`).toBeTruthy();
  return m[0].replace(/^(const|let) /, 'window.');
}

let frames;
beforeEach(() => {
  document.body.innerHTML = '<div id="box"></div>';
  frames = [];
  window.requestAnimationFrame = cb => { frames.push(cb); return frames.length; };
  evalGlobal([
    stateSrc('_LOG_BOX_MAX_LINES'), stateSrc('_logPending'), stateSrc('_logFlushReq'),
    fnSrc(src, '_trimLogLines'), fnSrc(src, '_logFlush'), fnSrc(src, '_logAppend'),
    'window._trimLogLines = _trimLogLines; window._logFlush = _logFlush; window._logAppend = _logAppend;',
  ].join('\n'));
});

describe('llama log coalescing (#672)', () => {
  it('queues lines and writes them in one flush per frame', () => {
    const box = document.getElementById('box');
    box.textContent = 'old\n';
    for (let i = 0; i < 500; i++) window._logAppend(box, 'line' + i);
    expect(frames.length).toBe(1);
    expect(box.textContent).toBe('old\n');
    frames[0]();
    expect(box.textContent.startsWith('old\nline0\nline1\n')).toBe(true);
    expect(box.textContent.endsWith('line499\n')).toBe(true);
    // Next line schedules a new frame.
    window._logAppend(box, 'more');
    expect(frames.length).toBe(2);
  });

  it('caps the box at _LOG_BOX_MAX_LINES, keeping the newest', () => {
    const box = document.getElementById('box');
    const max = window._LOG_BOX_MAX_LINES;
    for (let i = 0; i < max + 50; i++) window._logAppend(box, 'l' + i);
    frames[0]();
    const lines = box.textContent.split('\n').filter(Boolean);
    expect(lines.length).toBe(max);
    expect(lines[0]).toBe('l50');
    expect(lines[lines.length - 1]).toBe('l' + (max + 49));
  });

  it('_trimLogLines returns short text unchanged', () => {
    expect(window._trimLogLines('a\nb\n', 5)).toBe('a\nb\n');
    expect(window._trimLogLines('a\nb\nc\n', 2)).toBe('b\nc\n');
  });
});
