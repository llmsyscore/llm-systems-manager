// #565: pinned-card DOM adoption — the real card node moves into its Overall
// shell on tab entry and back to its exact home slot on exit. Runs the actual
// foundation.js function sources in jsdom with stubbed collaborators.
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundation = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');

function fnSrc(name) {
  const m = foundation.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found in foundation.js`).toBeTruthy();
  return m[0];
}

function loadAdoption() {
  // Evaluate the real sources at global scope so bare identifiers resolve
  // to the window stubs, matching classic-script behavior.
  const src = [
    'window._ovAdopted = new Set(); window._ovHomeMarks = {};',
    fnSrc('_homeCardEl'), fnSrc('_returnOneAdopted'),
    fnSrc('adoptPinnedCards'), fnSrc('returnPinnedCards'),
    fnSrc('_ovPinned'),
    'window._homeCardEl = _homeCardEl; window._returnOneAdopted = _returnOneAdopted;',
    'window.adoptPinnedCards = adoptPinnedCards; window.returnPinnedCards = returnPinnedCards;',
    'window._ovPinned = _ovPinned;',
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
      <div class="grid" id="overallGrid">
        <div class="card ov-shell" data-card="ov-borrow-gpu"></div>
        <div class="card ov-shell" data-card="ov-borrow-mgr-agents"></div>
        <div class="card ov-shell" data-card="ov-borrow-lms-power"></div>
      </div>
    </div>`;
  window.layout = { overallBorrowed: ['gpu', 'mgr-agents', 'lms-power'] };
  window._ensureSizeBtn = () => {};
  window._resizeChartsIn = () => {};
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
