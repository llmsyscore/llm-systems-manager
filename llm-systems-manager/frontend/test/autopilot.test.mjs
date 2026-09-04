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
  it("the vLLM manual-apply rule is stated in the card footer, not per row", () => {
    // The per-row badge became the card footer note in the #797 table layout.
    const indexSrc = srcFile("index.html");
    const card = indexSrc.slice(indexSrc.indexOf('id="apEntriesCard"'),
                                indexSrc.indexOf('id="apProposalsCard"'));
    expect(card).toContain("vLLM entries never auto-execute");
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

  it("placement is a select showing auto + capable agents by hostname", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama"});
    const sel = row.querySelector("[data-field=placement]");
    expect(sel.tagName).toBe("SELECT");
    const values = [...sel.querySelectorAll("option")].map(o => o.value);
    expect(values[0]).toBe("auto");
    expect(values).toContain("agent-llama-aaaaaaaa");
    // Pending (unapproved) agents are never offered as a placement target.
    expect(values).not.toContain("agent-pending-aaaaaa");
    // Options display the hostname, not the raw agent id.
    const opt = [...sel.querySelectorAll("option")].find(o => o.value === "agent-llama-aaaaaaaa");
    expect(opt.textContent).toBe("hostA");
    expect(sel.value).toBe("auto");
  });

  it("an unknown current placement stays selectable and labeled", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, placement: "gone-agent-aaaaaaaaa"});
    const sel = row.querySelector("[data-field=placement]");
    expect(sel.value).toBe("gone-agent-aaaaaaaaa");
    expect(sel.selectedOptions[0].textContent).toContain("unknown agent");
  });

  it("changing provider rebuilds the placement select for the new capability", () => {
    AP.setCatalog(catalog);
    const row = AP.entryRow({...E, provider: "llama"});
    const providerSel = row.querySelector("[data-field=provider]");
    const sel = row.querySelector("[data-field=placement]");
    providerSel.value = "vllm";
    providerSel.dispatchEvent(new Event("change", {bubbles: true}));
    const values = [...sel.querySelectorAll("option")].map(o => o.value);
    expect(values).toContain("agent-vllm-aaaaaaaaa");
    expect(values).not.toContain("agent-llama-aaaaaaaa");
    expect(sel.value).toBe("auto");
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

describe("placement select labels (#479 follow-up)", () => {
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
    const sel = row.querySelector("[data-field=placement]");
    expect(sel.value).toBe("agent-pending-aaaaaa");
    expect(sel.selectedOptions[0].textContent).toBe("hostC (not approved)");
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
