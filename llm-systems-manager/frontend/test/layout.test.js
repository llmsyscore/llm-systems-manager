import { describe, test, expect } from 'vitest';
import layoutLib from '../js/lib/layout.js';

const { PER_AGENT_HIDDEN, PER_AGENT_ORDER, resolveHiddenList, resolveOrderList, resolveSizeMap } = layoutLib;

describe('PER_AGENT_HIDDEN', () => {
  test('the llama.cpp, LMS and vLLM dashboard surfaces are per-agent', () => {
    expect(PER_AGENT_HIDDEN).toEqual({ hidden: 'llama', lmsHidden: 'lms', vllmHidden: 'vllm' });
  });
});

describe('resolveHiddenList — global surfaces', () => {
  test('non-per-agent keys always return the shared global list', () => {
    const layout = { managerHidden: ['mgr-ram'], hiddenOverall: ['ov-fleet'] };
    expect(resolveHiddenList(layout, 'managerHidden', 'agentA')).toBe(layout.managerHidden);
    expect(resolveHiddenList(layout, 'hiddenOverall', 'agentB')).toBe(layout.hiddenOverall);
  });

  test('seeds a missing global list to an empty array', () => {
    const layout = {};
    const list = resolveHiddenList(layout, 'managerHidden', null);
    expect(list).toEqual([]);
    expect(layout.managerHidden).toBe(list);
  });
});

describe('resolveHiddenList — per-agent surfaces', () => {
  test('falls back to the global list when no agent is selected (single-agent install)', () => {
    const layout = { hidden: ['aio'] };
    expect(resolveHiddenList(layout, 'hidden', null)).toBe(layout.hidden);
    expect(layout.hiddenByAgent).toBeUndefined();
  });

  test('seeds a new agent list from the legacy global so existing prefs carry over', () => {
    const layout = { hidden: ['aio', 'gpu'] };
    const list = resolveHiddenList(layout, 'hidden', 'llama1');
    expect(list).toEqual(['aio', 'gpu']);
    // A copy, not the same reference — diverges independently from the global.
    expect(list).not.toBe(layout.hidden);
    expect(layout.hiddenByAgent.llama.llama1).toBe(list);
  });

  test('deselecting a card on one agent does not affect another agent (issue #160)', () => {
    const layout = { hidden: [] };
    const llama1 = resolveHiddenList(layout, 'hidden', 'llama1');
    llama1.push('aio');                       // hide AIO on llama1
    const llama2 = resolveHiddenList(layout, 'hidden', 'llama2');
    expect(llama2).toEqual([]);               // llama2 unaffected
    expect(resolveHiddenList(layout, 'hidden', 'llama1')).toEqual(['aio']);
  });

  test('llama and lms providers keep independent per-agent buckets', () => {
    const layout = { hidden: [], lmsHidden: [] };
    resolveHiddenList(layout, 'hidden', 'host1').push('aio');
    resolveHiddenList(layout, 'lmsHidden', 'host1').push('lms-cpu');
    expect(layout.hiddenByAgent.llama.host1).toEqual(['aio']);
    expect(layout.hiddenByAgent.lms.host1).toEqual(['lms-cpu']);
  });

  test('returns a stable reference across calls so in-place mutation persists', () => {
    const layout = { hidden: [] };
    const a = resolveHiddenList(layout, 'hidden', 'llama1');
    a.push('ram');
    const b = resolveHiddenList(layout, 'hidden', 'llama1');
    expect(b).toBe(a);
    expect(b).toEqual(['ram']);
  });

  test('tolerates a malformed layout without throwing', () => {
    expect(resolveHiddenList(null, 'hidden', 'llama1')).toEqual([]);
    const layout = { hidden: 'not-an-array' };
    expect(resolveHiddenList(layout, 'hidden', null)).toEqual([]);
  });
});

describe('PER_AGENT_ORDER', () => {
  test('maps the per-agent order keys to their providers', () => {
    expect(PER_AGENT_ORDER).toEqual({ order: 'llama', lmsOrder: 'lms', vllmOrder: 'vllm' });
  });
});

