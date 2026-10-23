// DemoSse: an endless text/event-stream Response fed by a generator on a timer.
(function (self) {
  function frame(ev) {
    var data = typeof ev.data === 'string' ? ev.data : JSON.stringify(ev.data);
    return (ev.event ? 'event: ' + ev.event + '\n' : '') + 'data: ' + data + '\n\n';
  }

  self.DemoSse = {
    stream: function (gen, everyMs) {
      var enc = new TextEncoder();
      var timer = null;
      var i = 0;
      var logged = false;
      var body = new ReadableStream({
        start: function (ctl) {
          // Skips the tick while the reader is behind; a throwing generator sends nothing that tick.
          function tick() {
            if (ctl.desiredSize === null || ctl.desiredSize <= 0) return;
            var ev;
            try { ev = gen(Date.now(), i++); } catch (e) {
              if (!logged && self.console) { logged = true; self.console.error('demo stream error', e && e.message); }
              return;
            }
            if (ev) {
              try { ctl.enqueue(enc.encode(frame(ev))); } catch (e) { clearInterval(timer); }
            }
          }
          tick();
          timer = setInterval(tick, everyMs);
        },
        cancel: function () { clearInterval(timer); },
      });
      return new Response(body, { status: 200, headers: {
        'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } });
    },
  };
})(self);
