// Shape helpers: shapeOf builds a type tree; assertCompatible checks a handler answer against a template.
import assert from 'node:assert/strict';

export function shapeOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return v.length ? [shapeOf(v[0])] : [];
  if (typeof v === 'object') {
    const o = {};
    for (const k of Object.keys(v)) o[k] = shapeOf(v[k]);
    return o;
  }
  return typeof v;
}

function walk(out, tpl, path) {
  if (tpl === 'null') return;
  if (Array.isArray(tpl)) {
    assert.ok(Array.isArray(out), `${path}: expected array, got ${typeof out}`);
    if (tpl.length && out.length) walk(out[0], tpl[0], `${path}[0]`);
    return;
  }
  if (tpl && typeof tpl === 'object') {
    assert.ok(out && typeof out === 'object' && !Array.isArray(out), `${path}: expected object`);
    for (const k of Object.keys(tpl)) {
      assert.ok(k in out, `${path}.${k}: missing in handler output`);
      walk(out[k], tpl[k], `${path}.${k}`);
    }
    return;
  }
  const got = out === null ? 'null' : Array.isArray(out) ? 'array' : typeof out;
  assert.equal(got, tpl, `${path}: expected ${tpl}, got ${got}`);
}

// Fails when the handler output lacks a template key or has a different type; extra keys are fine.
export function assertCompatible(handlerOut, template) {
  walk(handlerOut, shapeOf(template), '$');
}
