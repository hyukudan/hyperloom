/* Shared pieces of the benchmark pages (bench-*.js, demo.js): target picker, admission guard card, run status,
   start/cancel, CLI preview, recent runs, share card. Exposed as window.HyperloomBench. Every page keeps its form
   state in a view object updated on input, so a 2 s re-render never loses what the user typed. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const views = {};
  const previews = {};
  const KIND_TITLES = {decode: "Decode throughput", prefill: "Prefill sweep", ab: "A/B exactness", quality: "Quality suite", tools: "Tool calling", demo: "Live demo"};

  function view(page, defaults) {
    if (!views[page]) {
      views[page] = {...defaults};
      try { Object.assign(views[page], JSON.parse(localStorage.getItem(`hyperloom-${page}`) || "{}"), {jobId: null}); } catch (_) { /* storage may be disabled */ }
    }
    return views[page];
  }
  function save(page) {
    const {jobId, cleared, ...kept} = views[page] || {};
    try { localStorage.setItem(`hyperloom-${page}`, JSON.stringify(kept)); } catch (_) {}
  }
  const targetsDoc = () => H().state.docs["bench/targets"];
  const targetRows = () => H().rows(targetsDoc()?.targets);
  const findTarget = key => targetRows().find(t => t.key === key);
  const argumentOf = key => { const [kind, ...rest] = String(key || "").split(":"); return kind === "profile" ? {profile: rest.join(":")} : {target: rest.join(":")}; };
  const guardEndpoint = key => key ? `bench/guard?${new URLSearchParams({target: key})}` : "bench/guard";

  function num(value, unit = "", digits = 2, why = "not reported") {
    if (value == null || !Number.isFinite(Number(value))) return H().unavailable(why);
    const n = Number(value);
    const text = Math.abs(n) >= 1000 ? Math.round(n).toLocaleString() : n.toFixed(Math.abs(n) >= 100 ? 0 : digits);
    return `${H().esc(text)}${unit ? ` ${H().esc(unit)}` : ""}`;
  }
  const pct = (value, why) => value == null ? H().unavailable(why || "not reported") : `${H().esc((value * 100).toFixed(1))} %`;

  function targetPicker(id, key, selected, label = "Target", {allowBlocked = false} = {}) {
    const {esc} = H();
    const rows = targetRows();
    if (!targetsDoc()) return `<p>${H().unavailable("targets loading")}</p>`;
    if (!rows.length) return `<p>${H().unavailable(targetsDoc()?.why?.adhoc || "no running profile and no ad-hoc target")}</p><p class="hint">Start a profile, or add an ad-hoc target on the Benchmark history page.</p>`;
    const options = rows.map(t => `<option value="${esc(t.key)}" ${t.key === selected ? "selected" : ""} ${t.blocker && !allowBlocked ? "disabled" : ""}>${esc(t.kind === "profile" ? `profile ${t.label}` : `${t.label} · ad-hoc`)}${t.blocker ? ` · ${esc(t.blocker)}` : ""}</option>`).join("");
    return `<div class="field"><label for="${esc(id)}">${esc(label)}</label><select class="inp" id="${esc(id)}" data-page-action="bench-field" data-key="${esc(key)}"><option value="">Choose a target</option>${options}</select></div>`;
  }

  function capabilityNotes(target) {
    if (!target) return "";
    const {esc} = H();
    const note = (ok, label, why) => `<li>${ok ? H().statusPill("ok") : H().statusPill("off")} ${esc(label)}${ok ? "" : ` · ${esc(why || "not available")}`}</li>`;
    return `<ul class="bench-caps">${note(target.ids, "token ids", target.ids_why)}</ul>`;
  }

  function guardCard(key) {
    const {esc, card, unavailable, tag} = H();
    const doc = H().state.docs[guardEndpoint(key)];
    if (!doc) return card("Admission", `<p>${unavailable("loading")}</p>`, "contract C11");
    const holds = doc.holds == null ? `<p>${unavailable(doc.why?.holds || "admission state unavailable")}</p>` : doc.holds.length
      ? H().table(["Kind", "Holds", "Label", "Since"], doc.holds.map(h => H().cells([esc(h.kind), h.resources.map(tag).join(" "), esc(h.label || h.id), esc(new Date(h.started * 1000).toLocaleTimeString())])).join(""), "Admission holds")
      : `<p class="muted">No holds: nothing is running on the lab's benchmark resources.</p>`;
    const preview = doc.preview ? (doc.preview.admitted
      ? `<div class="banner info">A run on this target would be admitted now · ${doc.preview.resources ? "holds " + doc.preview.resources.map(tag).join(" ") : esc(doc.why?.preview_resources || "machines unavailable")}</div>`
      : `<div class="banner warn">A run on this target would be refused: ${esc(doc.preview.refusal || doc.why?.preview || "admission state unavailable")}</div>`) : "";
    const rules = `<details><summary>Rules</summary><ul>${(doc.rules || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul></details>`;
    return card("Admission", preview + holds + rules, `${doc.running?.length || 0} benchmark run(s) in this portal · lab cap ${doc.cap}`);
  }
  function guardRefusal(key) {
    const doc = H().state.docs[guardEndpoint(key)];
    if (!doc) return "Waiting for the admission state";
    if (doc.preview && !doc.preview.admitted) return doc.preview.refusal || doc.why?.preview || "admission state unavailable";
    return "";
  }
  function startBlocker(page, key) {
    const v = views[page] || {};
    if (!H().csrf() || H().csrf() === "__HYPERLOOM_CSRF__") return "Launch the portal through hyperloom ui to enable actions";
    if (!H().state.fresh) return "Waiting for authoritative state";
    if (running(v)) return "A run started from this page is still running";
    if (!key) return "Choose a target";
    const target = findTarget(key);
    if (!target) return "The chosen target is no longer offered";
    if (target.blocker) return target.blocker;
    return guardRefusal(key);
  }
  const running = v => !!v.jobId && H().rows(H().state.lastJobs).some(j => (j.id || j.job) === v.jobId && j.state === "running");

  async function post(action, args) {
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify(args), signal: AbortSignal.timeout(30000)});
  }
  async function start(page, kind, args) {
    const v = views[page];
    try {
      const job = await post(`bench/${kind}`, args);
      H().own(job.id || job.job);
      v.jobId = job.id || job.job;
      v.runId = job.arguments?.run_id || v.runId;
      save(page);
      H().announce(`${KIND_TITLES[kind]} started (run ${v.runId}).`);
    } catch (error) { H().announce(`${KIND_TITLES[kind]} refused: ${error.message}`); }
    H().refresh();
  }
  async function cancel(page) {
    const v = views[page];
    if (!v?.jobId) return;
    try { await post("cancel", {job: v.jobId}); H().announce("Cancel requested; open streams are closed."); }
    catch (error) { H().announce(`Cancel failed: ${error.message}`); }
    H().refresh();
  }

  function preview(kind, args) {
    const key = kind + JSON.stringify(args);
    const entry = previews[kind] ||= {key: "", command: "", pending: false};
    if (entry.key !== key && !entry.pending && H().csrf() && H().csrf() !== "__HYPERLOOM_CSRF__") {
      entry.pending = true;
      post("bench/preview", {kind, arguments: args}).then(r => { entry.command = r.command; entry.shell = r.shell; entry.key = key; })
        .catch(e => { entry.command = `unavailable · ${e.message}`; entry.key = key; })
        .finally(() => { entry.pending = false; H().render(); });
    }
    const command = entry.key === key ? entry.command : entry.command ? `${entry.command} (updating)` : "";
    return `<div class="cli bench-cli"><span class="hint">Same run from a terminal${entry.shell ? ` (${entry.shell === "powershell" ? "PowerShell" : "POSIX shell"} quoting)` : ""}</span> <code>${H().esc(command || "hyperloom bench " + kind + " …")}</code></div>`;
  }

  // The run a page shows: the one it started or opened, else the newest stored run of its kind.
  function shownRun(v, kind) {
    if (v.runId && /unknown run/.test(H().state.errors[`bench/runs/${v.runId}`] || "")) { delete H().state.errors[`bench/runs/${v.runId}`]; v.runId = null; }
    return v.runId || H().state.docs[`bench/runs?kind=${kind}&limit=20`]?.runs?.[0]?.id || null;
  }
  // The shell remembers when every document was last fetched; a bench document this page no longer polls (another
  // target, another run) would otherwise mark the page stale forever. Only bench documents are pruned.
  function track(list) {
    const keep = new Set(list), state = H().state;
    for (const store of [state.fetched, state.errors]) for (const key of Object.keys(store)) if (key.startsWith("bench/") && !keep.has(key)) delete store[key];
    return list;
  }
  // The first runnable target is chosen when the page has none yet (nothing runs until Start).
  function autoTarget(v, key = "target", skip = "") {
    if (!v[key] && targetsDoc()) {
      const runnable = targetRows().filter(t => !t.blocker);
      v[key] = (runnable.find(t => t.key !== skip) || runnable[0])?.key || "";
    }
    return v[key];
  }
  function ready(v, kind) {
    const docs = H().state.docs;
    if (!docs["bench/targets"] || !docs[`bench/runs?kind=${kind}&limit=20`]) return false;
    const id = shownRun(v, kind);
    return !id || !!docs[`bench/runs/${id}`] || !!H().state.errors[`bench/runs/${id}`];
  }
  function runEndpoint(v, kind) { const id = shownRun(v, kind); return id ? `bench/runs/${id}` : null; }
  function runOf(v, kind) { const id = shownRun(v, kind); return (id && H().state.docs[`bench/runs/${id}`]?.run) || null; }

  function statusCard(page, kind, run) {
    const {esc, card, statusPill, button} = H();
    const v = views[page];
    const id = shownRun(v, kind);
    if (!id) return card("Run", `<p class="muted">No run stored yet. Start one above.</p>`);
    if (!run) return card("Run", `<p>${H().unavailable(H().state.errors[`bench/runs/${id}`] || "loading the run")}</p>`);
    const s = run.summary || {};
    const live = run.state === "running";
    const cancelButton = live && running(v) ? button("Cancel run", "", {style: "dan", extra: `data-page-action="bench-cancel" data-page="${esc(page)}"`}) : "";
    const facts = [["State", statusPill(run.state)], ["Target", esc(run.target_label || "")], ["Model", esc(run.model || "") || H().unavailable("not probed yet")],
      ["Started", esc(new Date(run.started * 1000).toLocaleString())], ["Generated tokens", `${num(s.tokens_used, "", 0)} of ${num(s.token_budget, "", 0)} budget`],
      ["Prompt tokens", num(s.prompt_tokens_used, "", 0, "not reported")], ["Requests", num(s.requests, "", 0)]];
    return card(`Run ${run.id}`, `<dl class="kv">${facts.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${val}</dd>`).join("")}</dl>${live ? `<p class="hint">${esc(s.progress || "running")}</p>` : ""}${s.reason ? `<div class="banner ${run.state === "succeeded" ? "info" : "warn"}">${esc(s.reason)}</div>` : ""}<div class="row">${cancelButton}<a class="btn" href="#/bench-history" data-page-action="bench-open-history" data-run="${esc(run.id)}">Open in history</a></div>`,
      `${KIND_TITLES[kind]} · ${live ? "live, refreshed every 2 s" : "stored"}`);
  }

  function recentRuns(page, kind) {
    const {esc, card, statusPill, table, cells} = H();
    const doc = H().state.docs[`bench/runs?kind=${kind}&limit=20`];
    if (!doc) return card("Recent runs", `<p>${H().unavailable("loading")}</p>`);
    if (doc.runs == null) return card("Recent runs", `<p>${H().unavailable(doc.why?.runs || "history unavailable")}</p>`);
    if (!doc.runs.length) return card("Recent runs", `<p class="muted">No ${esc(KIND_TITLES[kind].toLowerCase())} runs stored yet.</p>`);
    const shown = shownRun(views[page], kind);
    return card("Recent runs", table(["When", "Target", "Model", "State", ""], doc.runs.map(r => cells([esc(new Date(r.started * 1000).toLocaleString()), esc(r.target_label || ""), esc(r.model || ""), statusPill(r.state),
      `<button type="button" class="btn sm" data-page-action="bench-open" data-page="${esc(page)}" data-run="${esc(r.id)}" ${r.id === shown ? 'aria-pressed="true"' : ""}>${r.id === shown ? "Shown" : "Open"}</button>`])).join(""), "Recent runs"), "kept per kind; quality keeps 30 per target, the demo 20");
  }

  function budgetField(page, v) {
    return `<div class="field"><label for="${page}-budget">Generated-token budget</label><input class="inp" id="${page}-budget" type="number" min="1000" max="16000000" step="1000" value="${H().esc(v.token_budget)}" data-page-action="bench-field" data-key="token_budget" data-type="int"><span class="hint">Each request reserves its max tokens; the run stops before it would exceed the budget.</span></div>`;
  }
  function controlsFor(page, v, target) {
    const {esc} = H();
    const sampling = target?.sampling
      ? `<label class="check"><input type="checkbox" data-page-action="bench-field" data-key="sample" data-type="bool" ${v.sample ? "checked" : ""}><span>Sample (temperature 0.7, seed 1)</span></label>`
      : `<span class="disabled-control"><label class="check"><input type="checkbox" disabled><span>Sample</span></label><span class="hint">${esc(target?.sampling_why || "choose a target")}</span></span>`;
    const thinking = target?.thinking
      ? `<div class="field"><label for="${page}-thinking">Thinking</label><select class="inp" id="${page}-thinking" data-page-action="bench-field" data-key="thinking"><option value="">endpoint default</option><option value="on" ${v.thinking === "on" ? "selected" : ""}>on</option><option value="off" ${v.thinking === "off" ? "selected" : ""}>off</option></select></div>`
      : `<span class="disabled-control"><label class="check"><input type="checkbox" disabled><span>Thinking switch</span></label><span class="hint">${esc(target?.thinking_why || "choose a target")}</span></span>`;
    return `<div class="bench-switches">${sampling}${thinking}<label class="check"><input type="checkbox" data-page-action="bench-field" data-key="trace" data-type="bool" ${v.trace ? "checked" : ""}><span>Keep debug traces (prompt hashes, request ids, event times)</span></label></div>`;
  }
  function commonArgs(v, target) {
    const args = {target: argumentOf(v.target), token_budget: Number(v.token_budget) || 400000};
    if (v.trace) args.trace = true;
    if (target?.sampling && v.sample) args.sampling = {temperature: 0.7, top_p: 1, seed: 1};
    if (target?.thinking && v.thinking) args.thinking = v.thinking === "on";
    return args;
  }
  function startButton(page, kind, key) {
    const blocker = startBlocker(page, key);
    return H().button(`Start ${KIND_TITLES[kind].toLowerCase()}`, "", {style: "pri", disabled: blocker, extra: `data-page-action="bench-start" data-page="${H().esc(page)}"`});
  }
  function bars(rows, {label, value, unit, title}) {
    const {esc} = H();
    const data = rows.filter(r => Number.isFinite(r[value]));
    if (!data.length) return `<p>${H().unavailable("no completed level yet")}</p>`;
    const max = Math.max(...data.map(r => r[value])) || 1;
    return `<figure class="bench-bars" aria-label="${esc(title)}"><figcaption>${esc(title)}</figcaption>${rows.map(r => `<div class="bench-bar"><span class="bench-bar-l">${esc(r[label])}</span><span class="bench-bar-t"><i style="width:${Number.isFinite(r[value]) ? Math.max(1, r[value] / max * 100).toFixed(1) : 0}%"></i></span><span class="bench-bar-v">${Number.isFinite(r[value]) ? num(r[value], unit) : H().unavailable("not measured")}</span></div>`).join("")}</figure>`;
  }

  // Share card: drawn in a canvas from the allow-listed share document only.
  function shareCanvas(share, text) {
    const canvas = document.createElement("canvas");
    canvas.width = 1200; canvas.height = 630;
    const g = canvas.getContext("2d");
    g.fillStyle = "#0f1720"; g.fillRect(0, 0, 1200, 630);
    g.fillStyle = "#38bdf8"; g.fillRect(0, 0, 1200, 10);
    g.fillStyle = "#f8fafc"; g.font = "600 44px system-ui, sans-serif"; g.fillText(`${KIND_TITLES[share.kind] || share.kind}`, 60, 100);
    g.fillStyle = "#94a3b8"; g.font = "26px system-ui, sans-serif"; g.fillText(`${share.target} · ${share.state} · ${new Date(share.started * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC`, 60, 148);
    g.fillStyle = "#e2e8f0"; g.font = "24px ui-monospace, monospace";
    const lines = String(text).split("\n").slice(2, 18);
    lines.forEach((line, i) => g.fillText(line.length > 80 ? line.slice(0, 79) + "…" : line, 60, 210 + i * 34));
    g.fillStyle = "#64748b"; g.font = "20px system-ui, sans-serif"; g.fillText("hyperloom bench · share summary (no prompts, replies or addresses)", 60, 600);
    return canvas;
  }
  function downloadPng(share, text) {
    shareCanvas(share, text).toBlob(blob => {
      if (!blob) { H().announce("PNG could not be drawn in this browser"); return; }
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `hyperloom-bench-${share.kind}-${share.started}.png`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, "image/png");
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); H().announce("Share summary copied as text."); }
    catch (_) { H().announce("Clipboard unavailable here; select the text in the summary box instead."); }
  }

  // Delegated controls shared by every bench page: field updates, start, cancel, open.
  const handlers = {};
  function onStart(page, fn) { handlers[page] = fn; }
  function fieldEvent(event) {
    const control = event.target.closest?.('[data-page-action="bench-field"]');
    if (!control) return;
    const page = control.closest("[data-bench-page]")?.dataset.benchPage;
    if (!page || !views[page]) return;
    const key = control.dataset.key;
    const type = control.dataset.type;
    let value = type === "bool" ? control.checked : control.value;
    if (type === "int") value = value === "" ? "" : Number(value);
    if (type === "set") {
      const set = new Set(views[page][key] || []);
      if (control.checked) set.add(control.value); else set.delete(control.value);
      value = [...set];
    }
    views[page][key] = value;
    save(page);
    if (event.type === "change") H().refresh();
  }
  document.addEventListener("input", fieldEvent);
  document.addEventListener("change", fieldEvent);
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction, page = control.dataset.page;
    if (action === "bench-start" && handlers[page]) { handlers[page](); return; }
    if (action === "bench-cancel") { cancel(page); return; }
    if (action === "bench-open" && views[page]) { views[page].runId = control.dataset.run; views[page].jobId = null; save(page); H().refresh(); return; }
    if (action === "bench-open-history") { const hv = view("bench-history", {}); hv.selected = control.dataset.run; save("bench-history"); }
  });

  window.HyperloomBench = {view, save, autoTarget, ready, track, targetRows, findTarget, argumentOf, guardEndpoint, targetPicker, capabilityNotes, guardCard,
    startBlocker, start, cancel, preview, runEndpoint, runOf, statusCard, recentRuns, budgetField, controlsFor, commonArgs,
    startButton, bars, num, pct, onStart, post, shareCanvas, downloadPng, copyText, running, KIND_TITLES};
})();
