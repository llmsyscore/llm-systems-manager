// #931: every .btn-* tint utility in base.css derives its stops and text from
// theme tokens and stays legible in all themes (WCAG contrast floor).
import { describe, it, expect } from "vitest";
import { srcFile } from "./helpers/harness.js";

const css = srcFile("css/base.css");
const THEMES = {};
for (const m of css.matchAll(/:root(?:\[data-theme="(\w+)"\])?\s*\{([^}]*)\}/g)) {
  const toks = Object.fromEntries([...m[2].matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(t => [t[1], t[2].trim()]));
  if (toks["--bg-card"]) THEMES[m[1] || "dark"] = { ...(THEMES[m[1] || "dark"] || {}), ...toks };
}
const CLASSES = [...new Set([...css.matchAll(/^\s+\.(btn-(?:green|red|blue|amber|gray|slate|zinc|stone)[-a-z]*)\s*\{/gm)].map(m => m[1]))];

function hex(h) {
  h = h.trim().replace(/^#/, "");
  if (h.length === 3 || h.length === 4) h = [...h.slice(0, 3)].map(c => c + c).join("");
  return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
}
function splitTop(s) {
  const out = []; let d = 0, cur = "";
  for (const ch of s) {
    if (ch === "(") d++; if (ch === ")") d--;
    if (ch === "," && d === 0) { out.push(cur); cur = ""; } else cur += ch;
  }
  out.push(cur); return out.map(x => x.trim());
}
function resolve(v, t) {
  v = v.trim();
  let m = v.match(/^var\((--[\w-]+)\)$/);
  if (m) { expect(t[m[1]], `${m[1]} missing from theme`).toBeTruthy(); return hex(t[m[1]]); }
  m = v.match(/^color-mix\(in srgb,\s*(.+)\)$/);
  if (m) {
    const [a, b] = splitTop(m[1]); const pm = a.match(/^(.+?)\s+(\d+)%$/);
    const p = Number(pm[2]) / 100, ca = resolve(pm[1], t), cb = resolve(b, t);
    return ca.map((x, i) => Math.round(x * p + cb[i] * (1 - p)));
  }
  expect(v, "hardcoded colour in a .btn-* utility").toMatch(/^var\(|^color-mix\(/);
  return hex(v);
}
const lum = c => { const f = x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const la = lum(a), lb = lum(b); return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05); };

function rule(cls) {
  const body = css.match(new RegExp(`\\.${cls}\\s*\\{([^}]*)\\}`))[1];
  const bg = body.match(/background:\s*([^;]+);/)[1].trim();
  const fg = body.match(/color:\s*([^;]+);/)[1].trim();
  const g = bg.match(/^linear-gradient\([^,]+,\s*(.+)\)$/);
  return { fg, stops: g ? splitTop(g[1]) : [bg] };
}

describe("button tint utilities (#931)", () => {
  it("finds the nine themes and the utility block", () => {
    expect(Object.keys(THEMES).sort()).toEqual(["dark", "enterprise", "frost", "graphite", "light", "medium", "modern", "oled", "slate"]);
    expect(CLASSES.length).toBeGreaterThanOrEqual(28);
  });
  for (const cls of CLASSES) {
    it(`${cls} is token-derived and legible in every theme`, () => {
      const { fg, stops } = rule(cls);
      for (const [theme, t] of Object.entries(THEMES)) {
        const f = resolve(fg, t);
        for (const s of stops) {
          const r = ratio(f, resolve(s, t));
          expect(r, `${cls} on ${theme}: ${r.toFixed(2)}`).toBeGreaterThanOrEqual(3.0);
          if (t["--scheme"] === "light") expect(r, `${cls} on ${theme}: ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.0);
        }
      }
    });
  }
});
