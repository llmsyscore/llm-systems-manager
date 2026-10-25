// Demo handlers for the phone app: notification devices and the agent log on the Admin screen.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var DEVICES = [
    { endpoint: 'https://push.example/send/demo-phone-alex-7f3a9c', ago: 12 * 86400,
      ua: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1' },
    { endpoint: 'https://push.example/send/demo-phone-sam-21bd04', ago: 3 * 86400,
      ua: 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36' }
  ];

  function sec(ctx) { return Math.floor(ctx.now / 1000); }

  // One agent log line at time t (seconds).
  function logLine(m, t, n) {
    var ts = new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19);
    var msgs = ['pushed metrics to the manager', 'heartbeat ok', 'collected GPU and power readings', 'model list unchanged'];
    return ts + ' INFO llm-systems-agent[' + m.id + ']: ' + msgs[n % msgs.length];
  }

  R.route('GET /api/companion/push/public-key', function () {
    return { ok: true, key: 'BDemoPublicKeyNotUsedForRealPushMessages0000000000000000000000000000000000000000000000' };
  });
  R.route('GET /api/companion/push/subscriptions', function (ctx) {
    var t = sec(ctx);
    return { ok: true, count: DEVICES.length,
      endpoints: DEVICES.map(function (d) { return d.endpoint.slice(0, 48); }),
      devices: DEVICES.map(function (d) { return { endpoint: d.endpoint, ua: d.ua, created: t - d.ago }; }) };
  });

  R.route('GET /api/agents/:id/log/tail', function (ctx) {
    var m = F.byId(ctx.params.id) || F.byId('atlas');
    var t = sec(ctx), lines = [];
    for (var i = 19; i >= 0; i--) lines.push(logLine(m, t - i * 15, i));
    return { ok: true, lines: lines };
  });
  R.route('GET /api/agents/:id/log/stream', function (ctx) {
    var m = F.byId(ctx.params.id) || F.byId('atlas');
    return self.DemoSse.stream(function (nowMs, i) { return { data: logLine(m, Math.floor(nowMs / 1000), i) }; }, 5000);
  });
})(self);
