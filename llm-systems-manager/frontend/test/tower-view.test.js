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
    expect(TW.suggestions({ tab: 'overall', host: 'box' }).slice(0, 3)).toEqual(['Activity summary for today', 'Alarm summary for today', 'Why is box red?']);
    expect(TW.suggestions({ tab: 'events' }).slice(0, 2)).toEqual(['Alarm summary for today', 'Activity summary for today']);
    for (const p of [{ tab: 'llm' }, { tab: 'admin' }, { tab: 'tools' }, { tab: 'dashboard', sub: 'energy' }]) expect(TW.suggestions(p)[0]).toBe('Activity summary for today');
    expect(TW.suggestions({ tab: 'overall', host: 'box' })).toContain('What hardware does box have?');
    expect(TW.suggestions({ tab: 'events' })).toContain('Summarise active alarms');
    expect(TW.suggestions({ tab: 'events' }).length).toBeGreaterThanOrEqual(5);
    expect(TW.suggestions({ tab: 'admin' })[1]).toMatch(/alarm engine/i);
    expect(TW.suggestions({ tab: 'tools' })[1]).toMatch(/benchmark/i);
    expect(TW.suggestions({ tab: 'dashboard', sub: 'energy' })[1]).toMatch(/power/i);
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
  test('a question parks the turn; the answer closes it and becomes the next user turn (#1028)', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'restart it' });
    s = TW.reduce(s, { event: 'question', action_id: 'q1', tool: 'ask_operator', question: 'Which host?', choices: ['box', 'mac'], actor: 'tower via adriel', expires_s: 600 });
    expect(s.status).toBe('awaiting');
    expect(s.turns[1].actions[0]).toMatchObject({ id: 'q1', tool: 'ask_operator', status: 'pending', card: { question: 'Which host?', choices: ['box', 'mac'], questions: [] }, answer: null, expires: expect.any(Number) });
    const multi = TW.reduce(TW.initial(), { event: 'question', action_id: 'q2', questions: [{ question: 'Host?', choices: ['a'], label: 'Host' }, { question: 'Model?', choices: [] }] });
    expect(multi.turns[0].actions[0].card.questions).toEqual([{ question: 'Host?', choices: ['a'], label: 'Host' }, { question: 'Model?', choices: [], label: '' }]);
    const picker = TW.reduce(TW.initial(), { event: 'question', action_id: 'q3', questions: [{ question: 'Metric?', choices: ['RAM (%)'], multi: true, ids: { 'RAM (%)': 'ram_pct' } }] });
    expect(picker.turns[0].actions[0].card.questions).toEqual([{ question: 'Metric?', choices: ['RAM (%)'], label: '', multi: true }]);
    const mq = { multi: true, choices: ['a', 'b', 'c'] };
    expect(TW.qToggle(mq, TW.qToggle(mq, TW.qToggle(mq, undefined, 'c'), 'a'), 'c')).toEqual(['a']);
    expect(TW.qToggle({}, 'a', 'b')).toBe('b');
    expect(TW.qAnswer(mq, ['c', TW.Q_OTHER, 'a'], ' typed ')).toEqual(['a', 'c', 'typed']);
    expect(TW.qAnswer(mq, [TW.Q_OTHER], '  ')).toEqual([]);
    expect(TW.qAnswer({}, TW.Q_OTHER, ' x ')).toBe('x');
    expect([TW.qAnswered([]), TW.qAnswered(['a']), TW.qAnswered(''), TW.qAnswered('a')]).toEqual([false, true, false, true]);
    expect([TW.qPicked(mq, ['a'], 'a'), TW.qPicked(mq, 'a', 'a'), TW.qPicked({}, 'a', 'a')]).toEqual([true, false, true]);
    expect(TW.liveRun([...s.turns.slice(0, 1), { ...s.turns[1], actions: [{ ...s.turns[1].actions[0], runId: 'r1' }] }])).toEqual({ runId: 'r1', status: 'pending' });
    s = TW.reduce(s, { event: 'answer', action_id: 'q1', tool: 'ask_operator', status: 'answered', answer: 'mac', actor: 'adriel' });
    expect(s.status).toBe('thinking');
    expect(s.turns.map(t => t.role)).toEqual(['user', 'tower', 'user', 'tower']);
    expect(s.turns[1].actions[0]).toMatchObject({ status: 'done', answer: 'mac', actor: 'adriel' });
    expect(s.turns[1].done).toBe(true);
    expect(s.turns[2].text).toBe('mac');
    s = TW.reduce(s, { event: 'delta', text: 'restarting mac' });
    s = TW.reduce(s, { event: 'done', ok: true });
    expect(s.turns[3].text).toBe('restarting mac');
  });
  test('a repeated answer event for an already answered card changes nothing', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'restart it' });
    s = TW.reduce(s, { event: 'question', action_id: 'q1', question: 'Which host?', choices: ['box', 'mac'], expires_s: 600 });
    s = TW.reduce(s, { event: 'answer', action_id: 'q1', status: 'answered', answer: 'mac' });
    const once = JSON.stringify(s.turns);
    s = TW.reduce(s, { event: 'answer', action_id: 'q1', status: 'answered', answer: 'mac' });
    expect(JSON.stringify(s.turns)).toBe(once);
    expect(s.turns.map(t => t.role)).toEqual(['user', 'tower', 'user', 'tower']);
  });
  test('an expired question keeps the turn and adds no user turn', () => {
    let s = TW.reduce(TW.initial(), { event: 'user', text: 'restart it' });
    s = TW.reduce(s, { event: 'question', action_id: 'q1', question: 'Which host?', choices: ['box'], expires_s: 600 });
    s = TW.reduce(s, { event: 'answer', action_id: 'q1', status: 'expired', message: 'no answer from the operator' });
    expect(s.turns.map(t => t.role)).toEqual(['user', 'tower']);
    expect(s.turns[1].actions[0]).toMatchObject({ status: 'expired', message: 'no answer from the operator', answer: null });
    expect(s.status).toBe('thinking');
  });
  test('threadView carries a stored question card with its answer, followed by the answer as a user turn', () => {
    const v = TW.threadView([
      { role: 'user', content: 'restart it', ts: 1 },
      { role: 'action', content: JSON.stringify({ action_id: 'q1', tool: 'ask_operator', card: { question: 'Which host?', choices: ['box', 'mac'] }, status: 'done', answer: 'mac', actor: 'adriel' }), tool_name: 'ask_operator', tool_ok: 1, ts: 2 },
      { role: 'user', content: 'mac', ts: 3 },
      { role: 'assistant', content: 'restarting mac', ts: 4 },
    ]);
    expect(v.map(t => t.role)).toEqual(['user', 'tower', 'user', 'tower']);
    expect(v[1].actions[0]).toMatchObject({ id: 'q1', tool: 'ask_operator', status: 'done', answer: 'mac', card: { question: 'Which host?', choices: ['box', 'mac'] } });
    expect(v[3].text).toBe('restarting mac');
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

describe('liveRun (#956)', () => {
  const act = (over = {}) => ({ role: 'action', tool_name: 'wake_server',
    content: JSON.stringify({ action_id: 'a1', run_id: 'r7', tool: 'wake_server', args: { host: 'box' }, card: {}, status: 'pending', expires: 4102444800, message: null, ...over }) });
  test('threadView carries the run id and liveRun finds a pending card that has not expired', () => {
    const turns = TW.threadView([{ role: 'user', content: 'wake box' }, act()]);
    expect(turns[1].actions[0].runId).toBe('r7');
    expect(TW.liveRun(turns, 1000)).toEqual({ runId: 'r7', status: 'pending' });
    expect(TW.liveRun(TW.threadView([{ role: 'user', content: 'wake box' }, act({ status: 'running' })]), 1000)).toEqual({ runId: 'r7', status: 'running' });
  });
  test('an expired, decided or run-less card gives nothing; only the last turn counts', () => {
    expect(TW.liveRun(TW.threadView([{ role: 'user', content: 'x' }, act({ expires: 1 })]), 1000)).toBeNull();
    expect(TW.liveRun(TW.threadView([{ role: 'user', content: 'x' }, act({ status: 'done' })]), 1000)).toBeNull();
    expect(TW.liveRun(TW.threadView([{ role: 'user', content: 'x' }, act({ run_id: null })]), 1000)).toBeNull();
    expect(TW.liveRun(TW.threadView([{ role: 'user', content: 'x' }, act(), { role: 'user', content: 'later' }]), 1000)).toBeNull();
    expect(TW.liveRun([], 1000)).toBeNull();
  });
});

describe('historyGroups (#987)', () => {
  const now = new Date(2026, 8, 14, 22, 30).getTime();                      // local Sep 14 2026 22:30
  const at = (y, m, d, h, mi) => new Date(y, m - 1, d, h, mi).getTime() / 1000;
  test('groups newest-first under Today / Yesterday / date headers with a time per row', () => {
    const threads = [{ id: 'a', title: 'older today', updated: at(2026, 9, 14, 9, 5) },
                     { id: 'b', title: 'latest', updated: at(2026, 9, 14, 21, 41) },
                     { id: 'c', title: 'yesterday', updated: at(2026, 9, 13, 8, 0) },
                     { id: 'd', title: 'last week', updated: at(2026, 9, 7, 12, 0) },
                     { id: 'e', title: 'last year', created: at(2025, 12, 31, 23, 59) },
                     { id: 'f', title: 'undated' }];
    const g = TW.historyGroups(threads, now);
    expect(g.map(x => x.label)).toEqual(['Today', 'Yesterday', new Date(2026, 8, 7).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }),
                                          new Date(2025, 11, 31).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }), 'Undated']);
    expect(g[0].rows.map(r => r.id)).toEqual(['b', 'a']);
    expect(g[0].rows[0].time).toBe(new Date(2026, 8, 14, 21, 41).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }));
    expect(g[4].rows[0]).toMatchObject({ id: 'f', title: 'undated', time: '' });
    expect(TW.historyGroups([], now)).toEqual([]);
  });
});

