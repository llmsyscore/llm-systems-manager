// Admin → Settings sub-tab (#606): catalog-driven TOML settings editor.
// Talks to GET/PUT /api/admin/settings; restart buttons reuse _restartService.
(() => {
  'use strict';

  let _data = null;
  const _dirty = new Map();   // path -> raw value to submit (null = clear secret)

  function esc(s) {
    return String(s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function entryFor(path) {
    return _data.entries.find(e => e.path === path);
  }

  async function load() {
    if (_dirty.size && !window.confirm('Discard unsaved settings changes?')) return;
    _dirty.clear();
    const root = document.getElementById('adminSettingsRoot');
    if (!root) return;
    try {
      const r = await fetch('/api/admin/settings');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _data = await r.json();
      if (!_data.ok) throw new Error(_data.error || 'request failed');
    } catch (e) {
      root.innerHTML = `<div class="adm-card" style="padding:16px;color:var(--crit);">` +
        `Failed to load settings — ${esc(String(e && e.message || e))}</div>`;
      return;
    }
    render(root);
  }

  // ── rendering ─────────────────────────────────────────────────────

  function aeLocked(e) {
    return e.service === 'alarm_engine' &&
      _data.topology.split && !_data.topology.ae_config_reachable;
  }

  function currentValue(e) {
    const v = _data.values[e.path];
    if (v !== undefined) return v;
    return e.type === 'list' ? [] : '';
  }

  function secretChip(e) {
    const isSet = (_data.secrets[e.path] || 'unset') === 'set';
    return `<span class="status ${isSet ? 'status--ok' : 'status--muted'} status--square">` +
      `${isSet ? 'set' : 'not set'}</span>`;
  }

  function secretInput(e) {
    const p = esc(e.path);
    const isSet = (_data.secrets[e.path] || 'unset') === 'set';
    const field = e.type === 'list'
      ? `<textarea class="ap-input st-input" data-path="${p}" rows="2" style="width:100%;"
           placeholder="${isSet ? 'one per line — replaces all' : 'one per line'}"></textarea>`
      : `<input type="password" class="ap-input st-input" data-path="${p}" autocomplete="new-password"
           style="width:min(340px,100%);" placeholder="${isSet ? 'enter new value to replace' : 'enter value'}">`;
    const clearBtn = isSet
      ? `<button class="adm-btn warn" data-clear="${p}" style="margin-left:6px;">Clear</button>` : '';
    return `<div style="display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap;">` +
      `${secretChip(e)}${field}${clearBtn}</div>`;
  }

  function inputFor(e) {
    if (e.secret) return secretInput(e);
    const p = esc(e.path);
    const v = currentValue(e);
    if (aeLocked(e)) {
      return `<span class="adm-muted">🔒 ${esc(Array.isArray(v) ? v.join(', ') : String(v))}` +
        ` — alarm engine unreachable; edit the TOML on its host</span>`;
    }
    if (e.type === 'bool') {
      return `<input type="checkbox" class="st-input" data-path="${p}" ${v ? 'checked' : ''}>`;
    }
    if (e.type === 'choice') {
      return `<select class="ap-select st-input" data-path="${p}">` +
        e.choices.map(c => `<option value="${esc(c)}" ${c === v ? 'selected' : ''}>${esc(c)}</option>`).join('') +
        `</select>`;
    }
    if (e.type === 'list') {
      return `<textarea class="ap-input st-input" data-path="${p}" rows="3" style="width:100%;"
        placeholder="one per line">${esc((v || []).join('\n'))}</textarea>`;
    }
    const typ = (e.type === 'int' || e.type === 'float') ? 'number' : 'text';
    const step = e.type === 'float' ? ' step="any"' : '';
    const lim = (e.min !== undefined ? ` min="${e.min}"` : '') +
                (e.max !== undefined ? ` max="${e.max}"` : '');
    const width = typ === 'number' ? 'width:120px;' : 'width:min(340px,100%);';
    return `<input type="${typ}"${step}${lim} class="ap-input st-input" data-path="${p}"
      style="${width}" value="${esc(v ?? '')}">`;
  }

  function bothNote(e) {
    if (e.service === 'both' && _data.topology.split && !_data.topology.ae_config_reachable) {
      return `<div class="adm-muted" style="font-size:12px;margin-top:3px;">` +
        `saved locally — also update the alarm-engine host's copy</div>`;
    }
    return '';
  }

  function renderField(e) {
    return `<div class="settings-row" data-path="${esc(e.path)}"
        style="display:flex;gap:14px;padding:8px 0;border-bottom:1px solid var(--border-soft);align-items:flex-start;">
      <div style="flex:0 0 300px;min-width:220px;">
        <div style="color:var(--fg);">${esc(e.label)}</div>
        <div class="adm-muted" style="font-size:12px;">${esc(e.help)}</div>
      </div>
      <div style="flex:1;min-width:220px;">${inputFor(e)}${bothNote(e)}</div>
    </div>`;
  }

  function render(root) {
    const byGroup = {};
    _data.entries.forEach(e => (byGroup[e.group] ||= []).push(e));
    let html = '';
    for (const g of _data.groups) {
      const entries = byGroup[g.key];
      if (!entries) continue;
      html += `<div class="adm-card" style="padding:14px 18px;">
        <div class="adm-card-hdr" style="padding:0 0 6px;">
          <div class="adm-title">${esc(g.title)}</div>
        </div>
        ${entries.map(renderField).join('')}</div>`;
    }
    root.innerHTML = html;
    renderBanner();
    updateSaveBar();
    if (!root._stWired) {
      root._stWired = true;
      root.addEventListener('input', onInput);
      root.addEventListener('change', onInput);
      root.addEventListener('click', onClick);
    }
  }

  // ── editing ───────────────────────────────────────────────────────

  function readInput(el, entry) {
    if (entry.type === 'bool' && !entry.secret) return el.checked;
    if (entry.type === 'list') {
      return el.value.split('\n').map(s => s.trim()).filter(Boolean);
    }
    if (!entry.secret && (entry.type === 'int' || entry.type === 'float')) {
      return el.value === '' ? null : Number(el.value);
    }
    return el.value;
  }

  function onInput(ev) {
    const el = ev.target.closest('.st-input');
    if (!el || !_data) return;
    const entry = entryFor(el.dataset.path);
    if (!entry) return;
    const cur = readInput(el, entry);
    if (entry.secret) {
      // Blank secret input = leave unchanged (unless a Clear queued null).
      const blank = entry.type === 'list' ? !cur.length : cur === '';
      if (blank && _dirty.get(entry.path) !== null) _dirty.delete(entry.path);
      else if (!blank) _dirty.set(entry.path, cur);
    } else {
      const orig = currentValue(entry);
      const same = JSON.stringify(cur) === JSON.stringify(orig);
      if (same) _dirty.delete(entry.path);
      else _dirty.set(entry.path, cur);
    }
    updateSaveBar();
  }

  function onClick(ev) {
    const btn = ev.target.closest('[data-clear]');
    if (!btn) return;
    ev.preventDefault();
    _dirty.set(btn.dataset.clear, null);   // null = explicit clear (server contract)
    btn.textContent = 'Clear queued';
    btn.disabled = true;
    updateSaveBar();
  }

  function updateSaveBar() {
    let bar = document.getElementById('adminSettingsSaveBar');
    if (!_dirty.size) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'adminSettingsSaveBar';
      bar.style.cssText = 'position:sticky;bottom:0;display:flex;gap:12px;align-items:center;' +
        'padding:12px 16px;margin-top:12px;background:var(--bg-card);' +
        'border:1px solid var(--border-strong);border-radius:8px;z-index:5;';
      document.getElementById('adminSettingsRoot').appendChild(bar);
    }
    bar.innerHTML = `<span style="color:var(--fg);">${_dirty.size} unsaved change${_dirty.size > 1 ? 's' : ''}</span>
      <button class="adm-btn primary" id="adminSettingsSaveBtn">Save</button>
      <button class="adm-btn" id="adminSettingsDiscardBtn">Discard</button>
      <span class="adm-muted" id="adminSettingsSaveMsg"></span>`;
    document.getElementById('adminSettingsSaveBtn').onclick = save;
    document.getElementById('adminSettingsDiscardBtn').onclick = () => { _dirty.clear(); load(); };
  }

  async function save() {
    const changes = Object.fromEntries(_dirty);
    const msg = document.getElementById('adminSettingsSaveMsg');
    if (msg) msg.textContent = 'saving…';
    let r, d;
    try {
      r = await fetch('/api/admin/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes }),
      });
      d = await r.json().catch(() => ({}));
    } catch (e) {
      if (msg) msg.textContent = `save failed — ${String(e)}`;
      return;
    }
    if (r.ok && d.ok) {
      _dirty.clear();
      await load();
      showResult(d);
    } else {
      if (msg) msg.textContent = 'save failed — fix the errors below';
      showErrors(d.errors || { _: d.error || `HTTP ${r.status}` });
    }
  }

  function showErrors(errors) {
    document.querySelectorAll('.st-err').forEach(n => n.remove());
    for (const [path, m] of Object.entries(errors)) {
      const row = document.querySelector(`.settings-row[data-path="${CSS.escape(path)}"]`);
      const note = document.createElement('div');
      note.className = 'st-err';
      note.style.cssText = 'color:var(--crit);font-size:12px;margin-top:4px;';
      note.textContent = m;
      ((row && row.lastElementChild) || document.getElementById('adminSettingsRoot')).appendChild(note);
    }
  }

  function showResult(d) {
    const note = document.createElement('div');
    note.className = 'adm-muted';
    note.style.cssText = 'padding:8px 0;';
    note.textContent = d.ae_sync_failed
      ? `Saved locally, but alarm-engine sync failed: ${d.ae_sync_failed}`
      : '✓ Saved';
    const root = document.getElementById('adminSettingsRoot');
    root.insertBefore(note, root.firstChild);
    setTimeout(() => note.remove(), 6000);
  }

  // ── restart banner ────────────────────────────────────────────────

  const _UNIT = { manager: 'llm-systems-manager', alarm_engine: 'llm-systems-alarm-engine' };
  const _LABEL = { manager: 'Manager', alarm_engine: 'Alarm Engine' };

  function renderBanner() {
    const old = document.getElementById('adminSettingsRestartBanner');
    if (old) old.remove();
    const pending = _data.restart_pending || [];
    if (!pending.length) return;
    const topo = _data.topology || {};
    const bar = document.createElement('div');
    bar.id = 'adminSettingsRestartBanner';
    bar.className = 'adm-card';
    bar.style.cssText = 'padding:12px 16px;margin-bottom:14px;border-left:4px solid var(--warn);';
    bar.innerHTML = `<div style="color:var(--fg);"><b>Restart required</b> for saved changes to take effect:</div>` +
      pending.filter(svc => _UNIT[svc]).map(svc => {
        const remoteAe = svc === 'alarm_engine' && topo.split;
        const cmd = (remoteAe ? '(on the alarm-engine host) ' : '') +
          `sudo systemctl restart ${_UNIT[svc]}`;
        return `<div style="margin-top:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <code style="color:var(--fg-dim);">${esc(cmd)}</code>
          <button class="adm-btn warn" onclick="_restartService('${svc}')">Restart ${_LABEL[svc]}</button>
        </div>`;
      }).join('');
    const root = document.getElementById('adminSettingsRoot');
    root.insertBefore(bar, root.firstChild);
  }

  window.adminSettingsLoad = load;
})();
