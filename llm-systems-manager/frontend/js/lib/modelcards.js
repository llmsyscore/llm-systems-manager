// Shared model-card rendering (#765): card/list/compact views, status pills,
// overflow menus, per-surface filter/collapse/busy state. Dual-mode lib.
(function () {
  const VIEWS = ['card', 'list', 'compact'];

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function validView(v) { return VIEWS.includes(v) ? v : 'compact'; }

  function viewOf(layoutObj, surface) {
    const mv = layoutObj && layoutObj.modelView;
    return validView(mv && mv[surface]);
  }

  // Short human age for a benchmark timestamp; null when unparsable.
  function age(ts, nowMs) {
    if (!ts) return null;
    const t = Date.parse(ts);
    if (isNaN(t)) return null;
    const s = Math.max(0, ((nowMs != null ? nowMs : Date.now()) - t) / 1000);
    if (s < 90)        return 'just now';
    if (s < 5400)      return Math.round(s / 60) + 'm ago';
    if (s < 129600)    return Math.round(s / 3600) + 'h ago';
    if (s < 5184000)   return Math.round(s / 86400) + 'd ago';
    return Math.round(s / 2592000) + 'mo ago';
  }

  function pill(p) {
    const st = validState(p && p.state);
    return `<span class="mc-pill p-${st}">${esc((p && p.label) || '')}</span>`;
  }
  function validState(s) {
    return ['active', 'idle', 'sleeping', 'unloaded', 'busy'].includes(s) ? s : 'unloaded';
  }
  function dot(state) {
    const st = validState(state);
    return `<span class="mc-dot${st === 'unloaded' ? '' : ' d-' + st}"></span>`;
  }

  function specsHtml(specs) {
    if (!specs || !specs.length) return '';
    return '<dl class="mc-specs">' + specs.map(s =>
      `<div class="mc-spec"><dt>${esc(s.k)}</dt><dd>${s.em ? '<em>' + esc(s.v) + '</em>' : esc(s.v)}</dd></div>`
    ).join('') + '</dl>';
  }

  function statsHtml(stats, fresh, benchTitle) {
    const cells = (stats || []).map(s =>
      `<div class="mc-stat"><div class="l">${esc(s.l)}</div><div class="v${s.live ? ' live' : ''}"><b>${esc(s.v)}</b>${s.unit ? ' ' + esc(s.unit) : ''}</div></div>`
    ).join('');
    if (!cells && !(fresh && fresh.stale)) return '';
    const tag = cells
      ? `<span class="mc-benchtag" title="${esc(benchTitle || 'Benchmark results — not live throughput')}">bench</span>`
      : '';
    const f = (fresh && fresh.stale)
      ? `<span class="mc-stale" title="${esc(fresh.staleTitle || 'Config changed since this benchmark — run a fresh one from the ⋯ menu')}">re-bench</span>`
      : '';
    return `<div class="mc-stats">${tag}${f}${cells}</div>`;
  }

  function _menuItems(items, d) {
    return (items || []).map(it => it === '-' ? '<hr>' :
      `<button ${d.actAttr}="${esc(it.act)}" data-id="${esc(d.id)}" class="${it.danger ? 'danger' : ''}">${esc(it.label)}</button>`
    ).join('');
  }

  function menuHtml(d, items) {
    const rows = _menuItems(items, d);
    if (!rows) return '';
    return `<div class="mc-menuwrap"><button type="button" class="mcbtn mcbtn-ghost mcbtn-icon mc-menubtn" aria-label="More actions" aria-haspopup="true">⋯</button><div class="mc-menu">${rows}</div></div>`;
  }

  function btnHtml(d, b, cls) {
    return `<button class="mcbtn ${cls}" ${d.actAttr}="${esc(b.act)}" data-id="${esc(d.id)}"${d.transition ? ' disabled' : ''}>${esc(b.label)}</button>`;
  }

  function actionsHtml(d) {
    if (!d.primary && !(d.buttons || []).length && !(d.menu || []).length) return '';
    const parts = [];
    if (d.primary) parts.push(btnHtml(d, d.primary, 'mcbtn-pri'));
    (d.buttons || []).forEach(b => parts.push(btnHtml(d, b, 'mcbtn-ghost')));
    parts.push('<span class="mc-gap"></span>');
    parts.push(menuHtml(d, d.menu));
    return `<div class="mc-actions">${parts.join('')}</div>`;
  }

  function nameHtml(d) {
    const rename = d.renameAct
      ? ` title="Click to edit alias (blank = use Model ID)" ${d.actAttr}="${esc(d.renameAct)}" data-id="${esc(d.id)}"`
      : '';
    return `<div class="mc-title"><div class="mc-name"${rename}>${esc(d.name)}</div>${d.repo ? `<div class="mc-repo" title="${esc(d.repo)}">${esc(d.repo)}</div>` : ''}</div>`;
  }

  function card(d) {
    return `<div class="mc-card${d.transition ? ' mc-transition' : ''}" data-id="${esc(d.id)}">
      <div class="mc-head">${nameHtml(d)}${pill(d.pill)}</div>
      ${specsHtml(d.specs)}
      ${teleHtml(d)}
      ${d.perfHtml || ''}
      ${actionsHtml(d)}
    </div>`;
  }

  function teleHtml(d) {
    const prof = d.profileHtml || '';
    const stats = statsHtml(d.stats, d.fresh, d.benchTitle);
    if (!prof && !stats) return '';
    return `<div class="mc-tele">${prof}${stats}</div>`;
  }

  function compact(d) {
    return `<div class="mc-card${d.open ? ' open' : ''}${d.transition ? ' mc-transition' : ''}" data-id="${esc(d.id)}">
      <div class="mc-ctop" data-mctoggle="${esc(d.id)}" role="button" tabindex="0" aria-expanded="${d.open ? 'true' : 'false'}">
        <span class="mc-chev">▶</span>
        <div class="mc-title"><div class="mc-name">${esc(d.name)}</div>${d.csub ? `<div class="mc-csub">${d.csub}</div>` : ''}</div>
        ${pill(d.pill)}
      </div>
      <div class="mc-drawer">
        ${specsHtml(d.specs)}
        ${teleHtml(d)}
        ${d.perfHtml || ''}
        ${actionsHtml(d)}
      </div>
    </div>`;
  }

  function row(d) {
    const cfg = (d.specs || []).map(s => `${esc(String(s.k).toLowerCase())} ${esc(s.v)}`).join('<i>·</i>');
    const met = (d.stats || []).slice(0, 3).map(s =>
      `<div class="mc-stat"><div class="l">${esc(s.l)}</div><div class="v"><b>${esc(s.v)}</b></div></div>`).join('');
    const menuItems = [
      ...(d.buttons || []).map(b => ({ act: b.act, label: b.label })),
      ...((d.buttons || []).length && (d.menu || []).filter(i => i !== '-').length ? ['-'] : []),
      ...(d.menu || []),
    ];
    // Collapse doubled separators when buttons is empty but menu leads with one.
    return `<div class="mc-row${d.transition ? ' mc-transition' : ''}" data-id="${esc(d.id)}">
      ${dot(d.pill && d.pill.state)}
      <div class="mc-rowname"><div class="n">${esc(d.name)}</div>${d.repo ? `<div class="r">${esc(d.repo)}</div>` : ''}</div>
      <div class="mc-rowcfg">${cfg}</div>
      <div class="mc-rowmet">${met}</div>
      <div class="mc-rowprof">${d.profileHtml || ''}${d.fresh && d.fresh.stale ? ` <span class="mc-stale" title="${esc(d.fresh.staleTitle || 'Config changed since this benchmark')}">re-bench</span>` : ''}</div>
      <div class="mc-rowact">${d.primary ? btnHtml(d, d.primary, 'mcbtn-pri') : ''}${menuHtml(d, menuItems)}</div>
    </div>`;
  }

  function rowHeader(metLabel, profLabel, metTitle) {
    return `<div class="mc-row mc-row-hdr"><span></span><span>Model</span><span>Configuration</span><div class="mc-rowmet mc-methdr"${metTitle ? ` title="${esc(metTitle)}"` : ''}><span>${esc(metLabel || '')}</span></div><span class="mc-profhdr">${esc(profLabel || '')}</span><span></span></div>`;
  }

  function groupRow(name, count, collapsed) {
    return `<div class="mc-row mc-grouprow" data-mcgroup="${esc(name)}" role="button"><span></span><span class="g">${collapsed ? '▸' : '▾'} ${esc(name)} — ${count}</span></div>`;
  }

  function groupHeader(name, count, collapsed) {
    return `<div class="model-group-header mc-grouphead" data-mcgroup="${esc(name)}" role="button" style="grid-column:1/-1;cursor:pointer;">
      <span>${collapsed ? '▸' : '▾'} ${esc(name)}</span>
      <span class="rule"></span>
      <span class="count">${count}</span>
    </div>`;
  }

  // ---- per-surface UI state (browser-session only) ----
  const _filter = {};      // surface -> lowercased filter text
  const _collapsed = {};   // surface -> Set(group name)
  const _open = {};        // surface -> Set(model id) for compact drawers
  const _busy = {};        // surface -> { id: 'Loading…' }

  function filterOf(surface) { return _filter[surface] || ''; }
  function filterMatch(surface, ...hay) {
    const q = filterOf(surface);
    if (!q) return true;
    return hay.some(h => String(h || '').toLowerCase().includes(q));
  }
  function isCollapsed(surface, name) { return !!(_collapsed[surface] && _collapsed[surface].has(name)); }
  function toggleGroup(surface, name) {
    const set = _collapsed[surface] = _collapsed[surface] || new Set();
    if (set.has(name)) set.delete(name); else set.add(name);
  }
  function isOpen(surface, id) { return !!(_open[surface] && _open[surface].has(id)); }
  function toggleOpen(surface, id) {
    const set = _open[surface] = _open[surface] || new Set();
    if (set.has(id)) set.delete(id); else set.add(id);
  }
  function setBusy(surface, id, label) { (_busy[surface] = _busy[surface] || {})[id] = label; }
  function clearBusy(surface, id) { if (_busy[surface]) delete _busy[surface][id]; }
  function busyOf(surface, id) { return (_busy[surface] || {})[id] || null; }

  // ---- browser wiring (no-ops under test) ----
  const _renderers = {};

  function setView(surface, v) {
    v = validView(v);
    if (typeof layout === 'object' && layout) {
      layout.modelView = layout.modelView || {};
      layout.modelView[surface] = v;
      try { saveLayout(); } catch (_) {}
    }
    syncSeg(surface);
    if (_renderers[surface]) _renderers[surface]();
  }

  function syncSeg(surface) {
    const seg = typeof document !== 'undefined' && document.getElementById(_segIds[surface] || '');
    if (!seg) return;
    const v = viewOf(typeof layout === 'object' ? layout : null, surface);
    seg.querySelectorAll('button[data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  }

  const _segIds = {};

  function initToolbar(surface, opts) {
    _renderers[surface] = opts.render;
    if (opts.segId) {
      _segIds[surface] = opts.segId;
      const seg = document.getElementById(opts.segId);
      if (seg && !seg._mcWired) {
        seg._mcWired = true;
        seg.addEventListener('click', ev => {
          const b = ev.target.closest('button[data-view]');
          if (b) setView(surface, b.dataset.view);
        });
      }
      syncSeg(surface);
    }
    if (opts.filterId) {
      const box = document.getElementById(opts.filterId);
      const input = box && box.querySelector('input');
      if (box && input && !box._mcWired) {
        box._mcWired = true;
        input.addEventListener('input', () => {
          _filter[surface] = input.value.trim().toLowerCase();
          box.classList.toggle('has-text', !!_filter[surface]);
          if (_renderers[surface]) _renderers[surface]();
        });
        const clear = box.querySelector('.mc-filter-clear');
        if (clear) clear.addEventListener('click', () => {
          input.value = '';
          input.dispatchEvent(new Event('input'));
          input.focus();
        });
      }
    }
  }

  // Container-level wiring shared by every surface: overflow menus, compact
  // drawers, group collapse. Renderers pass their own re-render fn.
  function bindContainer(container, surface, render) {
    if (!container || container._mcBound) return;
    container._mcBound = true;
    container.addEventListener('click', ev => {
      const mb = ev.target.closest('.mc-menubtn');
      if (mb) {
        const menu = mb.parentElement.querySelector('.mc-menu');
        const wasOpen = menu.classList.contains('open');
        closeMenus();
        if (!wasOpen) { menu.classList.add('open'); _positionMenu(mb, menu); }
        ev.stopPropagation();
        return;
      }
      if (ev.target.closest('.mc-menu')) closeMenus();
      const grp = ev.target.closest('[data-mcgroup]');
      if (grp) { toggleGroup(surface, grp.dataset.mcgroup); render(); return; }
      const tog = ev.target.closest('[data-mctoggle]');
      if (tog && !ev.target.closest('button') && !ev.target.closest('select')) {
        toggleOpen(surface, tog.dataset.mctoggle); render(); return;
      }
    });
    container.addEventListener('keydown', ev => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      const tog = ev.target.closest('[data-mctoggle]');
      if (tog) { ev.preventDefault(); toggleOpen(surface, tog.dataset.mctoggle); render(); }
    });
  }

  function closeMenus() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('.mc-menu.open').forEach(m => {
      m.classList.remove('open');
      m.style.position = ''; m.style.top = ''; m.style.right = ''; m.style.left = '';
    });
  }

  // Fixed positioning: escapes the list wrap's overflow clipping; opens upward
  // when there is not enough room below the button.
  function _positionMenu(btn, menu) {
    const r = btn.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.right = 'auto';
    const mw = menu.offsetWidth;
    menu.style.left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8)) + 'px';
    const mh = menu.offsetHeight;
    const below = window.innerHeight - r.bottom;
    menu.style.top = (below < mh + 12 && r.top > mh + 12 ? r.top - mh - 6 : r.bottom + 6) + 'px';
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('click', ev => {
      if (!ev.target.closest('.mc-menuwrap') && !ev.target.closest('.mc-menu')) closeMenus();
    });
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeMenus(); });
    document.addEventListener('scroll', ev => {
      if (!(ev.target.closest && ev.target.closest('.mc-menu'))) closeMenus();
    }, true);
    window.addEventListener('resize', closeMenus);
  }

  const _MC_API = {
    VIEWS, esc, validView, viewOf, age, pill, dot, specsHtml, statsHtml,
    card, compact, row, rowHeader, groupRow, groupHeader, actionsHtml, menuHtml,
    filterOf, filterMatch, isCollapsed, toggleGroup, isOpen, toggleOpen,
    setBusy, clearBusy, busyOf, setView, syncSeg, initToolbar, bindContainer, closeMenus,
  };
  if (typeof window !== 'undefined') window.MC = _MC_API;
  if (typeof module !== 'undefined' && module.exports) module.exports = _MC_API;
})();
