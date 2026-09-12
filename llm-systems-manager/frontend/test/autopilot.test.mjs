import { describe, it, expect, vi } from "vitest";
import { srcFile } from "./helpers/harness.js";
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
  it("the vLLM manual-apply rule is stated in the help dialog, not per row", () => {
    // Per-row badge (#472) → card footer note (#797) → help dialog (#907).
    const indexSrc = srcFile("index.html");
    const help = indexSrc.slice(indexSrc.indexOf('id="apHelpOverlay"'), indexSrc.indexOf('id="svcConfigOverlay"'));
    expect(help).toContain("never auto-execute");
  });
  it("size_mb round-trips as an int (#474)", () => {
    const box = document.createElement("div");
    box.appendChild(AP.entryRow({...E, provider: "vllm", size_mb: 15000}));
    const out = AP.readEntries(box);
    expect(out[0].size_mb).toBe(15000);
  });
  it("a blank size (MB) input is omitted, not NaN/null (#474)", () => {
    const box = document.createElement("div");
    box.appendChild(AP.entryRow(E));           // no size_mb
    const row = box.querySelector(".ap-entry-row");
    expect(row.querySelector('[data-field="size_mb"]').value).toBe("");
    const out = AP.readEntries(box);
    expect("size_mb" in out[0]).toBe(false);
  });
  it("size placeholder tracks the provider: required for vllm, auto elsewhere (#474)", () => {
    const row = AP.entryRow({...E, provider: "vllm"});
    const size = row.querySelector('[data-field="size_mb"]');
    expect(size.placeholder).toBe("required");
    const providerSel = row.querySelector("[data-field=provider]");
    providerSel.value = "llama";
    providerSel.dispatchEvent(new Event("change", {bubbles: true}));
    expect(size.placeholder).toBe("auto");
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

  it("model is a select built from the injected catalog (#797)", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama", model: "llama-model"});
    const modelSel = row.querySelector("[data-field=model]");
    expect(modelSel.tagName).toBe("SELECT");
    expect([...modelSel.querySelectorAll("option")].map(o => o.value)).toEqual(["llama-model"]);
    expect(modelSel.value).toBe("llama-model");
  });

  it("an undiscovered current model stays selectable", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama", model: "gone-model"});
    const modelSel = row.querySelector("[data-field=model]");
    expect(modelSel.value).toBe("gone-model");
    expect(modelSel.selectedOptions[0].textContent).toContain("not discovered");
  });

  const chipsOf = (row) => [...row.querySelectorAll(".ap-host-chip")].map(c => [c.dataset.agent, c.textContent, c.classList.contains("on")]);

  it("placement is an auto toggle; off reveals capable-host chips by hostname (#907)", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama"});
    const hidden = row.querySelector("[data-field=placement]");
    const tgl = row.querySelector(".ap-place .mc-toggle");
    const hosts = row.querySelector(".ap-hosts");
    expect(hidden.value).toBe("auto");
    expect(tgl.classList.contains("on")).toBe(true);
    expect(hosts.hidden).toBe(true);
    tgl.click();
    expect(hosts.hidden).toBe(false);
    // Off pins to the first capable host; pending (unapproved) agents are never offered.
    expect(hidden.value).toBe("agent-llama-aaaaaaaa");
    expect(chipsOf(row)).toEqual([["agent-llama-aaaaaaaa", "hostA", true]]);
    tgl.click();
    expect(hidden.value).toBe("auto");
    expect(hosts.hidden).toBe(true);
  });

  it("auto cannot be turned off when the provider has no capable host", () => {
    AP.setCatalog({models: {}, agents: []});
    const row = AP.entryRow({...E, provider: "llama"});
    const tgl = row.querySelector(".ap-place .mc-toggle");
    tgl.click();
    expect(tgl.classList.contains("on")).toBe(true);
    expect(row.querySelector("[data-field=placement]").value).toBe("auto");
    expect(row.querySelector(".ap-hosts").hidden).toBe(true);
  });

  it("clicking a host chip pins the entry and round-trips (#907)", () => {
    AP.setCatalog({...catalog, agents: [...catalog.agents,
      {agent_id: "agent-llama-bbbbbbbb", hostname: "hostD", status: "approved", capabilities: {llama: true}}]});
    const box = document.createElement("div");
    const row = AP.entryRow({...E, placement: "agent-llama-aaaaaaaa"});
    box.appendChild(row);
    expect(row.querySelector(".ap-place .mc-toggle").classList.contains("on")).toBe(false);
    const chip = [...row.querySelectorAll(".ap-host-chip")].find(c => c.dataset.agent === "agent-llama-bbbbbbbb");
    chip.click();
    expect(chipsOf(row).filter(c => c[2]).map(c => c[0])).toEqual(["agent-llama-bbbbbbbb"]);
    expect(AP.readEntries(box)[0].placement).toBe("agent-llama-bbbbbbbb");
  });

  it("an unknown current placement stays pinned and labeled", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, placement: "gone-agent-aaaaaaaaa"});
    expect(row.querySelector("[data-field=placement]").value).toBe("gone-agent-aaaaaaaaa");
    const on = chipsOf(row).find(c => c[2]);
    expect(on[0]).toBe("gone-agent-aaaaaaaaa");
    expect(on[1]).toContain("unknown agent");
  });

  it("changing provider rebuilds the host chips for the new capability", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama", placement: "agent-llama-aaaaaaaa"});
    const providerSel = row.querySelector("[data-field=provider]");
    providerSel.value = "vllm";
    providerSel.dispatchEvent(new Event("change", {bubbles: true}));
    const ids = chipsOf(row).map(c => c[0]);
    expect(ids).toContain("agent-vllm-aaaaaaaaa");
    // The current pin survives the provider switch, labeled as not capable.
    expect(chipsOf(row).find(c => c[0] === "agent-llama-aaaaaaaa")[1]).toContain("not vllm-capable");
    expect(row.querySelector("[data-field=placement]").value).toBe("agent-llama-aaaaaaaa");
  });

  it("round-trip is unaffected by the datalist wiring", () => {
    AP.setCatalog(catalog);
    const box = document.createElement("div");
    box.appendChild(AP.entryRow(E));
    const out = AP.readEntries(box);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({model: "m1", provider: "llama", placement: "auto"});
  });

  it("changing provider swaps the model select's options", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama", model: "llama-model"});
    const providerSel = row.querySelector("[data-field=provider]");
    const modelSel = row.querySelector("[data-field=model]");
    expect([...modelSel.querySelectorAll("option")].map(o => o.value)).toContain("llama-model");

    providerSel.value = "vllm";
    providerSel.dispatchEvent(new Event("change", {bubbles: true}));

    const values = [...modelSel.querySelectorAll("option")].map(o => o.value);
    expect(values).toContain("vllm-model");
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
      .toBe("plan: 2 action(s), 1 waiting for approval");
  });
  it("failover is an auto/semi toggle that round-trips (#907)", () => {
    const box = document.createElement("div");
    const row = AP.entryRow(E);
    box.appendChild(row);
    const tgl = row.querySelector('[data-field="failover"]');
    expect(tgl.tagName).toBe("BUTTON");
    expect(tgl.classList.contains("on")).toBe(false);
    expect(AP.readEntries(box)[0].failover).toBe("semi");
    tgl.click();
    expect(tgl.getAttribute("aria-pressed")).toBe("true");
    expect(AP.readEntries(box)[0].failover).toBe("auto");
    expect(AP.entryRow({...E, failover: "auto"}).querySelector('[data-field="failover"]').classList.contains("on")).toBe(true);
  });
});

