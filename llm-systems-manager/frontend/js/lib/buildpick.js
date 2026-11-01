// Builds row (#1395): one selection circle per runnable llama.cpp build in a run form.
// IIFE-scoped, exposed as window.BUILDPICK.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.BUILDPICK = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Build name: the row's name, else the binary's folder with trailing bin/build segments dropped.
function nameOf(b) {
  if (b && b.name) return String(b.name);
  const parts = String((b && (b.dir || b.path)) || '').split('/').filter(Boolean);
  if (b && !b.dir && parts.length) parts.pop();
  while (parts.length > 1 && (parts[parts.length - 1] === 'bin' || parts[parts.length - 1] === 'build')) parts.pop();
  return parts.length ? parts[parts.length - 1] : '';
}

function runnable(list) {
  return (Array.isArray(list) ? list : []).filter(b => b && b.ok && b.path);
}

function _noteText(st) {
  return `The server switches to each picked build and back to ${st.activeName} when the run ends.`;
}

function _syncNote(el) {
  const st = el._bp, note = el.querySelector('.bp-note');
  if (!st || !note) return;
  const picked = selected(el);
  const show = st.needsSwitchNote && picked.some(p => p !== st.active);
  note.textContent = show ? _noteText(st) : '';
  note.style.display = show ? '' : 'none';
}

function _hide(el) {
  el._bp = null;
  el.innerHTML = '';
  el.style.display = 'none';
}

// Live bench / Report Card (needsSwitchNote) list non-active builds only for admins.
function render(el, builds, opts) {
  if (!el) return;
  const o = opts || {};
  const rows = runnable(builds);
  const act = rows.find(b => b.active);
  const shown = o.needsSwitchNote && !o.admin ? rows.filter(b => b.active) : rows;
  if (shown.length <= 1) { _hide(el); return; }
  el._bp = { rows: shown, active: act ? act.path : null, activeName: act ? nameOf(act) : 'the active build',
             needsSwitchNote: !!o.needsSwitchNote };
  const circles = shown.map(b => {
    const tag = b.active ? '<span class="bp-tag">Active</span>' : '';
    const meta = b.backend ? `<span class="bp-meta">${esc(b.backend)}</span>` : '';
    return `<label class="mc-pick bp-pick" title="${esc(b.path)}"><input type="checkbox" value="${esc(b.path)}"${b.active ? ' checked' : ''}>`
      + `<span class="dot"></span><span class="bp-name">${esc(nameOf(b))}</span>${meta}${tag}</label>`;
  }).join('');
  el.innerHTML = `<div class="bp-h">Builds</div><div class="bp-list">${circles}</div><div class="bp-note" style="display:none"></div>`;
  el.style.display = '';
  el.querySelectorAll('input').forEach(inp => inp.addEventListener('change', () => {
    // Keeps the last picked circle on when it is unpicked.
    if (!inp.checked && !selected(el).length) inp.checked = true;
    _syncNote(el);
  }));
  _syncNote(el);
}

// Picked paths in list order; [] when the row is hidden.
function selected(el) {
  if (!el || !el._bp) return [];
  return [...el.querySelectorAll('.bp-list input')].filter(i => i.checked).map(i => i.value);
}

// The `builds` field for a start body: null when only the active build is picked.
function payload(el) {
  const picked = selected(el);
  if (!picked.length) return null;
  const st = el._bp;
  if (picked.length === 1 && st && picked[0] === st.active) return null;
  return picked;
}

function activeName(el) {
  return el && el._bp ? el._bp.activeName : '';
}

// Progress line for a build_start / build_failed / switch_back event; null for anything else.
function eventText(ev, startName) {
  const kind = ev && (ev.type || ev.phase);
  if (kind === 'build_start') return `Build ${Number(ev.index) + 1} of ${ev.total}: ${ev.build || ''}`;
  if (kind === 'build_failed') return `${ev.build || 'A build'} could not be used: ${ev.error || 'unknown error'}`;
  if (kind === 'switch_back') {
    return ev.ok ? `Switched back to ${startName || 'the starting build'}` : `Could not switch back: ${ev.error || 'unknown error'}`;
  }
  return null;
}

// Fetches the build list once and renders the row; a failed request hides it.
async function load(el, url, opts) {
  if (!el) return;
  let d = null;
  try { d = await fetch(url || '/api/llm/builds').then(r => r.json()); } catch (_) { d = null; }
  render(el, d && d.ok !== false && Array.isArray(d.builds) ? d.builds : [], opts);
}

return { nameOf, runnable, render, selected, payload, activeName, eventText, load };
});
