/* Live streaming demo (F31): up to 32 parallel prompts in terminal-style tiles, events/s while streaming (not
   tokens), the end-to-end tok/s when usage arrives, peak and an aggregate sparkline, reasoning folded; admitted
   like a benchmark; the last 20 sessions per target reopen read-only. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "demo", SETS = ["structured", "prose", "code", "json"], LIMIT = 56 * 1024;
  const v = () => B().view(PAGE, {target: "", source: "set", prompt_set: "prose", count: 8, texts: "", max_tokens: 512, token_budget: 400000, sample: false, thinking: "", trace: false, runId: null});

  const texts = view => String(view.texts || "").split(/\n-{3,}\n/).map(t => t.trim()).filter(Boolean);
  function args() {
    const view = v(), target = B().findTarget(view.target);
    const result = {...B().commonArgs(view, target), max_tokens: Number(view.max_tokens) || 512};
    if (view.source === "texts") result.texts = texts(view);
    else { result.prompt_set = view.prompt_set; result.count = Number(view.count) || 8; }
    return result;
  }
  function blocker() {
    const view = v(), size = new TextEncoder().encode(JSON.stringify(args())).length;
    if (view.source === "texts") {
      const list = texts(view);
      if (!list.length) return "Write at least one prompt";
      if (list.length > 32) return "At most 32 prompts";
      if (list.some(t => t.length > 4000)) return "Each prompt has at most 4000 characters";
    }
    if (size > LIMIT) return "prompts exceed 56 KiB in total";
    return B().startBlocker(PAGE, view.target);
  }
  function form() {
    const {esc} = H(), view = v(), target = B().findTarget(view.target);
    const source = `<div class="field"><label for="demo-source">Prompts</label><select class="inp" id="demo-source" data-page-action="bench-field" data-key="source"><option value="set" ${view.source === "set" ? "selected" : ""}>from a prompt set</option><option value="texts" ${view.source === "texts" ? "selected" : ""}>my own prompts</option></select></div>`;
    const chosen = view.source === "texts"
      ? `<div class="field bench-wide"><label for="demo-texts">Prompts, separated by a line with ---</label><textarea class="inp" id="demo-texts" rows="5" data-page-action="bench-field" data-key="texts">${esc(view.texts)}</textarea><span class="hint">${esc(texts(view).length)} prompt(s) · ${esc(new TextEncoder().encode(JSON.stringify(args())).length.toLocaleString())} of ${LIMIT.toLocaleString()} bytes</span></div>`
      : `<div class="field"><label for="demo-set">Prompt set</label><select class="inp" id="demo-set" data-page-action="bench-field" data-key="prompt_set">${SETS.map(s => `<option value="${s}" ${s === view.prompt_set ? "selected" : ""}>${s}</option>`).join("")}</select></div><div class="field"><label for="demo-count">Parallel prompts</label><input class="inp" id="demo-count" type="number" min="1" max="32" value="${esc(view.count)}" data-page-action="bench-field" data-key="count" data-type="int"></div>`;
    return H().card("Start a live demo", `<div class="bench-fields">${B().targetPicker("demo-target", "target", view.target)}${source}${chosen}<div class="field"><label for="demo-max">Max tokens per prompt</label><input class="inp" id="demo-max" type="number" min="16" max="2048" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>${B().budgetField("demo", view)}</div>${B().controlsFor("demo", view, target)}
      <div class="row">${H().button("Start demo", "", {style: "pri", disabled: blocker(), extra: `data-page-action="bench-start" data-page="${PAGE}"`})}</div>${B().preview("demo", args())}`);
  }
  function tiles(run) {
    const {esc, card, statusPill} = H(), num = B().num;
    if (!run) return "";
    const s = run.summary || {}, d = run.detail || {};
    const list = d.tiles || [];
    const samples = (d.aggregate || []).map(p => p[1]);
    const head = `<div class="row between"><span>${esc(s.done ?? 0)} of ${esc(s.tiles ?? list.length)} done · ${esc(s.streaming ?? 0)} streaming · peak ${num(s.aggregate_events_per_s_peak, "events/s", 0, "no samples")}</span><span class="hint">${run.state === "running" ? "live" : "read-only session"}</span></div>${samples.length > 1 ? `<div class="bench-spark" aria-label="aggregate stream events per second">${H().spark(samples, "aggregate events/s")}</div><p class="hint">Aggregate: ${esc(s.labels?.events_per_s || "stream events per second")}, sampled every 0.5 s.</p>` : ""}`;
    const body = list.map(t => {
      const rate = t.e2e_rate != null ? `${num(t.e2e_rate, "tok/s", 1)} <span class="hint">end-to-end, measured</span>` : t.events_per_s != null ? `${num(t.events_per_s, "events/s", 0)} <span class="hint">not tokens</span>` : H().unavailable(t.error || "waiting");
      return `<section class="bench-tile" aria-label="Prompt ${esc(t.index + 1)}"><div class="row between"><b>#${esc(t.index + 1)}</b>${statusPill(t.state)}</div><p class="hint bench-prompt">${esc(t.prompt)}</p><div class="bench-rate">${rate} · peak ${num(t.peak_events_per_s, "events/s", 0, "no events")}${t.client_rate != null ? ` · decode ≈ ${num(t.client_rate, "tok/s", 1)} (estimate)` : ""}</div>${t.reasoning ? `<details><summary>Reasoning</summary><pre class="bench-term bench-reasoning">${esc(t.reasoning)}</pre></details>` : ""}<pre class="bench-term" tabindex="0">${esc(t.text) || "&nbsp;"}</pre></section>`;
    }).join("");
    return card("Tiles", head + `<div class="bench-tiles">${body || `<p>${H().unavailable("starting")}</p>`}</div>`, `${esc(run.target_label || "")} · ${esc(run.model || "")}`);
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Live demo", group: "Benchmarks", order: 16,
    icon: '<rect x="3" y="4" width="14" height="12" rx="1.5"/><path d="M6 8l2.5 2L6 12M10 12h4"/>',
    endpoints() {
      const view = v(), list = ["bench/targets", B().guardEndpoint(B().autoTarget(view)), "bench/runs?kind=demo&limit=20"];
      if (B().runEndpoint(view, "demo")) list.push(B().runEndpoint(view, "demo"));
      return B().track(list);
    },
    ready: () => B().ready(v(), "demo"),
    render() {
      const run = B().runOf(v(), "demo");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().target)}</div>${B().statusCard(PAGE, "demo", run)}${tiles(run)}${B().recentRuns(PAGE, "demo")}</div>`;
    },
    after(main) { main.querySelectorAll(".bench-term").forEach(el => { el.scrollTop = el.scrollHeight; }); },
  };
  B().onStart(PAGE, () => { if (!blocker()) B().start(PAGE, "demo", args()); });
})();