describe('insight snapshot + troubleshoot (#980)', () => {
  test('sparkline maps points into the box, draws the threshold and captions the range', () => {
    const snap = { metric: 'system/cpu_total', unit: '%', minutes: 60, points: [[0, 10], [30, 50], [60, 90]], threshold: 80 };
    const sp = TW.sparkline(snap, 240, 36);
    expect(sp.d).toBe('M2.0 34.0 L120.0 18.0 L238.0 2.0');
    expect(sp.thrY).toBe(6);
    expect(sp.caption).toBe('system/cpu_total · last 60 min · 10%–90% · threshold 80%');
    expect(TW.sparkline({ points: [[0, 1]] })).toBeNull();
    expect(TW.sparkline(null)).toBeNull();
    expect(TW.sparkline({ points: [[0, 'x'], [1, null], [2, 3]] })).toBeNull();
    const flat = TW.sparkline({ points: [[0, 5], [10, 5]], unit: 'C', minutes: 30 });
    expect(flat.thrY).toBeNull(); expect(flat.caption).toBe('metric · last 30 min · 5 C–5 C');
    expect(TW.sparkline({ points: [[0, 120.4], [1, 130.6]], unit: 'W' }).caption).toContain('120 W–131 W');
    expect(TW.insightView({ id: 'i', snapshot: snap }, {}, 1).snapshot).toBe(snap);
    expect(TW.insightView({ id: 'i', snapshot: { points: 'x' } }, {}, 1).snapshot).toBeNull();
  });
  test('troubleshoot title and prompt name the alert and carry the earlier read', () => {
    const r = { rule: 'GPU hot', host: 'box', alert_id: 'a1', summary: 'GPU at 91 C' };
    expect(TW.troubleshootTitle(r)).toBe('Troubleshoot: GPU hot · box');
    expect(TW.troubleshootTitle({})).toBe('Troubleshoot: Alert');
    const p = TW.troubleshootPrompt(r);
    expect(p).toMatch(/^Troubleshoot the alert "GPU hot" on box \(alert id a1\)\. Read the alert/);
    expect(p).toContain("Tower's earlier read: GPU at 91 C");
    expect(TW.troubleshootPrompt({ ...r, summary: 'Not diagnosed: asleep' })).not.toContain('earlier read');
    expect(TW.troubleshootPrompt({ rule: 'X' })).toContain('"X" (alert id ?)');
  });
});