describe('resolveOrderList', () => {
  test('falls back to the global order when no agent is selected', () => {
    const layout = { order: ['aio', 'gpu'] };
    expect(resolveOrderList(layout, 'order', null)).toBe(layout.order);
    expect(layout.orderByAgent).toBeUndefined();
  });

  test('seeds a new agent order from the global list as a copy', () => {
    const layout = { order: ['aio', 'gpu'] };
    const list = resolveOrderList(layout, 'order', 'llama1');
    expect(list).toEqual(['aio', 'gpu']);
    expect(list).not.toBe(layout.order);
    expect(layout.orderByAgent.llama.llama1).toBe(list);
  });

  test('reordering one agent does not affect another (issue #166)', () => {
    const layout = { lmsOrder: ['lms-cpu', 'lms-ram'] };
    const host1 = resolveOrderList(layout, 'lmsOrder', 'host1');
    host1.reverse();
    const host2 = resolveOrderList(layout, 'lmsOrder', 'host2');
    expect(host2).toEqual(['lms-cpu', 'lms-ram']);
    expect(resolveOrderList(layout, 'lmsOrder', 'host1')).toEqual(['lms-ram', 'lms-cpu']);
  });

  test('order and hidden buckets are independent', () => {
    const layout = { order: ['aio'], hidden: ['gpu'] };
    resolveOrderList(layout, 'order', 'llama1').push('ram');
    resolveHiddenList(layout, 'hidden', 'llama1').push('cpu-overall');
    expect(layout.orderByAgent.llama.llama1).toEqual(['aio', 'ram']);
    expect(layout.hiddenByAgent.llama.llama1).toEqual(['gpu', 'cpu-overall']);
  });
});

describe('resolveSizeMap', () => {
  test('returns the global cardSizes when no agent is selected', () => {
    const layout = { cardSizes: { aio: '2x2' } };
    expect(resolveSizeMap(layout, 'llama', null, ['aio'])).toBe(layout.cardSizes);
  });

  test('seeds an agent map from the global cardSizes entries in seedIds only', () => {
    const layout = { cardSizes: { aio: '2x2', gpu: '2x1', 'lms-cpu': '1x2' } };
    const map = resolveSizeMap(layout, 'llama', 'llama1', ['aio', 'gpu']);
    expect(map).toEqual({ aio: '2x2', gpu: '2x1' });   // lms-cpu excluded
    expect(map).not.toBe(layout.cardSizes);
    expect(layout.sizesByAgent.llama.llama1).toBe(map);
  });

  test('resizing a card on one agent does not affect another (issue #166)', () => {
    const layout = { cardSizes: {} };
    resolveSizeMap(layout, 'llama', 'llama1', ['aio'])['aio'] = '2x2';
    const llama2 = resolveSizeMap(layout, 'llama', 'llama2', ['aio']);
    expect(llama2.aio).toBeUndefined();
    expect(layout.sizesByAgent.llama.llama1.aio).toBe('2x2');
  });

  test('llama and lms size buckets stay independent', () => {
    const layout = { cardSizes: {} };
    resolveSizeMap(layout, 'llama', 'host1', ['aio'])['aio'] = '2x1';
    resolveSizeMap(layout, 'lms', 'host1', ['lms-cpu'])['lms-cpu'] = '1x2';
    expect(layout.sizesByAgent.llama.host1).toEqual({ aio: '2x1' });
    expect(layout.sizesByAgent.lms.host1).toEqual({ 'lms-cpu': '1x2' });
  });

  test('returns a stable reference and does not re-seed after mutation (1x1 delete persists)', () => {
    const layout = { cardSizes: { aio: '2x2' } };
    const a = resolveSizeMap(layout, 'llama', 'llama1', ['aio']);
    expect(a.aio).toBe('2x2');
    delete a.aio;                                  // user cycled aio back to 1x1
    const b = resolveSizeMap(layout, 'llama', 'llama1', ['aio']);
    expect(b).toBe(a);                             // same map, not re-seeded from global
    expect(b.aio).toBeUndefined();
  });

  test('tolerates a malformed layout without throwing', () => {
    expect(resolveSizeMap(null, 'llama', 'llama1', ['aio'])).toEqual({});
    const layout = { cardSizes: 'nope' };
    expect(resolveSizeMap(layout, 'llama', null, [])).toEqual({});
  });
});
