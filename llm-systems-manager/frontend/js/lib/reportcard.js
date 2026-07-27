// GPU report card rendering + submission helpers (#468). Pure DOM/string
// logic; the sub-tab wiring lives in js/report-card.js.
// IIFE-scoped: this loads as a classic script beside charts.js, which already
// defines a global `fmt`.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.RC = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const LEADERBOARD_REPO = 'https://github.com/llmsyscore/llm-report-cards';

const PROVIDER_LABEL = { llama: 'llama.cpp', vllm: 'vLLM', lms: 'LM Studio' };

function fmt(v, digits) {
  if (v == null || Number.isNaN(v)) return '—';
  const d = digits == null ? (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 1 ? 1 : 2)
                           : digits;
  return Number(v).toFixed(d);
}

function fmtMb(mb) {
  if (mb == null || Number.isNaN(mb)) return '—';
  return (mb / 1024).toFixed(1) + ' GB';
}

function fmtDate(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

// el(tag, className, text) — textContent only, never innerHTML, so
// provider-supplied model and GPU names cannot inject markup.
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function cell(parent, value, unit, label) {
  const c = el('div', 'rc-cell');
  const fig = el('div', 'rc-fig');
  fig.appendChild(el('span', 'rc-num', value));
  if (unit) fig.appendChild(el('span', 'rc-unit', unit));
  c.appendChild(fig);
  c.appendChild(el('div', 'rc-label', label));
  parent.appendChild(c);
  return c;
}

function buildCard(res) {
  const r = res || {};
  const frag = document.createDocumentFragment();
  const root = el('div', 'rc-card');

  const head = el('div', 'rc-head');
  head.appendChild(el('div', 'rc-gpu', r.gpu_config || 'Unknown GPU'));
  head.appendChild(el('div', 'rc-provider',
    PROVIDER_LABEL[r.provider] || r.provider || ''));
  root.appendChild(head);

  const hero = el('div', 'rc-hero');
  hero.appendChild(el('span', 'rc-hero-num', fmt(r.gen_tps)));
  hero.appendChild(el('span', 'rc-hero-unit', 'tok/s'));
  hero.appendChild(el('span', 'rc-hero-label', 'generation'));
  root.appendChild(hero);

  const grid = el('div', 'rc-grid');
  cell(grid, fmt(r.prefill_tps), 'tok/s', 'prefill');
  cell(grid, fmt(r.ttft_s, 2), 's', 'TTFT');
  cell(grid, r.vram_used_mb == null ? '—'
       : fmtMb(r.vram_used_mb).replace(' GB', ''),
       '/ ' + fmtMb(r.vram_total_mb), 'VRAM used');
  root.appendChild(grid);

  const energy = el('div', 'rc-energy');
  if (r.tokens_per_joule == null && r.avg_watts == null) {
    energy.classList.add('rc-muted');
    energy.appendChild(el('span', null, 'no power telemetry'));
  } else {
    const src = r.power_source === 'psu' ? 'wall' : 'GPU';
    energy.appendChild(el('span', null, `${fmt(r.avg_watts, 0)} W ${src}`));
    energy.appendChild(el('span', 'rc-sep', '·'));
    energy.appendChild(el('span', null, `${fmt(r.tokens_per_joule, 2)} tok/J`));
    energy.appendChild(el('span', 'rc-sep', '·'));
    energy.appendChild(el('span', null, `$${fmt(r.usd_per_mtok, 2)}/Mtok`));
  }
  root.appendChild(energy);

  const foot = el('div', 'rc-foot');
  foot.appendChild(el('span', 'rc-model', r.model || 'unknown model'));
  if (r.preset_version) {
    foot.appendChild(el('span', 'rc-sep', '·'));
    foot.appendChild(el('span', null, r.preset_version));
  }
  if (r.ts) {
    foot.appendChild(el('span', 'rc-sep', '·'));
    foot.appendChild(el('span', null, fmtDate(r.ts)));
  }
  root.appendChild(foot);

  frag.appendChild(root);
  return frag;
}

// Only standard-preset, reference-model runs are comparable, so only those
// get a submission link.
function submitUrl(card) {
  if (!card || !card.eligible || card.mode !== 'standard') return '';
  const pub = { ...card };
  delete pub.agent_id;
  const res = pub.result || {};
  const p = new URLSearchParams({
    template: 'submit.yml',
    title: `[card] ${res.gpu_config || 'GPU'} · ${pub.provider || ''}`,
    'card-json': JSON.stringify(pub),
  });
  return `${LEADERBOARD_REPO}/issues/new?${p}`;
}

function trendSeries(cards) {
  const s = [...(cards || [])].sort((a, b) => a.ts - b.ts);
  return {
    labels: s.map(c => new Date(c.ts * 1000)),
    gen: s.map(c => (c.result || {}).gen_tps),
    prefill: s.map(c => (c.result || {}).prefill_tps),
    tpj: s.map(c => (c.result || {}).tokens_per_joule),
  };
}

// Copies computed styles onto a clone so the serialized SVG renders without
// the page stylesheet, which foreignObject does not inherit.
const _INLINE_PROPS = [
  'font-family', 'font-size', 'font-weight', 'font-variant-numeric',
  'letter-spacing', 'line-height', 'color', 'background-color', 'border',
  'border-radius', 'padding', 'margin', 'display', 'grid-template-columns',
  'gap', 'align-items', 'justify-content', 'text-transform', 'opacity',
  'box-sizing', 'width', 'flex-wrap', 'border-top', 'border-bottom',
];

function _inlineStyles(src, dst) {
  const cs = getComputedStyle(src);
  dst.style.cssText = _INLINE_PROPS
    .map(p => `${p}:${cs.getPropertyValue(p)}`).join(';');
  const sk = src.children, dk = dst.children;
  for (let i = 0; i < sk.length; i++) _inlineStyles(sk[i], dk[i]);
}

async function exportPng(cardEl, scale) {
  const s = scale || 2;
  const rect = cardEl.getBoundingClientRect();
  const w = Math.ceil(rect.width), h = Math.ceil(rect.height);
  const clone = cardEl.cloneNode(true);
  _inlineStyles(cardEl, clone);
  const xml = new XMLSerializer().serializeToString(clone);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">`
    + `<foreignObject width="100%" height="100%">`
    + `<div xmlns="http://www.w3.org/1999/xhtml">${xml}</div>`
    + `</foreignObject></svg>`;
  const img = new Image();
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  await img.decode();
  const canvas = document.createElement('canvas');
  canvas.width = w * s;
  canvas.height = h * s;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = getComputedStyle(cardEl).backgroundColor || '#111';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  return new Promise(res => canvas.toBlob(res, 'image/png'));
}

return { buildCard, submitUrl, trendSeries, exportPng, fmt, fmtMb,
         fmtDate, LEADERBOARD_REPO, PROVIDER_LABEL };
});