describe('md ordered lists (#995)', () => {
  test('numbered lines become an ordered list, inline enumerations split one step per item', () => {
    expect(TW.md('Steps:\n1. Check RAM\n2. Restart it\n3) Done')).toBe('<p>Steps:</p><ol><li>Check RAM</li><li>Restart it</li><li>Done</li></ol>');
    expect(TW.md('3. third\n4. fourth')).toBe('<ol start="3"><li>third</li><li>fourth</li></ol>');
    const inline = TW.md('**Step-by-step fix:** 1. **Verify** the spike at 98.7% today. 2. Check for loads (use `models`). 3. Restart at 10:48.');
    expect(inline).toBe('<p><b>Step-by-step fix:</b></p><ol><li><b>Verify</b> the spike at 98.7% today.</li><li>Check for loads (use <code>models</code>).</li><li>Restart at 10:48.</li></ol>');
    expect(TW.md('1. only one')).toBe('<ol><li>only one</li></ol>');
    expect(TW.md('Value 98.7 exceeds 95.0 at step 2. Nothing else.')).toBe('<p>Value 98.7 exceeds 95.0 at step 2. Nothing else.</p>');
    expect(TW.md('Version 2. then 1. one 2. two')).toBe('<p>Version 2. then</p><ol><li>one</li><li>two</li></ol>');
  });
});

