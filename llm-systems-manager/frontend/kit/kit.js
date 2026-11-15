// Panel kit 1.0.0
// Shared panel kit: HTML renderers (panel shell, KPI, time series, timeline, ranked list, split bar, funnel, map, feed) and their helpers, using only colour tokens.
var KIT_RANGE_DAYS = { 'today': 0, '1': 1, '3': 3, '5': 5, '7': 7, '14': 14, '30': 30, '90': 90, '365': 365 };
var KIT_SERIES_OFF = {};
var KIT_MENU_ICON = '⋯';

// Kit settings: store key prefix, optional store in place of localStorage, extra double-click skip selector, and app hooks.
var KIT_CONF = { prefix: 'kit', store: null, dblSkip: '', onLegend: null, onRefit: null, holderRows: null };
// Sets prefix (non-empty string), store (getItem and setItem, or null), dblSkip (string or null) and the hooks onLegend(panel, chartId),
// onRefit(root) and holderRows(box) (function or null) in KIT_CONF; returns KIT_CONF.
function kitConfigure(opts) {
  opts = opts || {};
  if (typeof opts.prefix === 'string' && opts.prefix) KIT_CONF.prefix = opts.prefix;
  if (kitOwn(opts, 'store')) {
    var s = opts.store;
    if (s === null || (s && typeof s.getItem === 'function' && typeof s.setItem === 'function')) KIT_CONF.store = s;
  }
  if (kitOwn(opts, 'dblSkip') && (opts.dblSkip === null || typeof opts.dblSkip === 'string')) {
    var sel = opts.dblSkip || '', ok = true;
    // A selector the document cannot parse is logged and ignored, keeping the current one.
    if (sel && typeof document !== 'undefined') {
      try { document.querySelector(sel); } catch (e) { ok = false; if (typeof console !== 'undefined') console.error('kit dblSkip invalid', sel); }
    }
    if (ok) KIT_CONF.dblSkip = sel;
  }
  ['onLegend', 'onRefit', 'holderRows'].forEach(function(k) {
    if (kitOwn(opts, k) && (opts[k] === null || typeof opts[k] === 'function')) KIT_CONF[k] = opts[k];
  });
  return KIT_CONF;
}
// A store key under the kit prefix: prefix.name.
function kitKey(name) { return KIT_CONF.prefix + '.' + name; }
// The configured store, else the document's localStorage, or null where reading it throws.
function kitStore(doc) {
  if (KIT_CONF.store) return KIT_CONF.store;
  try { return doc.defaultView.localStorage; } catch (e) { return null; }
}
// Saved theme from a store: light only when the theme key holds light; dark otherwise, also for a missing or throwing store.
function kitTheme(storage) {
  try { return storage.getItem(kitKey('theme')) === 'light' ? 'light' : 'dark'; } catch (e) { return 'dark'; }
}
// Saved sidebar state from a store: collapsed only when the nav key holds collapsed; pinned otherwise, also for a missing or throwing store.
function kitNav(storage) {
  try { return storage.getItem(kitKey('nav')) === 'collapsed' ? 'collapsed' : 'pinned'; } catch (e) { return 'pinned'; }
}
// Saved Live refresh state from a store: false only when the live key holds off; true otherwise, also for a missing or throwing store.
function kitLive(storage) {
  try { return storage.getItem(kitKey('live')) !== 'off'; } catch (e) { return true; }
}
function kitEsc(v) {
  return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, function(c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
// Short number: '–' for none, an integer under 1000, then 1.23k / 12.3k.
function kitFmt(n) {
  if (n === null || n === undefined || n === '' || isNaN(+n)) return '–';
  n = +n;
  var a = Math.abs(n);
  if (a >= 10000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  if (a >= 1000) return (n / 1000).toFixed(2).replace(/0$/, '').replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}
// Chart grain for a range: day up to 90 days (named range or a YYYY-MM-DD start before today), else month.
function grainFor(range, today) {
  var r = String(range);
  if (r === 'all') return 'month';
  if (Object.prototype.hasOwnProperty.call(KIT_RANGE_DAYS, r)) return KIT_RANGE_DAYS[r] <= 90 ? 'day' : 'month';
  if (/^\d{4}-\d{2}-\d{2}$/.test(r)) {
    var days = (Date.parse(String(today) + 'T00:00:00Z') - Date.parse(r + 'T00:00:00Z')) / 86400000;
    if (isNaN(days)) return 'day';
    return days <= 90 ? 'day' : 'month';
  }
  return 'day';
}
function kitSum(a) {
  var t = 0;
  for (var i = 0; i < a.length; i++) t += +a[i] || 0;
  return t;
}
// Rounds a chart's top up to 1, 2, 5 or 10 times a power of ten; at least 4.
function kitNiceMax(m) {
  if (m <= 4) return 4;
  var e = Math.pow(10, Math.floor(Math.log(m) / Math.LN10)), f = m / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
}
// Every day (YYYY-MM-DD) or month (YYYY-MM) from fromDay through toDay, at most 4000 keys.
function kitTimeKeys(fromDay, toDay, grain) {
  var out = [], a = String(fromDay), b = String(toDay);
  if (a > b) return out;
  if (grain === 'month') {
    var y = +a.slice(0, 4), m = +a.slice(5, 7), end = b.slice(0, 7);
    if (isNaN(y) || isNaN(m)) return out;
    while (out.length < 4000) {
      var k = y + '-' + (m < 10 ? '0' : '') + m;
      if (k > end) break;
      out.push(k);
      if (++m > 12) { m = 1; y++; }
    }
    return out;
  }
  var t = Date.parse(a + 'T00:00:00Z'), last = Date.parse(b + 'T00:00:00Z');
  if (isNaN(t) || isNaN(last)) return out;
  for (; t <= last && out.length < 4000; t += 86400000) out.push(new Date(t).toISOString().slice(0, 10));
  return out;
}
// The chart bucket of a day: the day itself, or its YYYY-MM for month grain.
function kitBucket(day, grain) {
  return grain === 'month' ? String(day).slice(0, 7) : String(day);
}
// Series colour slot 1..5; a missing or bad slot follows the series order.
function kitSlotN(slot, i) {
  var s = +slot;
  return s >= 1 && s <= 5 ? Math.floor(s) : (i % 5) + 1;
}
function kitSlot(slot, i) { return 'var(--s' + kitSlotN(slot, i) + ')'; }
// Series colour: var(--bad) for color bad, otherwise its slot.
function kitSeriesFill(s, i) { return s.color === 'bad' ? 'var(--bad)' : kitSlot(s.slot, i); }
function kitN(v) { return String(Math.round(v * 10) / 10); }
// A table; columns after the first whose cells are all number-like ('', '—' and '–' aside), with at least one number, are right-aligned.
// Real numbers show through kitFmt and keep the raw value in data-v for Copy data; a {text, v} cell shows text and keeps raw v.
var KIT_NUMLIKE = /^[+\-\u2212]?\d[\d.,]*%?$/;
function kitCellObj(c) { return c !== null && typeof c === 'object'; }
function kitTable(cols, rows) {
  var right = cols.map(function(c, i) {
    var any = false;
    return i > 0 && rows.length > 0 && rows.every(function(r) {
      var v = kitCellObj(r[i]) ? r[i].v : r[i];
      if (v === null || v === undefined || v === '' || v === '—' || v === '–') return true;
      any = true;
      return typeof v === 'number' || KIT_NUMLIKE.test(String(v));
    }) && any;
  });
  var head = cols.map(function(c, i) { return '<th' + (right[i] ? ' class="num"' : '') + '>' + kitEsc(c) + '</th>'; }).join('');
  var body = rows.map(function(r) {
    return '<tr>' + r.map(function(c, i) {
      var cell = kitCellObj(c), raw = cell ? c.v : c, num = typeof raw === 'number';
      return '<td' + (right[i] ? ' class="num"' : '') + (num ? ' data-v="' + kitEsc(raw) + '"' : '') + '>' + kitEsc(cell ? c.text : num ? kitFmt(c) : c) + '</td>';
    }).join('') + '</tr>';
  }).join('');
  return '<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>';
}

// CSV of rows: text starting with = + - @ tab or CR gets a leading ' so spreadsheets do not run it; numbers stay as they are.
function kitCsv(rows) {
  return rows.map(function(r) {
    return r.map(function(c) {
      if (typeof c === 'number') return String(c);
      var v = String(c === null || c === undefined ? '' : c);
      if (/^[=+\-@\t\r]/.test(v)) v = "'" + v;
      return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(',');
  }).join('\n');
}

// Panel header: optional drag grip, title, note, the options menu (Show as table with opts.table, Show as chart with opts.bars) and the full-size close button.
function panelHead(id, title, opts) {
  opts = opts || {};
  var menu = (opts.table ? '<button type="button" role="menuitem" data-kit-do="table">Show as table</button>' : '') +
    (opts.bars ? '<button type="button" role="menuitem" data-kit-do="bars">Show as chart</button>' : '') +
    '<button type="button" role="menuitem" data-kit-do="copy">Copy data</button>' +
    '<button type="button" role="menuitem" data-kit-do="full">Open full size</button>';
  return '<header class="p-hd">' + (opts.grip ? '<span class="grip" title="Drag to move">⠿</span>' : '') +
    '<h3 class="p-title">' + kitEsc(title) + '</h3>' +
    (opts.note ? '<span class="p-note">' + kitEsc(opts.note) + '</span>' : '') +
    '<button type="button" class="p-menu" data-kit-menu="' + kitEsc(id) + '" aria-label="' + kitEsc('Panel options for ' + title) + '" aria-haspopup="true" aria-expanded="false">' + KIT_MENU_ICON + '</button>' +
    '<button type="button" class="p-close" data-kit-close aria-label="' + kitEsc('Close full size: ' + title) + '" title="Close full size">✕</button>' +
    '<div class="p-pop" role="menu" hidden>' + menu + '</div></header>';
}
// Panel shell: header with title, note and options menu, then the body; a table option adds a hidden table beside it.
function panelHtml(id, title, body, opts) {
  opts = opts || {};
  var t = opts.table;
  return '<section class="panel' + (opts.cls ? ' ' + kitEsc(opts.cls) : '') + '" data-kit-panel="' + kitEsc(id) + '">' +
    panelHead(id, title, { table: t, note: opts.note }) +
    '<div class="p-body">' + body + '</div>' +
    (t ? '<div class="p-table" hidden>' + kitTable(t.cols || [], t.rows || []) + '</div>' : '') +
    '</section>';
}

// KPI body: big value, change arrow (a good or bad tone adds that class), sub line and an optional sparkline with an end dot.
// A text value over 12 characters gets the smaller one-line style and its full text as a tooltip.
function kpiBody(o) {
  var val = typeof o.value === 'number' ? kitFmt(o.value) : (o.value === null || o.value === undefined ? '–' : o.value);
  var long = typeof o.value === 'string' && o.value.length > 12;
  var d = o.delta, body = '<div class="k-row"><span class="k-val' + (long ? ' long" title="' + kitEsc(val) : '') + '">' + kitEsc(val) + '</span>';
  if (d) body += '<span class="k-delta ' + (d.dir === 'up' || d.dir === 'down' ? d.dir : 'flat') + (d.tone === 'good' || d.tone === 'bad' ? ' ' + d.tone : '') + '">' + (d.dir === 'up' ? '↗ ' : d.dir === 'down' ? '↘ ' : '→ ') + kitEsc(d.text) + '</span>';
  body += '</div><div class="k-sub">' + kitEsc(o.sub || '') + '</div>';
  var sp = o.spark;
  if (sp && sp.length) {
    var w = 300, ht = 44, mx = Math.max.apply(null, [1].concat(sp.map(function(v) { return +v || 0; })));
    var pts = sp.map(function(v, i) { return [sp.length > 1 ? (i / (sp.length - 1)) * w : w, ht - 2 - ((+v || 0) / mx) * (ht - 6)]; });
    var line = pts.map(function(q, i) { return (i ? 'L' : 'M') + kitN(q[0]) + ' ' + kitN(q[1]); }).join('');
    var last = pts[pts.length - 1];
    body += '<div class="k-spark"><svg viewBox="0 0 ' + w + ' ' + ht + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path d="' + line + 'L' + w + ' ' + ht + 'L0 ' + ht + 'Z" fill="var(--s1)" fill-opacity=".14"/>' +
      '<path d="' + line + '" fill="none" stroke="var(--s1)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>' +
      '<i class="k-end" style="left:calc(' + kitN((last[0] / w) * 100) + '% - 3px);top:' + kitN(last[1] - 3) + 'px"></i></div>';
  }
  return body;
}
function kpiHtml(o) {
  return panelHtml(o.id, o.title, kpiBody(o), { cls: 'kpi' });
}

// Time series: line or stacked bars in an o.w-wide viewBox (default 600), gridlines at 0, half and the top, a legend of series toggles.
// o.marks ({day, label, release}) on a shown day draw dashed lines; data-kit-ts holds the options for the hover and for kitRefit.
function tsHtml(o) {
  var id = o.id, labels = o.labels || [], series = o.series || [], stack = o.mode === 'stack';
  var legend = '<div class="legend">' + series.map(function(s, i) {
    var key = id + '|' + s.name, off = !!KIT_SERIES_OFF[key];
    return '<button type="button" class="chip' + (off ? ' off' : '') + '" data-kit-series="' + kitEsc(key) + '" aria-pressed="' + (off ? 'false' : 'true') + '">' +
      '<i style="background:' + kitSeriesFill(s, i) + '"></i>' + kitEsc(s.name) + ' <span class="c-v">' + kitFmt(kitSum(s.values || [])) + '</span>' +
      (off ? ' <span class="c-off">hidden</span>' : '') + '</button>';
  }).join('') + '</div>';
  var n = labels.length;
  if (!n) return legend + '<div class="empty">Nothing in this period.</div>';
  var W = +o.w > 0 ? Math.round(+o.w) : 600, H = +o.height || 220, L = 34, R = 8, T = 10, B = 22, iw = W - L - R, ih = H - T - B, step = iw / n;
  var vis = [];
  series.forEach(function(s, i) { if (!KIT_SERIES_OFF[id + '|' + s.name]) vis.push({ name: s.name, values: s.values || [], slot: kitSlotN(s.slot, i), fill: kitSeriesFill(s, i) }); });
  var top = 1;
  for (var i = 0; i < n; i++) {
    var t = 0;
    for (var k = 0; k < vis.length; k++) { var v = +vis[k].values[i] || 0; t = stack ? t + v : Math.max(t, v); }
    top = Math.max(top, t);
  }
  var low = 0;
  if (!stack) vis.forEach(function(s) { for (var c = 0; c < n; c++) low = Math.min(low, +s.values[c] || 0); });
  // Lines with values below zero get a floor of their own nice step below zero; otherwise the axis starts at 0.
  var ymax = kitNiceMax(top), ylo = low < 0 ? -kitNiceMax(-low) : 0;
  function y(v) { return T + ih - ((v - ylo) / (ymax - ylo)) * ih; }
  function x(j) { return L + step * (j + 0.5); }
  var g = '';
  (ylo < 0 ? [ylo, 0, ymax] : [0, ymax / 2, ymax]).forEach(function(v) {
    var yy = kitN(y(v));
    g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + yy + '" y2="' + yy + '" stroke="var(--grid)" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>' +
      '<text x="' + (L - 6) + '" y="' + kitN(+yy + 3.5) + '" text-anchor="end" class="ax">' + kitFmt(v) + '</text>';
  });
  // At most 7 day labels, and at most one per 90px of width (at least 2).
  var every = Math.max(Math.ceil(n / 7), Math.ceil(n / Math.max(2, Math.floor(W / 90))));
  for (var j = 0; j < n; j += every) g += '<text x="' + kitN(x(j)) + '" y="' + (H - 6) + '" text-anchor="middle" class="ax ax-x">' + kitEsc(labels[j]) + '</text>';
  var marks = '';
  if (stack) {
    var bw = Math.max(3, Math.min(28, step * 0.62));
    for (var b = 0; b < n; b++) {
      var acc = 0, lastK = -1;
      for (var q = 0; q < vis.length; q++) if (+vis[q].values[b]) lastK = q;
      for (var m = 0; m < vis.length; m++) {
        var val = +vis[m].values[b] || 0;
        if (!val) continue;
        var y0 = y(acc) - (acc ? 2 : 0), y1 = y(acc + val), hgt = Math.max(1, y0 - y1), x0 = x(b) - bw / 2;
        acc += val;
        if (m === lastK) {
          var r = Math.min(3, bw / 2, hgt);
          marks += '<path class="ts-bar" d="M' + kitN(x0) + ' ' + kitN(y0) + 'V' + kitN(y0 - hgt + r) + 'q0 -' + kitN(r) + ' ' + kitN(r) + ' -' + kitN(r) +
            'h' + kitN(bw - 2 * r) + 'q' + kitN(r) + ' 0 ' + kitN(r) + ' ' + kitN(r) + 'V' + kitN(y0) + 'Z" fill="' + vis[m].fill + '"/>';
        } else {
          marks += '<rect class="ts-bar" x="' + kitN(x0) + '" y="' + kitN(y0 - hgt) + '" width="' + kitN(bw) + '" height="' + kitN(hgt) + '" fill="' + vis[m].fill + '"/>';
        }
      }
    }
  } else {
    vis.forEach(function(s) {
      var d = '';
      for (var p = 0; p < n; p++) d += (p ? 'L' : 'M') + kitN(x(p)) + ' ' + kitN(y(+s.values[p] || 0));
      marks += '<path d="' + d + '" fill="none" stroke="' + s.fill + '" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' +
        '<circle cx="' + kitN(x(n - 1)) + '" cy="' + kitN(y(+s.values[n - 1] || 0)) + '" r="3" fill="' + s.fill + '" stroke="var(--card)" stroke-width="2"/>';
    });
  }
  var inRange = (o.marks || []).filter(function(mk) { return /^\d{4}-\d{2}-\d{2}$/.test(String(mk.day)) && labels.indexOf(mk.day) >= 0; }), mkLines = '';
  inRange.forEach(function(mk) {
    var mx = kitN(x(labels.indexOf(mk.day)));
    mkLines += '<line class="mk" x1="' + mx + '" x2="' + mx + '" y1="' + T + '" y2="' + (T + ih) + '" stroke="' + (mk.release ? 'var(--muted)' : 'var(--c5)') +
      '" stroke-width="1.5" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>';
  });
  var hover = { id: id, labels: labels, mode: stack ? 'stack' : 'line', height: H, w: W, l: L, r: R,
    series: series.map(function(s, i) { return { name: s.name, values: s.values || [], slot: kitSlotN(s.slot, i), color: s.color === 'bad' ? 'bad' : undefined }; }),
    marks: inRange.map(function(mk) { return { day: mk.day, label: String(mk.label || ''), release: !!mk.release }; }) };
  return legend + '<div class="ts" data-kit-ts="' + kitEsc(JSON.stringify(hover)) + '">' +
    '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="' + kitEsc(series.map(function(s) { return s.name; }).join(', ')) + '">' + g + marks + mkLines +
    '<line class="xh" x1="0" x2="0" y1="' + T + '" y2="' + (T + ih) + '" stroke="var(--text-2)" stroke-width="1" vector-effect="non-scaling-stroke" visibility="hidden"/></svg>' +
    '<div class="tip" hidden></div></div>';
}

// Minutes into the day of an HH:MM text, or null when it is not a time from 00:00 to 24:00.
function kitLaneMins(at) {
  var m = /^(\d{1,2}):(\d{2})$/.exec(String(at)), v = m ? +m[1] * 60 + +m[2] : -1;
  return v >= 0 && v <= 1440 ? v : null;
}
// One state's shape for a timeline mark: passed a filled circle, failed a cross, skipped a dash, anything else a hollow ring.
// passed, failed and skipped are the kit's lane-state vocabulary.
function kitLaneShape(state, x, y) {
  var cx = kitN(x), cy = kitN(y), l = kitN(x - 4), r = kitN(x + 4), t = kitN(y - 4), b = kitN(y + 4);
  if (state === 'passed') return '<circle cx="' + cx + '" cy="' + cy + '" r="4" fill="var(--good)"/>';
  if (state === 'failed') return '<path d="M' + l + ' ' + t + 'L' + r + ' ' + b + 'M' + r + ' ' + t + 'L' + l + ' ' + b + '" fill="none" stroke="var(--bad)" stroke-width="2.4" stroke-linecap="round"/>';
  if (state === 'skipped') return '<line x1="' + l + '" x2="' + r + '" y1="' + cy + '" y2="' + cy + '" stroke="var(--muted)" stroke-width="2" stroke-linecap="round"/>';
  return '<circle cx="' + cx + '" cy="' + cy + '" r="4" fill="none" stroke="var(--muted)" stroke-width="1.5"/>';
}
// Day timeline: one lane per name across 24 hours in an o.w-wide viewBox (default 600), a now line and a mark per run.
// Marks with an unknown lane or time are left out; o.empty and o.label set the empty text and aria label; data-kit-lanes holds the options for kitRefit.
function lanesHtml(o) {
  o = o || {};
  var lanes = o.lanes || [], marks = (o.marks || []).filter(function(m) { return lanes.indexOf(m.lane) >= 0 && kitLaneMins(m.at) !== null; });
  var empty = o.empty === undefined || o.empty === null ? 'Nothing in this period.' : o.empty, label = o.label === undefined || o.label === null ? 'Timeline by hour' : o.label;
  if (!marks.length) return '<div class="empty">' + kitEsc(empty) + '</div>';
  var W = Math.max(120, +o.w > 0 ? Math.round(+o.w) : 600), ML = 86, MR = 10, LH = 21, MB = 20, pw = W - ML - MR, ih = lanes.length * LH, H = ih + MB;
  function x(mins) { return ML + (pw * mins) / 1440; }
  var g = '', step = [3, 6, 12, 24].filter(function(n) { return (pw * n) / 24 >= 90; })[0] || 48;
  for (var h = 0; h <= 24; h += step) {
    var hx = kitN(x(h * 60));
    g += '<line x1="' + hx + '" x2="' + hx + '" y1="0" y2="' + ih + '" stroke="var(--grid)" vector-effect="non-scaling-stroke"/>' +
      '<text x="' + hx + '" y="' + (H - 5) + '" text-anchor="' + (h === 24 ? 'end' : h === 0 ? 'start' : 'middle') + '" class="ax ax-x">' + (h < 10 ? '0' : '') + h + ':00</text>';
  }
  lanes.forEach(function(name, i) {
    var text = String(name), long = text.length > 13;
    g += '<text x="0" y="' + kitN(i * LH + LH / 2 + 4) + '" font-size="12" fill="var(--text-2)">' + kitEsc(long ? text.slice(0, 12) + '…' : text) + (long ? '<title>' + kitEsc(text) + '</title>' : '') + '</text>';
  });
  var nowAt = o.now === null || o.now === undefined ? null : kitLaneMins(o.now);
  if (nowAt !== null) g += '<line class="now-l" x1="' + kitN(x(nowAt)) + '" x2="' + kitN(x(nowAt)) + '" y1="0" y2="' + ih + '" stroke="var(--accent)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>';
  marks.forEach(function(m) {
    var cx = x(kitLaneMins(m.at)), cy = lanes.indexOf(m.lane) * LH + LH / 2, tip = String(m.tip === null || m.tip === undefined ? '' : m.tip);
    g += '<g class="lm" role="button" tabindex="0" data-mark="' + kitEsc(m.key) + '" data-tip="' + kitEsc(tip) + '" aria-label="' + kitEsc(tip) + '">' +
      '<circle class="hit" cx="' + kitN(cx) + '" cy="' + kitN(cy) + '" r="9" fill="none" pointer-events="all"/>' + kitLaneShape(m.state, cx, cy) + '</g>';
  });
  var opts = { id: o.id, w: W, now: o.now === undefined ? null : o.now, lanes: lanes, marks: marks, empty: empty, label: label };
  return '<div class="ts lanes" data-kit-lanes="' + kitEsc(JSON.stringify(opts)) + '">' +
    '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="group" aria-label="' + kitEsc(label) + '">' + g + '</svg><div class="tip" hidden></div></div>';
}

var KIT_ICONS = { only: 'M2 3h12l-4.5 5.2V13l-3-1.5V8.2z', out: 'M8 1.8a6.2 6.2 0 1 0 0 12.4A6.2 6.2 0 0 0 8 1.8zM3.6 3.6l8.8 8.8', copy: 'M5 5h8v8H5zM3 11V3h8' };
var KIT_ACT_WORDS = { only: 'Only show', out: 'Leave out', copy: 'Copy' };
// Ranked list of the top n rows with bars and row actions; a row's filter value is row.val, else its label.
// Only show and Leave out appear only when o.key names a filter; Copy always does.
function rankHtml(rows, o) {
  o = o || {};
  rows = rows || [];
  var n = o.n || 8, list = rows.slice(0, n);
  if (!list.length) return '<div class="empty">Nothing in this period.</div>';
  var mx = Math.max.apply(null, [0].concat(list.map(function(r) { return +r.v || 0; })));
  var items = list.map(function(r) {
    var val = r.val === undefined || r.val === null ? r.label : r.val;
    var acts = (o.key ? ['only', 'out', 'copy'] : ['copy']).map(function(a) {
      return '<button type="button" data-kit-act="' + a + '" data-key="' + kitEsc(o.key) + '" data-val="' + kitEsc(val) + '" aria-label="' + kitEsc(KIT_ACT_WORDS[a] + ' ' + r.label) + '" title="' + KIT_ACT_WORDS[a] + '">' +
        '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="' + KIT_ICONS[a] + '" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg></button>';
    }).join('');
    return '<li><span class="r-lab" title="' + kitEsc(r.label) + '">' + kitEsc(r.label) + '</span>' +
      '<span class="r-bar"><i style="width:' + kitN(mx ? ((+r.v || 0) / mx) * 100 : 0) + '%"></i></span>' +
      '<span class="r-val">' + kitFmt(r.v) + '</span><span class="r-act">' + acts + '</span></li>';
  }).join('');
  var out = '<ol class="rank"' + (o.dim ? ' aria-label="' + kitEsc(o.dim) + '"' : '') + '>' + items + '</ol>';
  if (rows.length > n) {
    var rest = kitSum(rows.slice(n).map(function(r) { return r.v; }));
    out += '<div class="r-more">' + (rows.length - n) + ' more · ' + kitFmt(rest) + (o.unit ? ' ' + kitEsc(o.unit) : '') + '</div>';
  }
  return out;
}

// Split bar: the top 4 rows and Other as one 10px bar, then chips with value and share.
// With o.slots ({label: 1..4}) only those labels keep their own slot and colour; every other label folds into Other.
function splitHtml(rows, o) {
  o = o || {};
  rows = rows || [];
  var top, rest, slots = o.slots;
  if (slots) {
    top = [];
    rest = 0;
    rows.forEach(function(r) {
      var s = Object.prototype.hasOwnProperty.call(slots, r.label) ? +slots[r.label] : 0;
      if (s >= 1 && s <= 4) top.push({ label: r.label, v: +r.v || 0, s: Math.floor(s) }); else rest += +r.v || 0;
    });
  } else {
    top = rows.slice(0, 4).map(function(r, i) { return { label: r.label, v: +r.v || 0, s: i + 1 }; });
    rest = kitSum(rows.slice(4).map(function(r) { return r.v; }));
  }
  if (rest) top.push({ label: 'Other', v: rest, s: 5 });
  var t = kitSum(top.map(function(r) { return r.v; }));
  if (!t) return '<div class="empty">Nothing in this period.</div>';
  var unit = o.unit ? ' ' + o.unit : '';
  var bar = top.map(function(r) {
    return '<i style="flex:' + r.v + ';background:var(--s' + r.s + ')" title="' + kitEsc(r.label + ': ' + kitFmt(r.v) + unit) + '"></i>';
  }).join('');
  var legend = top.map(function(r) {
    return '<span class="chip static"><i style="background:var(--s' + r.s + ')"></i>' + kitEsc(r.label) +
      ' <span class="c-v">' + kitFmt(r.v) + '</span> <span class="c-pc">' + Math.round((r.v / t) * 100) + '%</span></span>';
  }).join('');
  return '<div class="sbar" aria-hidden="true">' + bar + '</div><div class="legend stacked">' + legend + '</div>';
}

// Funnel: one bar per step; each step after the first shows its share of the step above when that is 100% or less.
function funnelHtml(steps) {
  steps = steps || [];
  var mx = Math.max.apply(null, [1].concat(steps.map(function(s) { return +s.v || 0; })));
  return '<ol class="fun">' + steps.map(function(s, i) {
    var prev = i ? +steps[i - 1].v || 0 : 0, v = +s.v || 0, r = prev ? v / prev : null;
    var pc = r !== null && r <= 1 ? Math.round(r * 100) + '% of the step above' : '';
    return '<li><span class="f-lab">' + kitEsc(s.label) + '</span><span class="f-bar"><i style="width:' + kitN((v / mx) * 100) + '%"></i></span>' +
      '<span class="f-val">' + kitFmt(v) + '</span><span class="f-pc">' + pc + '</span></li>';
  }).join('') + '</ol>';
}

var KIT_DOTS = null;
// WORLD_DOTS parsed once into dot centres grouped by country index.
function kitDots() {
  if (KIT_DOTS || typeof WORLD_DOTS === 'undefined') return KIT_DOTS;
  var by = {}, st = WORLD_DOTS.step, list = WORLD_DOTS.dots.split(';');
  for (var i = 0; i < list.length; i++) {
    if (!list[i]) continue;
    var p = list[i].split(','), c = p[2];
    (by[c] || (by[c] = [])).push(kitN((+p[0] + 0.5) * st) + '" cy="' + kitN((+p[1] + 0.5) * st));
  }
  KIT_DOTS = by;
  return by;
}
function kitCircles(pts) {
  return pts.map(function(xy) { return '<circle cx="' + xy + '" r="2.1"/>'; }).join('');
}
// Dot map: countries with visits use --s1, brighter with more; the ranked list of countries sits beside it.
function mapHtml(rows, names, o) {
  rows = rows || [];
  names = names || {};
  o = o || {};
  var vals = {}, mx = 0;
  rows.forEach(function(r) { var v = +r.v || 0; vals[r.code] = (vals[r.code] || 0) + v; });
  Object.keys(vals).forEach(function(c) { mx = Math.max(mx, vals[c]); });
  var svg = '';
  var dots = kitDots();
  if (dots) {
    var base = '', hot = '';
    WORLD_DOTS.codes.forEach(function(code, i) {
      var pts = dots[i];
      if (!pts) return;
      var v = vals[code] || 0;
      if (v > 0) hot += '<g fill="var(--s1)" fill-opacity="' + (Math.round((0.35 + 0.65 * Math.sqrt(v / mx)) * 100) / 100) + '">' + kitCircles(pts) + '</g>';
      else base += kitCircles(pts);
    });
    svg = '<svg class="map-cv" viewBox="0 0 ' + WORLD_DOTS.w + ' ' + WORLD_DOTS.h + '" width="100%" role="img" aria-label="Dot map of countries">' +
      '<g fill="var(--map-dot)">' + base + '</g>' + hot + '</svg>';
  }
  var named = rows.filter(function(r) { return +r.v > 0; }).map(function(r) { return { label: names[r.code] || r.code, v: +r.v, val: r.code }; })
    .sort(function(a, b) { return b.v - a.v; });
  return '<div class="map">' + svg + '<div class="map-list">' + rankHtml(named, { dim: 'Country', key: o.key, n: 7 }) + '</div></div>';
}

// Activity feed: kind, title with detail, and how long ago (hours under two days, then days).
function feedHtml(rows, now) {
  rows = rows || [];
  if (!rows.length) return '<div class="empty">Nothing in this period.</div>';
  var nowMs = typeof now === 'number' ? now : Date.parse(now);
  return '<ul class="fd">' + rows.map(function(r) {
    var at = typeof r.at === 'number' ? r.at : Date.parse(r.at);
    var hrs = Math.max(1, Math.round((nowMs - at) / 3600000));
    var ago = isNaN(hrs) ? '' : hrs < 48 ? hrs + ' h ago' : Math.round(hrs / 24) + ' d ago';
    return '<li><span class="fd-k">' + kitEsc(r.kind) + '</span><span class="fd-t">' + kitEsc(r.title) + '<small>' + kitEsc(r.detail) + '</small></span>' +
      '<span class="fd-a">' + ago + '</span></li>';
  }).join('') + '</ul>';
}

function kitOwn(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
// The object saved under a store key; {} for a missing, junk or throwing store.
function kitSavedObj(storage, key) {
  try {
    var o = JSON.parse(storage.getItem(key) || '{}');
    return o && typeof o === 'object' && !Array.isArray(o) ? o : {};
  } catch (e) { return {}; }
}
// One page's object inside a saved object; {} when it has none or holds junk.
function kitPageObj(all, page) {
  var o = kitOwn(all, page) ? all[page] : null;
  return o && typeof o === 'object' && !Array.isArray(o) ? o : {};
}
function kitSaveObj(storage, key, o) {
  try { storage.setItem(key, JSON.stringify(o)); } catch (e) {}
}

// Page helpers: panel filling, chart refit, toast, copy, table readers, menus and panel actions.
// Adds the kit header (title, note, menu, and a grip inside a card box) to every static panel with data-title that has none yet.
function kitDress(root) {
  root = root || document;
  kitBoxKeys(root);
  Array.prototype.forEach.call(root.querySelectorAll('section.panel[data-title]'), function(p) {
    if (p.querySelector('.p-hd')) return;
    p.insertAdjacentHTML('afterbegin', panelHead(p.getAttribute('data-card') || '', p.getAttribute('data-title'),
      { grip: !!(p.getAttribute('data-card') && p.closest && p.closest('[data-cards]')), table: p.hasAttribute('data-kit-table'), bars: !!p.querySelector('table[data-kit-bars]'), note: p.getAttribute('data-note') || '' }));
  });
}
// Fills a panel body; a {cols, rows} table also goes into the panel's hidden .p-table for Show as table and Copy data.
function kitFill(bodyId, html, table) {
  var body = document.getElementById(bodyId);
  if (!body) return;
  body.innerHTML = html;
  kitSetTable(bodyId, table);
}
// Puts a {cols, rows} table in the hidden .p-table of the panel holding a body, adding one after the body when it has none.
function kitSetTable(bodyId, table) {
  var body = document.getElementById(bodyId);
  if (!body || !table) return;
  var panel = body.closest ? body.closest('.panel') : null;
  if (!panel) return;
  var holder = panel.querySelector('.p-table');
  if (!holder) {
    holder = document.createElement('div');
    holder.className = 'p-table'; holder.hidden = true;
    body.parentNode.insertBefore(holder, body.nextSibling);
  }
  holder.innerHTML = kitTable(table.cols || [], table.rows || []);
}
// As kitSetTable, but a table with no rows leaves the .p-table empty, so the panel's own sentence stands alone.
function kitSetRows(bodyId, table) {
  kitSetTable(bodyId, table);
  var body = document.getElementById(bodyId), panel = body && body.closest ? body.closest('.panel') : null, holder = panel && panel.querySelector('.p-table');
  if (holder && table && !(table.rows || []).length) holder.innerHTML = '';
}
// Sets the header note of the panel holding a body, adding the note after the title when it has none.
function kitNote(bodyId, text) {
  var body = document.getElementById(bodyId), panel = body && body.closest ? body.closest('.panel') : null, hd = panel && panel.querySelector('.p-hd');
  if (!hd) return;
  var note = hd.querySelector('.p-note'), title = hd.querySelector('.p-title');
  if (!note && title) { title.insertAdjacentHTML('afterend', '<span class="p-note"></span>'); note = hd.querySelector('.p-note'); }
  if (note) note.textContent = text;
}
// Drawing width of a chart body in px; 600 while the body is hidden or not laid out.
function kitWidth(bodyId) {
  var el = document.getElementById(bodyId), w = el ? +el.clientWidth || 0 : 0;
  return w > 0 ? w : 600;
}
// Redraws each kit chart (legend and svg) and timeline under root at its body's current width; other body content stays.
// Then calls KIT_CONF.onRefit(root) with the same root (a panel, a row or the document) when it is set.
function kitRefit(root) {
  if (!root || !root.querySelectorAll) return;
  Array.prototype.forEach.call(root.querySelectorAll('.ts[data-kit-ts]'), function(ts) {
    var body = ts.parentNode, w = body ? Math.round(+body.clientWidth || 0) : 0, o;
    if (w <= 0) return;
    try { o = JSON.parse(ts.getAttribute('data-kit-ts')); } catch (e) { return; }
    if (!o || !o.series || o.w === w) return;
    kitRedrawTs(ts, o, w);
  });
  Array.prototype.forEach.call(root.querySelectorAll('.lanes[data-kit-lanes]'), function(el) {
    var body = el.parentNode, w = body ? Math.round(+body.clientWidth || 0) : 0, o;
    if (w <= 0) return;
    try { o = JSON.parse(el.getAttribute('data-kit-lanes')); } catch (e) { return; }
    if (!o || !o.lanes || o.w === w) return;
    if (el === kitTsOn) kitTsOn = null;
    o.w = w;
    el.insertAdjacentHTML('beforebegin', lanesHtml(o));
    body.removeChild(el);
  });
  if (KIT_CONF.onRefit) { try { KIT_CONF.onRefit(root); } catch (e) { if (typeof console !== 'undefined') console.error('kit hook onRefit failed', e); } }
}
// Replaces one chart and its legend with a fresh draw of its options at width w.
function kitRedrawTs(ts, o, w) {
  var body = ts.parentNode;
  if (ts === kitTsOn) kitTsOn = null;
  var leg = ts.previousElementSibling;
  if (leg && leg.classList && leg.classList.contains('legend')) body.removeChild(leg);
  ts.insertAdjacentHTML('beforebegin', tsHtml({ id: o.id, labels: o.labels, series: o.series, mode: o.mode, height: o.height, w: w, marks: o.marks }));
  body.removeChild(ts);
}
// Sets the header title of the panel holding a body.
function kitTitle(bodyId, text) {
  var body = document.getElementById(bodyId), panel = body && body.closest ? body.closest('.panel') : null, title = panel && panel.querySelector('.p-title');
  if (title) title.textContent = text;
}
var kitToastTimer = null;
function kitToast(text) {
  var t = document.getElementById('toast');
  if (!t) return;
  t.textContent = text;
  clearTimeout(kitToastTimer);
  kitToastTimer = setTimeout(function() { t.textContent = ''; }, 2200);
}
// Copies through a selected hidden textarea, the only way on a plain http page; returns whether it worked.
function kitCopyBySelection(text) {
  var ta = null, active = document.activeElement, ok = false;
  try {
    ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    ok = document.execCommand('copy');
  } catch (e) {}
  if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
  if (active && active.focus) active.focus();
  return ok;
}
function kitCopy(text, done) {
  function fallback() { kitToast(kitCopyBySelection(text) ? done : 'Could not copy'); }
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() { kitToast(done); }, fallback);
      return;
    }
  } catch (e) {}
  fallback();
}
// Cells of one row for kitCsv: raw numbers from data-v, else the cell text.
function kitRowCells(tr) {
  return Array.prototype.filter.call(tr.children, function(c) { return !(c.classList && c.classList.contains('no-copy')); })
    .map(function(c) { return c.hasAttribute('data-v') ? +c.getAttribute('data-v') : c.textContent; });
}
function kitTableRows(table) {
  return Array.prototype.map.call(table.querySelectorAll('tr'), kitRowCells);
}
function kitCloseMenus(except) {
  Array.prototype.forEach.call(document.querySelectorAll('.p-pop'), function(p) {
    if (p === except || p.hidden) return;
    p.hidden = true;
    var b = p.parentNode && p.parentNode.querySelector('[data-kit-menu]');
    if (b) b.setAttribute('aria-expanded', 'false');
  });
}
function kitPanelDo(panel, item) {
  var what = item.getAttribute('data-kit-do');
  if (what === 'table') {
    var body = panel.querySelector('.p-body'), tbl = panel.querySelector('.p-table'), show = tbl && tbl.hidden;
    if (!tbl) return;
    tbl.hidden = !show;
    if (body) body.hidden = show;
    item.textContent = show ? 'Show as chart' : 'Show as table';
    if (!show) kitRefit(panel);
  } else if (what === 'bars') {
    var on = !kitBarsShown(panel);
    kitShowBars(panel, on);
    var page = kitPageOf(panel), card = panel.getAttribute('data-card');
    if (page !== null && page !== '__proto__' && card && card !== '__proto__') {
      var storage = kitStore(document), all = kitSavedObj(storage, kitKey('view')), mine = kitPageObj(all, page);
      if (on) mine[card] = 'chart'; else delete mine[card];
      if (Object.keys(mine).length) all[page] = mine; else delete all[page];
      kitSaveObj(storage, kitKey('view'), all);
    }
  } else if (what === 'copy') {
    var t = panel.querySelector('.p-table table') || panel.querySelector('.p-body table');
    if (t && t.closest && t.closest('[data-kit-nocopy]')) t = null;
    // Copies the rows holderRows(the table's parent) returns (header first) when it gives an array, else the rendered table.
    var box = t ? null : panel.querySelector('[data-kit-text]'), all = null;
    if (t && KIT_CONF.holderRows) {
      try { all = KIT_CONF.holderRows(t.parentNode); } catch (e) { all = null; if (typeof console !== 'undefined') console.error('kit hook holderRows failed', e); }
    }
    if (Array.isArray(all)) kitCopy(kitCsv(all), 'Copied as CSV');
    else if (t) kitCopy(kitCsv(kitTableRows(t)), 'Copied as CSV');
    else if (box) kitCopy(box.textContent, 'Copied');
    else kitToast('This panel has no data to copy');
  } else if (what === 'full') {
    kitSetFull(panel, !panel.classList.contains('full'));
  }
}
// Opens or closes a panel full size and sets its menu item text to match; its charts scale through their viewBox.
// Then calls KIT_CONF.onRefit(panel) when it is set.
function kitSetFull(panel, on) {
  var fb = panel.querySelector('button[data-kit-do="full"]');
  if (on) panel.classList.add('full'); else panel.classList.remove('full');
  if (fb) fb.textContent = on ? 'Close full size' : 'Open full size';
  if (KIT_CONF.onRefit) { try { KIT_CONF.onRefit(panel); } catch (e) { if (typeof console !== 'undefined') console.error('kit hook onRefit failed', e); } }
}
var KIT_DBL_SKIP = 'button, a, input, select, textarea, label, summary, [role="link"], [role="button"], table[data-kit-sortable] th, .grip, .p-pop';
// Elements a double-click does not open full size from: KIT_DBL_SKIP plus KIT_CONF.dblSkip.
function kitDblSkip() { return KIT_DBL_SKIP + (KIT_CONF.dblSkip ? ', ' + KIT_CONF.dblSkip : ''); }
// Hides the crosshair and tooltip of the hovered chart.
var kitTsOn = null;
function kitTsOff() {
  if (!kitTsOn) return;
  var xh = kitTsOn.querySelector('.xh'), tp = kitTsOn.querySelector('.tip');
  if (xh) xh.setAttribute('visibility', 'hidden');
  if (tp) tp.hidden = true;
  kitTsOn = null;
}
// Hides the crosshair when the pointer leaves the window or the window loses focus.
function kitTsLeave(ev) { if (!ev || !ev.relatedTarget) kitTsOff(); }
// Shows a timeline mark's tip in the chart's tooltip; off a mark it hides the tooltip.
function kitLaneTip(box, ev) {
  var mark = ev.target.closest('[data-mark]'), tp = box.querySelector('.tip'), svg = box.querySelector('svg');
  if (!mark || !tp || !svg) { kitTsOff(); return; }
  kitTsOn = box;
  tp.textContent = mark.getAttribute('data-tip');
  tp.hidden = false;
  var rc = svg.getBoundingClientRect(), px = ev.clientX - rc.left, left = px + 12;
  if (left + tp.offsetWidth > rc.width) left = px - 12 - tp.offsetWidth;
  tp.style.left = Math.max(0, left) + 'px';
}
// Each page's default row order and per-box panel order, as first seen in the page.
var KIT_LAYOUT = Object.create(null);
// The rows under root that share the first row's parent.
function kitRowEls(root) {
  var rows = Array.prototype.slice.call(root.querySelectorAll('.row[data-row]')).filter(function(r) { return r.getAttribute('data-row'); });
  return rows.filter(function(r) { return r.parentNode === rows[0].parentNode; });
}
function kitCardEls(box) {
  return Array.prototype.filter.call(box.children, function(c) { return c.hasAttribute('data-card') && c.getAttribute('data-card'); });
}
function kitCardIds(box) { return kitCardEls(box).map(function(c) { return c.getAttribute('data-card'); }); }
// Records the defaults of a page's rows and boxes; ids new to the page join at their place, gone ones drop.
function kitDefaults(page, root) {
  var d = KIT_LAYOUT[page] || (KIT_LAYOUT[page] = { rows: null, cards: Object.create(null) });
  d.rows = kitReconcile(kitRowEls(root).map(function(r) { return r.getAttribute('data-row'); }), d.rows);
  Array.prototype.forEach.call(root.querySelectorAll('[data-cards]'), function(box) {
    var key = box.getAttribute('data-cards');
    d.cards[key] = kitReconcile(kitCardIds(box), d.cards[key]);
  });
  return d;
}
// Puts the given children in the id order, in the places they hold now; a parent's other children stay where they are.
function kitPlace(els, attr, ids) {
  if (!els.length) return;
  var byId = Object.create(null), parent = els[0].parentNode, i = 0;
  els.forEach(function(e) { byId[e.getAttribute(attr)] = e; });
  var queue = ids.filter(function(id) { return byId[id]; }).map(function(id) { return byId[id]; });
  if (queue.length !== els.length || queue.every(function(e, n) { return e === els[n]; })) return;
  Array.prototype.map.call(parent.children, function(c) { return els.indexOf(c) >= 0 ? queue[i++] : c; }).forEach(function(c) { parent.appendChild(c); });
}
// Draws a row folded or open: its class, its button's state and its chevron.
function kitRowDraw(row, folded) {
  row.classList.toggle('folded', folded);
  var b = row.querySelector('button[data-row-fold]');
  if (!b) return;
  if (!b.querySelector('.chev')) b.insertAdjacentHTML('afterbegin', '<span class="chev" aria-hidden="true"></span>');
  var ch = b.querySelector('.chev');
  b.setAttribute('aria-expanded', folded ? 'false' : 'true');
  if (ch) ch.textContent = folded ? '▸' : '▾';
}
// Known saved ids in saved order, then each new id after the last placed id that precedes it by default.
function kitReconcile(defaultIds, savedIds) {
  var out = [];
  (Array.isArray(savedIds) ? savedIds : []).forEach(function(id) { if (typeof id === 'string' && defaultIds.indexOf(id) >= 0 && out.indexOf(id) < 0) out.push(id); });
  defaultIds.forEach(function(id, i) {
    if (out.indexOf(id) >= 0) return;
    var at = 0;
    defaultIds.slice(0, i).forEach(function(p) { at = Math.max(at, out.indexOf(p) + 1); });
    out.splice(at, 0, id);
  });
  return out;
}
// Whether a row with no saved fold starts folded.
function kitRowFoldsByDefault(row) {
  return row.hasAttribute('data-folded') || row.hasAttribute('data-fold-default');
}
// Applies a page's saved folds (the rows key), row order (the rowOrder key) and panel order (the order key) under root.
// Each key holds {page: ...}; root is marked with the page for later fold and drag saves.
function kitLayout(page, root, storage) {
  if (!root || !root.querySelectorAll) return;
  root.setAttribute('data-kit-page', page);
  kitBoxKeys(root);
  kitRowDress(root);
  KIT_STORES[page] = storage;
  var d = kitDefaults(page, root), folds = kitPageObj(kitSavedObj(storage, kitKey('rows')), page), rowOrder = kitSavedObj(storage, kitKey('rowOrder')), cards = kitPageObj(kitSavedObj(storage, kitKey('order')), page);
  kitPlace(kitRowEls(root), 'data-row', kitReconcile(d.rows, kitOwn(rowOrder, page) ? rowOrder[page] : null));
  Array.prototype.forEach.call(root.querySelectorAll('[data-cards]'), function(box) {
    var key = box.getAttribute('data-cards');
    kitPlace(kitCardEls(box), 'data-card', kitReconcile(d.cards[key], kitOwn(cards, key) ? cards[key] : null));
  });
  Array.prototype.forEach.call(root.querySelectorAll('.row[data-row]'), function(row) {
    var id = row.getAttribute('data-row'), v = kitOwn(folds, id) ? folds[id] : null;
    kitRowDraw(row, v === 1 || v === 0 ? v === 1 : kitRowFoldsByDefault(row));
  });
  kitRowMenus(root);
  Array.prototype.forEach.call(root.querySelectorAll('table'), function(t) {
    if (!t.closest('[data-kit-nosort]') && kitSortPanel(t)) t.setAttribute('data-kit-sortable', '');
    var s = !t.closest('[data-kit-nosort]') && kitSortOf(t, storage);
    if (s) kitSortTable(t, s.col, s.dir);
  });
  var views = kitPageObj(kitSavedObj(storage, kitKey('view')), page);
  kitBarPanels(root).forEach(function(p) {
    var card = p.getAttribute('data-card');
    if (card && kitOwn(views, card) && views[card] === 'chart') kitShowBars(p, true);
  });
}
// The panels under root holding a bars table.
function kitBarPanels(root) {
  return Array.prototype.map.call(root.querySelectorAll('table[data-kit-bars]'), function(t) { return t.closest('.panel'); }).filter(Boolean);
}
// Each page's store, as passed to kitLayout, for sorts applied outside a click.
var KIT_STORES = Object.create(null);
// A cell's sort key: {n} from data-v, a duration (1h 05m, 12m 03s, 45s) or a leading signed number, else {s} lower-cased; null when empty.
function kitSortKey(cell) {
  if (cell.hasAttribute('data-v') && cell.getAttribute('data-v') !== '' && !isNaN(+cell.getAttribute('data-v'))) return { n: +cell.getAttribute('data-v') };
  var t = String(cell.textContent || '').trim();
  if (!t || t === '–' || t === '—' || t === '-') return null;
  var d = t.match(/^(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$/);
  if (d && (d[1] || d[2] || d[3])) return { n: (+d[1] || 0) * 3600 + (+d[2] || 0) * 60 + (+d[3] || 0) };
  var m = t.replace(/,/g, '').match(/^([-+]?\d+(?:\.\d+)?)(?:%|\s.*)?$/);
  return m ? { n: +m[1] } : { s: t.toLowerCase() };
}
function kitSortCmp(a, b) {
  if ('n' in a && 'n' in b) return a.n - b.n;
  if ('n' in a) return -1;
  if ('n' in b) return 1;
  return a.s < b.s ? -1 : a.s > b.s ? 1 : 0;
}
// The header cells of a table's first header row.
function kitHeads(table) {
  var tr = kitHeadRow(table);
  return tr ? Array.prototype.slice.call(tr.children) : [];
}
function kitHeadRow(table) {
  return table.querySelector('thead tr') || Array.prototype.filter.call(table.querySelectorAll('tr'), function(r) { return r.querySelector('th'); })[0] || null;
}
function kitHeadLabel(th) { return String(th.getAttribute('data-kit-col') || th.textContent || '').trim(); }
// The element holding a table's body rows (its tbody, else the table) and those rows.
function kitBody(table) { return table.querySelector('tbody') || table; }
function kitBodyRows(table) {
  var body = kitBody(table), head = kitHeadRow(table), kids = Array.prototype.slice.call(body.children || []).filter(function(r) { return r !== head; });
  return body === table ? kids.filter(function(r) { return String(r.tagName || r.tag || '').toUpperCase() === 'TR'; }) : kids;
}
// Whether a row is a group heading: fewer cells than the header, in a body of several rows.
function kitIsHeading(r, n, many) { return many && !r.hasAttribute('data-kit-child') && r.children.length < n; }
// Sorts a table's rows by the header named col (stable; empty cells last); group headings stay and each group sorts on its own.
// data-kit-child rows stay under the row above.
function kitSortTable(table, col, dir) {
  var heads = kitHeads(table), at = -1;
  heads.forEach(function(th, i) { if (at < 0 && kitHeadLabel(th) === col) at = i; });
  if (at < 0) return;
  var rows = kitBodyRows(table), many = rows.length > 1, sign = dir === 'desc' ? -1 : 1, out = [], seg = [];
  var cmp = function(a, b) {
    if (!a.key || !b.key) return (!a.key) - (!b.key) || a.i - b.i;
    return sign * kitSortCmp(a.key, b.key) || a.i - b.i;
  };
  var flush = function() { seg.sort(cmp); out = out.concat(seg); seg = []; };
  rows.forEach(function(r) {
    if (kitIsHeading(r, heads.length, many)) { flush(); out.push({ rows: [r] }); }
    else if (r.hasAttribute('data-kit-child') && seg.length) seg[seg.length - 1].rows.push(r);
    else seg.push({ rows: [r], key: r.children[at] ? kitSortKey(r.children[at]) : null, i: seg.length });
  });
  flush();
  kitPutGroups(table, out);
  heads.forEach(function(th, i) {
    if (i === at) th.setAttribute('aria-sort', dir === 'desc' ? 'descending' : 'ascending'); else th.removeAttribute('aria-sort');
  });
}
// Appends the row groups in order, unless the table's rows are already in that order.
function kitPutGroups(table, groups) {
  var want = [], now = kitBodyRows(table), body = kitBody(table);
  groups.forEach(function(g) { want = want.concat(g.rows); });
  if (want.every(function(r, i) { return now[i] === r; })) return;
  want.forEach(function(r) { body.appendChild(r); });
}
// Puts a body's rows back in the order they were filled (kitI from kitResort) and clears the header marks.
function kitUnsort(table) {
  kitHeads(table).forEach(function(th) { th.removeAttribute('aria-sort'); });
  var groups = [];
  kitBodyRows(table).forEach(function(r, i) {
    if (r.hasAttribute('data-kit-child') && groups.length) groups[groups.length - 1].rows.push(r);
    else groups.push({ rows: [r], k: typeof r.kitI === 'number' ? r.kitI : Infinity, i: i });
  });
  groups.sort(function(a, b) { return a.k - b.k || a.i - b.i; });
  kitPutGroups(table, groups);
}
// A table's key inside the saved sorts: its panel's data-card and header names, plus #n for the nth table with the same headers.
function kitTableKey(table, panel) {
  var sig = function(t) { return kitHeads(t).map(kitHeadLabel).join('|'); }, mine = sig(table);
  var same = Array.prototype.filter.call(panel.querySelectorAll('table'), function(t) { return sig(t) === mine; }), n = same.indexOf(table);
  return kitPanelKey(panel) + '#' + mine + (n > 0 ? '#' + (n + 1) : '');
}
// The panel a sortable table belongs to: the nearest one with data-card, else the nearest titled panel.
function kitSortPanel(table) { return table.closest('[data-card]') || table.closest('.panel[data-title]'); }
function kitPanelKey(panel) { return panel.getAttribute('data-card') || 'title:' + panel.getAttribute('data-title'); }
// A {col, dir} that names one of the table's headers, else null.
function kitSortValid(table, s) {
  if (!s || typeof s !== 'object' || typeof s.col !== 'string' || (s.dir !== 'asc' && s.dir !== 'desc')) return null;
  return kitHeads(table).some(function(th) { return kitHeadLabel(th) === s.col; }) ? { col: s.col, dir: s.dir } : null;
}
// The table's data-sort default ("<header> asc|desc"), or null.
function kitSortDefault(table) {
  var m = String(table.getAttribute('data-sort') || '').match(/^(.*\S)\s+(asc|desc)$/);
  return m ? kitSortValid(table, { col: m[1], dir: m[2] }) : null;
}
// The table's sort: the one saved in the sort key for its page and panel, else its data-sort default.
function kitSortOf(table, storage) {
  var page = kitPageOf(table), panel = kitSortPanel(table), card = panel && kitTableKey(table, panel);
  if (page !== null && card) {
    var mine = kitPageObj(kitSavedObj(storage === undefined ? KIT_STORES[page] : storage, kitKey('sort')), page);
    var saved = kitOwn(mine, card) ? kitSortValid(table, mine[card]) : null;
    if (saved) return saved;
  }
  return kitSortDefault(table);
}
// Fills a table body and applies the table's sort; pages fill sortable tables through this.
function kitRows(body, html) {
  body.innerHTML = html;
  kitResort(body);
}
// Applies the sort of the table holding a body to its current rows.
function kitResort(body) {
  var table = body.closest('table'), s = table && !table.closest('[data-kit-nosort]') && kitSortOf(table);
  if (table) kitBodyRows(table).forEach(function(r, i) { r.kitI = i; });
  if (s) kitSortTable(table, s.col, s.dir);
  var panel = table && table.hasAttribute('data-kit-bars') && table.closest('.panel');
  if (panel && kitBarsShown(panel)) kitShowBars(panel, true);
}
// Horizontal bars, one line per label and one bar per series, scaled to the largest value; No data when all are zero.
function kitBarsHtml(labels, series) {
  var max = 0;
  series.forEach(function(s) { s.values.forEach(function(v) { if (+v > max) max = +v; }); });
  if (!labels.length || !(max > 0)) return '<p class="hint">No data</p>';
  return '<div class="kbars">' + labels.map(function(l, i) {
    var bars = series.map(function(s, j) {
      var v = +s.values[i] || 0;
      return '<i title="' + kitEsc(s.name + ': ' + v) + '" style="width:' + +(v / max * 100).toFixed(1) + '%;background:var(--s' + (j % 5 + 1) + ')"></i>';
    }).join('');
    var vals = series.map(function(s) { return kitFmt(+s.values[i] || 0); }).join(' / ');
    return '<div class="kbar"><span class="kbar-l">' + kitEsc(l) + '</span><span class="kbar-t">' + bars + '</span><span class="kbar-v">' + kitEsc(vals) + '</span></div>';
  }).join('') + '</div>' + (series.length > 1 ? '<div class="legend">' + series.map(function(s, j) {
    return '<span><i style="background:var(--s' + (j % 5 + 1) + ')"></i>' + kitEsc(s.name) + '</span>';
  }).join('') + '</div>' : '');
}
// The panel's bars table as labels (data-kit-bars-label column, else the first) and one series per column named in data-kit-bars.
function kitBarsData(table) {
  var heads = kitHeads(table).map(kitHeadLabel), lc = Math.max(0, heads.indexOf(table.getAttribute('data-kit-bars-label')));
  var names = String(table.getAttribute('data-kit-bars') || '').split(',').map(function(n) { return n.trim(); }).filter(function(n) { var i = heads.indexOf(n); return i >= 0 && i !== lc; });
  var rows = kitBodyRows(table).filter(function(r) { return r.children.length === heads.length && !r.hasAttribute('data-kit-child'); });
  return {
    labels: rows.map(function(r) { return String(r.children[lc].getAttribute('data-label') || r.children[lc].textContent || '').trim(); }),
    series: names.map(function(n) {
      var at = heads.indexOf(n);
      return { name: n, values: rows.map(function(r) { var k = kitSortKey(r.children[at]); return k && 'n' in k ? k.n : 0; }) };
    }),
  };
}
function kitBarsShown(panel) { var h = panel.querySelector('.p-bars'); return !!(h && !h.hidden); }
// Shows the panel's bars table as bars (drawn from its current rows) or as the table again, and names the menu item.
function kitShowBars(panel, on) {
  var table = panel.querySelector('table[data-kit-bars]');
  if (!table) return;
  var wrap = table.closest('.tbl-wrap') || table, holder = panel.querySelector('.p-bars');
  if (on && !holder) {
    holder = document.createElement('div');
    holder.className = 'p-bars';
    wrap.parentNode.insertBefore(holder, wrap.nextSibling);
  }
  if (holder) {
    holder.hidden = !on;
    if (on) {
      var d = kitBarsData(table), html = kitBarsHtml(d.labels, d.series);
      if (holder.kitSig !== html) { holder.innerHTML = html; holder.kitSig = html; }
    }
  }
  wrap.hidden = on;
  if (on) wrap.setAttribute('data-kit-barwrap', ''); else wrap.removeAttribute('data-kit-barwrap');
  var item = panel.querySelector('button[data-kit-do="bars"]');
  if (item) item.textContent = on ? 'Show as table' : 'Show as chart';
}
// A header click: sorts by that column (numbers start highest first, text A to Z), a second click reverses; saved in the sort key.
function kitSortClick(doc, th) {
  var table = th.closest('table'), panel = table && kitSortPanel(table);
  if (!panel || !table.hasAttribute('data-kit-sortable') || kitHeads(table).indexOf(th) < 0 || th.classList.contains('no-copy')) return false;
  var col = kitHeadLabel(th);
  if (!col) return false;
  var storage = kitStore(doc), cur = kitSortOf(table, storage), dir;
  if (cur && cur.col === col) dir = cur.dir === 'desc' ? 'asc' : 'desc';
  else {
    var at = kitHeads(table).indexOf(th), first = null;
    kitBodyRows(table).some(function(r) { first = r.children[at] ? kitSortKey(r.children[at]) : null; return !!first; });
    dir = first && 'n' in first ? 'desc' : 'asc';
  }
  kitBodyRows(table).forEach(function(r, i) { if (typeof r.kitI !== 'number') r.kitI = i; });
  kitSortTable(table, col, dir);
  var page = kitPageOf(table), card = kitTableKey(table, panel);
  if (page !== null && page !== '__proto__' && card !== '__proto__') {
    var all = kitSavedObj(storage, kitKey('sort')), mine = kitPageObj(all, page);
    mine[card] = { col: col, dir: dir };
    all[page] = mine;
    kitSaveObj(storage, kitKey('sort'), all);
  }
  return true;
}
// Makes the tables under root (built at run time) sortable with their saved or default sort, and syncs each touched
// panel's Show as chart item and chart view with whether it holds a bars table.
function kitTablesIn(root) {
  if (!root || !root.querySelectorAll || !root.closest || !root.closest('[data-kit-page]')) return;
  var tables = Array.prototype.slice.call(root.querySelectorAll('table')), panels = [];
  if (String(root.tagName || root.tag || '').toUpperCase() === 'TABLE') tables.unshift(root);
  var own = root.closest('.panel');
  if (own) panels.push(own);
  tables.forEach(function(t) {
    var panel = kitSortPanel(t);
    if (!panel || t.closest('[data-kit-nosort]')) return;
    t.setAttribute('data-kit-sortable', '');
    kitBodyRows(t).forEach(function(r, i) { if (typeof r.kitI !== 'number') r.kitI = i; });
    var s = kitSortOf(t);
    if (s) kitSortTable(t, s.col, s.dir);
    if (panels.indexOf(panel) < 0) panels.push(panel);
  });
  panels.forEach(kitBarsSync);
}
// Whether a DOM node is a table or holds one.
function kitHasTable(n) { return n.nodeType === 1 && (n.tagName === 'TABLE' || !!n.querySelector('table')); }
// Adds or removes a panel's Show as chart item as it gains or loses a bars table, and redraws or restores its chart.
function kitBarsSync(panel) {
  var has = panel.querySelector('table[data-kit-bars]'), item = panel.querySelector('button[data-kit-do="bars"]'), pop = panel.querySelector('.p-pop');
  if (!has) {
    if (item && item.parentNode) item.parentNode.removeChild(item);
    var h = panel.querySelector('.p-bars');
    if (h) h.hidden = true;
    Array.prototype.forEach.call(panel.querySelectorAll('[data-kit-barwrap]'), function(w) { w.hidden = false; w.removeAttribute('data-kit-barwrap'); });
    return;
  }
  if (!item && pop) pop.insertAdjacentHTML('afterbegin', '<button type="button" role="menuitem" data-kit-do="bars">Show as chart</button>');
  var page = kitPageOf(panel), card = panel.getAttribute('data-card'), views = page === null ? {} : kitPageObj(kitSavedObj(KIT_STORES[page], kitKey('view')), page);
  if (kitBarsShown(panel) || (card && kitOwn(views, card) && views[card] === 'chart')) kitShowBars(panel, true);
}
// Gives each row's .panels box that holds panels and has no data-cards the row's id as its box key.
function kitBoxKeys(root) {
  Array.prototype.forEach.call(root.querySelectorAll('.row[data-row]'), function(row) {
    Array.prototype.forEach.call(row.children, function(c) {
      if (c.classList && c.classList.contains('panels') && !c.hasAttribute('data-cards') && kitCardEls(c).length) c.setAttribute('data-cards', row.getAttribute('data-row'));
    });
  });
}
// Adds a drag grip before each row's fold button and a ⋯ menu with Move up / Move down after it, once per row.
function kitRowDress(root) {
  kitRowEls(root).forEach(function(row) {
    var b = row.querySelector('button[data-row-fold]');
    if (!b || row.hasAttribute('data-kit-dressed')) return;
    row.setAttribute('data-kit-dressed', '');
    b.insertAdjacentHTML('beforebegin', '<span class="grip row-grip" title="Drag to move this section">⠿</span>');
    b.insertAdjacentHTML('afterend', '<div class="row-menu"><button type="button" class="p-menu" data-kit-menu aria-label="' + kitEsc('Section options for ' + b.textContent) +
      '" aria-haspopup="true" aria-expanded="false">' + KIT_MENU_ICON + '</button><div class="p-pop" role="menu" hidden>' +
      '<button type="button" role="menuitem" data-row-move="-1">Move up</button><button type="button" role="menuitem" data-row-move="1">Move down</button></div></div>');
  });
}
// Disables Move up on the first shown row and Move down on the last.
function kitRowMenus(root) {
  var shown = kitRowEls(root).filter(function(r) { return !r.hidden; });
  shown.forEach(function(r, i) {
    var up = r.querySelector('button[data-row-move="-1"]'), dn = r.querySelector('button[data-row-move="1"]');
    if (up) up.disabled = i === 0;
    if (dn) dn.disabled = i === shown.length - 1;
  });
}
// Saves a page's row order in the rowOrder key, dropping it when it is the default order.
function kitRowOrderSave(doc, page, ids) {
  if (page === null || page === '__proto__') return;
  var storage = kitStore(doc), all = kitSavedObj(storage, kitKey('rowOrder')), d = KIT_LAYOUT[page];
  if (d && d.rows && ids.join() === d.rows.join()) delete all[page]; else all[page] = ids;
  kitSaveObj(storage, kitKey('rowOrder'), all);
}
// Moves a row past the next shown row up (-1) or down (+1) and saves the order; hidden rows keep their place, false at the ends.
function kitMoveRow(doc, row, dir) {
  var holder = row.closest('[data-kit-page]');
  if (!holder) return false;
  var els = kitRowEls(holder), id = row.getAttribute('data-row');
  var ids = els.map(function(r) { return r.getAttribute('data-row'); });
  var vis = els.filter(function(r) { return !r.hidden; }).map(function(r) { return r.getAttribute('data-row'); });
  var i = vis.indexOf(id), j = i + (dir < 0 ? -1 : 1);
  if (i < 0 || j < 0 || j >= vis.length) return false;
  ids.splice(ids.indexOf(id), 1);
  var at = ids.indexOf(vis[j]);
  ids.splice(dir < 0 ? at : at + 1, 0, id);
  kitPlace(els, 'data-row', ids);
  kitRowOrderSave(doc, holder.getAttribute('data-kit-page'), ids);
  kitRowMenus(holder);
  return true;
}
// Forgets one page's saved folds and orders, then shows its default layout.
function kitLayoutReset(page, root, storage) {
  if (!root || !root.querySelectorAll) return;
  [kitKey('rows'), kitKey('rowOrder'), kitKey('order'), kitKey('sort'), kitKey('view')].forEach(function(key) {
    var all = kitSavedObj(storage, key);
    if (!kitOwn(all, page)) return;
    delete all[page];
    kitSaveObj(storage, key, all);
  });
  var d = kitDefaults(page, root);
  kitPlace(kitRowEls(root), 'data-row', d.rows);
  Array.prototype.forEach.call(root.querySelectorAll('[data-cards]'), function(box) { kitPlace(kitCardEls(box), 'data-card', d.cards[box.getAttribute('data-cards')]); });
  Array.prototype.forEach.call(root.querySelectorAll('.row[data-row]'), function(row) { kitRowDraw(row, kitRowFoldsByDefault(row)); });
  kitRowMenus(root);
  Array.prototype.forEach.call(root.querySelectorAll('table'), function(t) {
    var s = kitSortDefault(t);
    if (s) kitSortTable(t, s.col, s.dir); else kitUnsort(t);
  });
  kitBarPanels(root).forEach(function(p) { kitShowBars(p, false); });
  kitRefit(root);
}
// The page name marked by kitLayout around an element, or null.
function kitPageOf(el) {
  var holder = el.closest('[data-kit-page]');
  return holder ? holder.getAttribute('data-kit-page') : null;
}
// Folds or opens the row holding a fold button and saves it in the rows key; an opened row redraws its charts at their real width.
function kitFoldClick(doc, b) {
  var row = b.closest('.row[data-row]');
  if (!row) return;
  var open = row.classList.contains('folded'), id = row.getAttribute('data-row'), page = kitPageOf(row);
  kitRowDraw(row, !open);
  if (page !== null && id !== '__proto__') {
    var storage = kitStore(doc), all = kitSavedObj(storage, kitKey('rows')), mine = kitPageObj(all, page);
    mine[id] = open ? 0 : 1;
    all[page] = mine;
    kitSaveObj(storage, kitKey('rows'), all);
  }
  if (open) kitRefit(row);
}
// Panel drag in progress: {card, last, pointer}, with pointer null for a mouse's native drag.
var kitDrag = null;
// Moves the dragged panel before or after the panel under the pointer, once per panel entered, within its box.
function kitDragTo(doc, x, y) {
  var at = doc.elementFromPoint(x, y), over = at && at.closest ? at.closest('[data-card]') : null;
  if (over === kitDrag.last) return;
  kitDrag.last = over;
  if (!over || over === kitDrag.card || over.parentNode !== kitDrag.card.parentNode) return;
  var kids = Array.prototype.slice.call(over.parentNode.children);
  over.parentNode.insertBefore(kitDrag.card, kids.indexOf(over) > kids.indexOf(kitDrag.card) ? over.nextSibling : over);
}
function kitDragStart(card, pointer) {
  kitDrag = { card: card, last: card, pointer: pointer };
  card.classList.add('dragging');
}
// Ends a panel drag and saves the box's order in the order key, dropping it when it is the default order.
function kitDragEnd(doc) {
  if (!kitDrag) return;
  var card = kitDrag.card, box = card.parentNode, page = kitPageOf(box), key = box.getAttribute('data-cards');
  kitDrag = null;
  card.classList.remove('dragging');
  card.removeAttribute('draggable');
  if (page === null || key === '__proto__') return;
  var storage = kitStore(doc), all = kitSavedObj(storage, kitKey('order')), mine = kitPageObj(all, page), ids = kitCardIds(box), d = KIT_LAYOUT[page];
  if (d && d.cards[key] && ids.join() === d.cards[key].join()) delete mine[key]; else mine[key] = ids;
  if (Object.keys(mine).length) all[page] = mine; else delete all[page];
  kitSaveObj(storage, kitKey('order'), all);
}
// Row drag in progress: {row, last, pointer}, with pointer null for a mouse's native drag.
var kitRowDrag = null;
// Moves the dragged row before or after the row under the pointer, once per row entered, among its siblings.
function kitRowDragTo(doc, x, y) {
  var at = doc.elementFromPoint(x, y), over = at && at.closest ? at.closest('.row[data-row]') : null, row = kitRowDrag.row;
  if (over === kitRowDrag.last) return;
  kitRowDrag.last = over;
  if (!over || over === row || over.parentNode !== row.parentNode) return;
  var kids = Array.prototype.slice.call(over.parentNode.children);
  over.parentNode.insertBefore(row, kids.indexOf(over) > kids.indexOf(row) ? over.nextSibling : over);
}
// Ends a row drag and saves the page's row order in the rowOrder key.
function kitRowDragEnd(doc) {
  if (!kitRowDrag) return;
  var row = kitRowDrag.row, holder = row.closest('[data-kit-page]');
  kitRowDrag = null;
  row.classList.remove('dragging');
  row.removeAttribute('draggable');
  if (!holder) return;
  kitRowOrderSave(doc, holder.getAttribute('data-kit-page'), kitRowEls(holder).map(function(r) { return r.getAttribute('data-row'); }));
  kitRowMenus(holder);
}
// Attaches the row drag listeners: a mouse uses native dragging from the row grip, touch and pen use pointer events.
function kitWireRowDrag(doc) {
  doc.addEventListener('pointerdown', function(ev) {
    var g = ev.target && ev.target.closest && ev.target.closest('.row-grip'), row = g && g.closest('.row[data-row]');
    if (!row || kitRowDrag || kitDrag) return;
    if (ev.pointerType === 'mouse') { row.setAttribute('draggable', 'true'); return; }
    ev.preventDefault();
    g.setPointerCapture(ev.pointerId);
    kitRowDrag = { row: row, last: row, pointer: ev.pointerId };
    row.classList.add('dragging');
  });
  doc.addEventListener('pointerup', function(ev) {
    if (kitRowDrag && kitRowDrag.pointer === ev.pointerId) kitRowDragEnd(doc);
    else if (ev.pointerType === 'mouse' && !kitRowDrag) Array.prototype.forEach.call(doc.querySelectorAll('.row[data-row][draggable]'), function(r) { r.removeAttribute('draggable'); });
  });
  doc.addEventListener('pointercancel', function(ev) { if (kitRowDrag && kitRowDrag.pointer === ev.pointerId) kitRowDragEnd(doc); });
  doc.addEventListener('pointermove', function(ev) {
    if (!kitRowDrag || kitRowDrag.pointer !== ev.pointerId) return;
    var win = doc.defaultView;
    if (win && ev.clientY < 40) win.scrollBy(0, -12); else if (win && ev.clientY > win.innerHeight - 40) win.scrollBy(0, 12);
    kitRowDragTo(doc, ev.clientX, ev.clientY);
  });
  doc.addEventListener('dragstart', function(ev) {
    var row = ev.target && ev.target.closest && ev.target.closest('.row[data-row][draggable]');
    if (!row || kitDrag) return;
    kitRowDrag = { row: row, last: row, pointer: null };
    row.classList.add('dragging');
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', row.getAttribute('data-row'));
  });
  doc.addEventListener('dragover', function(ev) { if (kitRowDrag && kitRowDrag.pointer === null) { ev.preventDefault(); kitRowDragTo(doc, ev.clientX, ev.clientY); } });
  doc.addEventListener('drop', function(ev) { if (kitRowDrag && kitRowDrag.pointer === null) ev.preventDefault(); });
  doc.addEventListener('dragend', function() { if (kitRowDrag && kitRowDrag.pointer === null) kitRowDragEnd(doc); });
}
// Attaches the panel drag listeners: a mouse uses native dragging from the grip, touch and pen use pointer events.
function kitWireDrag(doc) {
  kitWireRowDrag(doc);
  doc.addEventListener('pointerdown', function(ev) {
    var g = ev.target && ev.target.closest && ev.target.closest('.grip'), card = g && g.closest('[data-card]');
    if (!card || kitDrag || !card.parentNode.hasAttribute('data-cards')) return;
    if (ev.pointerType === 'mouse') { card.setAttribute('draggable', 'true'); return; }
    ev.preventDefault();
    g.setPointerCapture(ev.pointerId);
    kitDragStart(card, ev.pointerId);
  });
  doc.addEventListener('pointerup', function(ev) {
    if (kitDrag && kitDrag.pointer === ev.pointerId) kitDragEnd(doc);
    else if (ev.pointerType === 'mouse' && !kitDrag) Array.prototype.forEach.call(doc.querySelectorAll('[data-card][draggable]'), function(c) { c.removeAttribute('draggable'); });
  });
  doc.addEventListener('pointercancel', function(ev) { if (kitDrag && kitDrag.pointer === ev.pointerId) kitDragEnd(doc); });
  doc.addEventListener('pointermove', function(ev) {
    if (!kitDrag || kitDrag.pointer !== ev.pointerId) return;
    var win = doc.defaultView;
    if (win && ev.clientY < 40) win.scrollBy(0, -12); else if (win && ev.clientY > win.innerHeight - 40) win.scrollBy(0, 12);
    kitDragTo(doc, ev.clientX, ev.clientY);
  });
  doc.addEventListener('dragstart', function(ev) {
    var card = ev.target && ev.target.closest && ev.target.closest('[data-card][draggable]');
    if (!card) return;
    kitDragStart(card, null);
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', card.getAttribute('data-card'));
  });
  doc.addEventListener('dragover', function(ev) { if (kitDrag && kitDrag.pointer === null) { ev.preventDefault(); kitDragTo(doc, ev.clientX, ev.clientY); } });
  doc.addEventListener('drop', function(ev) { if (kitDrag && kitDrag.pointer === null) ev.preventDefault(); });
  doc.addEventListener('dragend', function() { if (kitDrag && kitDrag.pointer === null) kitDragEnd(doc); });
}
// A ranked-list row button: copy copies the row's label; only and out fire a bubbling kit:act event ({act, key, value}) from the row, unless its document has no window.
function kitRankAct(b) {
  var act = b.getAttribute('data-kit-act'), li = b.closest('li'), lab = li && li.querySelector('.r-lab');
  if (!li) return;
  if (act === 'copy') { kitCopy(lab ? lab.textContent : '', 'Copied'); return; }
  if (act !== 'only' && act !== 'out') return;
  var win = li.ownerDocument && li.ownerDocument.defaultView;
  if (!win) return;
  li.dispatchEvent(new win.CustomEvent('kit:act', { bubbles: true, detail: { act: act, key: b.getAttribute('data-key'), value: b.getAttribute('data-val') } }));
}
// Attaches the delegated listeners for panel menus, table view, Copy data, full size (menu, close button, double-click, Escape),
// row folds, rank row actions, panel drag, legend chips and chart hover; the page calls it once at boot with its document.
function kitWire(doc) {
  doc.addEventListener('click', function(ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) return;
    var menu = t.closest('[data-kit-menu]');
    if (menu) {
      var pop = menu.parentNode.querySelector('.p-pop'), open = pop.hidden;
      kitCloseMenus(pop);
      pop.hidden = !open;
      menu.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    var item = t.closest('.p-pop button[data-kit-do]');
    if (item) { kitCloseMenus(); kitPanelDo(item.closest('.panel'), item); return; }
    var shut = t.closest('button[data-kit-close]');
    if (shut) {
      var sp = shut.closest('.panel'), sb = sp && sp.querySelector('[data-kit-menu]');
      kitCloseMenus();
      if (sp) kitSetFull(sp, false);
      if (sb) sb.focus();
      return;
    }
    var mv = t.closest('.p-pop button[data-row-move]'), mrow = mv && mv.closest('.row[data-row]');
    if (mrow) {
      kitCloseMenus();
      kitMoveRow(doc, mrow, +mv.getAttribute('data-row-move'));
      var mb = mrow.querySelector('.row-menu [data-kit-menu]');
      if (mb) mb.focus();
      return;
    }
    if (!t.closest('.p-pop')) kitCloseMenus();
    var act = t.closest('button[data-kit-act]');
    if (act) { kitRankAct(act); return; }
    var fold = t.closest('button[data-row-fold]');
    if (fold) { kitFoldClick(doc, fold); return; }
    var th = t.closest('th');
    if (th && kitSortClick(doc, th)) return;
    var chip = t.closest('button[data-kit-series]');
    if (!chip) return;
    var key = chip.getAttribute('data-kit-series');
    if (KIT_SERIES_OFF[key]) delete KIT_SERIES_OFF[key]; else KIT_SERIES_OFF[key] = true;
    var leg = chip.closest('.legend'), ts = leg && leg.nextElementSibling, o = null;
    if (ts && ts.hasAttribute('data-kit-ts')) { try { o = JSON.parse(ts.getAttribute('data-kit-ts')); } catch (e) { o = null; } }
    // With onLegend set the app redraws, given the panel and the chart id (from the chart, else the chip key before its last |).
    if (KIT_CONF.onLegend) {
      try { KIT_CONF.onLegend(chip.closest('.panel'), o && o.id !== undefined ? o.id : key.slice(0, Math.max(0, key.lastIndexOf('|')))); return; } catch (e) {
        if (typeof console !== 'undefined') console.error('kit hook onLegend failed', e);
      }
    }
    if (o && o.series && ts.parentNode) kitRedrawTs(ts, o, o.w);
  });
  doc.addEventListener('keydown', function(ev) {
    if (ev.key !== 'Escape') return;
    var open = Array.prototype.filter.call(doc.querySelectorAll('.p-pop'), function(p) { return !p.hidden; })[0];
    if (!open) {
      var full = doc.querySelectorAll('.panel.full')[0];
      if (full) kitSetFull(full, false);
      return;
    }
    kitCloseMenus();
    var b = open.parentNode.querySelector('[data-kit-menu]');
    if (b) b.focus();
  });
  // Double-click on a panel with a kit header opens it full size, unless on a skipped element, a KPI panel, or a panel is already full.
  doc.addEventListener('dblclick', function(ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t || t.closest(kitDblSkip()) || doc.querySelectorAll('.panel.full').length) return;
    var panel = t.closest('section.panel');
    if (!panel || panel.classList.contains('kpi') || !panel.querySelector('button[data-kit-do="full"]')) return;
    kitCloseMenus();
    kitSetFull(panel, true);
  });
  // Chart hover: crosshair at the nearest label and a tooltip with each shown series, plus the total for stacks.
  // Over a timeline, the tooltip shows the hovered mark's tip.
  doc.addEventListener('mousemove', function(ev) {
    var hit = ev.target && ev.target.closest ? ev.target : null;
    var box = hit && hit.closest('.ts[data-kit-ts]'), lanes = hit && !box && hit.closest('.lanes[data-kit-lanes]');
    if ((box || lanes) !== kitTsOn) kitTsOff();
    if (lanes) { kitLaneTip(lanes, ev); return; }
    if (!box) return;
    var d = box.kitTs;
    if (!d) { try { d = box.kitTs = JSON.parse(box.getAttribute('data-kit-ts')); } catch (e) { return; } }
    var svg = box.querySelector('svg'), n = d.labels.length, shown = d.series.filter(function(s) { return !KIT_SERIES_OFF[d.id + '|' + s.name]; });
    if (!svg || !n) return;
    var rc = svg.getBoundingClientRect(), sc = rc.width ? d.w / rc.width : 1, step = (d.w - d.l - d.r) / n;
    var i = Math.max(0, Math.min(n - 1, Math.floor(((ev.clientX - rc.left) * sc - d.l) / step))), cx = d.l + step * (i + 0.5);
    var xh = svg.querySelector('.xh'), tp = box.querySelector('.tip');
    kitTsOn = box;
    if (xh) { xh.setAttribute('x1', cx); xh.setAttribute('x2', cx); xh.setAttribute('visibility', 'visible'); }
    if (!tp) return;
    var tot = 0, rows = shown.map(function(s) {
      var v = +s.values[i] || 0;
      tot += v;
      return '<div><i style="background:' + kitSeriesFill(s, 0) + '"></i>' + kitEsc(s.name) + '<b>' + kitFmt(v) + '</b></div>';
    }).join('');
    var mks = (d.marks || []).filter(function(mk) { return mk.day === d.labels[i]; }).map(function(mk) { return '<div class="tip-m">' + kitEsc(mk.label) + '</div>'; }).join('');
    tp.innerHTML = '<div class="tip-d">' + kitEsc(d.labels[i]) + '</div>' + rows + (d.mode === 'stack' && shown.length > 1 ? '<div class="tip-t">Total<b>' + kitFmt(tot) + '</b></div>' : '') + mks;
    tp.hidden = false;
    var px = cx / sc, left = px + 12;
    if (left + tp.offsetWidth > rc.width) left = px - 12 - tp.offsetWidth;
    tp.style.left = Math.max(0, left) + 'px';
  });
  if (typeof MutationObserver === 'function' && doc.documentElement) {
    // Tables added or removed anywhere get their sort and chart menu through kitTablesIn on their container.
    new MutationObserver(function(muts) {
      var seen = [];
      muts.forEach(function(m) {
        var hit = Array.prototype.some.call(m.addedNodes, kitHasTable) || Array.prototype.some.call(m.removedNodes, kitHasTable);
        if (hit && seen.indexOf(m.target) < 0) seen.push(m.target);
      });
      seen.forEach(kitTablesIn);
    }).observe(doc.documentElement, { childList: true, subtree: true });
  }
  doc.addEventListener('mouseout', kitTsLeave);
  doc.addEventListener('mouseleave', kitTsLeave);
  var win = doc.defaultView;
  if (win && typeof win.addEventListener === 'function') {
    win.addEventListener('blur', kitTsOff);
    var refitDue = false;
    win.addEventListener('resize', function() {
      if (refitDue) return;
      refitDue = true;
      setTimeout(function() { refitDue = false; kitRefit(doc); }, 150);
    });
  }
  kitWireDrag(doc);
}
