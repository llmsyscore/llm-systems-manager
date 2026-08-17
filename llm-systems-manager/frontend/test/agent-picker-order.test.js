// #565 feedback: the provider's primary (is_default) agent always renders
// first in the Dashboard / LLM Control picker order. Runs the real
// _defaultFirst source from foundation.js.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundation = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');
const m = foundation.match(/function _defaultFirst\([^)]*\) \{[\s\S]*?\n\}/);
expect(m, '_defaultFirst not found in foundation.js').toBeTruthy();
// eslint-disable-next-line no-new-func
const _defaultFirst = new Function('list', m[0] + '\nreturn _defaultFirst(list);');

// Real /api/agents/list-by-provider llama rows (dev capture): backend order
// puts the default second.
const LLAMA_ROWS = [
  { age_s: 3.0, agent_id: '601964c9-eaeb-4c04-af3e-74e1b5808710',
    hostname: 'llm-systems-agent-llama2', is_default: false, online: true },
  { age_s: 4.5, agent_id: '056ba13c-689e-411e-b865-74a83b9086cd',
    hostname: 'llm-systems-agent-llama', is_default: true, online: true },
];

describe('_defaultFirst', () => {
  it('moves the primary agent to the front', () => {
    const out = _defaultFirst(LLAMA_ROWS);
    expect(out[0].is_default).toBe(true);
    expect(out[0].hostname).toBe('llm-systems-agent-llama');
  });

  it('keeps backend order for the non-default rest (stable)', () => {
    const rows = [
      { agent_id: 'a', is_default: false }, { agent_id: 'b', is_default: false },
      { agent_id: 'c', is_default: true }, { agent_id: 'd', is_default: false },
    ];
    expect(_defaultFirst(rows).map(r => r.agent_id)).toEqual(['c', 'a', 'b', 'd']);
  });

  it('does not mutate the input and tolerates null / no default', () => {
    const rows = [{ agent_id: 'a', is_default: false }];
    const out = _defaultFirst(rows);
    expect(out).not.toBe(rows);
    expect(out).toEqual(rows);
    expect(_defaultFirst(null)).toEqual([]);
  });

  it('foundation + report-card pickers both route through it', () => {
    const rc = readFileSync(join(here, '..', 'js', 'report-card.js'), 'utf8');
    expect(foundation.includes('llama: _defaultFirst(data.llama)')).toBe(true);
    expect(rc.includes('_defaultFirst(d[provider])')).toBe(true);
  });
});