describe('history groups with Discord threads (#996)', () => {
  test('discord conversations follow the dated groups, one group per Discord user, newest first', () => {
    const now = Date.UTC(2026, 8, 15, 12, 0, 0);
    const groups = TW.historyGroups([{ id: 't1', title: 'Mine', updated: now / 1000 }], now,
      [{ id: 'd1', user: 'discord:111', title: 'why is box red?', updated: now / 1000 - 100 },
       { id: 'd2', user: 'discord:111', title: 'and now?', updated: now / 1000 - 10 },
       { id: 'd3', user: 'discord:222', title: 'models?', updated: now / 1000 - 5000 }]);
    expect(groups.map(g => g.label)).toEqual(['Today', 'Discord · 111', 'Discord · 222']);
    expect(groups[1].rows.map(r => r.id)).toEqual(['d2', 'd1']);
    expect(groups[1].rows[0].time).toMatch(/\d/);
    expect(TW.historyGroups([], now).length).toBe(0);
  });
});

describe('waiting status (#1016)', () => {
  test('a waiting status records the wait and any other event clears it', () => {
    let s = TW.initial();
    expect(s.wait).toBeNull();
    s = TW.reduce(s, { event: 'user', text: 'wake box' });
    s = TW.reduce(s, { event: 'status', state: 'waiting', name: 'host awake · box', elapsed_s: 10, timeout_s: 300 });
    expect(s.status).toBe('waiting');
    expect(s.wait).toEqual({ name: 'host awake · box', elapsed_s: 10, timeout_s: 300 });
    expect(TW.waitText(s.wait)).toBe('waiting for host awake · box · 10 s of 300');
    s = TW.reduce(s, { event: 'status', state: 'waiting', name: 'host awake · box', elapsed_s: 15, timeout_s: 300 });
    expect(s.wait.elapsed_s).toBe(15);
    s = TW.reduce(s, { event: 'status', state: 'tool', name: 'wait_until' });
    expect(s.wait).toBeNull();
    expect(TW.waitText(null)).toBe('');
    expect(TW.waitText({ name: '', elapsed_s: 3, timeout_s: 0 })).toBe('waiting for the result · 3 s');
  });
});

