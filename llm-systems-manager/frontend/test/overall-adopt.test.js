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
      <div class="hp-root" id="hp-llama" data-hp-page="llama">
        <section class="row" data-row="llama-gpu"><div class="panels">
          <section class="panel" data-card="llama-gpu-busy"><div class="p-body" id="hp-b-llama-gpu-busy"><div class="ts" id="hp-llama-gpu-busy"></div></div></section>
          <section class="panel" data-card="llama-cpu-mem"><div class="p-body"></div></section>
        </div></section>
      </div>
      <div class="mg-root" id="mg-root">
        <section class="row" data-row="mgr-conn"><div class="panels">
          <section class="panel" data-card="mgr-streams"><div class="p-body" id="mg-b-streams"></div></section>
        </div></section>
      </div>
    </div>
    <div id="overallTab">
      <section class="row" data-row="pinned" data-kit-nosort>
        <button type="button" class="row-hd" data-row-fold>Pinned</button>
        <div class="grid" id="overallGrid">
          <div class="card ov-shell" data-card="ov-borrow-llama-gpu-busy"></div>
          <div class="card ov-shell" data-card="ov-borrow-mgr-streams"></div>
          <div class="card ov-shell" data-card="ov-borrow-lms-mac-power"></div>
        </div>
      </section>
    </div>`;
  window.layout = { overallBorrowed: ['llama-gpu-busy', 'mgr-streams', 'lms-mac-power'] };
  window._ensureSizeBtn = () => {};
  window._resizeChartsIn = () => {};
  loadAdoption();
});

describe('adoptPinnedCards', () => {
  it('moves the real node into its shell', () => {
    adoptPinnedCards();
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"]');
    expect(shell.querySelector('[data-card="llama-gpu-busy"]')).toBeTruthy();
    expect(document.querySelector('#hp-llama [data-card="llama-gpu-busy"]')).toBeNull();
    // The chart holder travels with the panel.
    expect(shell.querySelector('#hp-llama-gpu-busy')).toBeTruthy();
  });

  it('renders the ov-missing note when the home card does not exist', () => {
    adoptPinnedCards();
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-lms-mac-power"]');
    expect(shell.querySelector('.ov-missing')).toBeTruthy();
    expect(window._ovAdopted.has('lms-mac-power')).toBe(false);
  });

  it('is idempotent — double adopt keeps one card and one home marker', () => {
    adoptPinnedCards();
    adoptPinnedCards();
    expect(document.querySelectorAll('[data-card="llama-gpu-busy"]').length).toBe(1);
    const marks = [...document.querySelector('#hp-llama .panels').childNodes]
      .filter(n => n.nodeType === 8 && n.textContent.includes('ov-home:llama-gpu-busy'));
    expect(marks.length).toBe(1);
  });
});

describe('returnPinnedCards', () => {
  it('restores the exact home position (before its old next sibling)', () => {
    adoptPinnedCards();
    returnPinnedCards();
    const grid = document.querySelector('#hp-llama .panels');
    const cards = [...grid.querySelectorAll('.panel')].map(c => c.dataset.card);
    expect(cards).toEqual(['llama-gpu-busy', 'llama-cpu-mem']);
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] [data-card="llama-gpu-busy"]')).toBeNull();
    // No leftover comment markers.
    const marks = [...grid.childNodes].filter(n => n.nodeType === 8);
    expect(marks.length).toBe(0);
  });

  it('adopt → return → adopt round-trips cleanly', () => {
    adoptPinnedCards();
    returnPinnedCards();
    adoptPinnedCards();
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] [data-card="llama-gpu-busy"]')).toBeTruthy();
    returnPinnedCards();
    expect(document.querySelector('#hp-llama [data-card="llama-gpu-busy"]')).toBeTruthy();
  });

  it('double return is a no-op', () => {
    adoptPinnedCards();
    returnPinnedCards();
    returnPinnedCards();
    expect(document.querySelectorAll('[data-card="llama-gpu-busy"]').length).toBe(1);
    expect(window._ovAdopted.size).toBe(0);
  });
});

describe('_returnOneAdopted', () => {
  it('returns a single card home so its shell can be removed', () => {
    adoptPinnedCards();
    _returnOneAdopted('mgr-streams');
    expect(document.querySelector('#mg-root [data-card="mgr-streams"]')).toBeTruthy();
    expect(window._ovAdopted.has('mgr-streams')).toBe(false);
    // The other adopted card is untouched.
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] [data-card="llama-gpu-busy"]')).toBeTruthy();
  });
});

describe('_ovPinned', () => {
  it('reflects layout.overallBorrowed', () => {
    expect(_ovPinned('mgr-streams')).toBe(true);
    expect(_ovPinned('nope')).toBe(false);
    window.layout = null;
    expect(_ovPinned('mgr-streams')).toBe(false);
  });
});

describe('adoption visibility (hidden-at-home cards)', () => {
  it('clears a stale home display:none so the pinned card shows on Overall', () => {
    const gpu = document.querySelector('#hp-llama [data-card="llama-gpu-busy"]');
    gpu.style.display = 'none';
    adoptPinnedCards();
    const adopted = document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] [data-card="llama-gpu-busy"]');
    expect(adopted).toBeTruthy();
    expect(adopted.style.display).toBe('');
  });

  it('Manager page and host page panels keep the kit layout on return (#1432)', () => {
    const sized = [];
    window._applyCardSize = (card, size) => sized.push([card.dataset.card, size]);
    window._sizeMapFor = () => ({ 'mgr-streams': '2x1' });
    window._defaultCardSize = () => 'auto';
    adoptPinnedCards();
    returnPinnedCards();
    expect(sized).toEqual([]);
    for (const sel of ['#hp-llama [data-card="llama-gpu-busy"]', '#mg-root [data-card="mgr-streams"]']) {
      const panel = document.querySelector(sel);
      expect(panel.dataset.size, sel).toBeUndefined();
      expect([...panel.classList].some(c => c.startsWith('size-')), sel).toBe(false);
    }
    ['_applyCardSize', '_sizeMapFor', '_defaultCardSize'].forEach(k => { delete window[k]; });
  });
});

describe('_ensureSizeBtn on shells', () => {
  it('gives the shell its own direct-child button even when the adopted card has one', () => {
    (0, eval)(fnSrc('_ensureSizeBtn', charts) + '\nwindow._ensureSizeBtn = _ensureSizeBtn;');
    const shell = document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"]');
    const inner = document.createElement('div');
    inner.className = 'card';
    inner.dataset.card = 'llama-gpu-busy';
    const innerBtn = document.createElement('button');
    innerBtn.className = 'card-size-btn';
    inner.appendChild(innerBtn);
    shell.appendChild(inner);
    window._ensureSizeBtn(shell);
    const direct = [...shell.children].filter(k => k.classList && k.classList.contains('card-size-btn'));
    expect(direct.length).toBe(1);
  });
});

describe('hidden Pinned row (#1432)', () => {
  // The real row helpers next to the adoption helpers; saveLayout and the relayout and backfill hooks are stubbed.
  function loadRows() {
    (0, eval)([fnSrc('_kitRows'), fnSrc('_applyRowsHidden'), fnSrc('toggleOverallRow'), fnSrc('_ovBackfillPinnedProviders'),
      'window._kitRows = _kitRows; window._applyRowsHidden = _applyRowsHidden; window.toggleOverallRow = toggleOverallRow;',
      'window._ovBackfillPinnedProviders = _ovBackfillPinnedProviders;'].join('\n'));
    window.saveLayout = () => {};
    window._relayoutGridCards = () => {};
    window.CARD_LABELS = { 'llama-gpu-busy': 'GPU busy and memory' };
    window.CARD_LABELS_LMS = {};
    window.CARD_LABELS_VLLM = {};
    window.CARD_LABELS_MANAGER = { 'mgr-streams': 'Connection health' };
    window._activeTab = 'overall';
  }
  const home = (id) => document.querySelector(`#hp-llama [data-card="${id}"], #mg-root [data-card="${id}"]`);
  const inShell = (id) => document.querySelector(`#overallGrid [data-card="ov-borrow-${id}"] [data-card="${id}"]`);

  it('adopts nothing while hidden, returns adopted cards on hide and adopts again on show', () => {
    loadRows();
    window.layout.overallRowsHidden = ['pinned'];
    adoptPinnedCards();
    expect(home('llama-gpu-busy')).toBeTruthy();
    expect(inShell('llama-gpu-busy')).toBeNull();
    expect(window._ovAdopted.size).toBe(0);
    expect(window._ovPinned('mgr-streams')).toBe(false);

    window.toggleOverallRow('pinned', true);
    expect(window.layout.overallRowsHidden).toEqual([]);
    expect(inShell('llama-gpu-busy')).toBeTruthy();
    expect(inShell('mgr-streams')).toBeTruthy();
    expect(window._ovPinned('mgr-streams')).toBe(true);

    window.toggleOverallRow('pinned', false);
    expect(document.querySelector('section.row[data-row="pinned"]').hidden).toBe(true);
    expect(home('llama-gpu-busy')).toBeTruthy();
    expect(home('mgr-streams')).toBeTruthy();
    expect(inShell('llama-gpu-busy')).toBeNull();
    expect(window._ovAdopted.size).toBe(0);
  });

  it('showing the row on another page adopts nothing until Overall is entered', () => {
    loadRows();
    window.layout.overallRowsHidden = ['pinned'];
    window._activeTab = 'dashboard';
    window.toggleOverallRow('pinned', true);
    expect(inShell('llama-gpu-busy')).toBeNull();
    expect(home('llama-gpu-busy')).toBeTruthy();
    window._activeTab = 'overall';
    adoptPinnedCards();
    expect(inShell('llama-gpu-busy')).toBeTruthy();
  });
});

