// Threads / MoE offload / speculative fields are first-class editor fields (spec §6).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const here = dirname(fileURLToPath(import.meta.url));
const models = readFileSync(join(here, '..', 'js', 'llmcontrol-models.js'), 'utf8');
const modern = readFileSync(join(here, '..', 'js', 'editor-modern.js'), 'utf8');
const html = readFileSync(join(here, '..', 'index.html'), 'utf8');

function evalConst(src, name) {
  const m = src.match(new RegExp(`const ${name} = ([\\s\\S]*?);\\n`));
  expect(m, `${name} not found`).toBeTruthy();
  return (0, eval)(`(${m[1]})`);
}

const NEW_KEYS = ['threads', 'threads-batch', 'n-cpu-moe', 'model-draft', 'gpu-layers-draft',
  'spec-draft-n-min', 'spec-draft-p-min', 'cache-type-k-draft', 'cache-type-v-draft'];

describe('editor spec/threads/moe fields', () => {
  const EF_FIELDS = evalConst(models, 'EF_FIELDS');
  const EF_VALIDATION = evalConst(models, 'EF_VALIDATION');
  const SEC_MAP = evalConst(modern, 'SEC_MAP');
  const dom = new JSDOM(html);
  const doc = dom.window.document;

  it('every new key is an editor field with a matching input', () => {
    NEW_KEYS.forEach(k => {
      expect(EF_FIELDS, k).toContain(k);
      expect(doc.getElementById('ef-' + k), 'input #ef-' + k).toBeTruthy();
      expect(doc.querySelector(`.ef-field[data-ef="${k}"]`), 'wrapper ' + k).toBeTruthy();
    });
  });

  it('keys sit in the section SEC_MAP names', () => {
    const want = {
      'ef-sec-context': ['threads', 'threads-batch', 'n-cpu-moe'],
      'ef-sec-spec': ['spec-type', 'model-draft', 'gpu-layers-draft', 'spec-draft-n-min',
                      'spec-draft-n-max', 'spec-draft-p-min', 'cache-type-k-draft', 'cache-type-v-draft'],
    };
    Object.entries(want).forEach(([sec, keys]) => {
      keys.forEach(k => {
        expect(SEC_MAP[sec], `${k} in ${sec}`).toContain(k);
        const el = doc.getElementById('ef-' + k);
        expect(el.closest('.ef-sec').id, `${k} markup section`).toBe(sec);
      });
    });
    expect(SEC_MAP['ef-sec-behavior']).not.toContain('spec-type');
    expect(SEC_MAP['ef-sec-behavior']).not.toContain('spec-draft-n-max');
  });

  it('every SEC_MAP key exists in the markup and in EF_FIELDS', () => {
    Object.values(SEC_MAP).flat().filter(k => k !== '__custom' && k !== 'load-mode').forEach(k => {
      expect(EF_FIELDS, k).toContain(k);
      expect(doc.getElementById('ef-' + k), k).toBeTruthy();
    });
  });

  it('rail has the Speculative item and the new section is collapsible', () => {
    expect(doc.querySelector('#efRail .ef-rail-item[data-target="ef-sec-spec"]')).toBeTruthy();
    const sec = doc.getElementById('ef-sec-spec');
    expect(sec.classList.contains('ef-collapsible')).toBe(true);
    expect(sec.querySelector('.ef-acc-head .sum')).toBeTruthy();
  });

  it('select options and validation ranges', () => {
    const opts = sel => [...doc.getElementById(sel).options].map(o => o.value);
    expect(opts('ef-spec-type')).toEqual(['', 'draft-simple', 'draft-eagle3', 'draft-mtp', 'draft-dflash',
      'draft-dspark', 'ngram-simple', 'ngram-map-k', 'ngram-map-k4v', 'ngram-mod', 'ngram-cache']);
    expect(opts('ef-cache-type-k-draft')).toEqual(['', 'f16', 'bf16', 'f32', 'q8_0', 'q5_1', 'q5_0', 'q4_1', 'q4_0', 'iq4_nl']);
    expect(opts('ef-cache-type-v-draft')).toEqual(opts('ef-cache-type-k-draft'));
    expect(doc.getElementById('ef-model-draft').getAttribute('list')).toBe('efDraftList');
    expect(EF_VALIDATION['threads']).toEqual({ min: -1, max: 1024 });
    expect(EF_VALIDATION['threads-batch']).toEqual({ min: -1, max: 1024 });
    expect(EF_VALIDATION['n-cpu-moe']).toEqual({ min: 0, max: 999 });
    expect(EF_VALIDATION['spec-draft-n-min']).toEqual({ min: 0, max: 256 });
    expect(EF_VALIDATION['spec-draft-n-max']).toEqual({ min: 0, max: 256 });
    expect(EF_VALIDATION['spec-draft-p-min']).toEqual({ min: 0, max: 1, float: true });
  });

  it('efLoadDraftList fills the datalist from the cache listing', async () => {
    const m = models.match(/async function efLoadDraftList\(\) \{[\s\S]*?\n\}/);
    expect(m, 'efLoadDraftList not found').toBeTruthy();
    const w = new JSDOM('<datalist id="efDraftList"></datalist>').window;
    const calls = [];
    const fetchStub = url => { calls.push(url); return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, data: [{ repo: 'a/b', file: 'x.gguf', path: '/p/x.gguf', size: 1 }] }) }); };
    const fn = new Function('document', 'fetch', m[0] + '\nreturn efLoadDraftList;')(w.document, fetchStub);
    await fn();
    expect(calls).toEqual(['/api/llm/cache/gguf']);
    const o = [...w.document.getElementById('efDraftList').options];
    expect(o.map(x => x.value)).toEqual(['/p/x.gguf']);
    expect(o[0].label).toBe('a/b · x.gguf');
  });
});
