import { describe, test, expect } from 'vitest';
import TW from '../js/lib/tower-view.js';

describe('reduce', () => {
  test('a turn accumulates ticks and streamed text', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'why red?' });
    s = TW.reduce(s, { event: 'model', model: 'qwen3-14b', provider: 'llama', hosts: ['box'] });
    s = TW.reduce(s, { event: 'status', state: 'tool', name: 'host_detail' });
    s = TW.reduce(s, { event: 'tool', name: 'host_detail', ok: true, ms: 84, summary: 'read host detail · box · 84 ms', result: { gpu_temp_c: 91 } });
    s = TW.reduce(s, { event: 'delta', text: 'box is ' });
    s = TW.reduce(s, { event: 'delta', text: 'hot' });
    s = TW.reduce(s, { event: 'done', ok: true, calls: 1, elapsed_ms: 900 });
    expect(s.model.model).toBe('qwen3-14b');
    expect(s.turns).toHaveLength(2);
    expect(s.turns[1].ticks[0].summary).toBe('read host detail · box · 84 ms');
    expect(s.turns[1].text).toBe('box is hot');
    expect(s.status).toBe('idle');
  });
  test('error ends the turn with a message and keeps prior text', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'x' });
    s = TW.reduce(s, { event: 'delta', text: 'part' });
    s = TW.reduce(s, { event: 'error', message: 'Tower could not reach the model on its host — check the Gateway card.' });
    expect(s.turns[1].text).toBe('part');
    expect(s.turns[1].error).toMatch(/Gateway card/);
    expect(s.status).toBe('idle');
  });
  test('a second done event does not append an empty turn', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'x' });
    s = TW.reduce(s, { event: 'delta', text: 'answer' });
    s = TW.reduce(s, { event: 'done', ok: true });
    expect(s.turns).toHaveLength(2);
    s = TW.reduce(s, { event: 'done', ok: true });
    expect(s.turns).toHaveLength(2);
    expect(s.status).toBe('idle');
  });
  test('truncated marks the current tower turn as having dropped output', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'x' });
    s = TW.reduce(s, { event: 'delta', text: 'part' });
    s = TW.reduce(s, { event: 'truncated' });
    s = TW.reduce(s, { event: 'done', ok: true, calls: 0, elapsed_ms: 10 });
    expect(s.turns[1].text).toBe('part');
    expect(s.turns[1].truncated).toBe(true);
  });
});

describe('md', () => {
  test('escapes html, renders bullets, bold, code and a table', () => {
    const html = TW.md('Two alerts:\n- **GPU temp high** — 91 °C\n- Slot pressure <b>x</b>\n\n| Host | t/s |\n|---|---|\n| box | 38.2 |\n\nUse `--ctx-size`.');
    expect(html).toContain('<ul><li><b>GPU temp high</b> — 91 °C</li><li>Slot pressure &lt;b&gt;x&lt;/b&gt;</li></ul>');
    expect(html).toContain('<table><tr><th>Host</th><th>t/s</th></tr><tr><td>box</td><td>38.2</td></tr></table>');
    expect(html).toContain('<code>--ctx-size</code>');
  });
});

describe('threadView + suggestions + pageContext + stateView', () => {
  test('server rows fold tool rows into the following assistant turn', () => {
    const rows = [
      { role: 'user', content: 'q' },
      { role: 'tool', content: '{"a":1}', tool_name: 'alarms', tool_args: '{}', tool_ok: 1, tool_ms: 41 },
      { role: 'assistant', content: 'a' },
    ];
    const v = TW.threadView(rows);
    expect(v).toHaveLength(2);
    expect(v[1].ticks[0].summary).toBe('read alarms · 41 ms');
    expect(v[1].ticks[0].ok).toBe(true);
  });
  test('a tool round with a preamble merges preamble + tool rows + answer into one tower turn', () => {
    const rows = [
      { role: 'user', content: 'why is box red?' },
      { role: 'assistant', content: 'Let me check the host.' },
      { role: 'tool', content: '{"gpu_temp_c":91}', tool_name: 'host_detail', tool_args: '{"host":"box"}', tool_ok: 1, tool_ms: 84 },
      { role: 'assistant', content: 'box is hot.' },
    ];
    const v = TW.threadView(rows);
    expect(v).toHaveLength(2);
    expect(v[0]).toEqual({ role: 'user', text: 'why is box red?' });
    expect(v[1].role).toBe('tower');
    expect(v[1].text).toBe('Let me check the host.\nbox is hot.');
    expect(v[1].ticks).toHaveLength(1);
    expect(v[1].ticks[0].summary).toBe('read host detail · box · 84 ms');
  });
  test('suggestions depend on the page', () => {
    expect(TW.suggestions({ tab: 'overall', host: 'box' })[0]).toBe('Why is box red?');
    expect(TW.suggestions({ tab: 'overall', host: 'box' })).toContain('What hardware does box have?');
    expect(TW.suggestions({ tab: 'events' })).toContain('Summarise active alarms');
    expect(TW.suggestions({ tab: 'events' }).length).toBeGreaterThanOrEqual(5);
    expect(TW.suggestions({ tab: 'admin' })[0]).toMatch(/alarm engine/i);
    expect(TW.suggestions({ tab: 'tools' })[0]).toMatch(/benchmark/i);
    expect(TW.suggestions({ tab: 'dashboard', sub: 'energy' })[0]).toMatch(/power/i);
  });
  test('pageContext drops empties and caps cards', () => {
    const p = TW.pageContext({ tab: 'overall', sub: '', host: null, cards: Array.from({ length: 40 }, (_, i) => 'c' + i), alertId: 'a1' });
    expect(p).toEqual({ tab: 'overall', cards: p.cards, alert_id: 'a1' });
    expect(p.cards).toHaveLength(24);
  });
  test('stateView maps the API', () => {
    expect(TW.stateView({ ok: true, enabled: false, admin: true })).toMatchObject({ off: true, admin: true, noModel: false });
    expect(TW.stateView({ ok: true, enabled: true, admin: false, model: null })).toMatchObject({ off: false, noModel: true });
    expect(TW.stateView({ ok: true, enabled: true, model: 'q', provider: 'llama', hosts: ['box'] }).chip).toEqual({ model: 'q', provider: 'llama.cpp', host: 'box' });
  });
});

