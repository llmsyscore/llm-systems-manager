// #948 header pills never wrap; #979 Tower answers never widen the drawer.
import { describe, test, expect } from 'vitest';
import { srcFile } from './helpers/harness.js';

const rule = (css, selector) => {
  const m = css.match(new RegExp('(?:^|\\n)[ \\t]*' + selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'));
  expect(m, `${selector} rule not found`).toBeTruthy();
  return m[1];
};

describe('header state pills (#948)', () => {
  const css = srcFile('css/base.css');
  test('.state-pill keeps one line and never shrinks', () => {
    const body = rule(css, '.state-pill');
    expect(body).toMatch(/white-space:\s*nowrap/);
    expect(body).toMatch(/flex-shrink:\s*0/);
  });
  test('.state-banner never shrinks', () => {
    expect(rule(css, '.state-banner')).toMatch(/flex-shrink:\s*0/);
  });
});

describe('Tower transcript containment (#979)', () => {
  const css = srcFile('css/tower.css');
  test('the transcript and insights panes hide horizontal overflow', () => {
    expect(rule(css, '#towerAside .tw-b')).toMatch(/overflow-x:\s*hidden/);
    expect(rule(css, '#towerAside .tw-iv')).toMatch(/overflow-x:\s*hidden/);
  });
  test('turns and answers may shrink and break long tokens', () => {
    expect(rule(css, '#towerAside .t')).toMatch(/min-width:\s*0/);
    const ans = rule(css, '#towerAside .ans');
    expect(ans).toMatch(/min-width:\s*0/);
    expect(ans).toMatch(/overflow-wrap:\s*anywhere/);
    expect(rule(css, '#towerAside .u')).toMatch(/overflow-wrap:\s*anywhere/);
  });
  test('code blocks and tables scroll inside their box', () => {
    expect(rule(css, '#towerAside .ans pre')).toMatch(/max-width:\s*100%/);
    const table = rule(css, '#towerAside .ans table');
    expect(table).toMatch(/display:\s*block/);
    expect(table).toMatch(/overflow-x:\s*auto/);
  });
});

describe('Tower scroll ownership (#988)', () => {
  const css = srcFile('css/tower.css');
  test('the drawer and its scroll panes never chain scrolling to the page', () => {
    expect(rule(css, '#towerAside')).toMatch(/overscroll-behavior:\s*contain/);
    expect(rule(css, '#towerAside .tw-b')).toMatch(/overscroll-behavior:\s*contain/);
    expect(rule(css, '#towerAside .tw-iv')).toMatch(/overscroll-behavior:\s*contain/);
  });
  test('overlay mode locks the page with scrollbar compensation; docked mode insets the drawer', () => {
    const lock = rule(css, 'body.tw-lock');
    expect(lock).toMatch(/overflow:\s*hidden/);
    expect(lock).toMatch(/padding-right:\s*var\(--tw-sbw/);
    expect(rule(css, 'body.tw-docked #towerAside')).toMatch(/right:\s*var\(--tw-sbw/);
  });
});
