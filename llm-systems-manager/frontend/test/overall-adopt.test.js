// #565: pinned-card DOM adoption — the real card node moves into its Overall
// shell on tab entry and back to its exact home slot on exit. Runs the actual
// foundation.js function sources in jsdom with stubbed collaborators.
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundation = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');
const charts = readFileSync(join(here, '..', 'js', 'charts.js'), 'utf8');

function fnSrc(name, src = foundation) {
  const m = src.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found`).toBeTruthy();
  return m[0];
}

function loadAdoption() {
  // Evaluate the real sources at global scope so bare identifiers resolve
  // to the window stubs, matching classic-script behavior.
  const src = [
    'window._ovAdopted = new Set(); window._ovHomeMarks = {};',
    fnSrc('_homeCardEl'), fnSrc('_returnOneAdopted'),
    fnSrc('adoptPinnedCards'), fnSrc('returnPinnedCards'),
    fnSrc('_ovPinned'), fnSrc('_applyBandOrder'),
    'window._homeCardEl = _homeCardEl; window._returnOneAdopted = _returnOneAdopted;',
    'window.adoptPinnedCards = adoptPinnedCards; window.returnPinnedCards = returnPinnedCards;',
    'window._ovPinned = _ovPinned; window._applyBandOrder = _applyBandOrder;',
  ].join('\n');
  (0, eval)(src);
}

beforeEach(() => {
  document.body.innerHTML = `
    <div id="dashboardTab">
      <div class="grid" id="cardGrid">
        <div class="card" data-card="gpu"><h3>GPU</h3><div class="chart-wrap"><canvas id="gpuChart"></canvas></div></div>
        <div class="card" data-card="cpu-overall"><h3>CPU</h3></div>
      </div>
      <div class="grid" id="managerCardGrid">
        <div class="card" data-card="mgr-agents"><h3>Agents</h3></div>
      </div>
    </div>
    <div id="overallTab">
      <div class="ov-band">
        <section data-strip="hero"></section>
        <section data-strip="tiles"></section>
        <section data-strip="agents"></section>
        <section data-strip="alerts"></section>
      </div>
      <div class="grid" id="overallGrid">
        <div class="card ov-shell" data-card="ov-borrow-gpu"></div>
        <div class="card ov-shell" data-card="ov-borrow-mgr-agents"></div>
        <div class="card ov-shell" data-card="ov-borrow-lms-power"></div>
      </div>
    </div>`;
  window.layout = { overallBorrowed: ['gpu', 'mgr-agents', 'lms-power'] };
  window._ensureSizeBtn = () => {};
  window._resizeChartsIn = () => {};
  window._applyHiddenForGrid = () => {};
  loadAdoption();
});

describe('adoptPinnedCards', () => {
  it('moves the real node into its shell', () => {
    adoptPinnedCards();
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-gpu"]');
    expect(shell.querySelector('[data-card="gpu"]')).toBeTruthy();
    expect(document.querySelector('#cardGrid [data-card="gpu"]')).toBeNull();
    // Canvas travels with the card.
    expect(shell.querySelector('#gpuChart')).toBeTruthy();
  });

  it('renders the ov-missing note when the home card does not exist', () => {
    adoptPinnedCards();
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-lms-power"]');
    expect(shell.querySelector('.ov-missing')).toBeTruthy();
    expect(window._ovAdopted.has('lms-power')).toBe(false);
  });

  it('is idempotent — double adopt keeps one card and one home marker', () => {
    adoptPinnedCards();
    adoptPinnedCards();
    expect(document.querySelectorAll('[data-card="gpu"]').length).toBe(1);
    const marks = [...document.getElementById('cardGrid').childNodes]
      .filter(n => n.nodeType === 8 && n.textContent.includes('ov-home:gpu'));
    expect(marks.length).toBe(1);
  });
});

describe('returnPinnedCards', () => {
  it('restores the exact home position (before its old next sibling)', () => {
    adoptPinnedCards();
    returnPinnedCards();
    const grid = document.getElementById('cardGrid');
    const cards = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
    expect(cards).toEqual(['gpu', 'cpu-overall']);
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-gpu"] [data-card="gpu"]')).toBeNull();
    // No leftover comment markers.
    const marks = [...grid.childNodes].filter(n => n.nodeType === 8);
    expect(marks.length).toBe(0);
  });

  it('adopt → return → adopt round-trips cleanly', () => {
    adoptPinnedCards();
    returnPinnedCards();
    adoptPinnedCards();
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-gpu"] [data-card="gpu"]')).toBeTruthy();
    returnPinnedCards();
    expect(document.querySelector('#cardGrid [data-card="gpu"]')).toBeTruthy();
  });

  it('double return is a no-op', () => {
    adoptPinnedCards();
    returnPinnedCards();
    returnPinnedCards();
    expect(document.querySelectorAll('[data-card="gpu"]').length).toBe(1);
    expect(window._ovAdopted.size).toBe(0);
  });
});

describe('_returnOneAdopted', () => {
  it('returns a single card home so its shell can be removed', () => {
    adoptPinnedCards();
    _returnOneAdopted('mgr-agents');
    expect(document.querySelector('#managerCardGrid [data-card="mgr-agents"]')).toBeTruthy();
    expect(window._ovAdopted.has('mgr-agents')).toBe(false);
    // The other adopted card is untouched.
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-gpu"] [data-card="gpu"]')).toBeTruthy();
  });
});

describe('_ovPinned', () => {
  it('reflects layout.overallBorrowed', () => {
    expect(_ovPinned('mgr-agents')).toBe(true);
    expect(_ovPinned('nope')).toBe(false);
    window.layout = null;
    expect(_ovPinned('mgr-agents')).toBe(false);
  });
});

describe('adoption visibility (hidden-at-home cards)', () => {
  it('clears a stale home display:none so the pinned card shows on Overall', () => {
    const gpu = document.querySelector('#cardGrid [data-card="gpu"]');
    gpu.style.display = 'none';
    adoptPinnedCards();
    const adopted = document.querySelector('#overallGrid [data-card="ov-borrow-gpu"] [data-card="gpu"]');
    expect(adopted).toBeTruthy();
    expect(adopted.style.display).toBe('');
  });

  it('re-applies the home grid hidden state on return', () => {
    const gpu = document.querySelector('#cardGrid [data-card="gpu"]');
    gpu.style.display = 'none';
    const calls = [];
    window._applyHiddenForGrid = (gridId, key) => calls.push([gridId, key]);
    adoptPinnedCards();
    returnPinnedCards();
    expect(calls).toContainEqual(['cardGrid', 'hidden']);
    expect(calls).toContainEqual(['managerCardGrid', 'managerHidden']);
  });
});

describe('_ensureSizeBtn on shells', () => {
  it('gives the shell its own direct-child button even when the adopted card has one', () => {
    (0, eval)(fnSrc('_ensureSizeBtn', charts) + '\nwindow._ensureSizeBtn = _ensureSizeBtn;');
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-gpu"]');
    const inner = document.createElement('div');
    inner.className = 'card';
    inner.dataset.card = 'gpu';
    const innerBtn = document.createElement('button');
    innerBtn.className = 'card-size-btn';
    inner.appendChild(innerBtn);
    shell.appendChild(inner);
    window._ensureSizeBtn(shell);
    const direct = [...shell.children].filter(k => k.classList && k.classList.contains('card-size-btn'));
    expect(direct.length).toBe(1);
  });
});

describe('_applyBandOrder', () => {
  const order = () => [...document.querySelector('.ov-band').children].map(s => s.dataset.strip);

  it('reorders strips per layout.overallBandOrder', () => {
    window.layout.overallBandOrder = ['alerts', 'agents', 'tiles', 'hero'];
    _applyBandOrder();
    expect(order()).toEqual(['alerts', 'agents', 'tiles', 'hero']);
  });

  it('tolerates unknown ids and appends missing strips in current order', () => {
    window.layout.overallBandOrder = ['agents', 'bogus', 'hero'];
    _applyBandOrder();
    expect(order()).toEqual(['agents', 'hero', 'tiles', 'alerts']);
  });

  it('no-ops without a saved order or without the band', () => {
    delete window.layout.overallBandOrder;
    _applyBandOrder();
    expect(order()).toEqual(['hero', 'tiles', 'agents', 'alerts']);
    document.querySelector('.ov-band').remove();
    window.layout.overallBandOrder = ['hero'];
    expect(() => _applyBandOrder()).not.toThrow();
  });
});
