// Leaving the Admin tab must close the floating log panel (and its SSE
// stream) and hide the self-update panel (#268).
import { describe, test, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundation = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');

function switchTabBody() {
  const m = foundation.match(/function switchTab\(tab\) \{[\s\S]*?\n\}/);
  expect(m, 'switchTab not found in foundation.js').toBeTruthy();
  return m[0];
}

describe('switchTab non-admin branch', () => {
  test('stops admin auto-refresh (pre-existing behavior)', () => {
    expect(switchTabBody()).toMatch(/adminStopAutoRefresh\(\)/);
  });

  test('closes the admin log panel and its EventSource', () => {
    expect(switchTabBody()).toMatch(/_adminLogsClose\(\)/);
  });

  test('hides the admin self-update panel', () => {
    expect(switchTabBody()).toMatch(/_adminUpdateClose\(\)/);
  });
});