describe('Pinned row empty sentence (#1432)', () => {
  // The real pin add and remove with the sentence sync; layout saving, sizing and flow hooks are stubbed.
  function loadPins() {
    (0, eval)([fnSrc('_ovPinNoneSync'), fnSrc('addBorrowedCard'), fnSrc('removeBorrowedCard'),
      'window._ovPinNoneSync = _ovPinNoneSync; window.addBorrowedCard = addBorrowedCard; window.removeBorrowedCard = removeBorrowedCard;'].join('\n'));
    window.saveLayout = () => {};
    window._applyCardSize = () => {};
    window._defaultCardSize = () => 'm';
    window._sizeMapFor = () => ({});
    window._activeTab = 'overall';
    document.querySelector('section.row[data-row="pinned"]').insertAdjacentHTML('beforeend',
      '<div class="ov-empty ov-none" id="ovPinNone" hidden>Nothing pinned yet.</div>');
  }

  it('shows when the last pin goes and hides when a card is pinned again', () => {
    loadPins();
    window._ovPinNoneSync();
    const none = document.getElementById('ovPinNone');
    expect(none.hidden).toBe(true);
    adoptPinnedCards();
    ['llama-gpu-busy', 'mgr-streams', 'lms-mac-power'].forEach(id => window.removeBorrowedCard(id));
    expect(window.layout.overallBorrowed).toEqual([]);
    expect(none.hidden).toBe(false);
    expect(document.querySelector('#hp-llama [data-card="llama-gpu-busy"]')).toBeTruthy();
    window.addBorrowedCard('llama-gpu-busy');
    expect(none.hidden).toBe(true);
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] [data-card="llama-gpu-busy"]')).toBeTruthy();
  });
});
