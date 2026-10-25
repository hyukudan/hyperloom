/* Decode throughput benchmark (F24): concurrency levels, prompt set, max tokens; per level TTFT, streams,
   aggregate and per-stream rates, each labelled with its observer and basis. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-decode", LEVELS = [1, 2, 4, 8, 16, 32], SETS = ["structured", "prose", "code", "json"];
  const v = () => B().view(PAGE, {target: "", prompt_set: "prose", levels: ["1", "2", "4", "8"], max_tokens: 256, token_budget: 400000, sample: false, thinking: "", trace: false, runId: null});

  function args() {
    const view = v(), target = B().findTarget(view.target);
    return {...B().commonArgs(view, target), prompt_set: view.prompt_set, levels: (view.levels || []).map(Number).sort((a, b) => a - b), max_tokens: Number(view.max_tokens) || 256};
  }
  function form() {
    const {esc} = H(), view = v(), target = B().findTarget(view.target);
    const levels = `<fieldset><legend>Concurrency levels</legend><div class="row">${LEVELS.map(n => `<label class="check"><input type="checkbox" value="${n}" data-page-action="bench-field" data-key="levels" data-type="set" ${(view.levels || []).includes(String(n)) ? "checked" : ""}><span>${n}</span></label>`).join("")}</div></fieldset>`;
    const set = `<div class="field"><label for="decode-set">Prompt type</label><select class="inp" id="decode-set" data-page-action="bench-field" data-key="prompt_set">${SETS.map(s => `<option value="${s}" ${s === view.prompt_set ? "selected" : ""}>${s}</option>`).join("")}</select></div>`;
    const max = `<div class="field"><label for="decode-max">Max tokens per stream</label><input class="inp" id="decode-max" type="number" min="16" max="4096" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>`;
    const planned = (view.levels || []).reduce((sum, n) => sum + Number(n), 0) * (Number(view.max_tokens) || 0);
    return H().card("Run a decode benchmark", `<div class="bench-fields">${B().targetPicker("decode-target", "target", view.target)}${set}${max}${B().budgetField("decode", view)}</div>${levels}${B().controlsFor("decode", view, target)}${B().capabilityNotes(target)}<p class="hint">Plan: ${esc((view.levels || []).length)} level(s), up to ${esc(planned.toLocaleString())} generated tokens. Greedy decoding unless sampling is available and chosen.</p><div class="row">${B().startButton(PAGE, "decode", view.target)}</div>${B().preview("decode", args())}`);
  }
  function results(run) {
    const {esc, card, table, cells} = H(), num = B().num;
    const levels = run?.summary?.levels || [];
    if (!run) return "";
    if (!levels.length) return card("Levels", `<p>${H().unavailable(run.state === "running" ? "the first level is running" : "no level completed")}</p>`);
    const body = levels.map(l => cells([esc(l.level), `${esc(l.completed)} / ${esc(l.streams)}${l.failed ? ` · ${esc(l.failed)} failed` : ""}`,
      num(l.ttft_p50_s, "s", 3, l.why?.ttft_p50_s), num(l.ttft_p95_s, "s", 3, l.why?.ttft_p95_s), num(l.aggregate_rate, "tok/s", 1, l.why?.aggregate_rate),
      `${num(l.per_stream_rate_p50, "tok/s", 1, l.why?.per_stream_rate)}<span class="hint"> ${l.per_stream_rate_min != null ? `${num(l.per_stream_rate_min, "", 1)}–${num(l.per_stream_rate_max, "", 1)}` : ""}</span>`,
      num(l.per_stream_e2e_rate_p50, "tok/s", 1, "no usage"), num(l.engine_rate_p50, "tok/s", 1, l.why?.engine_rate_p50), esc((l.replicas || []).join(", ") || "—")])).join("");
    const observers = run.summary.observers || {};
    return `${card("Aggregate completed-token rate by level", B().bars(levels, {label: "level", value: "aggregate_rate", unit: "tok/s", title: "aggregate tok/s per concurrency level (measured, client window)"}))}${card("Levels", table(["Level", "Streams", "TTFT p50", "TTFT p95", "Aggregate", "Per stream p50 (min–max)", "Per stream end-to-end p50", "Engine p50", "Replica"], body, "Decode levels") + `<ul class="hint bench-notes">${Object.entries(observers).map(([k, text]) => `<li><b>${esc(k.replaceAll("_", " "))}</b>: ${esc(text)}</li>`).join("")}</ul>`, `${esc(run.summary.prompt_set || "")} prompts · max ${esc(run.summary.max_tokens || "")} tokens · ${esc(run.summary.sampling || "greedy")}`)}${levels.some(l => l.errors?.length) ? card("Errors", `<ul>${levels.flatMap(l => (l.errors || []).map(e => `<li>level ${esc(l.level)}: ${esc(e)}</li>`)).join("")}</ul>`) : ""}`;
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Decode throughput", group: "Benchmarks", order: 10,
    icon: '<path d="M3 15l3-4 3 2 4-6 4 3"/><path d="M3 18h14"/>',
    endpoints: () => B().track(["bench/targets", B().guardEndpoint(B().autoTarget(v())), "bench/runs?kind=decode&limit=20", ...(B().runEndpoint(v(), "decode") ? [B().runEndpoint(v(), "decode")] : [])]),
    ready: () => B().ready(v(), "decode"),
    render() {
      const run = B().runOf(v(), "decode");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().target)}</div>${B().statusCard(PAGE, "decode", run)}${results(run)}${B().recentRuns(PAGE, "decode")}</div>`;
    },
  };
  B().onStart(PAGE, () => B().start(PAGE, "decode", args()));
})();
