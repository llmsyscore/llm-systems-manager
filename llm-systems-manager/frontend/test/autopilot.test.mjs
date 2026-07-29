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

describe("model/placement datalists (#472)", () => {
  const catalog = {
    models: {
      llama: [{id: "llama-model", agents: ["hostA"]}],
      vllm: [{id: "vllm-model", agents: []}],
    },
    agents: [
      {agent_id: "agent-llama-aaaaaaaa", hostname: "hostA", status: "approved",
        capabilities: {llama: true}},
      {agent_id: "agent-vllm-aaaaaaaaa", hostname: "hostB", status: "approved",
        capabilities: {vllm: true}},
      {agent_id: "agent-pending-aaaaaa", hostname: "hostC", status: "pending",
        capabilities: {llama: true}},
    ],
  };

  it("entryRow renders datalist options from the injected catalog", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama"});
    const modelInput = row.querySelector("[data-field=model]");
    const modelDl = row.querySelector("#" + modelInput.getAttribute("list"));
    expect([...modelDl.querySelectorAll("option")].map(o => o.value)).toEqual(["llama-model"]);

    const placementInput = row.querySelector("[data-field=placement]");
    const placementDl = row.querySelector("#" + placementInput.getAttribute("list"));
    const placementValues = [...placementDl.querySelectorAll("option")].map(o => o.value);
    expect(placementValues).toContain("auto");
    expect(placementValues).toContain("agent-llama-aaaaaaaa");
    // Pending (unapproved) agents are never offered as a placement target.
    expect(placementValues).not.toContain("agent-pending-aaaaaa");
  });

  it("round-trip is unaffected by the datalist wiring", () => {
    AP.setCatalog(catalog);
    const box = document.createElement("div");
    box.appendChild(AP.entryRow(E));
    const out = AP.readEntries(box);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({model: "m1", provider: "llama", placement: "auto"});
  });

  it("changing provider swaps the model datalist", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama"});
    const providerSel = row.querySelector("[data-field=provider]");
    const modelInput = row.querySelector("[data-field=model]");
    const modelDl = row.querySelector("#" + modelInput.getAttribute("list"));
    expect([...modelDl.querySelectorAll("option")].map(o => o.value)).toContain("llama-model");

    providerSel.value = "vllm";
    providerSel.dispatchEvent(new Event("change", {bubbles: true}));

    const values = [...modelDl.querySelectorAll("option")].map(o => o.value);
    expect(values).toContain("vllm-model");
    expect(values).not.toContain("llama-model");
  });
});

describe("plan now surfaces the tick result (#472)", () => {
  it("reports a satisfied fleet when the tick finds zero actions", async () => {
    document.body.innerHTML = '<span id="apSaveStatus"></span>';
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("/tick")) {
        return Promise.resolve({ok: true,
          json: () => Promise.resolve({actions: [], proposals: []})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));
    await AP.planNow();
    expect(document.getElementById("apSaveStatus").textContent)
      .toBe("plan: no actions needed — desired state satisfied");
  });

  it("reports action/proposal counts when the tick does something", async () => {
    document.body.innerHTML = '<span id="apSaveStatus"></span>';
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("/tick")) {
        return Promise.resolve({ok: true,
          json: () => Promise.resolve({actions: [{}, {}], proposals: [{}]})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));
    await AP.planNow();
    expect(document.getElementById("apSaveStatus").textContent)
      .toBe("plan: 2 action(s), 1 proposal(s) pending");
  });

  it("surfaces a tick HTTP error instead of staying silent", async () => {
    document.body.innerHTML = '<span id="apSaveStatus"></span>';
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("/tick")) {
        return Promise.resolve({ok: false, status: 500,
          json: () => Promise.resolve({error: "boom"})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));
    await AP.planNow();
    expect(document.getElementById("apSaveStatus").textContent).toBe("✗ plan failed: boom");
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
