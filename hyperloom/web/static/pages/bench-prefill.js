/* Prefill sweep (F25): cold trials (nonce first, cached tokens must be 0), TTFT as TTFT, decode-engine computed
   prefill throughput only from engine counts and prefill time, the route that served each size. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-prefill";
  const v = () => B().view(PAGE, {target: "", sizes: "", max_context: "", trials: 2, max_tokens: 8, token_budget: 400000, trace: false, runId: null});

  function sizes(text) { return String(text || "").split(/[ ,]+/).filter(Boolean).map(Number).filter(Number.isFinite); }
  function args() {
    const view = v(), target = B().findTarget(view.target);
    const result = {...B().commonArgs(view, target), trials: Number(view.trials) || 2, max_tokens: Number(view.max_tokens) || 8};
    delete result.sampling; delete result.thinking;
    if (sizes(view.sizes).length) result.sizes = sizes(view.sizes);
    if (view.max_context) result.max_context = Number(view.max_context);
    return result;
  }
  function form() {
    const {esc} = H(), view = v(), target = B().findTarget(view.target);
    const context = target?.context_tokens ? `Context limit declared for this target: ${target.context_tokens.toLocaleString()} tokens.` : "The context limit is read from the endpoint when the run starts; give one here when it does not report it.";
    return H().card("Run a prefill sweep", `<div class="bench-fields">${B().targetPicker("prefill-target", "target", view.target)}
      <div class="field"><label for="prefill-sizes">Sizes (tokens, optional)</label><input class="inp" id="prefill-sizes" placeholder="default: 1024, 2048, … up to the limit" value="${esc(view.sizes)}" data-page-action="bench-field" data-key="sizes"></div>
      <div class="field"><label for="prefill-context">Context limit (declared, optional)</label><input class="inp" id="prefill-context" type="number" min="2048" value="${esc(view.max_context)}" data-page-action="bench-field" data-key="max_context" data-type="int"></div>
      <div class="field"><label for="prefill-trials">Cold trials per size</label><input class="inp" id="prefill-trials" type="number" min="1" max="5" value="${esc(view.trials)}" data-page-action="bench-field" data-key="trials" data-type="int"></div>
      <div class="field"><label for="prefill-max">Reply tokens</label><input class="inp" id="prefill-max" type="number" min="1" max="64" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>${B().budgetField("prefill", view)}</div>
      <p class="hint">${esc(context)} Each trial starts with a unique nonce before any reusable text; a trial counts only when the engine reports 0 cached tokens. Timeouts grow with the size.</p>
      <label class="check"><input type="checkbox" data-page-action="bench-field" data-key="trace" data-type="bool" ${view.trace ? "checked" : ""}><span>Keep debug traces</span></label>
      <div class="row">${B().startButton(PAGE, "prefill", view.target)}</div>${B().preview("prefill", args())}`);
  }
  function results(run) {
    const {esc, card, table, cells} = H(), num = B().num;
    if (!run) return "";
    const rows = run.summary?.sizes || [];
    if (!rows.length) return card("Sizes", `<p>${H().unavailable(run.state === "running" ? (run.summary?.progress || "calibrating") : "no size measured")}</p>`);
    const body = rows.map(s => cells([num(s.nominal_tokens, "", 0), num(s.measured_prompt_tokens, "", 0, s.why?.skipped || "no counted trial"),
      `${esc(s.counted)} / ${esc(s.trials)}`, num(s.ttft_p50_s, "s", 3, s.why?.skipped || s.why?.ttft_p50_s), num(s.throughput_p50, "tok/s", 0, s.why?.skipped || s.why?.throughput_p50),
      esc((s.routes || []).join("; ") || "—"), esc([s.why?.skipped, ...(s.excluded || [])].filter(Boolean).join("; ") || "—")])).join("");
    const chartRows = rows.filter(s => s.ttft_p50_s != null).map(s => ({label: `${(s.nominal_tokens / 1024).toFixed(0)}k`, value: s.ttft_p50_s}));
    return `${card("TTFT by size (client)", B().bars(chartRows, {label: "label", value: "value", unit: "s", title: "TTFT p50 per nominal size (measured, client)"}))}${card("Sizes", table(["Nominal", "Measured prompt", "Counted", "TTFT p50", "Computed prefill p50", "Route", "Excluded"], body, "Prefill sizes") + `<p class="hint">Computed prefill = (prompt − cached − imported) / engine prefill time: decode-engine computed prefill, engine counts. Characters per token from calibration: ${esc(run.summary.chars_per_token ?? "unavailable")}. Context limit ${esc(run.summary.context_limit ?? "unavailable")} (${esc(run.summary.context_basis || "")}).</p>`)}`;
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Prefill sweep", group: "Benchmarks", order: 11,
    icon: '<path d="M3 16h3V9H3zM8.5 16h3V6h-3zM14 16h3V3h-3z"/>',
    endpoints: () => B().track(["bench/targets", B().guardEndpoint(B().autoTarget(v())), "bench/runs?kind=prefill&limit=20", ...(B().runEndpoint(v(), "prefill") ? [B().runEndpoint(v(), "prefill")] : [])]),
    ready: () => B().ready(v(), "prefill"),
    render() {
      const run = B().runOf(v(), "prefill");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().target)}</div>${B().statusCard(PAGE, "prefill", run)}${results(run)}${B().recentRuns(PAGE, "prefill")}</div>`;
    },
  };
  B().onStart(PAGE, () => B().start(PAGE, "prefill", args()));
})();
