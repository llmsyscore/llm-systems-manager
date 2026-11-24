// #1468: the alarm engine page draws thin theme-coloured scrollbars whose thumb shows while a box is scrolled.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const ae = (f) => readFileSync(path.resolve(here, "../../../llm-systems-alarm-engine/frontend", f), "utf8");

describe("alarm engine scrollbars", () => {
  it("styles the scrollbar thin with a thumb only while scrolling", () => {
    const flat = ae("css/main.css").replace(/\s+/g, "");
    expect(flat).toContain("::-webkit-scrollbar{width:8px;height:8px;background:transparent;}");
    expect(flat).toContain(".mc-scrolling::-webkit-scrollbar-thumb,::-webkit-scrollbar-thumb:hover{background-color:var(--sb-thumb);}");
  });

  it("marks a box for 900 ms after a user scroll and ignores script scrolls", async () => {
    const js = ae("js/main.js");
    const snippet = js.slice(js.indexOf("// Marks a box .mc-scrolling"));
    const dom = new JSDOM("<div id=b></div>", { runScripts: "outside-only" });
    dom.window.eval(snippet);
    const b = dom.window.document.getElementById("b");
    b.dispatchEvent(new dom.window.Event("scroll"));
    expect(b.classList.contains("mc-scrolling")).toBe(false);
    b.dispatchEvent(new dom.window.Event("wheel", { bubbles: true }));
    b.dispatchEvent(new dom.window.Event("scroll"));
    expect(b.classList.contains("mc-scrolling")).toBe(true);
    await new Promise(r => setTimeout(r, 1000));
    expect(b.classList.contains("mc-scrolling")).toBe(false);
  });

  it("leaves another box hidden when the input was in a different box", () => {
    const js = ae("js/main.js");
    const dom = new JSDOM("<div id=a></div><div id=b></div>", { runScripts: "outside-only" });
    dom.window.eval(js.slice(js.indexOf("// Marks a box .mc-scrolling")));
    const d = dom.window.document, a = d.getElementById("a"), b = d.getElementById("b");
    a.dispatchEvent(new dom.window.Event("wheel", { bubbles: true }));
    b.dispatchEvent(new dom.window.Event("scroll"));
    expect(b.classList.contains("mc-scrolling")).toBe(false);
    d.body.dispatchEvent(new dom.window.Event("keydown", { bubbles: true }));
    b.dispatchEvent(new dom.window.Event("scroll"));
    expect(b.classList.contains("mc-scrolling"), "keys with focus on the page").toBe(true);
  });
});