describe("rank by + placement basis (#907)", () => {
  it("rank_by defaults to speed and round-trips", () => {
    const box = document.createElement("div");
    box.appendChild(AP.entryRow(E));
    box.appendChild(AP.entryRow({...E, model: "m2", rank_by: "energy"}));
    const out = AP.readEntries(box);
    expect(out[0].rank_by).toBe("speed");
    expect(out[1].rank_by).toBe("energy");
    const labels = [...box.querySelector('select[data-field="rank_by"]').options].map(o => o.text);
    expect(labels).toEqual(["speed", "energy", "capacity"]);
  });
  const rows = [
    {agent_id: "b".repeat(32), hostname: "hostB", gen_tps: 65.04, wh_per_ktok: 1.204, age_s: 3 * 86400, tier: "measured"},
    {agent_id: "a".repeat(32), hostname: "hostA", gen_tps: 40, wh_per_ktok: null, age_s: 40 * 86400, tier: "advisory"},
  ];
  it("measured chip names the pick and lists the table in its tooltip", () => {
    const chip = AP.basisChip(E, {basis: "measured", pick: "b".repeat(32), speed: rows});
    expect(chip.textContent).toBe("measured · hostB");
    expect(chip.className).toContain("info");
    expect(chip.getAttribute("data-tip").split("\n")).toEqual([
      "ranked by measured speed",
      "hostB · 65.0 t/s · 1.20 Wh/1k · 3d ago",
      "hostA · 40.0 t/s · 40d ago · advisory",
    ]);
  });
  it("advisory and capacity-only chips are muted; pinned entries get none", () => {
    expect(AP.basisChip(E, {basis: "advisory", pick: "a".repeat(32), speed: rows}).textContent).toBe("advisory · hostA");
    const cap = AP.basisChip({...E, rank_by: "energy"}, {basis: "capacity", pick: "a".repeat(32), speed: []});
    expect(cap.textContent).toBe("capacity only");
    expect(cap.className).toContain("dim");
    expect(cap.getAttribute("data-tip")).toContain("ranked by measured energy");
    expect(cap.getAttribute("data-tip")).toContain("no live benchmark run");
    expect(AP.basisChip(E, {basis: null, pick: "a".repeat(32), speed: rows})).toBeNull();
    expect(AP.basisChip(E, undefined)).toBeNull();
  });
  it("the bench link deep-links llama entries to Benchmark · Live with the ranking on", () => {
    globalThis.toolsDeepLink = vi.fn();
    const link = AP.benchLink(E);
    link.click();
    expect(globalThis.toolsDeepLink).toHaveBeenCalledWith("benchmark", "m1", {fleet: true});
    expect(AP.benchLink({...E, provider: "vllm"})).toBeNull();
    delete globalThis.toolsDeepLink;
  });
  it("save() carries speed_max_age_days through from the loaded state", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle">
      <div id="apEntriesBody"></div><div id="apProposalsBody"></div><span id="apSaveStatus"></span>`;
    const puts = [];
    vi.stubGlobal("fetch", vi.fn((url, opts) => {
      if (opts && opts.method === "PUT") {
        puts.push(JSON.parse(opts.body));
        return Promise.resolve({ok: true, json: () => Promise.resolve({ok: true, state: puts[0]})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve({
        state: {enabled: false, entries: [E], hosts: {}, speed_max_age_days: 7},
        proposals: [], entry_status: {}, last_plan_ts: null})});
    }));
    await AP.init();
    await AP.save();
    expect(puts).toHaveLength(1);
    expect(puts[0].speed_max_age_days).toBe(7);
    expect(puts[0].entries[0]).toMatchObject({model: "m1", placement: "auto", failover: "semi", rank_by: "speed"});
  });
});

describe("plan card visibility (#909)", () => {
  const st = (fo) => ({enabled: false, hosts: {}, entries: fo ? [{...E, failover: fo}] : []});
  it("is relevant only with a waiting proposal or a semi entry", () => {
    expect(AP.planCardRelevant(st(null), [])).toBe(false);
    expect(AP.planCardRelevant(st("auto"), [])).toBe(false);
    expect(AP.planCardRelevant(st("semi"), [])).toBe(true);
    expect(AP.planCardRelevant(st("auto"), [{id: "p1"}])).toBe(true);
    expect(AP.planCardRelevant(null, [])).toBe(false);
  });
  it("fetchState hides the card when nothing is planned and shows it when a proposal waits", async () => {
    document.body.innerHTML = `<div id="apProposalsCard" hidden><div id="apProposalsBody"></div></div>
      <span id="apProposalsPill"></span><tbody id="apEntriesBody"></tbody><span id="apSaveStatus"></span>`;
    let payload = {state: st("auto"), proposals: [], last_plan_ts: null};
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ok: true, json: () => Promise.resolve(payload)})));
    await AP.fetchState();
    expect(document.getElementById("apProposalsCard").hidden).toBe(true);
    payload = {state: st("auto"), proposals: [{id: "p1", entry_key: "m1/llama", reason: "x"}], last_plan_ts: null};
    await AP.fetchState();
    expect(document.getElementById("apProposalsCard").hidden).toBe(false);
    expect(document.getElementById("apProposalsPill").textContent).toBe("1 waiting");
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

describe("statusChip reports honest placement/blocked status (#472)", () => {
  const entry = { model: "m1", provider: "llama" };

  it("shows N/M placed when satisfied", () => {
    const chip = AP.statusChip(entry, [], { placed: 1, want: 1, blocked: null });
    expect(chip.textContent).toBe("1/1 placed");
    expect(chip.className).toContain("pill ok");
  });

  it("shows N/M plus the blocked reason when unplaceable", () => {
    const chip = AP.statusChip(entry, [],
      { placed: 0, want: 1, blocked: "model size unknown (set entry size MB)" });
    expect(chip.textContent).toBe("0/1 · model size unknown");
    expect(chip.title).toBe("model size unknown (set entry size MB)");
    expect(chip.className).toContain("pill warn");
  });

  it("a pending proposal still wins over status", () => {
    const chip = AP.statusChip(entry,
      [{ entry_key: "m1/llama" }],
      { placed: 0, want: 1, blocked: "model size unknown (set entry size MB)" });
    expect(chip.textContent).toBe("1 pending");
  });

  it("falls back to stable/muted when no status is given (back-compat)", () => {
    const chip = AP.statusChip(entry, []);
    expect(chip.textContent).toBe("stable");
    expect(chip.className).toContain("pill dim");
  });

  it("shows muted (not ok) when pending placement — under want but not blocked", () => {
    const chip = AP.statusChip(entry, [], { placed: 0, want: 1, blocked: null });
    expect(chip.textContent).toBe("0/1 placed");
    expect(chip.className).toContain("pill dim");
    expect(chip.className).not.toContain("pill ok");
  });
});

describe("planNow surfaces blocked entries instead of a false-satisfied message (#472)", () => {
  it("reports K blocked entries when the tick finds zero actions but entries are blocked", async () => {
    document.body.innerHTML = '<span id="apSaveStatus"></span>';
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("/tick")) {
        return Promise.resolve({ok: true, json: () => Promise.resolve({
          actions: [], proposals: [],
          entry_status: {
            "m1/llama": {placed: 0, want: 1, blocked: "no live agent supports this provider"},
            "m2/llama": {placed: 1, want: 1, blocked: null},
          },
        })});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));
    await AP.planNow();
    expect(document.getElementById("apSaveStatus").textContent)
      .toBe("plan: no plannable actions — 1 entry blocked (see status chips)");
  });

  it("pluralizes to K entries blocked", async () => {
    document.body.innerHTML = '<span id="apSaveStatus"></span>';
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("/tick")) {
        return Promise.resolve({ok: true, json: () => Promise.resolve({
          actions: [], proposals: [],
          entry_status: {
            "m1/llama": {placed: 0, want: 1, blocked: "model size unknown (set entry size MB)"},
            "m2/llama": {placed: 0, want: 1, blocked: "insufficient free VRAM on any candidate"},
          },
        })});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));
    await AP.planNow();
    expect(document.getElementById("apSaveStatus").textContent)
      .toBe("plan: no plannable actions — 2 entries blocked (see status chips)");
  });
});

describe("catalog refresh in-flight + 30s cadence guard (#472)", () => {
  it("collapses overlapping fetchState() calls, throttles a too-soon follow-up, but init() always forces through", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle">
      <div id="apEntriesBody"></div>
      <div id="apProposalsBody"></div>
      <span id="apSaveStatus"></span>
    `;
    let modelHits = 0;
    vi.stubGlobal("fetch", vi.fn(url => {
      if (String(url).includes("-models")) {
        modelHits++;
        return Promise.resolve({ok: true, json: () => Promise.resolve({models: []})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve(
        {state: {enabled: false, entries: [], hosts: {}}, proposals: [], last_plan_ts: null})});
    }));

    // Date.now() is mocked (not real timers) so this test controls the
    // 30s floor precisely and stays isolated from whatever real-time
    // refresh an earlier test in this file already did.
    const base = Date.now();
    let offset = 40000; // start already past the floor
    const nowSpy = vi.spyOn(Date, "now").mockImplementation(() => base + offset);

    try {
      // Two overlapping fetchState() calls: the second's _refreshCatalog()
      // sees the first still in-flight and is skipped, not queued.
      await Promise.all([AP.fetchState(), AP.fetchState()]);
      expect(modelHits).toBe(3); // one refresh x 3 providers (#479), not two

      // A follow-up call 1s later: nothing in-flight now, but well under
      // the 30s floor — skipped by the min-interval guard.
      offset += 1000;
      await AP.fetchState();
      expect(modelHits).toBe(3);

      // Re-entering the tab is exempt from the interval — forces a
      // refresh even though only 1s has passed since the last one.
      await AP.init();
      expect(modelHits).toBe(6);
    } finally {
      nowSpy.mockRestore();
    }
  });
});

