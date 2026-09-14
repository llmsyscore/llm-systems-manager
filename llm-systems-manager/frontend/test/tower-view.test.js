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

describe('insights', () => {
  const base = { id: 'i1', alert_id: 'a1', rule: 'llama-server asleep', host: 'box', severity: 'warning', summary: 'asleep', detail: 'd',
                 suggested_action: 'Wake it', status: 'new', playbook_id: 'wake_llama', playbook_title: 'Wake llama-server', playbook_safe: true,
                 created: 1000, checks: [{ name: 'host_detail', summary: 'read host detail · box · 8 ms', ok: true }] };
  test('apply needs the tier: safe at operate, unsafe only for admins at admin', () => {
    expect(TW.insightView(base, { capabilities: 'read' }, 1600).applyLabel).toBeNull();
    const v = TW.insightView(base, { capabilities: 'operate' }, 1600);
    expect(v.applyLabel).toBe('Wake llama-server'); expect(v.title).toBe('Wake llama-server'); expect(v.age).toBe('10 min'); expect(v.cls).toBe(''); expect(v.open).toBe(true);
    expect(v.checks).toHaveLength(1); expect(v.action).toBe('Wake it'); expect(v.alertId).toBe('a1'); expect(v.running).toBe(false);
    const unsafe = { ...base, playbook_safe: false, playbook_id: 'restart_llama', playbook_title: 'Restart llama-server', severity: 'critical' };
    expect(TW.insightView(unsafe, { capabilities: 'operate', admin: true }, 1600).applyLabel).toBeNull();
    expect(TW.insightView(unsafe, { capabilities: 'admin', admin: false }, 1600).adminOnly).toBe(true);
    expect(TW.insightView(unsafe, { capabilities: 'admin', admin: true }, 1600).applyLabel).toBe('Restart llama-server');
    expect(TW.insightView(unsafe, {}, 1600).cls).toBe('crit');
    expect(TW.insightView({ ...base, playbook_id: null }, { capabilities: 'admin', admin: true }, 1600).applyLabel).toBeNull();
    const busy = TW.insightView({ ...base, status: 'applying' }, { capabilities: 'operate' }, 1600);
    expect(busy.running).toBe(true); expect(busy.open).toBe(false); expect(busy.applyLabel).toBeNull(); expect(busy.adminOnly).toBe(false);
  });
  test('applied cards say what ran and when; failed applies surface the message; header counts', () => {
    const done = TW.insightView({ ...base, status: 'applied', applied_by: 'tower via alarm a1', resolved: 1590 }, { capabilities: 'operate' }, 1600);
    expect(done.cls).toBe('done'); expect(done.applied).toBe(true); expect(done.open).toBe(false); expect(done.age).toBe('now');
    expect(done.appliedLine).toBe('✓ Wake llama-server'); expect(done.appliedBy).toBe('auto'); expect(done.auditActor).toBe('tower via alarm a1'); expect(done.applyLabel).toBeNull();
    expect(TW.insightView({ ...base, status: 'applied', applied_by: 'tower via alice' }, {}, 1600).appliedBy).toBe('alice');
    expect(TW.insightView({ ...base, result: { ok: false, message: 'unknown host' } }, {}, 1600).failed).toBe('unknown host');
    expect(TW.insightView({ ...base, status: 'applied', result: { ok: true } }, {}, 1600).failed).toBeNull();
    expect(TW.insightsHeader([base, { ...base, status: 'applied' }, { ...base, status: 'dismissed' }])).toBe('1 new · 1 applied');
    expect(TW.insightsHeader([{ ...base, status: 'seen' }])).toBe('1');
    expect(TW.visibleInsights([base, { ...base, status: 'dismissed' }])).toHaveLength(1);
    expect(TW.ageText(30)).toBe('now'); expect(TW.ageText(7200)).toBe('2 h'); expect(TW.ageText(200000)).toBe('2 d');
  });
});
