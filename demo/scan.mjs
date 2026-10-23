// Endpoint inventory: finds API calls in the dashboard and alarm engine frontend sources.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SKIP_DIRS = new Set(['test', 'vendor', 'node_modules']);

// name -> { arg: index of the URL argument, method: default method, prefix: true to apply API_BASE }
const CALLS = {
  fetch: { arg: 0 }, jfetch: { arg: 0 }, _fetchT: { arg: 0 }, EventSource: { arg: 0 },
  towerAct: { arg: 1 }, postJson: { arg: 0, method: 'POST' }, putJson: { arg: 0, method: 'PUT' },
  jget: { arg: 0 }, jpost: { arg: 0, method: 'POST' }, _adminUsersApi: { arg: 0 },
  _streamCacheOp: { arg: 0, method: 'POST' }, _historyRows: { arg: 0 }, _vllmLoraCall: { arg: 0, method: 'POST' },
  _request: { arg: 0, prefix: true },
};
const UNWRAP = new Set(['tq', 'toolsUrl']);
const CALL_RE = new RegExp('(?<![\\w$.]\\w*)(?:new\\s+)?\\b(' + Object.keys(CALLS).join('|') + ')\\s*\\(|\\.(_request)\\s*\\(', 'g');

// Returns the index just past the string/template literal that starts at i.
function skipString(s, i) {
  const q = s[i];
  for (i++; i < s.length; i++) {
    if (s[i] === '\\') i++;
    else if (s[i] === q) return i + 1;
    else if (q === '`' && s[i] === '$' && s[i + 1] === '{') i = skipBalanced(s, i + 1, '{', '}') - 1;
  }
  return i;
}

// Returns the index just past the bracket group opening at i.
function skipBalanced(s, i, open, close) {
  let depth = 0;
  for (; i < s.length; i++) {
    const c = s[i];
    if (c === '"' || c === "'" || c === '`') { i = skipString(s, i) - 1; continue; }
    if (c === open) depth++;
    else if (c === close && --depth === 0) return i + 1;
  }
  return i;
}

// Splits the argument text of a call opening at i (the '(') on top-level commas.
function splitArgs(s, i) {
  const end = skipBalanced(s, i, '(', ')');
  const args = [];
  let cur = i + 1;
  for (let j = i + 1; j < end - 1; j++) {
    const c = s[j];
    if (c === '"' || c === "'" || c === '`') { j = skipString(s, j) - 1; continue; }
    if (c === '(' || c === '[' || c === '{') { j = skipBalanced(s, j, c, c === '(' ? ')' : c === '[' ? ']' : '}') - 1; continue; }
    if (c === ',') { args.push(s.slice(cur, j)); cur = j + 1; }
  }
  args.push(s.slice(cur, end - 1));
  return { args: args.map((a) => a.trim()), text: s.slice(i, end) };
}

// Splits an expression on top-level '+'.
function splitPlus(e) {
  const parts = [];
  let cur = 0;
  for (let j = 0; j < e.length; j++) {
    const c = e[j];
    if (c === '"' || c === "'" || c === '`') { j = skipString(e, j) - 1; continue; }
    if (c === '(' || c === '[' || c === '{') { j = skipBalanced(e, j, c, c === '(' ? ')' : c === '[' ? ']' : '}') - 1; continue; }
    if (c === '+') { parts.push(e.slice(cur, j).trim()); cur = j + 1; }
  }
  parts.push(e.slice(cur).trim());
  return parts;
}

// Text of a string/template literal with ${...} replaced by :p (consts substituted), or null.
function literalText(part, consts) {
  const q = part[0];
  if (!(q === '"' || q === "'" || q === '`') || skipString(part, 0) !== part.length) return null;
  let out = '';
  const body = part.slice(1, -1);
  for (let j = 0; j < body.length; j++) {
    if (body[j] === '$' && body[j + 1] === '{') {
      const end = skipBalanced(body, j + 1, '{', '}');
      const name = body.slice(j + 2, end - 1).trim();
      out += Object.prototype.hasOwnProperty.call(consts, name) ? consts[name] : ':p';
      j = end - 1;
    } else out += body[j];
  }
  return out;
}

