/* Tool-calling evaluation (F28): local scenarios answered by inert mock tools, presets short / regular / hard;
   greedy repeats are a determinism check, pass@k only where sampling is honoured; live preview of the CLI command. */
"use strict";
(() => {
  const H = () => window.Hyperloom, B = () => window.HyperloomBench;
  const PAGE = "bench-tools", PRESETS = ["short", "regular", "hard"];
  const v = () => B().view(PAGE, {target: "", preset: "short", repeats: 1, max_tokens: 256, token_budget: 400000, sample: false, thinking: "", trace: false, runId: null});

  function args() {
    const view = v(), target = B().findTarget(view.target);
    return {...B().commonArgs(view, target), preset: view.preset, repeats: Number(view.repeats) || 1, max_tokens: Number(view.max_tokens) || 256};
  }
  function form() {
    const {esc} = H(), view = v(), target = B().findTarget(view.target);
    const corpus = H().state.docs["bench/corpus"];
    const count = corpus?.tools?.presets?.[view.preset]?.length;
    const mode = target?.sampling && view.sample ? `pass@${esc(view.repeats)} over sampled draws (seed varied per trial)` : Number(view.repeats) > 1 ? "determinism check: greedy decoding, repeats should be identical" : "single trial";
    return H().card("Run the tool-calling evaluation", `<div class="bench-fields">${B().targetPicker("tools-target", "target", view.target)}
      <div class="field"><label for="tools-preset">Preset</label><select class="inp" id="tools-preset" data-page-action="bench-field" data-key="preset">${PRESETS.map(p => `<option value="${p}" ${p === view.preset ? "selected" : ""}>${p}</option>`).join("")}</select><span class="hint">${count ? `${esc(count)} scenarios` : ""}</span></div>
      <div class="field"><label for="tools-repeats">Repeats</label><input class="inp" id="tools-repeats" type="number" min="1" max="5" value="${esc(view.repeats)}" data-page-action="bench-field" data-key="repeats" data-type="int"><span class="hint">${esc(mode)}</span></div>
      <div class="field"><label for="tools-max">Max tokens per turn</label><input class="inp" id="tools-max" type="number" min="64" max="2048" value="${esc(view.max_tokens)}" data-page-action="bench-field" data-key="max_tokens" data-type="int"></div>${B().budgetField("tools", view)}</div>
      ${B().controlsFor("tools", view, target)}<p class="hint">Mock tools are inert: the runner never executes anything; a follow-up turn returns the scenario's canned result. Categories: single call, parallel calls, argument types, declining unsafe calls, using a tool result.</p>
      ${B().preview("tools", args())}<div class="row">${B().startButton(PAGE, "tools", view.target)}</div>`);
  }
  function results(run) {
    const {esc, card, table, cells, statusPill} = H();
    if (!run) return "";
    const s = run.summary || {}, all = run.detail?.results || [];
    const repeat = s.pass_at_k != null ? `pass@${esc(s.k)}: ${esc((s.pass_at_k * 100).toFixed(1))} %` : s.deterministic ? `determinism check: ${esc(s.deterministic)} scenarios identical across repeats` : "single trial";
    const cats = Object.entries(s.categories || {}).map(([name, c]) => cells([esc(name), `${esc(c.passed)} / ${esc(c.scored)}`])).join("");
    const rows = all.map(r => cells([esc(r.scenario), esc(r.category), esc(r.trial + 1), r.status === "scored" ? statusPill(r.passed ? "ok" : "failed") : statusPill("error"), esc(r.reason || ""),
      `<span class="mono bench-excerpt">${esc((r.calls || []).map(c => `${c.name}(${JSON.stringify(c.arguments)})`).join("; ") || r.excerpt || "")}</span>`])).join("");
    return `<div class="grid g2">${card("Score", `<p class="bench-big">${s.score == null ? H().unavailable("nothing scored yet") : esc((s.score * 100).toFixed(1)) + " %"}</p><p class="hint">${esc(s.passed ?? 0)} of ${esc(s.scored ?? 0)} scenarios passed on the first trial · ${repeat} · ${esc(s.mock_tools || "")}</p>`)}${card("By category", table(["Category", "Passed"], cats, "Tool categories"))}</div>${card("Scenarios", table(["Scenario", "Category", "Trial", "Result", "Why", "Calls made"], rows, "Tool scenarios"))}`;
  }

  (window.HyperloomPages ||= {})[PAGE] = {
    title: "Tool calling", group: "Benchmarks", order: 14,
    icon: '<path d="M12 3l5 5-7 7-5-5z"/><path d="M5 10l-2 7 7-2"/>',
    endpoints: () => B().track(["bench/targets", "bench/corpus", B().guardEndpoint(B().autoTarget(v())), "bench/runs?kind=tools&limit=20", ...(B().runEndpoint(v(), "tools") ? [B().runEndpoint(v(), "tools")] : [])]),
    ready: () => B().ready(v(), "tools"),
    render() {
      const run = B().runOf(v(), "tools");
      return `<div class="bench" data-bench-page="${PAGE}"><div class="bench-top">${form()}${B().guardCard(v().target)}</div>${B().statusCard(PAGE, "tools", run)}${results(run)}${B().recentRuns(PAGE, "tools")}</div>`;
    },
  };
  B().onStart(PAGE, () => B().start(PAGE, "tools", args()));
})();
