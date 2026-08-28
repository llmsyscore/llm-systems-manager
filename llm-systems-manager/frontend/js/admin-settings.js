// Admin → Settings sub-tab (#606): catalog-driven TOML settings editor.
// Talks to GET/PUT /api/admin/settings; restarts go through the service API.
(() => {
  'use strict';

  let _data = null;
  let _entryByPath = new Map();
  const _dirty = new Map();   // path -> raw value to submit (null = clear secret)

  // Shared escaper from foundation.js (loads before this file).
  const esc = s => _esc(String(s ?? ''));

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
        `Failed to load settings — ${esc(e && e.message || e)}</div>`;
      return;
    }
    _entryByPath = new Map(_data.entries.map(e => [e.path, e]));
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
    if (e.nullable) return null;   // absent = inherit/default
    return e.type === 'list' ? [] : '';
  }

  function secretChip(e) {
    const st = _data.secrets[e.path];   // undefined = unknown (AE unreachable)
    const cls = st === 'set' ? 'status--ok' : 'status--muted';
    return `<span class="status ${cls} status--square">${st ? (st === 'set' ? 'set' : 'not set') : 'unknown'}</span>`;
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
    if (aeLocked(e)) {
      const v = _data.values[e.path];
      const shown = e.secret ? secretChip(e)
        : esc(v === undefined ? 'unknown' : (Array.isArray(v) ? v.join(', ') : String(v)));
      return `<span class="adm-muted">🔒 ${shown}` +
        ` — alarm engine unreachable; edit the TOML on its host</span>`;
    }
    if (e.secret) return secretInput(e);
    const p = esc(e.path);
    const v = currentValue(e);
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
    const ph = e.nullable ? ' placeholder="inherit"' : '';
    const width = typ === 'number' ? 'width:120px;' : 'width:min(340px,100%);';
    return `<input type="${typ}"${step}${lim}${ph} class="ap-input st-input" data-path="${p}"
      style="${width}" value="${esc(v ?? '')}">`;
  }

  function bothNote(e) {
    if (e.service === 'both' && _data.topology.split && !_data.topology.ae_config_reachable) {
      return `<div class="adm-muted" style="font-size:12px;margin-top:3px;">` +
        `saved locally — also update the alarm-engine host's copy</div>`;
    }
    return '';
  }

  function driftVal(v) {
    if (v === null || v === undefined) return 'unset';
    return Array.isArray(v) ? v.join(', ') : String(v);
  }

  function driftNote(e) {
    const d = _data.drift && _data.drift[e.path];
    if (!d) return '';
    const detail = d.secret ? `local ${d.local} / AE ${d.ae}`
      : `local ${driftVal(d.local)} / AE ${driftVal(d.ae)}`;
    return `<div style="font-size:12px;margin-top:3px;color:var(--warn);">` +
      `⚠ differs on the alarm-engine host — ${esc(detail)}</div>`;
  }

  function renderField(e) {
    return `<div class="settings-row" data-path="${esc(e.path)}"
        style="display:flex;gap:14px;padding:8px 0;border-bottom:1px solid var(--border-soft);align-items:flex-start;">
      <div style="flex:0 0 300px;min-width:220px;">
        <div style="color:var(--fg);">${esc(e.label)}</div>
        <div class="adm-muted" style="font-size:12px;">${esc(e.help)}</div>
      </div>
      <div style="flex:1;min-width:220px;">${inputFor(e)}${bothNote(e)}${driftNote(e)}</div>
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
    renderDriftBanner();
    updateSaveBar();
  }

  // ── editing ───────────────────────────────────────────────────────

  function readInput(el, entry) {
    switch (entry.type) {
      case 'bool': return el.checked;
      case 'list': return el.value.split('\n').map(s => s.trim()).filter(Boolean);
      case 'int':
      case 'float': return el.value === '' ? null : Number(el.value);
      default: return (entry.nullable && el.value === '') ? null : el.value;
    }
  }

  function onInput(ev) {
    const el = ev.target.closest('.st-input');
    if (!el || !_data) return;
    const entry = _entryByPath.get(el.dataset.path);
    if (!entry) return;
    const cur = entry.secret && entry.type !== 'list' ? el.value : readInput(el, entry);
    if (entry.secret) {
      // Blank secret input = leave unchanged; typing overrides a queued Clear.
      const blank = Array.isArray(cur) ? !cur.length : cur === '';
      if (blank) _dirty.delete(entry.path);
      else _dirty.set(entry.path, cur);
      syncClearButton(entry.path);
    } else {
      const same = JSON.stringify(cur) === JSON.stringify(currentValue(entry));
      if (same) _dirty.delete(entry.path);
      else _dirty.set(entry.path, cur);
    }
    updateSaveBar();
  }

  function syncClearButton(path) {
    const btn = document.querySelector(`[data-clear="${CSS.escape(path)}"]`);
    if (!btn) return;
    const queued = _dirty.get(path) === null;
    btn.textContent = queued ? 'Clear queued' : 'Clear';
    btn.disabled = queued;
  }

  function onClick(ev) {
    const clr = ev.target.closest('[data-clear]');
    if (clr) {
      ev.preventDefault();
      _dirty.set(clr.dataset.clear, null);   // null = explicit clear (server contract)
      syncClearButton(clr.dataset.clear);
      updateSaveBar();
      return;
    }
    const rst = ev.target.closest('[data-restart]');
    if (rst) {
      ev.preventDefault();
      restartService(rst.dataset.restart);
    }
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
      bar.innerHTML = `<span id="adminSettingsDirtyCount" style="color:var(--fg);"></span>
        <button class="adm-btn primary" id="adminSettingsSaveBtn">Save</button>
        <button class="adm-btn" id="adminSettingsDiscardBtn">Discard</button>
        <span class="adm-muted" id="adminSettingsSaveMsg"></span>`;
      document.getElementById('adminSettingsRoot').appendChild(bar);
      document.getElementById('adminSettingsSaveBtn').onclick = save;
      document.getElementById('adminSettingsDiscardBtn').onclick = () => { _dirty.clear(); load(); };
    }
    document.getElementById('adminSettingsDirtyCount').textContent =
      `${_dirty.size} unsaved change${_dirty.size > 1 ? 's' : ''}`;
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
      if (d.ae_sync_failed) {
        // Keep unapplied (AE-side) edits in the form so they aren't lost.
        (d.applied || []).forEach(p => _dirty.delete(p));
        if (_dirty.size) {
          updateSaveBar();
          const m = document.getElementById('adminSettingsSaveMsg');
          if (m) m.textContent = `saved locally; AE sync failed: ${d.ae_sync_failed}${aeQueuedSuffix(d)}`;
          _data.ae_sync_pending = d.ae_sync_pending || [];
          renderAePendingNote();
          return;
        }
      }
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
      ? `Saved locally, but alarm-engine sync failed: ${d.ae_sync_failed}${aeQueuedSuffix(d)}`
      : '✓ Saved';
    const root = document.getElementById('adminSettingsRoot');
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

  function renderAePendingNote() {
    const oldNote = document.getElementById('adminSettingsAePending');
    if (oldNote) oldNote.remove();
    const q = _data.ae_sync_pending || [];
    if (!q.length) return;
    const note = document.createElement('div');
    note.id = 'adminSettingsAePending';
    note.className = 'adm-card';
    note.style.cssText = 'padding:12px 16px;margin-bottom:14px;border-left:4px solid var(--warn);';
    note.innerHTML = `<div style="color:var(--fg);"><b>${q.length} alarm-engine setting${q.length === 1 ? '' : 's'} queued</b>` +
      ` — not acknowledged yet; ${aeRetryText()}.</div>` +
      `<div class="adm-muted" style="margin-top:4px;">${q.map(esc).join(', ')}</div>`;
    const root = document.getElementById('adminSettingsRoot');
    root.insertBefore(note, root.firstChild);
    // One re-poll while queued so the note clears itself once the AE acks (not mid-edit).
    clearTimeout(_aePollTimer);
    _aePollTimer = setTimeout(() => {
      if (document.getElementById('adminSettingsAePending') && !_dirty.size) load();
    }, (((_data && _data.ae_sync_retry_s) || 30) + 5) * 1000);
  }

  // ── restart banner ────────────────────────────────────────────────

  const _UNIT = { manager: 'llm-systems-manager', alarm_engine: 'llm-systems-alarm-engine' };
  const _LABEL = { manager: 'Manager', alarm_engine: 'Alarm Engine' };

  function bannerMsg(text, isErr) {
    const el = document.getElementById('adminSettingsRestartMsg');
    if (el) {
      el.textContent = text;
      el.style.color = isErr ? 'var(--crit)' : 'var(--fg-dim)';
    }
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

  function renderBanner() {
    const old = document.getElementById('adminSettingsRestartBanner');
    if (old) old.remove();
    renderAePendingNote();
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
          <button class="adm-btn warn" data-restart="${esc(svc)}">Restart ${esc(_LABEL[svc])}</button>
        </div>`;
      }).join('') +
      `<div class="adm-muted" id="adminSettingsRestartMsg" style="margin-top:6px;font-size:12px;"></div>`;
    const root = document.getElementById('adminSettingsRoot');
    root.insertBefore(bar, root.firstChild);
  }

  // ── shared-section drift (#612, split installs) ───────────────────

  function renderDriftBanner() {
    const old = document.getElementById('adminSettingsDriftBanner');
    if (old) old.remove();
    const drift = _data.drift || {};
    const paths = Object.keys(drift);
    if (!paths.length) return;
    const names = paths.map(p => esc((_entryByPath.get(p) || { label: p }).label)).join(', ');
    const bar = document.createElement('div');
    bar.id = 'adminSettingsDriftBanner';
    bar.className = 'adm-card';
    bar.style.cssText = 'padding:12px 16px;margin-bottom:14px;border-left:4px solid var(--warn);';
    bar.innerHTML = `<div style="color:var(--fg);"><b>Config drift</b> — ` +
      `${paths.length} shared setting${paths.length > 1 ? 's' : ''} differ between this host and the alarm engine:</div>` +
      `<div class="adm-muted" style="font-size:12px;margin-top:4px;">${names}</div>` +
      `<div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
        <button class="adm-btn warn" id="adminSettingsResyncBtn">Re-sync local values to AE</button>
        <span class="adm-muted" id="adminSettingsResyncMsg" style="font-size:12px;"></span></div>`;
    const root = document.getElementById('adminSettingsRoot');
    root.insertBefore(bar, root.firstChild);
    document.getElementById('adminSettingsResyncBtn').onclick = () => resyncDrift(paths);
  }

  async function resyncDrift(paths) {
    const msg = document.getElementById('adminSettingsResyncMsg');
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

  const _root = document.getElementById('adminSettingsRoot');
  if (_root) {
    _root.addEventListener('input', onInput);
    _root.addEventListener('change', onInput);
    _root.addEventListener('click', onClick);
  }

  window.adminSettingsLoad = load;
})();