// Splits a top-level `cond ? a : b` into [a, b], or returns null.
function splitTernary(e) {
  let q = -1, depth = 0;
  for (let j = 0; j < e.length; j++) {
    const c = e[j];
    if (c === '"' || c === "'" || c === '`') { j = skipString(e, j) - 1; continue; }
    if (c === '(' || c === '[' || c === '{') { j = skipBalanced(e, j, c, c === '(' ? ')' : c === '[' ? ']' : '}') - 1; continue; }
    if (c === '?' && e[j + 1] !== '.' && e[j + 1] !== '?' && e[j - 1] !== '?') {
      if (q < 0) q = j; else depth++;
    } else if (c === ':' && q >= 0) {
      if (depth === 0) return [e.slice(q + 1, j).trim(), e.slice(j + 1).trim()];
      depth--;
    }
  }
  return null;
}

// Turns a URL expression into patterns ('' prefix kept); [null] when it is not a literal same-origin path.
function toPatterns(expr, ctx) {
  let e = expr.trim();
  const w = /^(\w+)\s*\(/.exec(e);
  if (w && UNWRAP.has(w[1])) e = splitArgs(e, w[0].length - 1).args[0] || '';
  if (/^[A-Za-z_$][\w$]*$/.test(e) && !ctx.consts[e]) {
    const re = new RegExp('(?:const|let|var)\\s+' + e.replace(/\$/g, '\\$') + '\\s*=\\s*', 'g');
    let m, last = null;
    while ((m = re.exec(ctx.before))) last = m;
    if (last) {
      let k = last.index + last[0].length;
      let depth = 0;
      const start = k;
      for (; k < ctx.before.length; k++) {
        const c = ctx.before[k];
        if (c === '"' || c === "'" || c === '`') { k = skipString(ctx.before, k) - 1; continue; }
        if (c === '(' || c === '[' || c === '{') depth++;
        else if (c === ')' || c === ']' || c === '}') depth--;
        else if (c === ';' && depth === 0) break;
      }
      e = ctx.before.slice(start, k).trim();
    }
  }
  if (ctx.consts[e]) return [ctx.consts[e]];
  const branches = splitTernary(e);
  if (branches) return branches.flatMap((b) => toPatterns(b, { ...ctx, before: '' }));
  return [toPattern(e, ctx)];
}

function toPattern(e, ctx) {
  const parts = splitPlus(e);
  let out = '';
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (i === 0 && p === 'base') continue;
    const lit = literalText(p, ctx.consts);
    out += lit === null ? ':p' : lit;
  }
  if (ctx.prefix) out = ctx.prefix + out;
  if (!/^\/(?![/\\])/.test(out)) return null;
  out = out.split('?')[0];
  if (ctx.consts.API_BASE && out === ctx.consts.API_BASE + ':p') return null;
  const segs = out.split('/');
  const last = segs.length - 1;
  segs[last] = segs[last].replace(/(.)(:p)+$/, '$1');
  if (segs.some((s) => s !== ':p' && s.includes(':p'))) return null;
  return segs.join('/');
}

