import { describe, it, expect, vi } from "vitest";
import { AP } from "../js/autopilot.js";

const E = {model: "m1", provider: "llama", placement: "auto",
  failover: "semi", priority: 100, min_replicas: 1, max_replicas: 1};

describe("entry editor round-trip", () => {
  it("readEntries returns what entryRow rendered", () => {
    const box = document.createElement("div");
    box.appendChild(AP.entryRow(E));
    box.appendChild(AP.entryRow({...E, model: "m2", provider: "vllm",
                                 max_replicas: 3}));
    const out = AP.readEntries(box);
    expect(out).toHaveLength(2);
    expect(out[0].model).toBe("m1");
    expect(out[1].max_replicas).toBe(3);
  });
  it("vllm rows carry the manual-only badge", () => {
    const el = AP.entryRow({...E, provider: "vllm"});
    expect(el.textContent).toContain("manual-apply only");
  });
});

describe("proposalRow", () => {
  it("apply button fires the callback with the proposal id", () => {
    const onApply = vi.fn();
    const el = AP.proposalRow({id: "p1", reason: "failover: m1",
      action: {kind: "load", model: "m1", agent_id: "x".repeat(32)}},
      {onApply, onDismiss: () => {}});
    el.querySelector("[data-act=apply]").click();
    expect(onApply).toHaveBeenCalledWith("p1");
  });
});

describe("poll no longer clobbers unsaved edits (#472)", () => {
  it("keeps a dirty toggle + added row across a poll-triggered fetchState, but still re-renders proposals", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle">
      <div id="apEntriesBody"></div>
      <div id="apProposalsBody"></div>
      <span id="apSaveStatus"></span>
    `;
    const emptyState = {state: {enabled: false, entries: [], hosts: {}},
      proposals: [], last_plan_ts: null};
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: () => Promise.resolve(emptyState),
    })));

    AP.init();
    await Promise.resolve();
    await Promise.resolve();

    const toggle = document.getElementById("apEnabledToggle");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", {bubbles: true}));
    AP.addEntry();
    expect(document.querySelectorAll("#apEntriesBody .ap-entry-row")).toHaveLength(1);

    // Stale marker proves _renderProposals() still ran unconditionally.
    const proposalsBody = document.getElementById("apProposalsBody");
    const marker = document.createElement("div");
    marker.className = "stale-marker";
    proposalsBody.appendChild(marker);

    await AP.fetchState(); // simulates the 10s poll tick

    expect(toggle.checked).toBe(true);
    expect(document.querySelectorAll("#apEntriesBody .ap-entry-row")).toHaveLength(1);
    expect(proposalsBody.querySelector(".stale-marker")).toBeNull();
  });
});
