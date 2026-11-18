// #1303, #1432: the llama.cpp Server details panel's prompt-cache reuse and draft-acceptance lines
// show a state word (Not reported / Idle / Reset / Paused) instead of a fake 0%. Real js/lib/host-view.js.
import { describe, test, expect } from 'vitest';
import HP from '../js/lib/host-view.js';

const NOW = Date.parse('2026-11-16T12:00:00Z');
// The Server details value for one windowed block under its llama payload key.
const line = (key, block, label) => {
  const rows = HP.details('llama', 'details', null, { live: { llama: { [key]: block } }, now: NOW }, true);
  return rows.find(r => r[0] === label)[1];
};
const reuse = (block) => line('cache_reuse', block, 'Prompt cache reuse');
const accept = (block) => line('spec_accept', block, 'Draft tokens accepted');

describe('llama window lines on the Server details panel', () => {
  test('an ok ratio reads as a percent with its counts', () => {
    expect(reuse({ state: 'ok', processed: 10, cached: 8200, ratio: 0.9988 })).toBe('99.9% (8,200 of 10)');
    expect(accept({ state: 'ok', drafted: 93, accepted: 68, drafts: 30, ratio: 0.7312, accepted_per_draft: 2.27 }))
      .toBe('73.1% (68 of 93)');
  });

  test('speculative decoding not configured reads "Not in use"', () => {
    expect(accept({ state: 'off', drafted: 0, accepted: 0, drafts: 0, ratio: null, accepted_per_draft: null })).toBe('Not in use');
  });

  test('an unsupported build says so and never shows 0%', () => {
    expect(reuse({ state: 'unsupported', processed: null, cached: null, ratio: null })).toBe('Not reported');
  });

  test('idle, reset and paused windows are labelled by state', () => {
    expect(reuse({ state: 'idle', processed: 0, cached: 0, ratio: null })).toBe('Idle');
    expect(reuse({ state: 'reset', processed: 0, cached: 0, ratio: null })).toBe('Reset');
    expect(reuse({ state: 'reset', processed: 30, cached: 30, ratio: 0.5 })).toBe('50.0% · Reset (30 of 30)');
    expect(reuse({ state: 'paused', processed: 0, cached: 0, ratio: null })).toBe('Paused');
    expect(reuse({ state: 'idle', processed: 4, cached: 0, ratio: 0 })).toBe('0.0% (0 of 4)');
  });

  test('a missing block (sleeping or old agent) and an offline host read as a dash', () => {
    expect(reuse(null)).toBe('—');
    expect(reuse(undefined)).toBe('—');
    const off = HP.details('llama', 'details', null,
      { live: { llama: { cache_reuse: { state: 'ok', ratio: 0.5, cached: 1, processed: 2 } } }, now: NOW }, false);
    expect(off.find(r => r[0] === 'Prompt cache reuse')[1]).toBe('—');
  });

  test('the llama.cpp page has the Server details panel in its Inference row', () => {
    const p = HP.panelsFor('llama').find(x => x.name === 'details');
    expect(p).toMatchObject({ row: 'inf', title: 'Server details', kind: 'kv' });
  });
});