describe('help suggestions (#1018)', () => {
  test('the Overall list ends with the developer and help prompts', () => {
    const l = TW.suggestions({ tab: 'overall' });
    expect(l.slice(-2)).toEqual(['Who develops LLM Systems Manager?', 'How do I get help?']);
    expect(TW.HELP_SUGS).toEqual(['Who develops LLM Systems Manager?', 'How do I get help?']);
    expect(TW.suggestions({ tab: 'events' })).not.toContain('How do I get help?');
  });
});

describe('links in answers (#1024)', () => {
  test('plain URLs and e-mail addresses become links; code spans and bold are untouched', () => {
    const html = TW.md('Site https://www.llmsyscore.com, mail support@llmsyscore.com. Not `https://x.example/in-code` and **bold**.');
    expect(html).toContain('<a href="https://www.llmsyscore.com" target="_blank" rel="noopener">https://www.llmsyscore.com</a>,');
    expect(html).toContain('<a href="mailto:support@llmsyscore.com">support@llmsyscore.com</a>.');
    expect(html).toContain('<code>https://x.example/in-code</code>');
    expect(html).toContain('<b>bold</b>');
    expect(TW.md('- repo https://github.com/llmsyscore/llm-systems-manager/issues)')).toContain('href="https://github.com/llmsyscore/llm-systems-manager/issues"');
    expect(TW.md('<script>https://evil.example/x</script>')).not.toContain('<script>');
    const mixed = TW.md('See https://mail.example.com/reset?user=foo@bar.com&token=1 now');
    expect(mixed).toContain('<a href="https://mail.example.com/reset?user=foo@bar.com&amp;token=1" target="_blank" rel="noopener">');
    expect(mixed).not.toContain('mailto:');
  });
});

