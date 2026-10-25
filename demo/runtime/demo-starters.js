// Starter questions for Tower: a "Try asking" panel shown while a conversation is empty.
(function (self) {
  if (self.DemoStarters) return;
  var doc = self.document;
  var STYLE_ID = 'demoStartersStyle';
  var PANEL_CLASS = 'demo-starters';

  // Each question maps to a canned answer in handlers/tower.js (key = topic).
  var QUESTIONS = [
    { key: 'slow', text: 'Which machine is slow right now?' },
    { key: 'whatis', text: 'What can LLM Systems Manager do?' },
    { key: 'trendtemp', text: 'Show GPU temperature on forge.example over the last day' },
    { key: 'trendpower', text: 'Chart power use over time' },
    { key: 'alarms', text: 'How do alarms work?' },
    { key: 'watch', text: 'Watch the forge GPU temperature' },
    { key: 'alarm', text: 'What alarms are open right now?' },
    { key: 'autotune', text: 'How does autotune work?' },
    { key: 'cost', text: 'What does each machine cost to run?' },
    { key: 'phone', text: 'Is there a phone app?' }
  ];

  // The dashboard drawer and the phone app each have a message list, input and send button.
  var SURFACES = [
    { body: 'twBody', input: 'twInput', send: 'twSend', empty: '.empty .fu [data-sug]', host: '.empty' },
    { body: 'towerBody', input: 'towerInput', send: 'towerSend', empty: '.chips.sugs [data-sug]', host: null, blocked: '.muted-note' }
  ];

  function track(key) {
    if (typeof self.DemoTrack === 'function') self.DemoTrack('click', { target: 'starter-' + key, href: '' });
  }

  function addStyle() {
    if (doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement('style');
    s.id = STYLE_ID;
    s.textContent = '.demo-starters{margin:14px 0 4px;padding:12px;border:1px solid var(--border-strong,#3a4150);border-radius:10px;background:var(--bg-card,#161a22);color:var(--fg,#e6e9ef)}'
      + '.demo-starters h4{margin:0 0 8px;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--accent,#5b9dff)}'
      + '.demo-starters .ds-list{display:flex;flex-direction:column;gap:6px}'
      + '.demo-starters button{text-align:left;font:inherit;font-size:13px;line-height:1.35;padding:8px 10px;border-radius:8px;cursor:pointer;color:var(--fg,#e6e9ef);background:transparent;border:1px solid var(--border-strong,#3a4150)}'
      + '.demo-starters button:hover,.demo-starters button:focus-visible{border-color:var(--accent,#5b9dff);outline:none}';
    (doc.head || doc.documentElement).appendChild(s);
  }

  function ask(surface, q, panel) {
    var input = doc.getElementById(surface.input);
    var send = doc.getElementById(surface.send);
    if (!input || !send || input.disabled) return;
    input.value = q.text;
    input.dispatchEvent(new self.Event('input', { bubbles: true }));
    track(q.key);
    if (panel.parentNode) panel.parentNode.removeChild(panel);
    send.click();
  }

  function build(surface) {
    var panel = doc.createElement('div');
    panel.className = PANEL_CLASS;
    var h = doc.createElement('h4');
    h.textContent = 'Try asking';
    var list = doc.createElement('div');
    list.className = 'ds-list';
    QUESTIONS.forEach(function (q) {
      var b = doc.createElement('button');
      b.type = 'button';
      b.textContent = q.text;
      b.setAttribute('data-starter', q.key);
      b.addEventListener('click', function () { ask(surface, q, panel); });
      list.appendChild(b);
    });
    panel.appendChild(h);
    panel.appendChild(list);
    return panel;
  }

  // Adds the panel next to the empty-state suggestions of a surface, once per paint.
  function refresh() {
    SURFACES.forEach(function (s) {
      var body = doc.getElementById(s.body);
      if (!body) return;
      var has = body.querySelector('.' + PANEL_CLASS);
      var sug = body.querySelector(s.empty);
      var input = doc.getElementById(s.input);
      if (!sug || (input && input.disabled) || (s.blocked && body.querySelector(s.blocked))) {
        if (has && has.parentNode) has.parentNode.removeChild(has);
        return;
      }
      if (has) return;
      addStyle();
      var host = s.host ? body.querySelector(s.host) : body;
      if (host) host.appendChild(build(s));
    });
  }

  var timer = null;
  function schedule() {
    if (timer) return;
    timer = self.setTimeout(function () { timer = null; refresh(); }, 60);
  }

  function start() {
    refresh();
    if (typeof self.MutationObserver !== 'function' || !doc.body) return;
    new self.MutationObserver(schedule).observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  }

  if (doc) {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start);
    else start();
  }

  self.DemoStarters = { QUESTIONS: QUESTIONS, refresh: refresh };
})(self);