describe("themed number steppers (#907)", () => {
  it("▴/▾ step priority by 1 (never below min) and size by 256 MB, blank ▾ is a no-op", () => {
    const box = document.createElement("div");
    box.appendChild(AP.entryRow({...E, priority: 0}));
    const row = box.querySelector(".ap-entry-row");
    const up = (f) => row.querySelector(`.ap-num:has([data-field="${f}"]) .ap-step.up`).click();
    const down = (f) => row.querySelector(`.ap-num:has([data-field="${f}"]) .ap-step.down`).click();
    const changes = []; row.addEventListener("change", e => changes.push(e.target.dataset.field));
    up("priority"); up("priority"); down("priority"); down("priority"); down("priority");
    expect(AP.readEntries(box)[0].priority).toBe(0);
    up("priority");
    expect(AP.readEntries(box)[0].priority).toBe(1);
    down("size_mb");
    expect("size_mb" in AP.readEntries(box)[0]).toBe(false);
    up("size_mb"); up("size_mb");
    expect(AP.readEntries(box)[0].size_mb).toBe(512);
    expect(changes.length).toBe(8);
    // native spinner is replaced, so the arrows must always be in the DOM
    expect(row.querySelectorAll(".ap-num .ap-step").length).toBe(8);
  });
});

describe("placement pick beside the auto toggle (#907)", () => {
  it("shows the planner's host next to an auto toggle and hides it for a pinned entry", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle"><table><tbody id="apEntriesBody"></tbody></table>
      <div id="apProposalsBody"></div><span id="apSaveStatus"></span>`;
    AP.setCatalog({models: {}, agents: [
      {agent_id: "a".repeat(32), hostname: "hostA", status: "approved", capabilities: {llama: true}}]});
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ok: true, json: () => Promise.resolve({
      state: {enabled: false, entries: [E, {...E, model: "m2", placement: "a".repeat(32)}], hosts: {}},
      proposals: [], last_plan_ts: null,
      entry_status: {"m1/llama": {placed: 1, want: 1, blocked: null, basis: "capacity", pick: "a".repeat(32), speed: []},
                     "m2/llama": {placed: 0, want: 1, blocked: null, basis: null, pick: "a".repeat(32), speed: []}}})})));
    await AP.init();
    const picks = [...document.querySelectorAll(".ap-place-pick")];
    expect(picks[0].hidden).toBe(false);
    expect(picks[0].textContent).toBe("→ hostA");
    expect(picks[1].hidden).toBe(true);
  });
});

describe("help dialog (#907)", () => {
  it("the footer keeps a short note plus a help link; the long text lives in the overlay", () => {
    const indexSrc = srcFile("index.html");
    const card = indexSrc.slice(indexSrc.indexOf('id="apEntriesCard"'), indexSrc.indexOf('id="apProposalsCard"'));
    expect(card).toContain('id="apHelpBtn"');
    expect(card).toContain("Autopilot help");
    expect(card).not.toContain("Rank by orders");
    expect(card).not.toContain("vLLM entries never auto-execute");
    const help = indexSrc.slice(indexSrc.indexOf('id="apHelpOverlay"'), indexSrc.indexOf('id="svcConfigOverlay"'));
    expect(help).toContain("Rank by");
    expect(help).toContain("advisory");
    expect(help).toContain("never auto-execute");
    expect(help).toContain("Replicas");
    expect(help).toContain('role="dialog"');
  });
  it("opens on the link, closes on ✕, backdrop click and Escape", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle"><div id="apEntriesBody"></div><div id="apProposalsBody"></div>
      <span id="apSaveStatus"></span><button id="apHelpBtn"></button>
      <div class="svcconfig-overlay" id="apHelpOverlay"><div class="svcconfig-panel"><button id="apHelpClose"></button></div></div>`;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ok: true, json: () => Promise.resolve(
      {state: {enabled: false, entries: [], hosts: {}}, proposals: [], entry_status: {}, last_plan_ts: null})})));
    await AP.init();
    const ov = document.getElementById("apHelpOverlay");
    document.getElementById("apHelpBtn").click();
    expect(ov.classList.contains("open")).toBe(true);
    document.getElementById("apHelpClose").click();
    expect(ov.classList.contains("open")).toBe(false);
    AP.openHelp();
    ov.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    expect(ov.classList.contains("open")).toBe(false);
    AP.openHelp();
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
    expect(ov.classList.contains("open")).toBe(false);
  });
});

