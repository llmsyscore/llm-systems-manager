// Builds the static demo site into dist/: stamped dashboard, alarm UI, runtime files and worker.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { cp, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');
const MGR_FRONTEND = join(ROOT, 'llm-systems-manager', 'frontend');
const AE_FRONTEND = join(ROOT, 'llm-systems-alarm-engine', 'frontend');
const RUNTIME = join(HERE, 'runtime');
const PAGES = join(HERE, 'pages');

const WS_URL = 'wss://demo.invalid/ws';
// Default (indigo) brand palette, as _brand_css_vars renders it.
const BRAND_CSS = ':root{--brand-accent:#5b7cfa;--brand-core:#5b7cfa;--brand-mid:#4257b0;'
  + '--brand-stroke:#2f3d80;--brand-chip1:#0a0e1f;--brand-chip2:#0e1430;}';
const TEXT_EXT = /\.(html|js|mjs|css|json|webmanifest|svg|txt)$/;

const STATIC_URL_RE = /((?:src|href)=")(\/static\/[^"?#]+)(?:\?[^"#]*)?(")/g;

// Rewrites every /static src/href to ?v=<10-hex sha256 of the file>; missing files stay as-is.
export function stampStaticUrls(html, staticDir) {
  return html.replace(STATIC_URL_RE, (whole, pre, url, post) => {
    const file = join(staticDir, url.slice('/static/'.length));
    if (!existsSync(file)) return whole;
    const sync = createHash('sha256').update(readFileSync(file)).digest('hex').slice(0, 10);
    return `${pre}${url}?v=${sync}${post}`;
  });
}

// Inserts the snippet right after the opening head tag so it runs before every page script.
export function injectHead(html, snippet) {
  const m = /<head[^>]*>/i.exec(html);
  if (!m) return snippet + html;
  const at = m.index + m[0].length;
  return html.slice(0, at) + '\n' + snippet + html.slice(at);
}

// Inserts the snippet right before the closing body tag so it runs after every page script.
export function injectBodyEnd(html, snippet) {
  const i = html.search(/<\/body>(?![\s\S]*<\/body>)/i);
  if (i < 0) return html + snippet;
  return html.slice(0, i) + snippet + '\n' + html.slice(i);
}

// Sample machines, series and alarm data (DemoAlarms) load ahead of demo-page.js for its WebSocket stand-in.
const PAGE_SCRIPTS = ['demo-analytics.js', 'demo-fleet.js', 'demo-series.js', 'handlers/alarms.js', 'demo-page.js'];
const pageScript = (release) => PAGE_SCRIPTS
  .map((s) => `<script src="/demo/${s}?v=${release}"></script>`).join('\n');

async function walkText(dir, fn) {
  for (const ent of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, ent.name);
    if (ent.isDirectory()) await walkText(p, fn);
    else if (TEXT_EXT.test(ent.name)) await fn(p);
  }
}

// Throws for an output directory that must never be wiped: empty, a filesystem root, the repo root or the home directory.
export function checkOutDir(outDir) {
  if (typeof outDir !== 'string' || !outDir.trim()) throw new Error('build: --out must not be empty');
  const dir = resolve(outDir);
  if (dir === resolve(dir, '/') || dir === ROOT || dir === HERE || dir === resolve(homedir())) {
    throw new Error(`build: refusing to replace ${dir}`);
  }
  return dir;
}

// Reads --release and --out; each flag needs a value that is not another flag.
export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a !== '--release' && a !== '--out') throw new Error(`build: unknown argument ${a}`);
    const v = argv[i + 1];
    if (v === undefined || v === '' || v.startsWith('-')) throw new Error(`build: ${a} needs a value`);
    out[a.slice(2)] = v;
    i++;
  }
  if (!out.release) throw new Error('build: --release is required');
  return out;
}

export async function build({ outDir, release }) {
  outDir = checkOutDir(outDir);
  await rm(outDir, { recursive: true, force: true });
  await mkdir(outDir, { recursive: true });

  const skip = (src) => !/[\\/](test|node_modules)([\\/]|$)/.test(src.slice(MGR_FRONTEND.length))
    && !/^[\\/](package\.json|package-lock\.json|vitest\.config\.mjs|sw\.js|companion\.html)$/.test(src.slice(MGR_FRONTEND.length));
  await cp(MGR_FRONTEND, join(outDir, 'static'), {
    recursive: true,
    filter: (src) => (src.startsWith(MGR_FRONTEND) ? skip(src) : true),
  });
  await cp(AE_FRONTEND, join(outDir, 'alarm'), { recursive: true });
  await cp(RUNTIME, join(outDir, 'demo'), { recursive: true });

  const globals = `<script>window.DEMO_RELEASE="${release}";</script>\n` + '<script>window.__MGR_AGENT="demo-mgr";window.__LMS_AGENT="demo-mgr";'
    + `window.__VLLM_AGENT="demo-mgr";window.__AE_WS_URL__="${WS_URL}";</script>\n`
    + `<style>${BRAND_CSS}</style>\n${pageScript(release)}`;
  const dash = await readFile(join(MGR_FRONTEND, 'index.html'), 'utf8');
  const tour = `<link rel="stylesheet" href="/demo/demo-tour.css?v=${release}">\n`
    + `<script src="/demo/demo-tour.js?v=${release}"></script>`;
  await writeFile(join(outDir, 'dashboard.html'),
    injectBodyEnd(injectHead(stampStaticUrls(dash, MGR_FRONTEND), globals), tour));

  const ae = await readFile(join(AE_FRONTEND, 'index.html'), 'utf8');
  await writeFile(join(outDir, 'alarm', 'index.html'),
    injectHead(ae, `<script>window.ALARM_WS_URL="${WS_URL}";</script>\n${pageScript(release)}`));

  await cp(join(RUNTIME, 'bootstrap.html'), join(outDir, 'index.html'));
  await rm(join(outDir, 'demo', 'bootstrap.html'), { force: true });
  await rm(join(outDir, 'demo', 'demo-sw.js'), { force: true });
  for (const f of ['_worker.js', '_routes.json']) await cp(join(PAGES, f), join(outDir, f));

  const hdir = join(RUNTIME, 'handlers');
  const handlers = existsSync(hdir)
    ? (await readdir(hdir)).filter((f) => f.endsWith('.js')).sort().map((f) => `/demo/handlers/${f}`)
    : [];
  const sw = await readFile(join(RUNTIME, 'demo-sw.js'), 'utf8');
  await writeFile(join(outDir, 'demo-sw.js'),
    `// release ${release}\nself.DEMO_RELEASE=${JSON.stringify(release)};\n`
    + `self.DEMO_HANDLERS=${JSON.stringify(handlers)};\n${sw}`);

  await walkText(outDir, async (p) => {
    const t = await readFile(p, 'utf8');
    if (t.includes('__MGR_VERSION__')) await writeFile(p, t.replaceAll('__MGR_VERSION__', release));
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
    checkOutDir(args.out || join(HERE, 'dist'));
  } catch (e) {
    console.error(`${e.message}\nusage: node build.mjs --release <tag> [--out <dir>]`);
    process.exit(2);
  }
  await build({ outDir: args.out || join(HERE, 'dist'), release: args.release });
}
