// Admin → Settings sub-tab (#606, redesigned #797): catalog-driven editor with
// grouped cards, client-side validation and a sticky save bar.
(() => {
  'use strict';

  let _data = null;
  let _entryByPath = new Map();
  const _dirty = new Map();     // path -> raw value to submit (null = clear/default)
  const _invalid = new Map();   // path -> message
  const _open = new Set();      // expanded group keys
  let _filter = '';
  let _booted = false;
  const MOST_USED = '__most_used__';

  const esc = s => _esc(String(s ?? ''));
  const $ = id => document.getElementById(id);

  async function load() {
    if (_dirty.size && !window.confirm('Discard unsaved settings changes?')) return;
    _dirty.clear();
    _invalid.clear();
    const root = $('adminSettingsRoot');
    if (!root) return;
    try {
      const r = await fetch('/api/admin/settings');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _data = await r.json();
      if (!_data.ok) throw new Error(_data.error || 'request failed');
    } catch (e) {
      root.innerHTML = `<div class="card"><div class="card-b" style="color:var(--crit);">`
        + `Failed to load settings — ${esc(e && e.message || e)}</div></div>`;
      return;
    }
    _entryByPath = new Map(_data.entries.map(e => [e.path, e]));
    if (!_booted) {
      _booted = true;
      _open.add(MOST_USED); // leading card starts open; group cards start collapsed
    }
    render();
  }

  // ── topology helpers ──────────────────────────────────────────────
  function aeUnavailable() {
    const t = (_data && _data.topology) || {};
    return !!t.split && !t.ae_config_reachable;
  }
  function aeLocked(e) { return e.service === 'alarm_engine' && aeUnavailable(); }

  function defaults() { return (_data && _data.defaults) || {}; }
  function defsOf(opt) { return (opt && opt.defs) || defaults(); }
  function hasDefault(e, opt) { return Object.prototype.hasOwnProperty.call(defsOf(opt), e.path); }

  function serverValue(e) {
    const v = _data.values[e.path];
    if (v !== undefined) return v;
    if (hasDefault(e)) return defaults()[e.path];
    if (e.nullable) return null;
    return e.type === 'list' ? [] : (e.type === 'bool' ? false : '');
  }
  function shownValue(e) {
    if (_dirty.has(e.path)) return _dirty.get(e.path);
    return serverValue(e);
  }
  function fmtVal(v) {
    if (v === null || v === undefined) return 'unset';
    if (Array.isArray(v)) return v.join(', ') || 'empty';
    if (typeof v === 'boolean') return v ? 'on' : 'off';
    return String(v) === '' ? 'blank' : String(v);
  }

  // ── field rendering ───────────────────────────────────────────────
  // "Idle poll interval (s)" → {label: "Idle poll interval", unit: "s"}.
  function splitUnit(e) {
    const m = /^(.*?)\s*\(([^()]{1,12})\)\s*$/.exec(e.label || '');
    if (m && (e.type === 'int' || e.type === 'float')) return { label: m[1], unit: m[2] };
    return { label: e.label || e.path, unit: '' };
  }
  function firstSentence(s) {
    const t = String(s || '').trim();
    const i = t.search(/\.(\s|$)/);
    return i === -1 ? t : t.slice(0, i);
  }
  function restSentences(s) {
    const t = String(s || '').trim();
    const i = t.search(/\.(\s|$)/);
    return i === -1 ? '' : t.slice(i + 1).trim();
  }

  function tagHtml(e) {
    if (e.service === 'alarm_engine') return ' <span class="tag ae">alarm engine</span>';
    if (e.service === 'both') return ' <span class="tag both">both hosts</span>';
    return '';
  }

  function secretChip(e, secrets) {
    const st = (secrets || {})[e.path];
    const cls = st === 'set' ? 'ok' : (st ? 'dim' : 'dim');
    return `<span class="pill ${cls}">${st ? (st === 'set' ? 'set' : 'not set') : 'unknown'}</span>`;
  }

  function secretControl(e, opt) {
    const p = esc(e.path);
    const isSet = ((opt.secrets || {})[e.path] || 'unset') === 'set';
    const clear = isSet
      ? `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-clear="${p}">Clear</button>` : '';
    if (e.type === 'list') {
      return `<div class="st-secret st-secret--list">`
        + `<div class="st-secret-head row">${secretChip(e, opt.secrets)}${clear}</div>`
        + `<textarea class="st-ta st-input" data-path="${p}" rows="2" `
        + `placeholder="${isSet ? 'one per line — replaces all' : 'one per line'}"></textarea></div>`;
    }
    return `<div class="row st-secret st-secret--inline">${secretChip(e, opt.secrets)}`
      + `<input type="password" class="st-in w st-input" data-path="${p}" autocomplete="new-password" `
      + `placeholder="${isSet ? 'enter a new value to replace' : 'enter value'}">${clear}</div>`;
  }

  function controlHtml(e, opt) {
    const p = esc(e.path);
    const v = opt.shown(e);
    const dirty = opt.dirty && opt.dirty.has(e.path) ? ' dirty' : '';
    const { unit } = splitUnit(e);
    if (e.type === 'bool') {
      const on = v === true;
      return `<button type="button" class="mc-toggle st-input${on ? ' on' : ''}" data-path="${p}" `
        + `data-type="bool" aria-pressed="${on}"><span class="track"></span>`
        + `<span class="tlbl">${esc(firstSentence(e.help) || splitUnit(e).label)}</span></button>`;
    }
    if (e.type === 'choice') {
      return `<div class="row"><select class="sel st-input${dirty}" data-path="${p}">`
        + (e.choices || []).map(c =>
          `<option value="${esc(c)}"${String(c) === String(v) ? ' selected' : ''}>${esc(c)}</option>`).join('')
        + '</select>' + resetHtml(e, opt) + '</div>';
    }
    const fromDom = !!(opt.dirty && opt.dirty.has(e.path));
    if (e.type === 'list') {
      return `<textarea class="st-ta st-input${dirty}" data-path="${p}" rows="3" `
        + `placeholder="one per line">${fromDom ? '' : esc((v || []).join('\n'))}</textarea>`;
    }
    const numeric = e.type === 'int' || e.type === 'float';
    const cls = numeric ? 'st-in' : 'st-in w';
    const ph = e.nullable ? 'inherit' : (hasDefault(e, opt) ? String(defsOf(opt)[e.path] ?? '') : '');
    const val = (v === null || v === undefined) ? '' : (Array.isArray(v) ? v.join(', ') : String(v));
    return `<div class="row"><input type="text" class="${cls} st-input${dirty}" data-path="${p}" `
      + `inputmode="${numeric ? 'numeric' : 'text'}" value="${fromDom ? '' : esc(val)}" placeholder="${esc(ph)}">`
      + (unit ? `<span class="unit">${esc(unit)}</span>` : '')
      + defaultHtml(e, opt) + resetHtml(e, opt) + '</div>';
  }

  // "default 0" / "cleared → default 0" once the field differs from its default.
  function defaultHtml(e, opt) {
    if (e.secret || !hasDefault(e, opt)) return '';
    const d = defsOf(opt)[e.path];
    const v = opt.shown(e);
    if (opt.dirty && opt.dirty.get(e.path) === null) {
      return `<span class="dflt" data-dflt="${esc(e.path)}">cleared → default <b>${esc(fmtVal(d))}</b></span>`;
    }
    if (JSON.stringify(v) === JSON.stringify(d)) return `<span class="dflt" data-dflt="${esc(e.path)}"></span>`;
    return `<span class="dflt" data-dflt="${esc(e.path)}">default <b>${esc(fmtVal(d))}</b></span>`;
  }
  function resetHtml(e, opt) {
    if (e.secret || !hasDefault(e, opt)) return '';
    const v = opt.shown(e);
    const same = JSON.stringify(v) === JSON.stringify(defsOf(opt)[e.path]);
    if (same || (opt.dirty && opt.dirty.get(e.path) === null)) return '';
    return `<button type="button" class="ib" data-reset="${esc(e.path)}" data-tip="Reset to default">↺</button>`;
  }

  function driftHtml(e) {
    const d = _data && _data.drift && _data.drift[e.path];
    if (!d) return '';
    const detail = d.secret ? `local <b>${esc(d.local)}</b> / engine <b>${esc(d.ae)}</b>`
      : `local <b>${esc(fmtVal(d.local))}</b> / engine <b>${esc(fmtVal(d.ae))}</b>`;
    return `<div class="drift">Differs on the alarm-engine host — ${detail}</div>`;
  }

  function bothNoteHtml(e) {
    if (e.service === 'both' && aeUnavailable()) {
      return '<div class="drift">Saved locally — also update the alarm-engine host’s copy</div>';
    }
    return '';
  }

  function fieldHtml(e, opt) {
    const { label } = splitUnit(e);
    const isDirty = !!(opt.dirty && opt.dirty.has(e.path));
    const err = opt.invalid && opt.invalid.get(e.path);
    const help = e.type === 'bool' ? restSentences(e.help) : e.help;
    let control;
    if (opt.locked && opt.locked(e)) {
      const v = _data.values[e.path];
      const shown = e.secret ? secretChip(e, opt.secrets)
        : esc(v === undefined ? 'unknown' : (Array.isArray(v) ? v.join(', ') : String(v)));
      control = `<div class="row"><span class="lock">🔒 ${shown}</span>`
        + '<span class="help">read-only; see the alarm-engine notice above</span></div>';
    } else if (e.secret) {
      control = secretControl(e, opt);
    } else {
      control = controlHtml(e, opt);
    }
    return `<div class="st-field settings-row${isDirty ? ' dirty' : ''}${err ? ' invalid' : ''}" `
      + `data-path="${esc(e.path)}" data-label="${esc(e.label)}">`
      + `<label>${esc(label)}${tagHtml(e)}</label>${control}`
      + (err ? `<div class="err">${esc(err)}</div>` : '')
      + (help ? `<div class="help">${esc(help)}</div>` : '')
      + driftHtml(e) + bothNoteHtml(e) + '</div>';
  }

  // Unsaved edits are typed text: set them by property, never via the HTML string.
  // A setting can render twice (Most used + its group), so every match is set.
  function applyDirtyValues(root, dirty, byPath) {
    dirty.forEach((val, path) => {
      const e = byPath.get(path);
      if (!e || e.secret || e.type === 'bool' || e.type === 'choice') return;
      const v = (val === null || val === undefined) ? '' : (Array.isArray(val) ? val.join('\n') : String(val));
      root.querySelectorAll(`.st-input[data-path="${CSS.escape(path)}"]`).forEach(el => { el.value = v; });
    });
  }

  // Mirrors one control's live value onto its twin (Most used <-> group card),
  // by property only, never through an HTML string.
  function syncTwin(e, el) {
    document.querySelectorAll(`.st-input[data-path="${CSS.escape(e.path)}"]`).forEach(other => {
      if (other === el) return;
      if (e.type === 'bool') {
        const on = el.classList.contains('on');
        other.classList.toggle('on', on);
        other.setAttribute('aria-pressed', String(on));
      } else {
        other.value = el.value;
      }
    });
  }

  // Standalone renderer reused by the Backup & Restore settings card (#797).
  function renderFields(entries, values, defs, over) {
    const opt = Object.assign({
      defs: defs || defaults(),
      secrets: (_data && _data.secrets) || {},
      dirty: _dirty, invalid: _invalid, locked: aeLocked,
      shown: e => {
        if (_dirty.has(e.path)) return _dirty.get(e.path);
        if (values && values[e.path] !== undefined) return values[e.path];
        if (defs && Object.prototype.hasOwnProperty.call(defs, e.path)) return defs[e.path];
        return e.type === 'list' ? [] : (e.type === 'bool' ? false : '');
      },
    }, over || {});
    return `<div class="st-grid">${entries.map(e => fieldHtml(e, opt)).join('')}</div>`;
  }

  // ── validation ────────────────────────────────────────────────────
  function rangeText(e) {
    const kind = e.type === 'int' ? 'a whole number' : 'a number';
    if (e.min != null && e.max != null) return `Must be ${kind} from ${e.min} to ${e.max}.`;
    if (e.min != null) return `Must be ${kind} of at least ${e.min}.`;
    if (e.max != null) return `Must be ${kind} of at most ${e.max}.`;
    return `Must be ${kind}.`;
  }
  function validate(e, v) {
    if (v === null || v === undefined) return null;
    if (e.type === 'int' || e.type === 'float') {
      if (typeof v !== 'number' || !isFinite(v)) return rangeText(e);
      if (e.type === 'int' && !Number.isInteger(v)) return rangeText(e);
      if (e.min != null && v < e.min) return rangeText(e);
      if (e.max != null && v > e.max) return rangeText(e);
      return null;
    }
    if (e.type === 'choice' && !(e.choices || []).includes(v)) {
      return `Must be one of: ${(e.choices || []).join(', ')}.`;
    }
    return null;
  }

  // ── group cards ───────────────────────────────────────────────────
  function groupMeta(entries) {
    const m = entries.filter(e => e.service === 'manager').length;
    const a = entries.filter(e => e.service === 'alarm_engine').length;
    const b = entries.filter(e => e.service === 'both').length;
    const label = (m && a) || (m && b) || (!m && !a) ? (m && !a && !b ? 'manager' : 'both hosts')
      : (a ? 'alarm engine' : 'manager');
    const shared = label === 'both hosts' ? 0 : b;
    return `<b>${entries.length}</b> settings · ${label === 'manager' ? 'manager' : `<b>${label}</b>`}`
      + (shared ? ` · ${shared} shared` : '');
  }

  function matchesFilter(e) {
    if (!_filter) return true;
    const q = _filter.toLowerCase();
    return (e.label || '').toLowerCase().includes(q) || (e.path || '').toLowerCase().includes(q)
      || (e.help || '').toLowerCase().includes(q);
  }

  function render() {
    const root = $('adminSettingsRoot');
    if (!root || !_data) return;
    const byGroup = {};
    _data.entries.forEach(e => (byGroup[e.group] ||= []).push(e));
    let html = noticesHtml();

    const common = _data.entries.filter(e => e.common).filter(matchesFilter);
    if (common.length) {
      const forced = common.some(e => _dirty.has(e.path) || _invalid.has(e.path)) || !!_filter;
      const open = forced || _open.has(MOST_USED);
      html += `<div class="card${open ? '' : ' collapsed'}" data-group="${MOST_USED}">`
        + `<div class="card-h tog"><span class="chev">▾</span><h3>Most used</h3>`
        + `<span class="meta"><b>${common.length}</b> settings</span><span class="gap"></span></div>`
        + `<div class="card-b">${renderFields(common, _data.values, defaults())}</div></div>`;
    }

    const groups = [..._data.groups].sort((a, b) => a.title.localeCompare(b.title));
    for (const g of groups) {
      const all = byGroup[g.key];
      if (!all) continue;
      const entries = all.filter(matchesFilter);
      if (!entries.length) continue;
      const forced = entries.some(e => _dirty.has(e.path) || _invalid.has(e.path)) || !!_filter;
      const open = forced || _open.has(g.key);
      html += `<div class="card${open ? '' : ' collapsed'}" data-group="${esc(g.key)}">`
        + `<div class="card-h tog"><span class="chev">▾</span><h3>${esc(g.title)}</h3>`
        + `<span class="meta">${groupMeta(all)}</span><span class="gap"></span></div>`
        + `<div class="card-b">${renderFields(entries, _data.values, defaults())}</div></div>`;
    }
    root.innerHTML = html + saveBarHtml();
    applyDirtyValues(root, _dirty, _entryByPath);
    renderSummary();
    bindOnce(root);
  }

  function renderSummary() {
    const sum = $('stSummary');
    if (!sum || !_data) return;
    const groups = new Set(_data.entries.map(e => e.group)).size;
    sum.innerHTML = `<span><b>${_data.entries.length}</b> settings</span>`
      + `<span><b>${groups}</b> groups</span>`
      + `<span><b class="${_dirty.size ? 'warn' : ''}">${_dirty.size}</b> unsaved</span>`
      + `<span><b class="${_invalid.size ? 'crit' : ''}">${_invalid.size}</b> invalid</span>`;
  }

  // ── notices ───────────────────────────────────────────────────────
  const _UNIT = { manager: 'llm-systems-manager', alarm_engine: 'llm-systems-alarm-engine' };
  const _LABEL = { manager: 'Manager', alarm_engine: 'Alarm Engine' };
  const _AE_ERR_TITLE = {
    unauthorized: 'Alarm engine rejected this host’s token',
    unsupported: 'Alarm engine has no settings API',
    unreachable: 'Alarm engine unreachable',
  };

  function noticesHtml() {
    let out = '';
    const topo = _data.topology || {};
    const pending = (_data.restart_pending || []).filter(s => _UNIT[s]);
    if (pending.length) {
      const detail = pending.map(svc => {
        const names = pendingLabels(svc).map(n => `<b>${esc(n)}</b>`);
        return `${_LABEL[svc]}: ${names.length ? names.join(', ') : 'changed on its host'}`;
      }).join(' · ');
      out += '<div class="notice" id="adminSettingsRestartBanner"><b>Restart required</b>'
        + `<span class="d">Saved changes take effect after a restart — ${detail}.</span>`
        + pending.map(svc => {
          const remote = svc === 'alarm_engine' && topo.split;
          const cmd = (remote ? '(on the alarm-engine host) ' : '') + `sudo systemctl restart ${_UNIT[svc]}`;
          return `<code>${esc(cmd)}</code>`
            + `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" data-restart="${esc(svc)}">`
            + `Restart ${esc(_LABEL[svc])}</button>`;
        }).join('')
        + '<span class="gap"></span><span class="d" id="adminSettingsRestartMsg"></span></div>';
    }
    if (aeUnavailable()) {
      const err = topo.ae_config_error || {};
      const title = _AE_ERR_TITLE[err.kind] || 'Alarm-engine settings unavailable';
      out += '<div class="notice crit" id="adminSettingsAeConfigBanner">'
        + `<b>${esc(title)}</b><span class="d">Its settings are shown read-only and cannot be edited from here.`
        + (err.remedy ? ` ${esc(err.remedy)}` : '') + (err.detail ? ` (${esc(err.detail)})` : '')
        + '</span></div>';
    }
    const drift = _data.drift || {};
    const paths = Object.keys(drift);
    if (paths.length) {
      const names = paths.map(p => `<b>${esc((_entryByPath.get(p) || { label: p }).label)}</b>`).join(', ');
      out += '<div class="notice" id="adminSettingsDriftBanner"><b>Config drift</b>'
        + `<span class="d">${paths.length} shared setting${paths.length > 1 ? 's' : ''} differ between `
        + `this host and the alarm engine: ${names}.</span><span class="gap"></span>`
        + '<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" id="adminSettingsResyncBtn">'
        + 'Re-sync to alarm engine</button>'
        + '<span class="d" id="adminSettingsResyncMsg"></span></div>';
    }
    const q = _data.ae_sync_pending || [];
    if (q.length) {
      out += '<div class="notice" id="adminSettingsAePending">'
        + `<b>${q.length} alarm-engine setting${q.length === 1 ? '' : 's'} queued</b>`
        + `<span class="d">Not acknowledged yet; ${esc(aeRetryText())}. ${esc(q.join(', '))}</span></div>`;
      clearTimeout(_aePollTimer);
      _aePollTimer = setTimeout(() => {
        if ($('adminSettingsAePending') && !_dirty.size) load();
      }, (((_data && _data.ae_sync_retry_s) || 30) + 5) * 1000);
    }
    return out;
  }

  // Labels of the drifted non-hot paths that belong to svc (both-owned count for each).
  function pendingLabels(svc) {
    return (_data.restart_pending_paths || [])
      .filter(p => { const e = _entryByPath.get(p); return e && (e.service === svc || e.service === 'both'); })
      .map(labelOf);
  }

  // Inline notice for a PUT response that flagged a restart; hosts route the
  // data-restart buttons to _restartService.
  function restartNoticeHtml(d, labelFn) {
    const svcs = (d.restart_required || []).filter(s => _UNIT[s]);
    if (!svcs.length) return '';
    const paths = d.restart_paths || [];
    const names = paths.map(p => `<b>${esc(labelFn ? labelFn(p) : p)}</b>`).join(', ');
    const lead = !names ? 'These changes take' : names + (paths.length === 1 ? ' takes' : ' take');
    return '<div class="notice"><b>Restart required</b>'
      + `<span class="d">${lead} effect after a `
      + `${svcs.map(s => _LABEL[s].toLowerCase()).join(' and ')} restart.</span>`
      + svcs.map(s => `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" data-restart="${esc(s)}">Restart ${esc(_LABEL[s])}</button>`).join('')
      + '</div>';
  }

  // ── save bar ──────────────────────────────────────────────────────
  function labelOf(p) { return (_entryByPath.get(p) || { label: p }).label; }

  function saveNote() {
    if (_invalid.size) {
      return `Fix ${[..._invalid.keys()].map(labelOf).map(esc).join(', ')} to save`;
    }
    const bits = [];
    for (const p of _dirty.keys()) {
      const e = _entryByPath.get(p);
      if (!e) continue;
      bits.push(e.hot
        ? `${esc(e.label)} applies without restart`
        : `${esc(e.label)} needs a <b>${e.service === 'alarm_engine' ? 'alarm engine' : 'manager'} restart</b>`);
    }
    return bits.slice(0, 4).join(' · ');
  }

  function saveBarHtml() {
    if (!_dirty.size && !_invalid.size) return '';
    return '<div class="savebar" id="adminSettingsSaveBar">'
      + `<span class="cnt" id="adminSettingsDirtyCount"><b>${_dirty.size}</b> unsaved change`
      + `${_dirty.size === 1 ? '' : 's'}`
      + (_invalid.size ? ` · <b class="bad">${_invalid.size} invalid</b>` : '') + '</span>'
      + `<button type="button" class="mcbtn mcbtn-pri mcbtn-sm" id="adminSettingsSaveBtn"`
      + `${_invalid.size ? ' disabled' : ''}>Save</button>`
      + '<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" id="adminSettingsDiscardBtn">Discard</button>'
      + `<span class="note">${saveNote()}</span>`
      + '<span class="note" id="adminSettingsSaveMsg"></span></div>';
  }

  function updateSaveBar() {
    const root = $('adminSettingsRoot');
    if (!root) return;
    const bar = $('adminSettingsSaveBar');
    if (!_dirty.size && !_invalid.size) { if (bar) bar.remove(); renderSummary(); return; }
    if (!bar) { root.insertAdjacentHTML('beforeend', saveBarHtml()); renderSummary(); return; }
    bar.outerHTML = saveBarHtml();
    renderSummary();
  }

  // ── editing ───────────────────────────────────────────────────────
  function readInput(el, e) {
    if (e.type === 'bool') return el.classList.contains('on');
    if (e.type === 'list') return el.value.split('\n').map(s => s.trim()).filter(Boolean);
    if (e.type === 'int' || e.type === 'float') {
      const raw = el.value.trim();
      if (raw === '') return null;
      const nv = Number(raw);
      return isNaN(nv) ? raw : nv;
    }
    return el.value === '' ? null : el.value;
  }

  function noteChange(e, cur) {
    if (e.secret) {
      const blank = Array.isArray(cur) ? !cur.length : (cur === '' || cur == null);
      if (blank) _dirty.delete(e.path); else _dirty.set(e.path, cur);
      _invalid.delete(e.path);
      return;
    }
    let same;
    if (cur === null) {
      const base = hasDefault(e) ? defaults()[e.path] : (e.nullable ? null : undefined);
      same = base !== undefined && JSON.stringify(serverValue(e)) === JSON.stringify(base);
    } else {
      same = JSON.stringify(cur) === JSON.stringify(serverValue(e));
    }
    if (same) _dirty.delete(e.path); else _dirty.set(e.path, cur);
    const err = validate(e, cur);
    if (err) _invalid.set(e.path, err); else _invalid.delete(e.path);
  }

  // A setting can render twice (Most used + its group); paint every row.
  function paintField(e) {
    document.querySelectorAll(`.settings-row[data-path="${CSS.escape(e.path)}"]`).forEach(row => {
      row.classList.toggle('dirty', _dirty.has(e.path));
      const err = _invalid.get(e.path);
      row.classList.toggle('invalid', !!err);
      if (_dirty.has(e.path) || err) { const card = row.closest('.card'); if (card) card.classList.remove('collapsed'); }
      let node = row.querySelector('.err');
      if (err && !node) {
        node = document.createElement('div');
        node.className = 'err';
        row.insertBefore(node, row.querySelector('.help') || null);
      }
      if (node) { if (err) node.textContent = err; else node.remove(); }
      const dflt = row.querySelector('[data-dflt]');
      if (dflt && hasDefault(e)) {
        const d = defaults()[e.path];
        const v = shownValue(e);
        if (_dirty.get(e.path) === null) dflt.innerHTML = `cleared → default <b>${esc(fmtVal(d))}</b>`;
        else if (JSON.stringify(v) === JSON.stringify(d)) dflt.textContent = '';
        else dflt.innerHTML = `default <b>${esc(fmtVal(d))}</b>`;
      }
      row.querySelectorAll('.st-in, .st-ta, .sel').forEach(c => c.classList.toggle('dirty', _dirty.has(e.path)));
    });
  }

  function onInput(ev) {
    const el = ev.target.closest('.st-input');
    if (!el || !_data || el.dataset.type === 'bool') return;
    const entry = _entryByPath.get(el.dataset.path);
    if (!entry) return;
    noteChange(entry, entry.secret && entry.type !== 'list' ? el.value : readInput(el, entry));
    if (entry.secret) syncClearButton(entry.path);
    syncTwin(entry, el);
    paintField(entry);
    updateSaveBar();
  }

  function syncClearButton(path) {
    document.querySelectorAll(`[data-clear="${CSS.escape(path)}"]`).forEach(btn => {
      const queued = _dirty.get(path) === null;
      btn.textContent = queued ? 'Clear queued' : 'Clear';
      btn.disabled = queued;
    });
  }

  function onClick(ev) {
    const tog = ev.target.closest('.card-h.tog');
    if (tog) {
      const card = tog.closest('.card');
      const key = card && card.dataset.group;
      card.classList.toggle('collapsed');
      if (key) { if (card.classList.contains('collapsed')) _open.delete(key); else _open.add(key); }
      return;
    }
    const bool = ev.target.closest('.mc-toggle[data-type="bool"]');
    if (bool) {
      const entry = _entryByPath.get(bool.dataset.path);
      if (!entry) return;
      bool.classList.toggle('on');
      bool.setAttribute('aria-pressed', String(bool.classList.contains('on')));
      noteChange(entry, bool.classList.contains('on'));
      syncTwin(entry, bool);
      paintField(entry);
      updateSaveBar();
      return;
    }
    const clr = ev.target.closest('[data-clear]');
    if (clr) {
      ev.preventDefault();
      _dirty.set(clr.dataset.clear, null);
      syncClearButton(clr.dataset.clear);
      const e = _entryByPath.get(clr.dataset.clear);
      if (e) paintField(e);
      updateSaveBar();
      return;
    }
    const rst = ev.target.closest('[data-reset]');
    if (rst) {
      ev.preventDefault();
      _dirty.set(rst.dataset.reset, null);
      _invalid.delete(rst.dataset.reset);
      render();
      return;
    }
    const svc = ev.target.closest('[data-restart]');
    if (svc) { ev.preventDefault(); restartService(svc.dataset.restart); return; }
    if (ev.target.closest('#adminSettingsSaveBtn')) { ev.preventDefault(); save(); return; }
    if (ev.target.closest('#adminSettingsDiscardBtn')) {
      ev.preventDefault(); _dirty.clear(); _invalid.clear(); load(); return;
    }
    if (ev.target.closest('#adminSettingsResyncBtn')) {
      ev.preventDefault(); resyncDrift(Object.keys(_data.drift || {}));
    }
  }

  function bindOnce(root) {
    if (root._stBound) return;
    root._stBound = true;
    root.addEventListener('input', onInput);
    root.addEventListener('change', onInput);
    root.addEventListener('click', onClick);
  }

  // ── save ──────────────────────────────────────────────────────────
  async function save() {
    if (_invalid.size) return;
    const changes = Object.fromEntries(_dirty);
    const msg = $('adminSettingsSaveMsg');
    if (msg) msg.textContent = 'saving…';
    let r, d;
    try {
      r = await fetch('/api/admin/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes }),
      });
      d = await r.json().catch(() => ({}));
    } catch (e) {
      if (msg) msg.textContent = `save failed — ${String(e)}`;
      return;
    }
    if (r.ok && d.ok) {
      if (d.ae_sync_failed) {
        (d.applied || []).forEach(p => _dirty.delete(p));
        if (_dirty.size) {
          _data.ae_sync_pending = d.ae_sync_pending || [];
          render();
          const m = $('adminSettingsSaveMsg');
          if (m) m.textContent = `saved locally; AE sync failed: ${d.ae_sync_failed}${aeQueuedSuffix(d)}`;
          return;
        }
      }
      _dirty.clear();
      _invalid.clear();
      await load();
      showResult(d);
      if ((d.restart_required || []).length && typeof adminLoadHealth === 'function') adminLoadHealth();
    } else {
      if (msg) msg.textContent = 'save failed — fix the errors below';
      showErrors(d.errors || { _: d.error || `HTTP ${r.status}` });
    }
  }

  function showErrors(errors) {
    for (const [path, m] of Object.entries(errors)) {
      _invalid.set(path, m);
      const e = _entryByPath.get(path);
      if (e) paintField(e);
    }
    updateSaveBar();
  }

  function showResult(d) {
    const note = document.createElement('div');
    note.className = 'notice info';
    note.textContent = d.ae_sync_failed
      ? `Saved locally, but alarm-engine sync failed: ${d.ae_sync_failed}${aeQueuedSuffix(d)}`
      : '✓ Saved';
    const root = $('adminSettingsRoot');
    root.insertBefore(note, root.firstChild);
    setTimeout(() => note.remove(), 6000);
  }

  // ── queued alarm-engine edits (#667) ──────────────────────────────
  let _aePollTimer = null;
  function aeRetryText() {
    const s = Math.round((_data && _data.ae_sync_retry_s) || 30);
    return `the manager retries every ${s} s until the alarm engine acks`;
  }
  function aeQueuedSuffix(d) {
    return d.ae_sync_pending && d.ae_sync_pending.length ? ` — queued; ${aeRetryText()}` : '';
  }

  async function resyncDrift(paths) {
    const msg = $('adminSettingsResyncMsg');
    if (msg) msg.textContent = 're-syncing…';
    let r, d;
    try {
      r = await fetch('/api/admin/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes: {}, resync_ae: paths }),
      });
      d = await r.json().catch(() => ({}));
    } catch (e) {
      if (msg) msg.textContent = `re-sync failed — ${e}`;
      return;
    }
    if (r.ok && d.ok && !d.ae_sync_failed) { await load(); return; }
    if (msg) msg.textContent = `re-sync failed — ${d.ae_sync_failed || d.error || `HTTP ${r.status}`}`;
  }

  // ── restart ───────────────────────────────────────────────────────
  function bannerMsg(text, isErr) {
    const el = $('adminSettingsRestartMsg');
    if (!el) return;
    el.textContent = text;
    el.style.color = isErr ? 'var(--crit)' : 'var(--fg-dim)';
  }

  async function restartService(svc) {
    const label = _LABEL[svc] || svc;
    const okGo = typeof _themedConfirm === 'function'
      ? await _themedConfirm({
          title: `Restart ${label}?`,
          bodyHtml: svc === 'manager'
            ? 'The manager will restart and the dashboard will be briefly unavailable.'
            : 'The alarm engine will restart. Agents buffer and retry, so no data is lost.',
          confirmLabel: 'Restart', cancelLabel: 'Cancel', danger: true,
        })
      : window.confirm(`Restart ${label}?`);
    if (!okGo) return;
    bannerMsg(`restarting ${label}…`);
    try {
      const r = await fetch(`/api/admin/service/${svc}/restart`, { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        if (svc === 'manager') {
          bannerMsg('manager restarting — reloading in ~6s');
          setTimeout(() => location.reload(), 6000);
        } else {
          bannerMsg(`✓ ${label} restart requested`);
          setTimeout(load, 4000);
        }
      } else {
        bannerMsg(`${label} restart failed — ${d.error || `HTTP ${r.status}`}`, true);
      }
    } catch (e) {
      if (svc === 'manager') {
        bannerMsg('manager restarting — reloading in ~6s');
        setTimeout(() => location.reload(), 6000);
      } else {
        bannerMsg(`${label} restart error — ${e}`, true);
      }
    }
  }

  // ── header controls ───────────────────────────────────────────────
  function wireHeader() {
    const f = $('stFilter');
    if (f && !f._stBound) {
      f._stBound = true;
      f.addEventListener('input', () => { _filter = f.value.trim(); render(); });
    }
    const x = $('stExpandAll');
    if (x && !x._stBound) {
      x._stBound = true;
      x.addEventListener('click', () => {
        const keys = [MOST_USED, ...(_data ? _data.groups : []).map(g => g.key)];
        const expand = _open.size < keys.length;
        _open.clear();
        if (expand) keys.forEach(k => _open.add(k));
        x.textContent = expand ? 'Collapse all' : 'Expand all';
        render();
      });
    }
  }
  if (typeof document !== 'undefined') {
    wireHeader();
    document.addEventListener('DOMContentLoaded', wireHeader);
  }

  window.adminSettingsLoad = load;
  window.SettingsFields = {
    render: (entries, values, defs, over) => renderFields(entries, values, defs, over),
    fieldHtml, validate, splitUnit, groupMeta, readInput, firstSentence, applyDirtyValues,
    restartNotice: restartNoticeHtml,
  };
})();
