// LLM Control page sections (#767): drag-to-reorder + collapse persistence.
// Order/open state lives in layout.llmSections keyed by sub-tab.
const LLMSections = (function () {
  const PANELS = {
    llamacpp: { container: 'llm-llamacpp' },
    lmstudio: { container: 'llm-lmstudio' },
    vllm:     { container: 'llm-vllm' },
  };

  function _container(key) { return document.getElementById(PANELS[key].container); }
  function _sections(key) {
    const c = _container(key);
    return c ? [...c.querySelectorAll(':scope > .llm-section')] : [];
  }
  function _panelOf(sectionId) {
    for (const key of Object.keys(PANELS)) {
      if (_sections(key).some(el => el.id === sectionId)) return key;
    }
    return null;
  }
  function _saved(key) {
    const all = (typeof layout === 'object' && layout && layout.llmSections) || {};
    const st = all[key];
    return (st && Array.isArray(st.order) && Array.isArray(st.open)) ? st : null;
  }

  function _persist(key) {
    if (typeof layout !== 'object' || !layout) return;
    layout.llmSections = layout.llmSections || {};
    layout.llmSections[key] = {
      order: _sections(key).map(el => el.id),
      open: _sections(key).filter(el => !el.classList.contains('collapsed')).map(el => el.id),
    };
    try { saveLayout(); } catch (_) {}
  }

  // Swap sections into the existing section slots so non-section siblings
  // (dividers, the model editor) keep their DOM positions.
  function _applyOrder(key, order) {
    const secs = _sections(key);
    const byId = new Map(secs.map(el => [el.id, el]));
    const target = order.filter(id => byId.has(id));
    secs.forEach(el => { if (!target.includes(el.id)) target.push(el.id); });
    const marks = secs.map(el => {
      const m = document.createComment('sec-slot');
      el.parentNode.insertBefore(m, el);
      return m;
    });
    target.forEach((id, i) => { if (marks[i]) marks[i].parentNode.insertBefore(byId.get(id), marks[i]); });
    marks.forEach(m => m.remove());
  }

  function _initPanel(key) {
    const c = _container(key);
    if (!c || c._llmSecInited || !_sections(key).length) return;
    c._llmSecInited = true;
    const st = _saved(key);
    if (st) {
      _applyOrder(key, st.order);
      _sections(key).forEach(el => el.classList.toggle('collapsed', !st.open.includes(el.id)));
    }
    if (typeof Sortable !== 'undefined') {
      new Sortable(c, {
        handle: '.llm-drag',
        draggable: '.llm-section',
        animation: 150,
        ghostClass: 'mc-sec-ghost',
        onEnd: () => _persist(key),
      });
    }
  }

  function noteToggle(sectionId) {
    const key = _panelOf(sectionId);
    if (key) _persist(key);
  }

  function init() {
    Object.keys(PANELS).forEach(_initPanel);
    // Section-toolbar overflow menus (llama + vLLM server control).
    document.querySelectorAll('.llm-toolbar .mc-menubtn').forEach(btn => {
      if (btn._secWired) return;
      btn._secWired = true;
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        const menu = btn.parentElement.querySelector('.mc-menu');
        const wasOpen = menu.classList.contains('open');
        MC.closeMenus();
        if (!wasOpen) menu.classList.add('open');
      });
    });
  }

  // layout arrives async at boot — initialize once it lands.
  if (typeof document !== 'undefined') {
    let tries = 0;
    const t = setInterval(() => {
      const ready = (typeof layout === 'object' && layout);
      if (ready || ++tries > 100) {
        clearInterval(t);
        try { init(); } catch (e) { console.error('LLMSections init failed:', e); }
      }
    }, 100);
  }

  return { init, noteToggle };
})();
