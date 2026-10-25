/* Quality suite with paired comparison (F27): deterministic local suite, per-category scores, per-item table with
   reply excerpts; comparison with an earlier run of the same suite pairs items by id, item and request digest. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-quality";
  const CATEGORIES = ["short QA", "arithmetic chains", "state tracking", "instruction following", "long-context recall"];
  const v = () => B().view(PAGE, {target: "", categories: [...CATEGORIES], max_tokens: "", concurrency: 4, token_budget: 400000, sample: false, thinking: "", trace: false, runId: null, other: ""});

  function args() {
    const view = v(), target = B().findTarget(view.target);
    const result = {...B().commonArgs(view, target), categories: CATEGORIES.filter(c => (view.categories || []).includes(c)), concurrency: Number(view.concurrency) || 4};
    if (view.max_tokens) result.max_tokens = Number(view.max_tokens);
    return result;
  }
  function form() {
    const {esc} = H(), view = v(), target = B().findTarget(view.target);
    const cats = `<fieldset><legend>Categories</legend><div class="row">${CATEGORIES.map(c => `<label class="check"><input type="checkbox" value="${esc(c)}" data-page-action="bench-field" data-key="categories" data-type="set" ${(view.categories || []).includes(c) ? "checked" : ""}><span>${esc(c)}</span></label>`).join("")}</div></fieldset>`;
    return H().card("Run the quality suite", `<div class="bench-fields">${B().targetPicker("quality-target", "target", view.target)}
      <div class="field"><label for="quality-max">Max tokens per item (optional)</label><input class="inp" id="quality-max" type="number" min="16" max="4096" placeholder="per category: 32 (instructions 128)" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>
      <div class="field"><label for="quality-conc">Parallel requests</label><input class="inp" id="quality-conc" type="number" min="1" max="8" value="${esc(view.concurrency)}" data-page-action="bench-field" data-key="concurrency" data-type="int"></div>${B().budgetField("quality", view)}</div>${cats}
      ${B().controlsFor("quality", view, target)}<p class="hint">Suite hyperloom-quality-v1, fixed seed, deterministic scorers. Long-context recall runs at about 8k / 32k / 128k tokens where the context allows. 30 runs are kept per target.</p>
      <div class="row">${B().startButton(PAGE, "quality", view.target)}</div>${B().preview("quality", args())}`);
  }
  function compareCard(run) {
    const {esc, card} = H(), view = v();
    const list = H().state.docs["bench/runs?kind=quality&limit=20"]?.runs || [];
    const others = list.filter(r => r.id !== run.id && r.state === "succeeded" && r.summary?.suite === run.summary?.suite);
    const picker = `<div class="field"><label for="quality-other">Compare with</label><select class="inp" id="quality-other" data-page-action="bench-field" data-key="other"><option value="">Choose an earlier run</option>${others.map(r => `<option value="${esc(r.id)}" ${r.id === view.other ? "selected" : ""}>${esc(new Date(r.started * 1000).toLocaleString())} · ${esc(r.target_label)} · ${esc(r.model || "")} · ${r.summary?.score == null ? "unscored" : esc((r.summary.score * 100).toFixed(1)) + " %"}</option>`).join("")}</select></div>`;
    if (!others.length) return card("Paired comparison", `<p class="muted">No earlier succeeded run of this suite to compare with.</p>`);
    const doc = view.other ? H().state.docs[`bench/compare?a=${run.id}&b=${view.other}`] : null;
    if (!doc?.comparison?.paired) return card("Paired comparison", picker + (view.other ? `<p>${H().unavailable(H().state.errors[`bench/compare?a=${run.id}&b=${view.other}`] || "loading")}</p>` : ""));
    const p = doc.comparison.paired, pct = x => x == null ? H().unavailable("no paired items") : `${esc((x * 100).toFixed(1))} %`;
    const exclusions = Object.entries(p.exclusions || {}).map(([k, n]) => `${esc(n)} ${esc(k)}`).join(" · ") || "none";
    return card("Paired comparison", `${picker}<div class="banner ${p.verdict.startsWith("significant") ? "warn" : "info"}"><b>${esc(p.verdict)}</b></div><dl class="kv"><dt>Paired items</dt><dd>${esc(p.paired)} (excluded: ${exclusions})</dd><dt>This run / other run</dt><dd>${pct(p.this_score)} / ${pct(p.other_score)} on the paired items</dd><dt>Identical replies</dt><dd>${esc(p.identical_replies)}</dd><dt>Only this / only other</dt><dd>${esc(p.only_this)} / ${esc(p.only_other)} (both ${esc(p.both)}, neither ${esc(p.neither)})</dd><dt>Exact McNemar p</dt><dd>${esc(p.mcnemar_p)}</dd><dt>Paired difference</dt><dd>${p.difference == null ? H().unavailable("no paired items") : esc((p.difference * 100).toFixed(1)) + " points"}; 95 % interval ${p.interval_95 ? esc(p.interval_95.map(x => (x * 100).toFixed(1)).join(" … ")) + " points" : "unavailable"} <span class="hint">(${esc(p.interval_basis)})</span></dd></dl>`);
  }
  function results(run) {
    const {esc, card, table, cells, statusPill} = H();
    if (!run) return "";
    const s = run.summary || {}, items = run.detail?.items || [];
    const cats = Object.entries(s.categories || {}).map(([name, c]) => cells([esc(name), `${esc(c.passed)} / ${esc(c.scored)}`, c.score == null ? H().unavailable("nothing scored") : `${esc((c.score * 100).toFixed(1))} %`, esc(c.errors || 0)])).join("");
    const rows = items.map(i => cells([esc(i.id), esc(i.category), i.status === "scored" ? statusPill(i.passed ? "ok" : "failed") : statusPill("error"), `<span class="mono bench-excerpt">${esc(i.excerpt || i.error || "")}</span>`])).join("");
    return `<div class="grid g2">${card("Scores", `<p class="bench-big">${s.score == null ? H().unavailable("nothing scored yet") : esc((s.score * 100).toFixed(1)) + " %"}</p><p class="hint">${esc(s.passed ?? 0)} of ${esc(s.scored ?? 0)} scored items passed · ${esc(s.errors ?? 0)} errors · suite ${esc(s.suite || "")} ${esc((s.suite_digest || "").slice(0, 12))}${s.recall_note ? " · " + esc(s.recall_note) : ""}</p>` + table(["Category", "Passed", "Score", "Errors"], cats, "Quality categories"))}${run.state === "succeeded" ? compareCard(run) : ""}</div>${card("Items", `<details><summary>${esc(items.length)} items with reply excerpts</summary>${table(["Item", "Category", "Result", "Reply excerpt (200 characters)"], rows, "Quality items")}</details>`)}`;
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Quality suite", group: "Benchmarks", order: 13,
    icon: '<path d="M4 10l4 4 8-9"/><circle cx="10" cy="10" r="8"/>',
    endpoints() {
      const view = v(), list = ["bench/targets", B().guardEndpoint(B().autoTarget(view)), "bench/runs?kind=quality&limit=20"];
      if (B().runEndpoint(view, "quality")) list.push(B().runEndpoint(view, "quality"));
      const shown = B().runOf(view, "quality");
      if (shown && view.other && shown.id !== view.other) list.push(`bench/compare?a=${shown.id}&b=${view.other}`);
      return B().track(list);
    },
    ready: () => B().ready(v(), "quality"),
    error: () => ["bench/targets", "bench/runs?kind=quality&limit=20"].map(e => H().state.errors[e]).filter(Boolean).join(" · "),
    render() {
      const run = B().runOf(v(), "quality");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().target)}</div>${B().statusCard(PAGE, "quality", run)}${results(run)}${B().recentRuns(PAGE, "quality")}</div>`;
    },
  };
  B().onStart(PAGE, () => B().start(PAGE, "quality", args()));
})();
