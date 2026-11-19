// #1447: the alarm engine's Dark and Light tokens resolve to the dashboard's values.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { srcFile } from "./helpers/harness.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const aeCss = readFileSync(path.resolve(here, "../../../llm-systems-alarm-engine/frontend/css/main.css"), "utf8");

// Token maps per theme: bare :root (and the dark selector) blocks, then the light override.
function blocks(sheet) {
  const out = { root: {}, light: {} };
  for (const m of sheet.matchAll(/:root(?:,\s*:root\[data-theme="dark"\])?(\[data-theme="(\w+)"\])?\s*\{([^}]*)\}/g)) {
    if (m[2] && m[2] !== "light") continue;
    const toks = Object.fromEntries(m[3].split(";").map(d => d.match(/^\s*(--[\w-]+)\s*:\s*(.+?)\s*$/s)).filter(Boolean).map(t => [t[1], t[2]]));
    Object.assign(m[2] ? out.light : out.root, toks);
  }
  return out;
}
function resolve(v, t, depth = 0) {
  const m = v.trim().match(/^var\((--[\w-]+)\)$/);
  if (!m || depth > 10) return v.trim().toLowerCase();
  return resolve(t[m[1]], t, depth + 1);
}
function themes(...sheets) {
  const bs = sheets.map(blocks);
  const dark = Object.assign({}, ...bs.map(b => b.root));
  return { dark, light: Object.assign({ ...dark }, ...bs.map(b => b.light)) };
}

const DASH = themes(srcFile("kit/kit.css"), srcFile("css/base.css"));
const AE = themes(aeCss);
const TOKENS = ["--bg", "--bg-card", "--bg-card-alt", "--bg-elev", "--bg-tabnav", "--border", "--border-soft",
  "--border-strong", "--fg", "--fg-muted", "--fg-dim", "--fg-faint", "--accent", "--accent-2", "--ok", "--warn", "--crit", "--note"];
const TEXT = { "--ok-text": "--good-text", "--warn-text": "--warn-text", "--crit-text": "--bad-text" };

describe("alarm engine palette matches the dashboard", () => {
  for (const theme of ["dark", "light"]) {
    it(`${theme} tokens`, () => {
      for (const tok of TOKENS) {
        expect(DASH[theme][tok], `dashboard ${theme} ${tok}`).toBeTruthy();
        expect(resolve(AE[theme][tok] ?? "", AE[theme]), `${theme} ${tok}`).toBe(resolve(DASH[theme][tok] ?? "", DASH[theme]));
      }
      for (const [ae, dash] of Object.entries(TEXT)) {
        expect(resolve(AE[theme][ae] ?? "", AE[theme]), `${theme} ${ae}`).toBe(resolve(DASH[theme][dash] ?? "", DASH[theme]));
      }
    });
  }
});
