// Model editor layouts (#765): rail/essentials modes, toggle switches,
// dirty tracking with was-value breadcrumbs, accordion summaries.
const EFL = (function () {
  const ESS_KEYS = ['temperature', 'ctx-size', 'reasoning', 'reasoning-budget'];
  const SEC_MAP = {
    'ef-sec-sampling': ['temperature', 'dynatemp-range', 'dynatemp-exp', 'top-p', 'top-k', 'min-p', 'presence-penalty', 'repeat-penalty'],
    'ef-sec-context':  ['ctx-size', 'batch-size', 'ubatch-size', 'n-gpu-layers', 'predict', 'load-mode', 'fit', 'fit-ctx'],
    'ef-sec-kv':       ['cache-type-k', 'cache-type-v', 'cache-ram'],
    'ef-sec-behavior': ['flash-attn', 'reasoning', 'reasoning-budget', 'swa-full', 'swa-checkpoints', 'check-tensors'],
    'ef-sec-custom':   ['__custom'],
  };
  let _snap = null;

  const $ = id => document.getElementById(id);
  const editor = () => $('llmEditor');
  const fieldEl = k => $('ef-' + k);

  function currentMode() {
    const v = (typeof layout === 'object' && layout && layout.editLayout) || 'rail';
    return v === 'essentials' ? 'essentials' : 'rail';
  }

  function setMode(v, persist) {
    v = v === 'essentials' ? 'essentials' : 'rail';
    if (persist && typeof layout === 'object' && layout) {
      layout.editLayout = v;
      try { saveLayout(); } catch (_) {}
    }
    apply();
  }

  function apply() {
    const ed = editor(); if (!ed) return;
    const mode = currentMode();
    ed.classList.toggle('ef-railmode', mode === 'rail');
    ed.classList.toggle('ef-essmode', mode === 'essentials');
    const seg = $('efLayoutSeg');
    if (seg) seg.querySelectorAll('button[data-eflayout]').forEach(b =>
      b.classList.toggle('on', b.dataset.eflayout === mode));
    const ess = $('efEssBox');
    if (ess) ess.style.display = mode === 'essentials' ? '' : 'none';
    moveEssFields(mode === 'essentials');
    refreshSummaries();
  }

  // Essentials mode relocates four field wrappers into the essentials box;
  // hidden placeholder spans mark their home slots for the way back.
  function moveEssFields(toEss) {
    const grid = $('efEssGrid'); if (!grid) return;
    ESS_KEYS.forEach(k => {
      const f = document.querySelector('#llmEditor .ef-field[data-ef="' + k + '"]');
      if (!f) return;
      if (toEss) {
        if (f.parentElement === grid) return;
        const ph = document.createElement('span');
        ph.className = 'ef-home'; ph.style.display = 'none'; ph.dataset.for = k;
        f.parentElement.insertBefore(ph, f);
        grid.appendChild(f);
      } else {
        const ph = document.querySelector('#llmEditor .ef-home[data-for="' + k + '"]');
        if (ph) { ph.parentElement.insertBefore(f, ph); ph.remove(); }
      }
    });
  }

  function customBlob() {
    return [...document.querySelectorAll('#ef-custom-params .custom-param-row')]
      .map(r => ((r.querySelector('.cp-key') || {}).value || '') + '=' + ((r.querySelector('.cp-val') || {}).value || ''))
      .filter(s => s !== '=').join('\n');
  }

  function readAll() {
    const vals = {};
    const keys = (typeof EF_FIELDS !== 'undefined') ? EF_FIELDS.concat(['load-mode']) : ['load-mode'];
    keys.forEach(k => { const el = fieldEl(k); if (el) vals[k] = el.value; });
    const alias = $('ef-alias'); if (alias) vals['__alias'] = alias.value;
    const idEl = $('ef-id'); if (idEl && !idEl.disabled) vals['__id'] = idEl.value;
    vals['__custom'] = customBlob();
    return vals;
  }

  function snapshot() { _snap = readAll(); refreshDirty(); }
  function markClean() { _snap = null; refreshDirty(); }
  function countDirty() {
    if (!_snap) return { n: 0, byField: {} };
    const now = readAll();
    const byField = {};
    let n = 0;
    Object.keys(now).forEach(k => {
      if (String(now[k]) !== String(_snap[k] != null ? _snap[k] : '')) { byField[k] = _snap[k] != null ? _snap[k] : ''; n++; }
    });
    return { n, byField };
  }
  function isDirty() { return countDirty().n > 0; }

  function refreshDirty() {
    const ed = editor(); if (!ed) return;
    const { n, byField } = countDirty();
    const has = k => Object.prototype.hasOwnProperty.call(byField, k);
    ed.querySelectorAll('.ef-field[data-ef]').forEach(f => {
      const k = f.dataset.ef;
      const dirty = has(k);
      f.classList.toggle('modded', dirty);
      const was = f.querySelector('.ef-was');
      if (was) was.textContent = dirty ? 'was ' + (byField[k] === '' ? '(empty)' : byField[k]) : '';
    });
    document.querySelectorAll('#efRail .ef-rail-item').forEach(it => {
      const keys = SEC_MAP[it.dataset.target] || [];
      it.classList.toggle('has-mod', keys.some(has));
    });
    const chip = $('efChgChip');
    if (chip) {
      chip.textContent = n === 1 ? '1 unsaved change' : n + ' unsaved changes';
      chip.classList.toggle('show', n > 0);
    }
    const rst = $('efResetBtn'); if (rst) rst.style.display = n > 0 ? '' : 'none';
    refreshSummaries();
  }

  // ---- toggle switches over the hidden on/off(/auto) selects ----
  function _onOffVals(sel) {
    const opts = [...sel.options].map(o => o.value);
    return {
      on:  opts.includes('on') ? 'on' : 'true',
      off: opts.includes('off') ? 'off' : '',
      hasAuto: opts.includes('auto'),
    };
  }

  function syncToggles() {
    document.querySelectorAll('#llmEditor .mc-toggle[data-eftoggle]').forEach(t => {
      const sel = $(t.dataset.eftoggle); if (!sel) return;
      const v = sel.value;
      const isAuto = v === 'auto';
      const isOn = v === 'on' || v === 'true';
      t.classList.toggle('on', isOn);
      t.setAttribute('aria-checked', String(isOn));
      t.style.opacity = isAuto ? '.55' : '';
      const lbl = t.querySelector('.tlbl');
      if (lbl) lbl.textContent = isAuto ? 'auto' : (isOn ? 'on' : 'off');
      const chip = document.querySelector('#llmEditor .mc-auto-chip[data-efauto="' + t.dataset.eftoggle + '"]');
      if (chip) chip.classList.toggle('on', isAuto);
    });
  }

  function _setSel(sel, v) {
    sel.value = v;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    syncToggles();
  }

  function reset() {
    if (!_snap) return;
    const keys = (typeof EF_FIELDS !== 'undefined') ? EF_FIELDS.concat(['load-mode']) : ['load-mode'];
    keys.forEach(k => { const el = fieldEl(k); if (el && _snap[k] != null) el.value = _snap[k]; });
    const alias = $('ef-alias'); if (alias && _snap['__alias'] != null) alias.value = _snap['__alias'];
    const idEl = $('ef-id'); if (idEl && !idEl.disabled && _snap['__id'] != null) idEl.value = _snap['__id'];
    const container = $('ef-custom-params');
    if (container) {
      container.innerHTML = '';
      (_snap['__custom'] || '').split('\n').filter(Boolean).forEach(line => {
        const i = line.indexOf('=');
        if (i > 0 && typeof addCustomParam === 'function') addCustomParam(line.slice(0, i), line.slice(i + 1));
      });
    }
    syncToggles();
    refreshDirty();
  }

  async function confirmDiscard() {
    if (typeof _themedConfirm !== 'function') return true;
    return _themedConfirm({
      title: 'Discard unsaved changes?',
      bodyHtml: 'The edits in this form have not been saved.',
      confirmLabel: 'Discard', cancelLabel: 'Keep editing', danger: true,
    });
  }

  // ---- accordion summaries (essentials mode) ----
  function refreshSummaries() {
    const ed = editor();
    if (!ed || !ed.classList.contains('ef-essmode')) return;
    ed.querySelectorAll('.ef-collapsible').forEach(sec => {
      const head = sec.querySelector('.ef-acc-head'); if (!head) return;
      const sum = head.querySelector('.sum');
      const ndef = head.querySelector('.ndef');
      if (sec.id === 'ef-sec-custom') {
        const rows = [...document.querySelectorAll('#ef-custom-params .custom-param-row')];
        const keys = rows.map(r => (r.querySelector('.cp-key') || {}).value || '').filter(Boolean);
        if (sum) sum.textContent = keys.slice(0, 3).join(' · ') + (keys.length > 3 ? ' …' : '');
        if (ndef) ndef.textContent = keys.length ? keys.length + ' set' : '';
        return;
      }
      const keys = (SEC_MAP[sec.id] || []).filter(k => k !== '__custom');
      let nNonDef = 0;
      keys.forEach(k => {
        const el = fieldEl(k); if (!el) return;
        const def = (typeof EF_DEFAULTS !== 'undefined' && EF_DEFAULTS[k] != null) ? EF_DEFAULTS[k]
                  : (k === 'load-mode' ? 'auto' : null);
        if (def != null && String(el.value) !== String(def)) nNonDef++;
      });
      const parts = keys
        .filter(k => !ESS_KEYS.includes(k))
        .slice(0, 3)
        .map(k => {
          const f = document.querySelector('#llmEditor .ef-field[data-ef="' + k + '"]');
          const el = fieldEl(k);
          const label = (f && f.dataset.sum) || k;
          return label + ' ' + (el && el.value !== '' ? el.value : '—');
        });
      if (sum) sum.textContent = parts.join(' · ');
      if (ndef) ndef.textContent = nNonDef ? nNonDef + ' non-default' : '';
    });
  }

  // Called by openEditModel/openAddModel/copyFromProfile after populating.
  function onOpen() {
    apply();
    syncToggles();
    snapshot();
    const pane = $('efPane');
    if (pane) pane.scrollTop = 0;
    document.querySelectorAll('#efRail .ef-rail-item').forEach((it, i) =>
      it.classList.toggle('on', i === 0));
  }

  function _wire() {
    const ed = editor(); if (!ed || ed._eflWired) return;
    ed._eflWired = true;

    ed.addEventListener('click', ev => {
      const t = ev.target.closest('.mc-toggle[data-eftoggle]');
      if (t) {
        const sel = $(t.dataset.eftoggle);
        if (sel) {
          const { on, off } = _onOffVals(sel);
          const v = sel.value;
          _setSel(sel, (v === 'on' || v === 'true') ? off : on);
        }
        return;
      }
      const chip = ev.target.closest('.mc-auto-chip[data-efauto]');
      if (chip) {
        const sel = $(chip.dataset.efauto);
        if (sel) _setSel(sel, sel.value === 'auto' ? _onOffVals(sel).on : 'auto');
        return;
      }
      const acc = ev.target.closest('.ef-acc-head[data-efacc]');
      if (acc) { acc.closest('.ef-collapsible').classList.toggle('open'); return; }
      const rail = ev.target.closest('.ef-rail-item[data-target]');
      if (rail) {
        document.querySelectorAll('#efRail .ef-rail-item').forEach(it =>
          it.classList.toggle('on', it === rail));
        const sec = $(rail.dataset.target);
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
    });
    ed.addEventListener('keydown', ev => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      const tgt = ev.target.closest('.mc-toggle[data-eftoggle], .mc-auto-chip[data-efauto], .ef-acc-head[data-efacc]');
      if (tgt) { ev.preventDefault(); tgt.click(); }
    });
    ed.addEventListener('input', refreshDirty);
    ed.addEventListener('change', refreshDirty);

    const seg = $('efLayoutSeg');
    if (seg) seg.addEventListener('click', ev => {
      const b = ev.target.closest('button[data-eflayout]');
      if (b) setMode(b.dataset.eflayout, true);
    });

    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape') return;
      if (!ed || ed.style.display === 'none') return;
      if (document.querySelector('.mc-menu.open')) return;
      const ae = document.activeElement;
      if (ae && ae.tagName === 'INPUT' && ae.closest('.mc-name')) return;
      closeEditor();
    });
  }

  if (typeof document !== 'undefined') _wire();

  return { apply, setMode, onOpen, snapshot, markClean, isDirty, reset, confirmDiscard, syncToggles, refreshDirty };
})();
