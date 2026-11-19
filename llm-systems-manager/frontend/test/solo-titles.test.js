// #1432 phase 4d: a row's only panel titled like its row shows that title to screen readers only (mcSoloTitles).
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const foundation = srcFile('js/foundation.js');
const indexDoc = new JSDOM(srcFile('index.html')).window.document;
evalGlobal(srcFile('kit/kit.js'));
evalGlobal(fnSrc(foundation, 'mcSoloTitles') + '\nwindow.mcSoloTitles = mcSoloTitles;');

const row = (id, title, panels) => '<section class="row" data-row="' + id + '"><button type="button" class="row-hd" data-row-fold>' + title
  + '</button><div class="panels">' + panels.map(([card, t]) => '<section class="panel" data-card="' + card + '" data-title="' + t
  + '" data-kit-table><div class="p-body" id="b-' + card + '"></div></section>').join('') + '</div></section>';
const solo = (card) => document.querySelector('[data-card="' + card + '"]').classList.contains('mc-solo');

describe('mcSoloTitles (#1432)', () => {
  test('marks only a lone panel whose title equals its row title, ignoring case; note, menu and grip stay', () => {
    document.body.innerHTML = '<div id="pg">' + row('a', 'Snapshot', [['a-tiles', 'SNAPSHOT']]) + row('b', 'Tower', [['b-tower', 'Tower'], ['b-x', 'Other']])
      + row('c', 'Host', [['c-tiles', 'Snapshot']]) + '</div>';
    const pg = document.getElementById('pg');
    window.kitDress(pg);
    window.kitLayout('solo-test', pg, { getItem: () => null, setItem() {} });
    window.mcSoloTitles(pg);
    expect([solo('a-tiles'), solo('b-tower'), solo('b-x'), solo('c-tiles')]).toEqual([true, false, false, false]);
    const hd = document.querySelector('[data-card="a-tiles"] .p-hd');
    expect(hd.querySelector('.p-title').textContent).toBe('SNAPSHOT');
    expect(hd.querySelector('.p-menu')).toBeTruthy();
    // The row title is read without the grip or menu the kit adds around it.
    expect(document.querySelector('[data-row="a"] .row-grip')).toBeTruthy();
  });

  test('a panel gains or loses the mark when its row changes', () => {
    document.body.innerHTML = '<div id="pg">' + row('a', 'Findings', [['a-f', 'Findings']]) + '</div>';
    const pg = document.getElementById('pg');
    window.kitDress(pg);
    window.mcSoloTitles(pg);
    expect(solo('a-f')).toBe(true);
    pg.querySelector('.panels').insertAdjacentHTML('beforeend', '<section class="panel" data-card="a-g" data-title="More"></section>');
    window.kitDress(pg);
    window.mcSoloTitles(pg);
    expect(solo('a-f')).toBe(false);
  });

  test('the style hides the title only for a lone marked panel in its row box and not at full size, so a pinned panel on Overall shows it', () => {
    const css = srcFile('css/base.css');
    const rule = css.match(/\.row\[data-row\] > \.panels > \.panel\.mc-solo:only-child:not\(\.full\) > \.p-hd > \.p-title \{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule[1]).toMatch(/position: absolute; width: 1px; height: 1px;/);
    expect(rule[1]).toMatch(/clip-path: inset\(50%\)/);
    expect(rule[1]).not.toMatch(/display: none|visibility: hidden/);
    // Overall's Pinned row holds its cards in #overallGrid, not a .panels box.
    expect(indexDoc.getElementById('overallGrid').classList.contains('panels')).toBe(false);
  });

  test('the Forecast and All hosts rows of index.html mark their lone same-titled panels', () => {
    document.body.innerHTML = indexDoc.getElementById('dash-forecast').outerHTML + indexDoc.getElementById('dash-all').outerHTML;
    ['fc-root', 'dash-all'].forEach(id => { window.kitDress(document.getElementById(id)); window.mcSoloTitles(document.getElementById(id)); });
    expect([solo('fc-tiles'), solo('fc-findings'), solo('fc-checks'), solo('ah-now'), solo('ah-cpu')]).toEqual([true, true, false, true, false]);
  });

  test('every kit page calls it right after kitDress', () => {
    for (const f of ['js/overall.js', 'js/allhosts.js', 'js/energy.js', 'js/forecast.js', 'js/hostpage.js', 'js/dashboard-manager.js']) {
      expect(srcFile(f), f).toMatch(/kitDress\(root\);\n\s*if \(typeof mcSoloTitles === 'function'\) mcSoloTitles\(root\);/);
    }
  });
});
