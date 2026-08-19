// Pure axis-option logic for the benchmark chart. Drops constant dimensions,
// keeps seq as the ordinal fallback, and picks sane defaults (n_depth x avg_ts).
function computeBenchAxisOptions(rows, switches, labelFn) {
  // avg_ts/ms_tok are throughput metrics (Y only), never sweep dimensions.
  const SKIP = new Set(['ts', 'seq', 'gen_tps', 'ppt_tps', 'model_id', 'avg_ts', 'ms_tok']);
  const label = typeof labelFn === 'function' ? labelFn : (k) => k;
  rows = Array.isArray(rows) ? rows : [];

  const distinct = {};
  rows.forEach((r) => {
    Object.entries(r || {}).forEach(([k, v]) => {
      if (SKIP.has(k) || typeof v !== 'number') return;
      (distinct[k] = distinct[k] || new Set()).add(v);
    });
  });
  const varied = Object.keys(distinct).filter((k) => distinct[k].size >= 2);

  // Map short llama-bench flags to the JSONL field they sweep, so axis options
  // use the canonical field name (e.g. -d -> n_depth) that actually has data.
  const FLAG_TO_FIELD = {
    p: 'n_prompt', n: 'n_gen', d: 'n_depth', b: 'n_batch', ub: 'n_ubatch',
    t: 'n_threads', ngl: 'n_gpu_layers', fa: 'flash_attn', ctk: 'type_k', ctv: 'type_v', mmp: 'no_mmap', lm: 'load_mode',
  };
  const switchKeys = [];
  (switches || []).forEach((sw) => {
    if (!sw || typeof sw.flag !== 'string') return;
    const name = sw.flag.replace(/^--?/, '').trim();
    if (name) switchKeys.push(FLAG_TO_FIELD[name] || name);
  });

  const fieldKeys = [...new Set([...varied, ...switchKeys])].sort();
  const xOptions = [...fieldKeys, 'seq'].map((k) => ({ v: k, t: label(k) }));
  const yOptions = [
    { v: 'avg_ts', t: 'Avg tokens/sec' },
    { v: 'ms_tok', t: 'Milliseconds per token' },
    ...fieldKeys.filter((k) => k !== 'avg_ts').map((k) => ({ v: k, t: label(k) })),
  ];
  const defaultX = fieldKeys.includes('n_depth') ? 'n_depth' : (fieldKeys[0] || 'seq');
  return { xOptions, yOptions, defaultX, defaultY: 'avg_ts' };
}

const _BENCH_API = { computeBenchAxisOptions };
if (typeof window !== 'undefined') window.computeBenchAxisOptions = computeBenchAxisOptions;
if (typeof module !== 'undefined' && module.exports) module.exports = _BENCH_API;
