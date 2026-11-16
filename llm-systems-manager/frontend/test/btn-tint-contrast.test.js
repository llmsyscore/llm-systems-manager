// #931: every .btn-* tint utility in base.css derives its stops and text from
// theme tokens and stays legible in the Dark and Light themes (WCAG contrast floor).
import { describe, it, expect } from "vitest";
import { srcFile } from "./helpers/harness.js";

const css = srcFile("css/base.css");
const kitCss = srcFile("kit/kit.css");
// Token blocks per sheet: bare :root ones merged, and the light override block.
function blocks(sheet) {
  const out = { root: {}, light: {} };
  for (const m of sheet.matchAll(/:root(\[data-theme="(\w+)"\])?\s*\{([^}]*)\}/g)) {
    if (m[2] && m[2] !== "light") continue;
    const toks = Object.fromEntries(m[3].split(";").map(d => d.match(/^\s*(--[\w-]+)\s*:\s*(.+?)\s*$/s)).filter(Boolean).map(t => [t[1], t[2]]));
    Object.assign(m[2] ? out.light : out.root, toks);
  }
  return out;
}
const KIT = blocks(kitCss), BRIDGE = blocks(css);
const dark = { ...KIT.root, ...BRIDGE.root };
const THEMES = { dark, light: { ...dark, ...KIT.light, ...BRIDGE.light } };
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
// Resolves var() chains and color-mix(); `token` marks a value read from a theme token.
function resolve(v, t, token = false, depth = 0) {
  v = v.trim();
  expect(depth, `var() chain too deep at ${v}`).toBeLessThan(10);
  let m = v.match(/^var\((--[\w-]+)\)$/);
  if (m) { expect(t[m[1]], `${m[1]} missing from theme`).toBeTruthy(); return resolve(t[m[1]], t, true, depth + 1); }
  m = v.match(/^color-mix\(in srgb,\s*(.+)\)$/);
  if (m) {
    const [a, b] = splitTop(m[1]); const pm = a.match(/^(.+?)\s+(\d+)%$/);
    const p = Number(pm[2]) / 100, ca = resolve(pm[1], t, token, depth + 1), cb = resolve(b, t, token, depth + 1);
    return ca.map((x, i) => Math.round(x * p + cb[i] * (1 - p)));
  }
  if (!token) expect(v, "hardcoded colour in a .btn-* utility").toMatch(/^var\(|^color-mix\(/);
  expect(v, "token resolves to a non-hex colour").toMatch(/^#[0-9a-fA-F]{3,8}$/);
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
  it("finds the Dark and Light token maps and the utility block", () => {
    expect(Object.keys(THEMES).sort()).toEqual(["dark", "light"]);
    expect(THEMES.dark["--bg-card"]).toBe("var(--card)");
    expect(THEMES.light["--scheme"]).toBe("light");
    expect(CLASSES.length).toBeGreaterThanOrEqual(28);
  });
  for (const cls of CLASSES) {
    it(`${cls} is token-derived and legible in Dark and Light`, () => {
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