describe('timers (#1029)', () => {
  const live = { id: 'tm1', label: 'RAM on box', status: 'running', count: 3, times: 10, next_in_s: 42, left_s: 430, thread_id: 't1', run_id: null };
  test('timerLine counts down from the fetched view; reporting and short remainders read differently', () => {
    expect(TW.timerLine(live, 0)).toBe('3/10 · next in 42 s · 7 min left');
    expect(TW.timerLine(live, 30)).toBe('3/10 · next in 12 s · 7 min left');
    expect(TW.timerLine({ ...live, left_s: 80, next_in_s: 5 }, 10)).toBe('3/10 · next in 0 s · 70 s left');
    expect(TW.timerLine({ ...live, status: 'reporting', next_in_s: null, left_s: null }, 0)).toBe('3/10 · reporting…');
    expect(TW.timerLine(null, 0)).toBe('');
  });
  test('liveTimers keeps queued, running and reporting; finishedTimers is the live-to-done set with a run', () => {
    const rows = [live, { ...live, id: 'tm2', status: 'queued' }, { ...live, id: 'tm3', status: 'reporting' }, { ...live, id: 'tm4', status: 'done', run_id: 'r7' }, { ...live, id: 'tm5', status: 'cancelled' }];
    expect(TW.liveTimers(rows).map(t => t.id)).toEqual(['tm1', 'tm2', 'tm3']);
    const next = [{ ...live, status: 'done', run_id: 'r9' }, { ...live, id: 'tm2', status: 'failed', run_id: null }, { ...live, id: 'tm8', status: 'done', run_id: 'r1' }];
    expect(TW.finishedTimers(rows, next).map(t => t.id)).toEqual(['tm1']);
    expect(TW.finishedTimers([], next)).toEqual([]);
  });
  test('historyCharts finds the UI-only chart on single- and multi-host host_history results (#1043)', () => {
    const chart = { points: [[1000, 10], [1060, 30], [1120, 20]], unit: '%', metric: 'ram_pct', host: 'box', minutes: 2 };
    expect(TW.historyCharts({ min: 10, _chart: chart })).toEqual([chart]);
    expect(TW.historyCharts({ hosts: [{ _chart: chart }, { error: 'unknown host' }, { _chart: { points: [[1, 1]] } }] })).toEqual([chart]);
    expect(TW.historyCharts({ series: [] })).toEqual([]);
    expect(TW.historyCharts(null)).toEqual([]);
    const c = TW.historyChart(chart, 240, 60);
    expect(c.hi).toBe('30%'); expect(c.lo).toBe('10%');
    expect(c.peakPct).toEqual({ x: 50, y: 0 });
    expect(c.peak).toMatch(/^peak 30% at /);
    expect(c.caption).toBe('ram_pct · box · 10% → 20% · 10%–30%');
    expect(c.d.startsWith('M')).toBe(true);
    expect(TW.historyChart({ points: [[0, 1]] }, 240, 60)).toBeNull();
    const flat = TW.historyChart({ points: [[0, 5], [10, 5]], unit: 'W' }, 240, 60);
    expect(flat.peakPct.y).toBe(50);
    expect(flat.label).toBe('metric');
  });

  test('a failed-timer insight carries no alert to open (#1042)', () => {
    const v = TW.insightView({ id: 'i1', alert_id: 'timer:abc', rule: 'Timer failed', status: 'new', summary: 'Timer failed: x', created: 1500 }, { capabilities: 'read' }, 1600);
    expect(v.alertId).toBe('');
    expect(v.rule).toBe('Timer failed');
    expect(TW.insightView({ id: 'i2', alert_id: 'a9', status: 'new', created: 1500 }, { capabilities: 'read' }, 1600).alertId).toBe('a9');
  });

  test('timerSnapshot turns a timer result series into a sparkline snapshot', () => {
    const snap = TW.timerSnapshot({ label: 'RAM on box', metric: 'ram_pct', unit: '%', series: [[1000, 41], [1060, 42], [1120, 44]] });
    expect(snap).toEqual({ points: [[1000, 41], [1060, 42], [1120, 44]], unit: '%', metric: 'ram_pct', minutes: 2 });
    const sp = TW.sparkline(snap, 240, 36);
    expect(sp.caption).toBe('ram_pct · last 2 min · 41%–44%');
    expect(TW.timerSnapshot({ series: [[1, 2]] })).toBeNull();
    expect(TW.timerSnapshot({ ok: true })).toBeNull();
    expect(TW.timerSnapshot('nope')).toBeNull();
    expect(TW.timerSnapshot({ pick: 'ram.used_pct', series: [[0, 1], [60, 2]] }).metric).toBe('ram.used_pct');
  });
});

describe('timer rows in a stored thread (#1029)', () => {
  test('tickSummary names the timer, its tick count and a failure', () => {
    const v = TW.threadView([
      { role: 'user', content: 'poll it' },
      { role: 'tool', tool_name: 'schedule', tool_args: '{"label":"RAM on box","every_s":60}', tool_ok: 1, tool_ms: 2, content: '{"ok":true,"timer_id":"tm1"}' },
      { role: 'assistant', content: 'Scheduled.' },
      { role: 'tool', tool_name: 'timer', tool_args: '{"label":"RAM on box","timer_id":"tm1"}', tool_ok: 0, tool_ms: 0, content: '{"ok":false,"status":"cancelled","message":"cancelled by the operator","ticks":1,"label":"RAM on box"}' },
    ]);
    expect(v[1].ticks[0].summary).toBe('scheduled schedule · RAM on box · 2 ms');
    expect(v[1].ticks[1].summary).toBe('timer · RAM on box · 1 tick · cancelled by the operator');
    expect(v[1].ticks[1].ok).toBe(false);
  });
});

