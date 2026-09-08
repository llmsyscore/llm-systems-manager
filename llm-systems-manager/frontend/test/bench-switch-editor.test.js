import { describe, it, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

function openEditor() {
  const win = runHarness({
    sources: [srcFile('js/bench-autotune.js')],
    bodyHtml: `
      <button class="bench-tab active" data-tab="llama-bench"></button>
      <div id="benchSwitchList"></div>
      <span id="benchSwitchLabel"></span>
      <div id="benchSwitchPanel"></div>
    `,
    bootstrap: `
      switchBenchTab('llama-bench');
      window.__switches = () => _benchSwitches;
    `,
  });
  return win;
}

const rowByLabel = (win, text) =>
  [...win.document.querySelectorAll('.bench-opt-row')]
    .find(r => r.querySelector('.bench-opt-label').textContent.startsWith(text));

describe('benchmark structured switch editor', () => {
  it('renders every default switch checked with its default value', () => {
    const win = openEditor();
    const rows = [...win.document.querySelectorAll('.bench-opt-row')];
    expect(rows.length).toBe(11);
    const dRow = rowByLabel(win, '-d');
    expect(dRow.querySelector('input[type=checkbox]').checked).toBe(true);
    expect(dRow.querySelector('.bench-input').value).toBe('0,8192,32768');
    const faRow = rowByLabel(win, '-fa');
    expect(faRow.querySelector('select.bench-input').value).toBe('1');
  });

  it('unchecking removes the switch; re-checking restores it', () => {
    const win = openEditor();
    const cb = rowByLabel(win, '-d').querySelector('input[type=checkbox]');
    cb.click();
    expect(win.__switches().some(s => s.flag === '-d')).toBe(false);
    cb.click();
    expect(win.__switches().find(s => s.flag === '-d').value).toBe('0,8192,32768');
  });

  it('editing a value updates the switch list', () => {
    const win = openEditor();
    const input = rowByLabel(win, '-ub').querySelector('.bench-input');
    input.value = '256';
    input.dispatchEvent(new win.Event('change'));
    expect(win.__switches().find(s => s.flag === '-ub').value).toBe('256');
  });

  it('custom rows round-trip through the custom section', () => {
    const win = openEditor();
    win.addBenchSwitch();
    const row = win.document.querySelector('.bench-switch-row');
    const [flagInput, valInput] = row.querySelectorAll('.bench-input');
    flagInput.value = '-mmp';
    flagInput.dispatchEvent(new win.Event('change'));
    valInput.value = '0';
    valInput.dispatchEvent(new win.Event('change'));
    expect(win.__switches().find(s => s.flag === '-mmp').value).toBe('0');
    expect(win.document.querySelector('.bench-opt-custom-h').textContent).toBe('Custom');
  });

  it('-ctk / -ctv render as chips whose CSV value follows option order', () => {
    const win = openEditor();
    const row = [...win.document.querySelectorAll('#benchSwitchList .bench-opt-row')].find(r => r.textContent.includes('-ctk'));
    const chips = row.querySelectorAll('.bench-chip');
    expect(chips.length).toBe(7);
    expect(row.querySelector('.bench-chip.on').dataset.opt).toBe('f16');
    // turn on q4_0 then q8_0 → CSV keeps option order f16,q8_0,q4_0
    [...chips].find(c => c.dataset.opt === 'q4_0').click();
    [...chips].find(c => c.dataset.opt === 'q8_0').click();
    expect(win.__switches().find(s => s.flag === '-ctk').value).toBe('f16,q8_0,q4_0');
    // the last remaining chip cannot be turned off
    [...chips].filter(c => c.classList.contains('on')).forEach(c => c.click());
    expect(row.querySelectorAll('.bench-chip.on').length).toBe(1);
  });

  it('warns when a quantized V cache is selected without -fa 1', () => {
    const win = openEditor();
    const switches = win.__switches();
    const idx = switches.findIndex(s => s.flag === '-fa');
    if (idx !== -1) switches.splice(idx, 1);
    win._renderBenchSwitches();
    const row = [...win.document.querySelectorAll('#benchSwitchList .bench-opt-row')].find(r => r.textContent.includes('-ctv'));
    [...row.querySelectorAll('.bench-chip')].find(c => c.dataset.opt === 'q8_0').click();
    expect(row.querySelector('.bench-sw-hint').textContent).toMatch(/needs -fa 1/);
  });
});
