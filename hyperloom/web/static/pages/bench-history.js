/* Benchmark history, compare and share (F29), plus the ad-hoc target allow-list (F32): every stored run with
   open / compare / share / clear; any two runs of one kind side by side; copy as text or a PNG card drawn in the
   browser from the allow-listed share summary only. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-history", KINDS = ["decode", "prefill", "ab", "quality", "tools", "demo"];
  const v = () => B().view(PAGE, {kind: "", selected: "", a: "", b: "", clear_kind: "", confirm: "", t_id: "", t_label: "", t_scheme: "http", t_host: "", t_port: "", t_key: "", t_machines: "", t_context: "", t_sampling: false, t_thinking: false});
  const runsEndpoint = () => `bench/runs?${new URLSearchParams({...(v().kind ? {kind: v().kind} : {}), limit: "100"})}`;

  function list() {
    const {esc, card, table, cells, statusPill} = H(), view = v();
    const doc = H().state.docs[runsEndpoint()];
    const filter = `<div class="field"><label for="history-kind">Kind</label><select class="inp" id="history-kind" data-page-action="bench-field" data-key="kind"><option value="">all kinds</option>${KINDS.map(k => `<option value="${k}" ${k === view.kind ? "selected" : ""}>${esc(B().KIND_TITLES[k])}</option>`).join("")}</select></div>`;
    if (!doc) return card("Runs", filter + `<p>${H().unavailable("loading")}</p>`);
    if (doc.runs == null) return card("Runs", filter + `<p>${H().unavailable(doc.why?.runs || "history unavailable")}</p>`);
    const rows = doc.runs.map(r => cells([esc(new Date(r.started * 1000).toLocaleString()), esc(B().KIND_TITLES[r.kind] || r.kind), esc(r.target_label || ""), esc(r.model || ""), statusPill(r.state), esc(headline(r)),
      `<div class="row"><button type="button" class="btn sm" data-page-action="history-pick" data-slot="selected" data-run="${esc(r.id)}" ${view.selected === r.id ? 'aria-pressed="true"' : ""}>View</button><button type="button" class="btn sm" data-page-action="history-pick" data-slot="a" data-run="${esc(r.id)}" ${view.a === r.id ? 'aria-pressed="true"' : ""}>A</button><button type="button" class="btn sm" data-page-action="history-pick" data-slot="b" data-run="${esc(r.id)}" ${view.b === r.id ? 'aria-pressed="true"' : ""}>B</button></div>`])).join("");
    return card("Runs", `<div class="row between">${filter}<span class="hint">Pick A and B to compare two runs of one kind (any targets or machines).</span></div>${doc.runs.length ? table(["When", "Kind", "Target", "Model", "State", "Result", ""], rows, "Stored runs") : `<p class="muted">No runs stored yet.</p>`}`, "every run is kept with its summary; open one to see it");
  }
  function headline(r) {
    const s = r.summary || {};
    if (r.kind === "decode") return s.best_aggregate_rate != null ? `best ${s.best_aggregate_rate} tok/s aggregate` : "";
    if (r.kind === "prefill") return `${(s.sizes || []).length} sizes`;
    if (r.kind === "ab") return s.claim || "";
    if (r.kind === "quality" || r.kind === "tools") return s.score != null ? `${(s.score * 100).toFixed(1)} %` : "";
    if (r.kind === "demo") return `${s.completed ?? 0} of ${s.tiles ?? 0} prompts`;
    return "";
  }
  function compare() {
    const {esc, card, table, cells} = H(), view = v();
    if (!view.a || !view.b) return card("Compare", `<p class="muted">Pick run A and run B in the list.</p>`);
    const key = `bench/compare?a=${view.a}&b=${view.b}`, doc = H().state.docs[key];
    if (!doc) return card("Compare", `<p>${H().unavailable(H().state.errors[key] || "loading")}</p>`);
    const c = doc.comparison, fmt = x => x == null ? H().unavailable("not measured") : esc(typeof x === "number" ? x.toLocaleString(undefined, {maximumFractionDigits: 4}) : x);
    const head = `<p>A: ${esc(c.runs[0].target_label)} · ${esc(c.runs[0].model || "")} · ${esc(new Date(c.runs[0].started * 1000).toLocaleString())}<br>B: ${esc(c.runs[1].target_label)} · ${esc(c.runs[1].model || "")} · ${esc(new Date(c.runs[1].started * 1000).toLocaleString())}</p>`;
    let body = "";
    if (c.paired) {
      const p = c.paired;
      body = `<div class="banner ${p.verdict.startsWith("significant") ? "warn" : "info"}"><b>${esc(p.verdict)}</b></div><dl class="kv"><dt>Paired items</dt><dd>${esc(p.paired)}</dd><dt>A / B on paired items</dt><dd>${fmt(p.this_score)} / ${fmt(p.other_score)}</dd><dt>Only A / only B</dt><dd>${esc(p.only_this)} / ${esc(p.only_other)}</dd><dt>Exact McNemar p</dt><dd>${esc(p.mcnemar_p)}</dd><dt>95 % interval (A − B)</dt><dd>${p.interval_95 ? esc(p.interval_95.join(" … ")) : H().unavailable("no paired items")} <span class="hint">${esc(p.interval_basis)}</span></dd></dl>`;
    } else if (c.rows.length) {
      const keys = Object.keys(c.rows[0]);
      body = table(keys.map(k => k.replaceAll("_", " ")), c.rows.map(row => cells(keys.map(k => Array.isArray(row[k]) ? `${fmt(row[k][0])} → ${fmt(row[k][1])}` : fmt(row[k])))).join(""), "Comparison");
    } else body = `<p class="muted">Nothing to compare.</p>`;
    return card("Compare", head + body, `${esc(B().KIND_TITLES[c.kind] || c.kind)} · A → B`);
  }
  function selected() {
    const {esc, card, button} = H(), view = v();
    if (!view.selected) return card("Run", `<p class="muted">Pick a run with View.</p>`);
    const share = H().state.docs[`bench/share/${view.selected}`], run = H().state.docs[`bench/runs/${view.selected}`]?.run;
    if (!share || !run) return card("Run", `<p>${H().unavailable(H().state.errors[`bench/runs/${view.selected}`] || "loading")}</p>`);
    const detail = `<details><summary>Stored detail (JSON)</summary><pre class="raw">${esc(JSON.stringify(run, null, 1).slice(0, 200000))}</pre></details>`;
    return card(`Run ${run.id}`, `<div class="grid g2"><div><h3 class="bench-h3">Share summary</h3><pre class="bench-share" tabindex="0">${esc(share.text)}</pre><p class="hint">The share summary carries no prompts, replies, addresses, paths, request or session ids; the target is shown by its engine family only.</p><div class="row">${button("Copy as text", "", {extra: `data-page-action="history-copy"`})}${button("Download PNG card", "", {extra: `data-page-action="history-png"`})}${button("Download JSON", "", {extra: `data-page-action="history-json"`})}</div></div><div><canvas class="bench-canvas" width="1200" height="630" aria-label="Share card preview"></canvas></div></div>${detail}`, `${esc(B().KIND_TITLES[run.kind] || run.kind)} · ${esc(run.state)}`);
  }
  function clearCard() {
    const {esc, card, button} = H(), view = v();
    const ready = view.confirm === "clear";
    return card("Clear history", `<div class="grid g2"><div class="field"><label for="history-clear-kind">Scope</label><select class="inp" id="history-clear-kind" data-page-action="bench-field" data-key="clear_kind"><option value="">every kind</option>${KINDS.map(k => `<option value="${k}" ${k === view.clear_kind ? "selected" : ""}>${esc(B().KIND_TITLES[k])}</option>`).join("")}</select></div><div class="field"><label for="history-confirm">Type clear to confirm</label><input class="inp" id="history-confirm" value="${esc(view.confirm)}" autocomplete="off" data-page-action="bench-field" data-key="confirm"></div></div><div class="row">${button(view.selected ? "Clear the viewed run" : "Clear the viewed run", "", {style: "dan", disabled: !view.selected ? "View a run first" : !ready ? "Type clear to confirm" : "", extra: `data-page-action="history-clear" data-scope="run"`})}${button("Clear the scope", "", {style: "dan", disabled: ready ? "" : "Type clear to confirm", extra: `data-page-action="history-clear" data-scope="kind"`})}</div><p class="hint">Running runs are never cleared. Clearing is recorded in the audit log.</p>`);
  }
  function targetsCard() {
    const {esc, card, table, cells, button} = H(), view = v();
    const rows = B().targetRows();
    const adhoc = rows.filter(t => t.kind === "adhoc").map(t => cells([esc(t.id), esc(t.label), `<span class="mono">${esc(t.address)}</span>`, esc(t.key_ref || "none"), esc(Array.isArray(t.machines) ? t.machines.join(", ") : t.machines || "everything (not declared)"), esc([t.sampling ? "sampling" : "", t.thinking ? "thinking" : ""].filter(Boolean).join(", ") || "greedy only"), button("Remove", "", {extra: `data-page-action="history-target-remove" data-id="${esc(t.id)}"`})])).join("");
    const profiles = rows.filter(t => t.kind === "profile").map(t => cells([esc(t.id), esc(t.state || ""), `<label class="check"><input type="checkbox" data-page-action="history-thinking" data-profile="${esc(t.id)}" ${t.thinking ? "checked" : ""}><span>declares a thinking switch</span></label>`])).join("");
    const input = (key, label, extra = "") => `<div class="field"><label for="history-${key}">${esc(label)}</label><input class="inp" id="history-${key}" value="${esc(view[key])}" data-page-action="bench-field" data-key="${key}" ${extra}></div>`;
    const form = `<div class="grid g2">${input("t_id", "Id (lowercase)")}${input("t_label", "Label")}<div class="field"><label for="history-t_scheme">Scheme</label><select class="inp" id="history-t_scheme" data-page-action="bench-field" data-key="t_scheme"><option ${view.t_scheme === "http" ? "selected" : ""}>http</option><option ${view.t_scheme === "https" ? "selected" : ""}>https</option></select></div>${input("t_host", "Host (name or address)")}${input("t_port", "Port", 'type="number" min="1" max="65535" data-type="int"')}${input("t_key", "Key: secret name (optional)")}${input("t_machines", "Machines it uses (ids, comma-separated, or external)")}${input("t_context", "Context tokens (optional)", 'type="number" data-type="int"')}</div><div class="row"><label class="check"><input type="checkbox" data-page-action="bench-field" data-key="t_sampling" data-type="bool" ${view.t_sampling ? "checked" : ""}><span>honours sampling</span></label><label class="check"><input type="checkbox" data-page-action="bench-field" data-key="t_thinking" data-type="bool" ${view.t_thinking ? "checked" : ""}><span>honours a thinking switch</span></label>${button("Save target", "", {style: "pri", disabled: view.t_id && view.t_host && view.t_port ? "" : "Id, host and port are required", extra: `data-page-action="history-target-save"`})}</div><p class="hint">Nothing is probed until a run starts. Redirects are never followed; the key comes only from the secret store and is never sent anywhere else. Without declared machines a target holds every resource while it runs.</p>`;
    return card("Benchmark targets", `${adhoc ? table(["Id", "Label", "Address", "Key", "Machines", "Honours", ""], adhoc, "Ad-hoc targets") : `<p class="muted">No ad-hoc target allow-listed.</p>`}<details><summary>Add an ad-hoc target</summary>${form}</details>${profiles ? `<h3 class="bench-h3">Profiles</h3>${table(["Profile", "State", "Declaration"], profiles, "Profile declarations")}` : ""}`, "allow-list (F32)");
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Benchmark history", group: "Benchmarks", order: 20,
    icon: '<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/>',
    endpoints() {
      const view = v(), list = ["bench/targets", runsEndpoint()];
      if (!view.selected && !view.cleared) view.selected = H().state.docs[runsEndpoint()]?.runs?.[0]?.id || "";
      if (view.selected) list.push(`bench/runs/${view.selected}`, `bench/share/${view.selected}`);
      if (view.a && view.b) list.push(`bench/compare?a=${view.a}&b=${view.b}`);
      return B().track(list);
    },
    ready: () => !!H().state.docs["bench/targets"] && !!H().state.docs[runsEndpoint()],
    render() {
      for (const slot of ["selected", "a", "b"]) {
        const value = v()[slot];
        if (value && /unknown run/.test(H().state.errors[`bench/runs/${value}`] || H().state.errors[`bench/share/${value}`] || "")) v()[slot] = "";
      }
      return `<div class="bench" data-bench-page="${PAGE}">${list()}<div class="grid g2">${compare()}${clearCard()}</div>${selected()}${targetsCard()}</div>`;
    },
    after(main) {
      const share = H().state.docs[`bench/share/${v().selected}`], canvas = main.querySelector(".bench-canvas");
      if (share && canvas) canvas.getContext("2d").drawImage(B().shareCanvas(share.share, share.text), 0, 0);
    },
  };

  async function act(action, args, done) {
    try { const result = await B().post(action, args); H().announce(done(result)); }
    catch (error) { H().announce(`${action} refused: ${error.message}`); }
    H().refresh();
  }
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled || !control.closest(`[data-bench-page="${PAGE}"]`)) return;
    const view = v(), action = control.dataset.pageAction;
    const share = H().state.docs[`bench/share/${view.selected}`];
    if (action === "history-pick") { view[control.dataset.slot] = view[control.dataset.slot] === control.dataset.run ? "" : control.dataset.run; B().save(PAGE); H().refresh(); }
    else if (action === "history-copy" && share) B().copyText(share.text);
    else if (action === "history-png" && share) B().downloadPng(share.share, share.text);
    else if (action === "history-json" && share) H().download(`hyperloom-bench-${share.share.kind}-${share.share.started}.json`, JSON.stringify(share.share, null, 1));
    else if (action === "history-clear") {
      const args = control.dataset.scope === "run" ? {confirm: "clear", run: view.selected} : {confirm: "clear", ...(view.clear_kind ? {kind: view.clear_kind} : {})};
      view.confirm = ""; view.selected = ""; view.cleared = true;
      act("bench/runs/clear", args, r => `${r.removed} run(s) cleared.`);
    } else if (action === "history-target-remove") act("bench/targets/remove", {id: control.dataset.id}, r => `Target ${r.removed} removed.`);
    else if (action === "history-target-save") {
      const entry = {id: view.t_id.trim(), scheme: view.t_scheme, host: view.t_host.trim(), port: Number(view.t_port)};
      if (view.t_label) entry.label = view.t_label.trim();
      if (view.t_key) entry.key = view.t_key.trim();
      if (view.t_machines) entry.machines = view.t_machines.trim() === "external" ? "external" : view.t_machines.split(",").map(m => m.trim()).filter(Boolean);
      if (view.t_context) entry.context_tokens = Number(view.t_context);
      if (view.t_sampling) entry.sampling = true;
      if (view.t_thinking) entry.thinking = true;
      act("bench/targets/save", entry, r => `Target ${r.saved} saved.`);
    }
  });
  document.addEventListener("change", event => {
    const control = event.target.closest?.('[data-page-action="history-thinking"]');
    if (control) act("bench/targets/capabilities", {profile: control.dataset.profile, thinking: control.checked}, r => `Profile ${r.profile}: thinking switch ${r.thinking ? "declared" : "not declared"}.`);
  });
})();
