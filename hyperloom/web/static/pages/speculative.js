/* Speculative decoding (F15): acceptance over time per engine and method, accepted tokens per cycle, per-depth
   reach and hit where the engine counts them, and draft time against the time it saved (estimated). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const docName = "observe/speculative";
  const rows = () => H().state.docs[docName]?.engines || [];
  const pct = v => v == null ? null : `${(v * 100).toFixed(1)} %`;

  function bars(items, color, label) {
    const max = Math.max(1, ...items.map(i => i.value || 0));
    return `<div class="ob-bars" role="img" aria-label="${H().esc(label)}">${items.map(i => `<div class="ob-bar"><span>${H().esc(i.name)}</span><span class="track"><i style="width:${((i.value || 0) / max * 100).toFixed(1)}%;background:var(${color})"></i></span><span>${H().esc(i.text)}</span></div>`).join("")}</div>`;
  }

  function chart(endpoint, title, unit) {
    const h = H().state.docs[endpoint];
    if (!h) return "";
    if (h.lines == null) return `<p>${H().display(null, h.why?.lines)}</p>`;
    return window.HyperloomTS.chart({title, unit, step: h.step || 2, cadence: h.tier === "raw" ? h.interval_s || 0 : 0, start: h.start, end: h.end,
      lines: window.HyperloomTS.lines(h, {label: l => `${l.method || "draft"} · ${l.instance || ""}`}), group: "speculative", height: 150, max: unit ? undefined : 1});
  }

  function card(row) {
    const {esc, card: box, unavailable, table, cells} = H();
    const why = row.why || {};
    const summary = `<dl class="kv"><dt>Method</dt><dd>${row.method ? esc(row.method) + (row.scope ? ` · ${esc(row.scope)}` : "") : unavailable(why.acceptance)}</dd>`
      + `<dt>Accepted / proposed</dt><dd>${row.proposed ? esc(`${row.accepted.toLocaleString()} / ${row.proposed.toLocaleString()} · ${pct(row.acceptance)}`) : unavailable(why.acceptance || "no drafts yet")}</dd>`
      + `<dt>Accepted per cycle</dt><dd>${row.accepted_per_cycle != null ? esc(row.accepted_per_cycle.toFixed(2)) : unavailable(why.acceptance || "engine reports no cycles")}</dd>`
      + `<dt>Draft time</dt><dd>${row.draft_seconds != null ? esc(`${row.draft_seconds.toFixed(1)} s`) : unavailable(why.draft_seconds)}</dd>`
      + `<dt>Target time saved</dt><dd>${row.saved_seconds != null ? esc(`${row.saved_seconds.toFixed(1)} s (estimated)`) : unavailable(why.saved_seconds)}</dd></dl>`
      + (row.basis ? `<p class="hint">${esc(row.basis)}${row.saved_basis ? ` · ${esc(row.saved_basis)}` : ""}</p>` : "");
    const depth = row.depth ? bars(row.depth.map(d => ({name: `depth ${d.depth}`, value: d.hit_rate, text: d.hit_rate == null ? "—" : `hit ${pct(d.hit_rate)} · reach ${pct(d.reach_rate) || "—"}`})), "--s3", "Hit rate per draft depth")
      + `<details><summary>View as table</summary>${table(["Depth", "Reached", "Hit", "Reach rate", "Hit rate"], row.depth.map(d => cells([esc(d.depth), esc(d.reached ?? "—"), esc(d.hit ?? "—"), esc(pct(d.reach_rate) || "—"), esc(pct(d.hit_rate) || "—")])).join(""), "Per-depth reach and hit")}</details>`
      : `<p>${unavailable(why.depth)}</p>`;
    const total = (row.window_rows || []).reduce((a, b) => a + b, 0);
    const windows = row.window_rows ? `<p class="t2">${esc(row.window_label || "rows per window")}</p>` + bars(row.window_rows.map((v, i) => ({name: String(i), value: v, text: total ? `${v} (${(v / total * 100).toFixed(0)} %)` : String(v)})), "--s4", row.window_label || "rows per window") : `<p>${unavailable(why.window_rows || "not reported")}</p>`;
    return box(`${row.profile} / ${row.replica}`, `<div class="ob-grid">${box("Acceptance", summary)}${box("Per-depth reach and hit", depth)}${box("Verification windows", windows)}</div>`
      + chart(row.acceptance_series, "Acceptance over time (accepted / proposed per interval)", "") + chart(row.per_cycle_series, "Accepted tokens per cycle", "tokens"),
      row.model || "");
  }

  (window.HyperloomPages ||= {}).speculative = {
    title: "Speculative decoding",
    group: "Inference",
    order: 23,
    icon: '<path d="M3 15l4-4 3 2 7-8"/><path d="M13 5h4v4"/>',
    endpoints: () => [docName, ...rows().flatMap(r => [r.acceptance_series, r.per_cycle_series])],
    ready: () => !!H().state.docs[docName],
    render() {
      const d = H().state.docs[docName] || {};
      if (d.engines == null) return H().empty("Speculative decoding unavailable", d.why?.engines || "metrics store not open");
      if (!d.engines.length) return H().empty("No engine observed", d.why?.engines || "no running profile");
      return `<div class="grid">${d.engines.map(card).join("")}</div>`;
    },
    after(main) { window.HyperloomTS.bind(main); },
  };
})();
