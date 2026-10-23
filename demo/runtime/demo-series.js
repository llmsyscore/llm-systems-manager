// DemoSeries: deterministic, cheap time-based sample values per machine and metric.
(function (self) {
  var SLOT = 1800, RAMP = 30, TAU = 2 * Math.PI;

  function hash(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0);
  }
  function unit(str) { return hash(str) / 4294967296; }
  function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

  function machine(id) { return self.DemoFleet && self.DemoFleet.byId(id); }
  function vramTotal(m) {
    var s = 0;
    if (!m) return s;
    for (var i = 0; i < m.gpus.length; i++) s += m.gpus[i].vramGb;
    return s;
  }

  function mulberry(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var x = Math.imul(a ^ (a >>> 15), 1 | a);
      x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
      return (((x ^ (x >>> 14)) >>> 0) / 4294967296);
    };
  }

  // Hour of day (0-24) in the viewer's time zone.
  function localHour(t) { var d = new Date(t * 1000); return d.getHours() + d.getMinutes() / 60; }

  // Gap stretch by local hour of day: quiet overnight, busiest from 9:00 to 23:00.
  function gapScale(h) {
    if (h < 7) return 5;
    if (h < 9) return 5 - 2 * (h - 7);
    if (h < 23) return 1;
    return 1 + 4 * (h - 23);
  }

  var slotCache = {}, slotCount = 0;
  function cached(key, make) {
    if (slotCache[key]) return slotCache[key];
    if (++slotCount > 2000) { slotCache = {}; slotCount = 1; }
    return (slotCache[key] = make());
  }

  // Bursts [start, end) starting in one half-hour slot: 2-15 min bursts after 1-20 min gaps; the last may run past the slot.
  function rawBursts(id, slot) {
    return cached('r|' + id + '|' + slot, function () {
      var r = mulberry(hash(id + '|' + slot + '|burst')), start = slot * SLOT, end = start + SLOT, out = [];
      var t = start + Math.floor(r() * 600);
      while (t < end) {
        var gap = Math.floor((60 + r() * 1140) * gapScale(localHour(t)));
        var len = Math.floor(120 + r() * 780);
        var s0 = t + gap;
        if (s0 >= end) break;
        out.push([s0, s0 + len]);
        t = s0 + len;
      }
      return out;
    });
  }

  // A slot's bursts without the ones starting within a minute of the previous slot's last burst end.
  function bursts(id, slot) {
    return cached('b|' + id + '|' + slot, function () {
      var prev = rawBursts(id, slot - 1), last = prev.length ? prev[prev.length - 1][1] : -Infinity;
      return rawBursts(id, slot).filter(function (b) { return b[0] >= last + 60; });
    });
  }

  function burstAt(id, t) {
    var slot = Math.floor(t / SLOT);
    for (var k = slot; k >= slot - 1; k--) {
      var list = bursts(id, k);
      for (var i = 0; i < list.length; i++) if (t >= list[i][0] && t < list[i][1]) return list[i];
    }
    return null;
  }

  // 0..1 busy level with 30 s ramps at each end of a burst.
  function level(id, t) {
    var b = burstAt(id, t);
    return b ? clamp01(Math.min((t - b[0]) / RAMP, (b[1] - t) / RAMP)) : 0;
  }

  // 0..1 wobble: two sines plus per-10-s hashed jitter, linearly interpolated.
  function wobble(id, metric, t) {
    var k = id + '|' + metric;
    var s = 0.5 + 0.25 * Math.sin(TAU * t / 97 + unit(k + '|a') * TAU) +
                  0.25 * Math.sin(TAU * t / 41 + unit(k + '|b') * TAU);
    var g = Math.floor(t / 10), f = t / 10 - g;
    var j = unit(k + '|' + g) * (1 - f) + unit(k + '|' + (g + 1)) * f;
    return clamp01(0.85 * s + 0.15 * j);
  }

  function ranges(id, metric) {
    var m = machine(id);
    if (!m) return null;
    var total = vramTotal(m);
    switch (metric) {
      case 'tok_s': return { min: 0, max: 140 };
      case 'gpu_util': return total ? { min: 2, max: 99 } : { min: 0, max: 0 };
      case 'vram_used_gb': return { min: 0, max: total };
      case 'power_w': return { min: m.powerW.idle, max: m.powerW.load };
      case 'cpu_pct': return { min: 3, max: 60 };
      case 'temp_c': return m.tempC ? { min: m.tempC.min, max: m.tempC.max } : { min: 38, max: 82 };
    }
    return null;
  }

  function value(id, metric, t) {
    var r = ranges(id, metric);
    if (!r) return 0;
    if (typeof t !== 'number' || !isFinite(t)) return r.min;
    var lv = level(id, t), w = wobble(id, metric, t), v;
    switch (metric) {
      case 'tok_s': v = lv * (35 + 105 * w); break;
      case 'gpu_util': v = r.max === 0 ? 0 : (1 - lv) * (2 + 6 * w) + lv * (70 + 29 * w); break;
      case 'vram_used_gb': v = r.max * (0.3 + 0.25 * lv + 0.1 * w * (0.4 + lv)); break;
      case 'power_w': v = r.min + (r.max - r.min) * clamp01(0.05 * w + lv * (0.55 + 0.4 * w)); break;
      case 'cpu_pct': v = 3 + 57 * clamp01(0.08 * w + lv * (0.35 + 0.5 * w)); break;
      case 'temp_c':
        var tc = machine(id).tempC || {}, hot = tc.busy == null ? 0.45 : tc.busy;
        var hw = tc.busyWobble == null ? 0.3 : tc.busyWobble;
        v = r.min + (r.max - r.min) * clamp01(0.12 + 0.1 * w + lv * (hot + hw * w)); break;
    }
    return Math.min(r.max, Math.max(r.min, v));
  }

  self.DemoSeries = {
    ranges: ranges,
    value: value,
    busy: function (id, t) { return !!burstAt(id, Math.floor(t)); },
    range: function (id, metric, fromSec, toSec, maxPoints) {
      var n = Math.max(1, Math.floor(maxPoints || 300));
      var from = Math.floor(fromSec), to = Math.floor(toSec);
      if (!isFinite(from) || !isFinite(to) || to < from) return [];
      if (n === 1) return [[to, value(id, metric, to)]];
      var step = Math.max(1, Math.ceil((to - from) / (n - 1)));
      var out = [];
      for (var t = Math.ceil(from / step) * step; t < to; t += step) out.push([t, value(id, metric, t)]);
      out.push([to, value(id, metric, to)]);
      return out;
    }
  };
})(self);
