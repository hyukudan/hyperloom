/* A/B runner with exactness receipts (F26, C12): two predeployed arms, the same seeded greedy corpus interleaved
   ABA x3; per case equal or the first divergent token; the claim only between arms with the same model and
   tokenizer; the receipt downloads as JSON and verifies offline with `hyperloom bench verify-receipt`. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-ab", SETS = ["structured", "prose", "code", "json"];
  const v = () => B().view(PAGE, {a: "", b: "", a_tokenizer: "", b_tokenizer: "", prompt_set: "prose", cases: 8, max_tokens: 64, seed: 1, token_budget: 400000, trace: false, runId: null});

  function args() {
    const view = v();
    const result = {arms: {a: B().argumentOf(view.a), b: B().argumentOf(view.b)}, prompt_set: view.prompt_set, cases: Number(view.cases) || 8,
      max_tokens: Number(view.max_tokens) || 64, seed: Number(view.seed) || 0, token_budget: Number(view.token_budget) || 400000};
    const tokenizers = {};
    if (view.a_tokenizer) tokenizers.a = view.a_tokenizer.trim();
    if (view.b_tokenizer) tokenizers.b = view.b_tokenizer.trim();
    if (Object.keys(tokenizers).length) result.tokenizers = tokenizers;
    if (view.trace) result.trace = true;
    return result;
  }
  function blocker() {
    const view = v();
    if (!view.a || !view.b) return "Choose both arms";
    return B().startBlocker(PAGE, view.a) || B().startBlocker(PAGE, view.b);
  }
  function form() {
    const {esc} = H(), view = v();
    const tokenizer = arm => `<div class="field"><label for="ab-tok-${arm}">Arm ${arm.toUpperCase()} tokenizer id (declared, optional)</label><input class="inp" id="ab-tok-${arm}" value="${esc(view[arm + "_tokenizer"])}" placeholder="used only when the endpoint reports none" data-page-action="bench-field" data-key="${arm}_tokenizer"></div>`;
    const same = view.a && view.a === view.b ? `<div class="banner info">Both arms are the same target: an A/A control (determinism and timing spread).</div>` : "";
    const ids = [view.a, view.b].map(k => B().findTarget(k)).filter(t => t && !t.ids);
    const stop = blocker();
    return H().card("Run an A/B comparison", `<div class="bench-fields">${B().targetPicker("ab-a", "a", view.a, "Arm A")}${B().targetPicker("ab-b", "b", view.b, "Arm B")}${tokenizer("a")}${tokenizer("b")}
      <div class="field"><label for="ab-set">Prompt set</label><select class="inp" id="ab-set" data-page-action="bench-field" data-key="prompt_set">${SETS.map(s => `<option value="${s}" ${s === view.prompt_set ? "selected" : ""}>${s}</option>`).join("")}</select></div>
      <div class="field"><label for="ab-cases">Cases</label><input class="inp" id="ab-cases" type="number" min="1" max="64" value="${esc(view.cases)}" data-page-action="bench-field" data-key="cases" data-type="int"></div>
      <div class="field"><label for="ab-max">Max tokens per case</label><input class="inp" id="ab-max" type="number" min="8" max="1024" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>
      <div class="field"><label for="ab-seed">Corpus seed</label><input class="inp" id="ab-seed" type="number" min="0" value="${esc(view.seed)}" data-page-action="bench-field" data-key="seed" data-type="int"></div>${B().budgetField("ab", view)}</div>${same}
      ${ids.map(t => `<div class="banner warn">${esc(t.label)}: ${esc(t.ids_why)}; its arm cannot support an identity claim.</div>`).join("")}
      <p class="hint">Greedy only. Order: A, B, A three times over the whole corpus (A six passes, B three). The runner never deploys or switches anything: both arms must already be serving.</p>
      <label class="check"><input type="checkbox" data-page-action="bench-field" data-key="trace" data-type="bool" ${view.trace ? "checked" : ""}><span>Keep debug traces</span></label>
      <div class="row">${H().button("Start A/B run", "", {style: "pri", disabled: stop, extra: `data-page-action="bench-start" data-page="${PAGE}"`})}</div>${B().preview("ab", args())}`);
  }
  function results(run) {
    const {esc, card, table, cells, statusPill, button} = H(), num = B().num;
    if (!run) return "";
    const s = run.summary || {}, receipt = run.detail?.receipt;
    if (!receipt) return card("Receipt", `<p>${H().unavailable(run.state === "running" ? `trial ${s.trials_done || 0} of ${9 * (s.cases || 0) || "?"}` : "the run did not complete; no receipt")}</p>`);
    const claimStyle = /^token-identical/.test(s.claim) ? (s.identical === s.cases ? "info" : "warn") : "warn";
    const arms = ["a", "b"].map(arm => { const a = receipt.arms[arm] || {}; return cells([esc(arm.toUpperCase()), esc(a.label || ""), esc(a.model ?? "(hex)"), `${esc(a.tokenizer_id ?? (a.vocab_hex ? "(hex)" : "unavailable"))} <span class="hint">${esc(a.tokenizer_source || "")}</span>`,
      esc((a.binary_sha256 || []).map(x => x.slice(0, 12)).join(", ") || "unavailable"), esc(a.recipe_digest ? a.recipe_digest.slice(0, 12) : "unavailable"), esc(a.router_policy || "unavailable")]); }).join("");
    const comparison = receipt.comparison.map(c => cells([esc(c.case + 1), c.result === "equal" ? statusPill("ok") + " equal" : c.result === "diverged" ? `${statusPill("mismatch")} first divergent token ${esc(c.first_divergence)}` : H().unavailable(c.why), esc(c.length ?? (c.lengths || []).join(" / "))])).join("");
    const speed = receipt.speed.map(r => cells([esc(r.round + 1), esc(r.matched_cases.length), ...r.rates.map(x => num(x, "tok/s", 1, "no matched case")), r.delta_b_vs_a == null ? H().unavailable(r.why) : `${esc((r.delta_b_vs_a * 100).toFixed(2))} %`])).join("");
    return `<div class="banner ${claimStyle}"><b>${esc(s.claim)}</b> · repeats deterministic: A ${esc(s.deterministic?.a)}, B ${esc(s.deterministic?.b)}</div>
      ${card("Arms", table(["Arm", "Target", "Model", "Tokenizer", "Build", "Recipe", "Router policy"], arms, "A/B arms"))}
      <div class="grid g2">${card("Per case (first pass of each arm)", table(["Case", "Result", "Length"], comparison, "A/B cases"))}${card("Speed (end-to-end, client)", table(["Round", "Matched", "A", "B", "A", "B vs A"], speed, "A/B speed") + `<p class="hint">Median ${s.speed_delta_median == null ? "unavailable" : esc((s.speed_delta_median * 100).toFixed(2)) + " %"} · range ${s.speed_delta_range ? s.speed_delta_range.map(x => (x * 100).toFixed(2) + " %").join(" … ") : "unavailable"}. Rate = completed tokens / time from request write to last output, over the cases that succeeded in all three passes of the round.</p>`)}</div>
      ${card("Receipt", `<p>${esc(receipt.schema)} · corpus ${esc(receipt.corpus.corpus_digest.slice(0, 16))} · ${esc(receipt.trials.length)} trials</p><div class="row">${button("Download receipt (JSON)", "", {extra: `data-page-action="bench-ab-receipt" data-run="${esc(run.id)}"`})}</div><div class="cli"><span class="hint">Verify offline</span> <code>hyperloom bench verify-receipt hyperloom-ab-${esc(run.id)}.json</code></div>`)}`;
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "A/B exactness", group: "Benchmarks", order: 12,
    icon: '<path d="M4 4h5v12H4zM11 4h5v12h-5z"/><path d="M6.5 8v4M13.5 8v4"/>',
    endpoints() {
      const view = v(), a = B().autoTarget(view, "a"), b = B().autoTarget(view, "b", a);
      return B().track(["bench/targets", B().guardEndpoint(a), ...(b && b !== a ? [B().guardEndpoint(b)] : []), "bench/runs?kind=ab&limit=20", ...(B().runEndpoint(view, "ab") ? [B().runEndpoint(view, "ab")] : [])]);
    },
    ready: () => B().ready(v(), "ab"),
    render() {
      const run = B().runOf(v(), "ab");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().a)}</div>${B().statusCard(PAGE, "ab", run)}${results(run)}${B().recentRuns(PAGE, "ab")}</div>`;
    },
  };
  B().onStart(PAGE, () => B().start(PAGE, "ab", args()));
  document.addEventListener("click", async event => {
    const control = event.target.closest('[data-page-action="bench-ab-receipt"]');
    if (!control) return;
    try {
      const doc = await H().request(`bench/receipt/${encodeURIComponent(control.dataset.run)}`);
      H().download(`hyperloom-ab-${control.dataset.run}.json`, JSON.stringify(doc.receipt, null, 1));
    } catch (error) { H().announce(`Receipt unavailable: ${error.message}`); }
  });
})();