describe("placement chip labels (#479 follow-up)", () => {
  const catalog = {
    models: {},
    agents: [
      {agent_id: "agent-pending-aaaaaa", hostname: "hostC", status: "pending",
        capabilities: {llama: true}},
    ],
  };
  it("a pending-but-capable current placement says not approved, not not-capable", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({model: "m1", provider: "llama",
      placement: "agent-pending-aaaaaa", failover: "semi", priority: 100,
      min_replicas: 1, max_replicas: 1});
    expect(row.querySelector("[data-field=placement]").value).toBe("agent-pending-aaaaaa");
    const on = [...row.querySelectorAll(".ap-host-chip")].find(c => c.classList.contains("on"));
    expect(on.textContent).toBe("hostC (not approved)");
  });
});


describe("protect other models toggle (#779)", () => {
  it("renders from state and round-trips through save()", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle">
      <input type="checkbox" id="apProtectToggle">
      <div id="apEntriesBody"></div>
      <div id="apProposalsBody"></div>
      <span id="apSaveStatus"></span>
    `;
    const puts = [];
    vi.stubGlobal("fetch", vi.fn((url, opts) => {
      if (opts && opts.method === "PUT") {
        puts.push(JSON.parse(opts.body));
        return Promise.resolve({ok: true, json: () => Promise.resolve({ok: true, state: puts[0]})});
      }
      return Promise.resolve({ok: true, json: () => Promise.resolve({
        state: {enabled: true, protect_unmanaged: true, entries: [], hosts: {}},
        proposals: [], last_plan_ts: null})});
    }));
    await AP.init();
    const protect = document.getElementById("apProtectToggle");
    expect(protect.checked).toBe(true);

    protect.checked = false;
    protect.dispatchEvent(new Event("change", {bubbles: true}));
    await AP.save();
    expect(puts).toHaveLength(1);
    expect(puts[0].protect_unmanaged).toBe(false);
    expect(puts[0].enabled).toBe(true);
  });
});

describe("status pills keep refreshing while the editor is dirty (#849)", () => {
  it("an abandoned edit freezes only its own row's chip; other rows follow the poll", async () => {
    document.body.innerHTML = `
      <input type="checkbox" id="apEnabledToggle">
      <table><tbody id="apEntriesBody"></tbody></table>
      <div id="apProposalsBody"></div>
      <span id="apSaveStatus"></span>
      <span id="apDirtyNote"></span>
    `;
    const entries = [{...E}, {...E, model: "m2"}];
    let status = {"m1/llama": {placed: 0, want: 1}, "m2/llama": {placed: 0, want: 1}};
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: () => Promise.resolve({
        state: {enabled: true, entries, hosts: {}}, proposals: [],
        last_plan_ts: null, entry_status: status,
      }),
    })));

    await AP.init();
    const rows = () => document.querySelectorAll("#apEntriesBody .ap-entry-row");
    const chip = i => rows()[i].querySelector(".ap-entry-chip");
    expect(chip(0).textContent).toBe("0/1 placed");
    expect(chip(1).textContent).toBe("0/1 placed");

    // Edit row 0 and abandon it: the editor is dirty from here on.
    const prio = rows()[0].querySelector('[data-field="priority"]');
    prio.value = "7";
    prio.dispatchEvent(new Event("input", {bubbles: true}));
    expect(document.getElementById("apDirtyNote").textContent).toBe("unsaved changes");
    AP.addEntry();                       // blank third row, also unsaved
    expect(rows()).toHaveLength(3);

    // Placement changes out of band: the next poll repaints the untouched
    // row, leaves the edited row's chip + value alone, and paints nothing
    // into the blank new row.
    status = {"m1/llama": {placed: 1, want: 1}, "m2/llama": {placed: 0, want: 1, blocked: "no capable agent"}};
    await AP.fetchState();
    expect(rows()).toHaveLength(3);
    expect(rows()[0].querySelector('[data-field="priority"]').value).toBe("7");
    expect(chip(0).textContent).toBe("0/1 placed");
    expect(chip(1).textContent).toContain("0/1");
    expect(chip(1).title).toBe("no capable agent");
    expect(rows()[2].querySelector(".ap-entry-status").childElementCount).toBe(0);
    expect(document.getElementById("apDirtyNote").textContent).toBe("unsaved changes");

    // Saving clears the flags: every row renders from the server again.
    vi.stubGlobal("fetch", vi.fn((url, opts) => Promise.resolve({
      ok: true, json: () => Promise.resolve(opts && opts.method === "PUT"
        ? {state: {enabled: true, entries, hosts: {}}}
        : {state: {enabled: true, entries, hosts: {}}, proposals: [], entry_status: status}),
    })));
    await AP.save();
    expect(rows()).toHaveLength(2);
    expect(chip(0).textContent).toBe("1/1 placed");
    expect(document.getElementById("apDirtyNote").textContent).toBe("");
  });
});
