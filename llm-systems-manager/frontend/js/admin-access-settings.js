// Admin → Access Control → "Access settings" card (#846): the Auth & Security
// catalog group rendered with the shared settings fields, saved via /api/admin/settings.
(() => {
  'use strict';

  const GROUP = 'auth';
  const OMIT = new Set(['manager.auth.mode']);   // the Login card owns the mode
  const $ = id => document.getElementById(id);
  const esc = s => (typeof adminEsc === 'function' ? adminEsc(s) : String(s == null ? '' : s));

  let _data = null;
  let _entries = [];
  let _byPath = new Map();
  const _dirty = new Map();
  const _invalid = new Map();
  let _loading = false;

  function pick(data) {
    return (data.entries || []).filter(e => e.group === GROUP && !OMIT.has(e.path));
  }

  function serverValue(e) {
    const v = _data.values || {};
    if (v[e.path] !== undefined) return v[e.path];
    const d = _data.defaults || {};
    return Object.prototype.hasOwnProperty.call(d, e.path) ? d[e.path] : (e.nullable ? null : undefined);
  }

  function shown(e) {
    if (_dirty.has(e.path)) return _dirty.get(e.path);
    const v = serverValue(e);
    if (v !== undefined) return v;
    return e.type === 'list' ? [] : (e.type === 'bool' ? false : '');
  }

  function noteChange(e, cur) {
    const base = serverValue(e);
    const same = cur === null
      ? JSON.stringify(base) === JSON.stringify((_data.defaults || {})[e.path] ?? null)
      : JSON.stringify(cur) === JSON.stringify(base);
    if (same) _dirty.delete(e.path); else _dirty.set(e.path, cur);
    const err = window.SettingsFields.validate(e, cur);
    if (err) _invalid.set(e.path, err); else _invalid.delete(e.path);
  }

  function metaText() {
    if (!_data) return 'session · lockout · networks';
    return window.SettingsFields.groupMeta(_entries);
  }

  function render() {
    const body = $('acCfgBody'), foot = $('acCfgFoot'), meta = $('acCfgMeta');
    if (meta) meta.innerHTML = metaText();
    if (!body || !_data) return;
    const SF = window.SettingsFields;
    body.innerHTML = SF.render(_entries, _data.values || {}, _data.defaults || {}, {
      dirty: _dirty, invalid: _invalid, secrets: _data.secrets || {}, locked: () => false, shown,
    });
    SF.applyDirtyValues(body, _dirty, _byPath);
    if (foot) foot.innerHTML = footHtml();
  }

  function footHtml(msg, cls) {
    const n = _dirty.size, bad = _invalid.size;
    const state = bad ? `<span class="msg err">${bad} invalid</span>`
      : (n ? `<span class="msg">${n} unsaved change${n === 1 ? '' : 's'}</span>` : '');
    return `<button type="button" class="mcbtn mcbtn-pri mcbtn-sm" data-ac-save="1"${(!n || bad) ? ' disabled' : ''}>Save</button>`
      + `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-ac-discard="1"${n ? '' : ' disabled'}>Discard</button>`
      + state
      + `<span class="msg${cls ? ' ' + cls : ''}" id="acCfgMsg">${esc(msg || '')}</span>`
      + '<span class="gap"></span><span class="msg">Same settings as Settings › Auth &amp; Security.</span>';
  }

  function fmtVal(v) {
    if (v === null || v === undefined) return 'unset';
    if (Array.isArray(v)) return v.join(', ') || 'empty';
    if (typeof v === 'boolean') return v ? 'on' : 'off';
    return String(v) === '' ? 'blank' : String(v);
  }

  // Repaint one row's chrome in place so the focused control survives.
  function paintField(e) {
    const body = $('acCfgBody');
    const row = body && body.querySelector(`.settings-row[data-path="${CSS.escape(e.path)}"]`);
    if (!row) return;
    row.classList.toggle('dirty', _dirty.has(e.path));
    const err = _invalid.get(e.path);
    row.classList.toggle('invalid', !!err);
    let node = row.querySelector('.err');
    if (err && !node) {
      node = document.createElement('div');
      node.className = 'err';
      row.insertBefore(node, row.querySelector('.help') || null);
    }
    if (node) { if (err) node.textContent = err; else node.remove(); }
    const defs = _data.defaults || {};
    const dflt = row.querySelector('[data-dflt]');
    if (dflt && Object.prototype.hasOwnProperty.call(defs, e.path)) {
      const d = defs[e.path], v = shown(e);
      if (_dirty.get(e.path) === null) dflt.innerHTML = `cleared → default <b>${esc(fmtVal(d))}</b>`;
      else if (JSON.stringify(v) === JSON.stringify(d)) dflt.textContent = '';
      else dflt.innerHTML = `default <b>${esc(fmtVal(d))}</b>`;
    }
    row.querySelectorAll('.st-in, .st-ta, .sel').forEach(c => c.classList.toggle('dirty', _dirty.has(e.path)));
  }

  function refreshFoot(msg, cls) {
    const foot = $('acCfgFoot');
    if (foot) foot.innerHTML = footHtml(msg, cls);
  }

  function onInput(ev) {
    const el = ev.target.closest('.st-input');
    if (!el || !_data || el.dataset.type === 'bool') return;
    const e = _byPath.get(el.dataset.path);
    if (!e) return;
    noteChange(e, window.SettingsFields.readInput(el, e));
    paintField(e);
    refreshFoot();
  }

  function onClick(ev) {
    const rs = ev.target.closest('[data-restart]');
    if (rs) { ev.preventDefault(); if (typeof _restartService === 'function') _restartService(rs.dataset.restart); return; }
    const bool = ev.target.closest('.mc-toggle[data-type="bool"]');
    if (bool) {
      const e = _byPath.get(bool.dataset.path);
      if (!e) return;
      bool.classList.toggle('on');
      bool.setAttribute('aria-pressed', String(bool.classList.contains('on')));
      noteChange(e, bool.classList.contains('on'));
      paintField(e); refreshFoot();
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
    if (ev.target.closest('[data-ac-save]')) { ev.preventDefault(); save(); return; }
    if (ev.target.closest('[data-ac-discard]')) { ev.preventDefault(); _dirty.clear(); _invalid.clear(); render(); }
  }

  async function load() {
    const body = $('acCfgBody');
    if (_loading) return;
    _loading = true;
    try {
      const r = await fetch('/api/admin/settings');
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      _data = d;
      _entries = pick(d);
      _byPath = new Map(_entries.map(e => [e.path, e]));
      render();
    } catch (e) {
      if (body) body.innerHTML = `<div class="empty">Failed to load access settings — ${esc(e.message)}</div>`;
    } finally { _loading = false; }
  }

  async function save() {
    if (!_data || !_dirty.size || _invalid.size) return;
    const changes = Object.fromEntries(_dirty);
    refreshFoot('saving…');
    let r, d;
    try {
      r = await fetch('/api/admin/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ changes }),
      });
      d = await r.json().catch(() => ({}));
    } catch (e) { refreshFoot(`save failed — ${e}`, 'err'); return; }
    if (!r.ok || !d.ok) {
      for (const [p, m] of Object.entries(d.errors || {})) { _invalid.set(p, m); const e = _byPath.get(p); if (e) paintField(e); }
      refreshFoot(d.error || (Object.keys(d.errors || {}).length ? 'fix the highlighted fields' : `HTTP ${r.status}`), 'err');
      return;
    }
    _dirty.clear(); _invalid.clear();
    await load();
    refreshFoot('✓ Saved', 'ok');
    if (typeof _adminShowRestartNotice === 'function') _adminShowRestartNotice($('acCfgBody'), d, _entries);
    if (typeof adminAuthLoad === 'function') adminAuthLoad();
  }

  function bind() {
    const head = $('acCfgHead'), card = $('adminAccessSettingsCard');
    if (!head || !card || card._acBound) return;
    card._acBound = true;
    head.addEventListener('click', () => {
      card.classList.toggle('collapsed');
      if (!card.classList.contains('collapsed') && !_data) load();
    });
    const body = $('acCfgBody');
    if (body) { body.addEventListener('input', onInput); body.addEventListener('change', onInput); body.addEventListener('click', onClick); }
    const foot = $('acCfgFoot');
    if (foot) foot.addEventListener('click', onClick);
    const meta = $('acCfgMeta');
    if (meta) meta.innerHTML = metaText();
  }

  // Reload after a Settings-tab save so the mirror never shows stale values.
  function invalidate() { if (_data && !_dirty.size) load(); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();

  window.AccessSettings = { pick, load, save, bind, invalidate, dirty: _dirty, invalid: _invalid, noteChange: (p, v) => { const e = _byPath.get(p); if (e) noteChange(e, v); } };
})();