export function scanSource(source, file = '') {
  const found = [];
  const consts = {};
  const cre = /const\s+(API_BASE)\s*=\s*(['"])([^'"]+)\2/;
  const cm = cre.exec(source);
  if (cm) consts[cm[1]] = cm[3];
  CALL_RE.lastIndex = 0;
  let m;
  while ((m = CALL_RE.exec(source))) {
    const name = m[1] || m[2];
    const spec = CALLS[name];
    if (spec.prefix && !consts.API_BASE) continue;
    const before = source.slice(0, m.index);
    if (/(function\s*\*?|async)\s*$/.test(before)) continue;
    const lineStart = before.lastIndexOf('\n') + 1;
    if (/^\s*(\/\/|\*|\/\*)/.test(source.slice(lineStart, m.index + 1))) continue;
    const open = m.index + m[0].length - 1;
    const { args, text } = splitArgs(source, open);
    const urlArg = args[spec.arg];
    if (!urlArg) continue;
    const line = before.split('\n').length;
    const literalStart = /^(['"`]\/(?![/\\])|base\b|tq\s*\(|toolsUrl\s*\()/.test(urlArg);
    const mm = /\bmethod\s*:\s*(['"`])(\w+)\1/.exec(text);
    const method = (mm ? mm[2].toUpperCase() : spec.method || 'GET');
    for (const pattern of toPatterns(urlArg, { consts, before, prefix: spec.prefix ? consts.API_BASE : '' })) {
      if (pattern === null && !literalStart && !/^[A-Za-z_$][\w$.]*$/.test(urlArg) && !/^[`'"]/.test(urlArg)) continue;
      if (pattern === null && /^[`'"]/.test(urlArg) && !literalStart) continue;
      found.push({ method, pattern, file, line, expr: urlArg.replace(/\s+/g, ' ') });
    }
  }
  return found;
}

function walk(path, out) {
  const st = statSync(path);
  if (st.isFile()) { out.push(path); return; }
  for (const name of readdirSync(path).sort()) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(path, name);
    const s = statSync(full);
    if (s.isDirectory()) walk(full, out);
    else if (/\.(js|html)$/.test(name)) out.push(full);
  }
}

export function scanEndpoints(dirs) {
  const files = [];
  for (const d of dirs) walk(d, files);
  const all = [];
  for (const f of files) all.push(...scanSource(readFileSync(f, 'utf8'), relative(ROOT, f)));
  return all;
}

// True when a router key covers the scanned method and pattern (':p' matches any ':name').
export function covers(keys, method, pattern) {
  const want = pattern.split('/');
  return keys.some((k) => {
    const sp = k.indexOf(' ');
    if (k.slice(0, sp) !== method) return false;
    const have = k.slice(sp + 1).split('/');
    return have.length === want.length && have.every((h, i) =>
      want[i] === ':p' ? h.startsWith(':') : (h === want[i] || h.startsWith(':')));
  });
}

// Formats one missing endpoint as 'METHOD pattern  (file:line)'.
export function formatMissing(e) {
  return `${e.method} ${e.pattern}  (${e.file}:${e.line})`;
}

// Local script files an HTML page loads via <script src>; /static/ maps to staticRoot, relative paths to the page dir.
export function pageScripts(htmlPath, staticRoot) {
  const html = readFileSync(htmlPath, 'utf8');
  const out = [];
  for (const m of html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)) {
    const src = m[1].split('?')[0];
    if (/^[a-z]+:\/\//.test(src) || !src.endsWith('.js') || src.includes('/vendor/')) continue;
    out.push(src.startsWith('/static/') ? join(staticRoot, src.slice('/static/'.length)) : join(dirname(htmlPath), src));
  }
  return out;
}

const MGR = join(ROOT, 'llm-systems-manager/frontend');
const AE = join(ROOT, 'llm-systems-alarm-engine/frontend');
export const DEFAULT_DIRS = [
  join(MGR, 'index.html'), ...pageScripts(join(MGR, 'index.html'), MGR),
  join(AE, 'index.html'), ...pageScripts(join(AE, 'index.html'), AE),
];

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url) && process.argv.includes('--list')) {
  const all = scanEndpoints(DEFAULT_DIRS);
  const seen = new Set();
  let nonLiteral = 0;
  const nonLits = [];
  for (const e of all) {
    if (e.pattern === null) { nonLiteral++; nonLits.push(`? ${e.expr} ${e.file}:${e.line}`); continue; }
    const key = `${e.method} ${e.pattern}  ${e.file}:${e.line}`;
    if (!seen.has(key)) { seen.add(key); console.log(key); }
  }
  for (const l of nonLits) console.log(l);
  console.log(`${seen.size} endpoint calls, ${nonLiteral} non-literal calls`);
}