describe('md fenced code', () => {
  test('a ``` block renders as one escaped <pre>', () => {
    const html = TW.md('Chart:\n```\ngpu  ████ 4\ncpu  ██ 2 <b>\n```\nDone.');
    expect(html).toBe('<p>Chart:</p><pre>gpu  ████ 4\ncpu  ██ 2 &lt;b&gt;</pre><p>Done.</p>');
  });
});

describe('md never hangs on partial input', () => {
  test('a table row with no separator line yet renders as a paragraph', () => {
    expect(TW.md('Here:\n| host | watts |')).toBe('<p>Here:</p><p>| host | watts |</p>');
    expect(TW.md('| a |\n| b |')).toBe('<p>| a |</p><p>| b |</p>');
    expect(TW.md('| h |\n|---|\n| 1 |')).toContain('<table>');
  });
});

describe('actions', () => {
  const CARD = { title: 'Wake llama-server', target: 'box · llama.cpp', does: 'Sends a one-token completion.', not: 'No model is loaded or unloaded.' };
  test('confirm parks the turn on a pending card and action resolves it', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'wake box' });
    s = TW.reduce(s, { event: 'confirm', action_id: 'a1', tool: 'wake_server', args: { host: 'box' }, card: CARD, tier: 'operate', role: 'operator', actor: 'tower via adriel', expires_s: 600 });
    expect(s.status).toBe('awaiting');
    expect(s.turns[1].actions).toEqual([{ id: 'a1', tool: 'wake_server', args: { host: 'box' }, card: CARD, status: 'pending', tier: 'operate', role: 'operator', actor: 'tower via adriel', message: null, ms: null, expires: expect.any(Number) }]);
    const now = Math.floor(Date.now() / 1000);
    expect(s.turns[1].actions[0].expires).toBeGreaterThanOrEqual(now + 599);
    expect(s.turns[1].actions[0].expires).toBeLessThanOrEqual(now + 601);
    s = TW.reduce(s, { event: 'action', action_id: 'a1', tool: 'wake_server', status: 'done', message: 'done', ms: 1200, actor: 'adriel' });
    expect(s.status).toBe('thinking');
    expect(s.turns[1].actions[0]).toMatchObject({ status: 'done', ms: 1200, message: 'done' });
    s = TW.reduce(s, { event: 'delta', text: 'awake' });
    s = TW.reduce(s, { event: 'done', ok: true });
    expect(s.turns[1].text).toBe('awake');
  });
  test('an action resolves the card in whichever turn holds it, appending nothing', () => {
    let s = { ...TW.initial(), turns: TW.threadView([
      { role: 'action', content: JSON.stringify({ action_id: 'a1', tool: 'wake_server', card: CARD, status: 'pending' }), tool_name: 'wake_server' },
      { role: 'user', content: 'and then?' },
    ]) };
    expect(s.turns).toHaveLength(2);
    expect(s.turns[0].done).toBe(true);
    s = TW.reduce(s, { event: 'action', action_id: 'a1', status: 'done', message: 'done', ms: 900, actor: 'adriel' });
    expect(s.turns).toHaveLength(2);
    expect(s.turns[0].actions[0]).toMatchObject({ status: 'done', ms: 900, message: 'done' });
    expect(s.status).toBe('thinking');
  });
  test('an action for an unknown id is ignored', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'x' });
    s = TW.reduce(s, { event: 'action', action_id: 'zz', status: 'denied' });
    expect(s.turns[1].actions).toEqual([]);
  });
  test('threadView folds stored action rows into the turn', () => {
    const rows = [
      { role: 'user', content: 'wake box' },
      { role: 'assistant', content: 'I will wake it.' },
      { role: 'action', content: JSON.stringify({ action_id: 'a1', tool: 'wake_server', args: { host: 'box' }, card: CARD, status: 'pending', actor: null, expires: 1900000000, message: null }), tool_name: 'wake_server', tool_ok: null, tool_ms: null },
      { role: 'assistant', content: 'done' },
    ];
    const t = TW.threadView(rows);
    expect(t).toHaveLength(2);
    expect(t[1].actions[0]).toMatchObject({ id: 'a1', tool: 'wake_server', status: 'pending', expires: 1900000000, card: CARD });
    expect(t[1].text).toBe('I will wake it.\ndone');
  });
  test('suggestions add one act chip above read tier', () => {
    expect(TW.suggestions({ tab: 'llm' }, 'read')).not.toContain('Wake llama-server');
    expect(TW.suggestions({ tab: 'llm' }, 'operate').at(-1)).toBe('Wake llama-server');
    expect(TW.suggestions({ tab: 'events' }, 'admin').at(-1)).toBe('Acknowledge the oldest active alert');
    expect(TW.suggestions({ tab: 'overall' }, 'operate').at(-1)).toBe('Unload a model nobody is using');
  });
});
