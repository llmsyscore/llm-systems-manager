// Types the tour's two questions into the Tower drawer (or the phone Tower screen) and waits for each answer.
(function (self) {
  if (self.DemoTourAsk) return;
  var doc = self.document;
  var QUESTIONS = ['What does each machine cost to run?', 'Show GPU temperature on forge.example over the last day'];
  var SURFACES = {
    desk: { neu: 'twNew', input: 'twInput', send: 'twSend', body: 'twBody' },
    phone: { neu: 'towerNew', input: 'towerInput', send: 'towerSend', body: 'towerBody' }
  };

  function track(n) {
    if (typeof self.DemoTrack === 'function') self.DemoTrack('click', { target: 'tour-ask-' + n, href: '' });
  }
  // Polls cond every step ms until it is true or max ms pass; resolves to whether it became true.
  function until(cond, max, step) {
    return new Promise(function (resolve) {
      var left = max;
      (function poll() {
        var ok = false;
        try { ok = !!cond(); } catch (e) { ok = false; }
        if (ok) return resolve(true);
        if (left <= 0) return resolve(false);
        left -= step;
        self.setTimeout(poll, step);
      })();
    });
  }
  function el(id) { return doc.getElementById(id); }
  function idle(surface) {
    if (surface === 'phone') { var stop = el('towerStop'); return !stop || stop.hidden === true; }
    var send = el('twSend'), aside = el('towerAside');
    return !(send && send.classList.contains('stop')) && !(aside && aside.classList.contains('streaming'));
  }
  function answers(s) { var b = el(s.body); return b ? b.querySelectorAll('.t .ans').length : 0; }

  // Resolves true when the element next gets child changes, false after max ms.
  function repaint(node, trigger, max) {
    return new Promise(function (resolve) {
      var done = false, mo = null;
      function end(ok) { if (done) return; done = true; if (mo) mo.disconnect(); resolve(ok); }
      if (!node || typeof self.MutationObserver !== 'function') { trigger(); return end(false); }
      mo = new self.MutationObserver(function () { end(true); });
      mo.observe(node, { childList: true });
      self.setTimeout(function () { end(false); }, max);
      trigger();
    });
  }

  var active = false;
  function run(surface, isCurrent) {
    if (active) return Promise.resolve(0);
    active = true;
    var phone = surface === 'phone';
    var s = SURFACES[phone ? 'phone' : 'desk'];
    var sent = 0;
    function ok() { return !!isCurrent(); }
    function waitIdle() { return until(function () { return idle(surface); }, 15000, 200); }
    function ask(i) {
      if (i >= QUESTIONS.length) return Promise.resolve();
      return waitIdle().then(function (free) {
        var input = el(s.input), send = el(s.send);
        if (!free || !ok() || !input || !send || input.disabled) return;
        var before = answers(s);
        self.DemoTourAsking = true;
        input.value = QUESTIONS[i];
        input.dispatchEvent(new self.Event('input', { bubbles: true }));
        send.click();
        sent += 1;
        return until(function () { return answers(s) > before && idle(surface); }, 15000, 200).then(function (done) {
          if (!done) return;
          track(i + 1);
          return ask(i + 1);
        });
      });
    }
    return Promise.resolve().then(function () {
      if (phone) return until(function () { var i = el('towerInput'); return i && !i.disabled; }, 3000, 100);
      return until(function () { var o = el('towerOverlay'); return o && o.classList.contains('open'); }, 3000, 100);
    }).then(function (ready) {
      if (!ready || !ok()) return;
      return waitIdle().then(function (free) {
        if (!free || !ok()) return;
        var nb = el(s.neu), bd = el(s.body);
        if (!bd || !bd.querySelector('.t')) return ask(0);
        return repaint(el(s.body), function () { if (nb) nb.click(); }, 3000).then(function (painted) {
          if (painted) return ask(0);
        });
      });
    }).catch(function () {}).then(function () {
      self.DemoTourAsking = false;
      active = false;
      return sent;
    });
  }

  self.DemoTourAsk = { QUESTIONS: QUESTIONS, run: run };
})(self);