describe('checkChips (#1039)', () => {
  test('grades map to operator wording, class and tooltip', () => {
    expect(TW.checkChips(null)).toEqual([]);
    expect(TW.checkChips({ grade: 'pending', model: 'm' })).toEqual([{ text: 'Checking…', short: 'Checking…', cls: 'dim', title: 'Checking whether the model can call Tower’s tools' }]);
    expect(TW.checkChips({ grade: 'native', size_b: 27, small: false })).toEqual([{ text: 'Tools OK', short: 'Tools', cls: 'ok', title: 'Tool calls work: the model uses built-in function calling' }]);
    expect(TW.checkChips({ grade: 'fenced', size_b: 4, small: true })).toEqual([
      { text: 'Tools OK', short: 'Tools', cls: 'ok outline', title: 'Tool calls work: the model writes them in text prompt mode' },
      { text: 'small model', short: 'small', cls: 'warn', title: 'Small model: expect the occasional tool call written as text; Tower corrects it once per question (4B)' }]);
    expect(TW.checkChips({ grade: 'unknown', detail: 'error: GatewayError' })).toEqual([
      { text: 'Not checked', short: 'Not checked', cls: 'dim', title: 'The model did not answer the check (error: GatewayError); it runs again automatically' }]);
    expect(TW.checkChips({ grade: 'failed', size_b: null, small: false, detail: 'no call' })).toEqual([
      { text: 'No tool support', short: 'No tools', cls: 'crit', title: 'No tool support: the model made no tool call in either mode (no call)' }]);
  });
  test('stateView carries the check and the fallback', () => {
    expect(TW.stateView({ ok: true, enabled: true, model: 'm', check: { grade: 'native' } }).check).toEqual({ grade: 'native' });
    expect(TW.stateView({ ok: true, enabled: true, model: 'm' }).check).toBeNull();
    expect(TW.stateView({ ok: true, enabled: true, model: 'm', fallback: { model: 'g', check: { grade: 'failed' } } }).fallback.model).toBe('g');
    expect(TW.stateView({ ok: true, enabled: true, model: 'm' }).fallback).toBeNull();
  });
});

describe('conversation eval (#1047)', () => {
  const R = { id: 'e1', model: 'qwen3-14b', quant: 'Q4_K_M', server: 'llama.cpp b6400', at: 1000, ms: 48400,
              passed: 7, total: 8, calls_per_case: 1.4, corrections: 1, retries: 2, score_pct: 87.5 };
  test('evalSummary grades by score and lists the counters', () => {
    const v = TW.evalSummary(R, 1000 + 3600 * 3);
    expect(v.text).toBe('7/8');
    expect(v.cls).toBe('warn');
    expect(v.line).toBe('1.4 calls per question · 1 corrected · 2 retries · 48 s');
    expect(v.meta).toBe('Q4_K_M · llama.cpp b6400');
    expect(v.when).toBe('3 h');
    expect(v.short).toBe('qwen3-14b');
    expect(v.title).toBe('7/8 passed · qwen3-14b · Q4_K_M · llama.cpp b6400 · 3 h ago');
    expect(TW.evalSummary({ ...R, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M' }).short).toBe('Qwen3.8-27B-GGUF:Q4_K_M');
    expect(TW.evalSummary({ ...R, passed: 8, score_pct: 100, corrections: 0, retries: 1 }, 1010).cls).toBe('ok');
    expect(TW.evalSummary({ ...R, passed: 8, score_pct: 100, corrections: 0, retries: 1 }, 1010).line).toBe('1.4 calls per question · 1 retry · 48 s');
    expect(TW.evalSummary({ ...R, passed: 2, score_pct: 25 }).cls).toBe('crit');
    expect(TW.evalSummary({ ...R, quant: null, server: null }).meta).toBe('');
    expect(TW.evalSummary(null)).toBeNull();
  });
  test('evalProgress words each phase', () => {
    expect(TW.evalProgress({ status: 'queued', state: {} })).toBe('Queued…');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'download', pct: 40 } })).toBe('Downloading · 40 %…');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'load', waited_s: 25 } })).toBe('Loading · 25 s…');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'check' } })).toBe('Checking tool calls…');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'eval', case: 3, total: 8, title: 'Timer', passed: 2 } })).toBe('Question 3/8 · Timer · 2 passed so far');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'config' } })).toBe('Adding it to the host…');
    expect(TW.evalProgress({ status: 'running', state: { phase: 'restart', waited_s: 12 } })).toBe('Restarting llama.cpp · 12 s…');
    expect(TW.evalProgress(null)).toBe('');
  });
});
