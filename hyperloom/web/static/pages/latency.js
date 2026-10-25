/* Latency and throughput per model, route and caller (F9): router-observed and engine-observed percentiles from
   the stored request records, and the components' own histograms (estimated quantiles). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const ranges = [["15 min", 900], ["1 h", 3600], ["8 h", 28800], ["24 h", 86400], ["7 d", 604800]];
  const view = {by: "model", range: 3600};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-latency") || "{}")); } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-latency", JSON.stringify(view)); } catch (_) {} };
  const doc = () => `observe/latency?${new URLSearchParams({by: view.by, range: String(view.range)})}`;
  const history = kind => window.HyperloomTS.endpoint("engine_ttft_seconds_p50", {range: view.range, labels: {kind}});
  const METRICS = [["ttft_ms", "Time to first token"], ["queue_ms", "Queue wait"], ["total_ms", "Total time"], ["decode_tok_s", "Decode rate per request"]];
  const OBSERVERS = [["router", "--s1"], ["engine", "--s2"]];
  const fmt = (v, unit) => v == null ? null : unit === "ms" ? (v >= 10000 ? `${(v / 1000).toFixed(1)} s` : `${v.toFixed(v >= 100 ? 0 : 1)} ms`) : `${v.toFixed(1)} tok/s`;

  function quantiles(q, unit) {
    if (!q || !q.n) return H().unavailable(q?.n === 0 ? "no measured requests" : "not reported");
    return H().esc(`${fmt(q.p50, unit)} · ${fmt(q.p95, unit)} · ${fmt(q.p99, unit)}`) + ` <span class="muted">n ${H().esc(q.n)}</span>`;
  }

  function histogram(q, unit, color, label) {
    if (!q?.histogram) return "";
    const max = Math.max(1, ...q.histogram.map(b => b.count));
    let lower = 0;
    const rows = q.histogram.map(b => {
      const name = b.le == null ? `> ${fmt(lower, unit)}` : `≤ ${fmt(b.le, unit)}`;
      lower = b.le ?? lower;
      return `<div class="ob-bar"><span>${H().esc(name)}</span><span class="track"><i style="width:${(b.count / max * 100).toFixed(1)}%;background:var(${color})"></i></span><span>${H().esc(b.count)}</span></div>`;
    }).join("");
    let low = 0;
    const tableRows = q.histogram.map(b => { const name = b.le == null ? `> ${fmt(low, unit)}` : `≤ ${fmt(b.le, unit)}`; low = b.le ?? low; return H().cells([H().esc(name), H().esc(b.count)]); }).join("");
    return `<div class="ob-bars" role="img" aria-label="${H().esc(label)}">${rows}</div><details><summary>View as table</summary>${H().table(["Bin", "Requests"], tableRows, label)}</details>`;
  }

  function group(item, definitions) {
    const {esc, table, cells} = H();
    const rows = METRICS.map(([key, label]) => {
      const metric = item.metrics[key];
      return cells([esc(label), ...OBSERVERS.map(([observer]) => quantiles(metric[observer], metric.unit))]);
    }).join("");
    const outcomes = Object.entries(item.outcomes).map(([k, v]) => `<span class="tag">${esc(k)} ${esc(v)}</span>`).join(" ");
    const ttft = item.metrics.ttft_ms;
    const hist = OBSERVERS.map(([observer, color]) => ttft[observer]?.histogram ? `<div><p class="t2">TTFT · ${observer}-observed (requests per bin)</p>${histogram(ttft[observer], "ms", color, `TTFT histogram, ${observer}-observed, ${item.group}`)}</div>` : "").join("");
    const defs = `<details><summary>Definitions</summary><ul class="hint">${METRICS.map(([key, label]) => OBSERVERS.map(([o]) => `<li>${esc(label)} · ${o}: ${esc(definitions?.[o]?.[key] || "")}</li>`).join("")).join("")}</ul></details>`;
    return H().card(String(item.group), `<p>${outcomes}</p>` + table(["Measure (p50 · p95 · p99)", "Router-observed", "Engine-observed"], rows, `Latency of ${item.group}`) + `<div class="ob-grid">${hist}</div>` + defs,
      `${item.requests} requests · percentiles of served requests`);
  }

  function estimated(all) {
    const {esc, table, cells} = H();
    const cuts = all.filter(h => h.family === "_cut");
    const items = all.filter(h => h.family !== "_cut");
    if (!items.length) return "";
    const unit = family => family.includes("tokens_per_second") ? "tok/s" : "s";
    const show = (v, family) => v == null ? "—" : unit(family) === "s" ? (v >= 10 ? `${v.toFixed(1)} s` : `${(v * 1000).toFixed(v >= 0.1 ? 0 : 1)} ms`) : `${v.toFixed(1)} tok/s`;
    const when = h => h.as_of == null ? H().unavailable("no reading yet") : (h.stale ? `<span class="pill p-warn">stale</span> ` : "") + esc(new Date(h.as_of * 1000).toLocaleTimeString());
    const rows = items.map(h => cells([esc(h.observer), esc(h.replica ? `${h.profile}/${h.replica}` : h.profile) + (h.instance ? `<br><span class="muted mono">${esc(String(h.instance).slice(0, 16))}</span>` : ""), when(h), esc(h.family.replaceAll("_", " ")),
      esc(Object.entries(h.labels || {}).map(([k, v]) => `${k}=${v}`).join(" ") || "—"), esc(h.count ?? "—"),
      esc(`${show(h.p50, h.family)} · ${show(h.p95, h.family)} · ${show(h.p99, h.family)}`)])).join("");
    const notes = [...new Set(items.map(h => h.label).filter(Boolean))].map(n => `<p class="hint">${esc(n)}</p>`).join("");
    return H().card("Component histograms", table(["Observer", "Component", "As of", "Family", "Labels", "Count", "p50 · p95 · p99 (estimated)"], rows, "Component histograms")
      + `<p class="hint">${esc(items[0].basis)}</p>` + notes + cuts.map(c => `<p class="hint">${esc(c.observer)} ${esc(c.replica ? `${c.profile}/${c.replica}` : c.profile)}: ${esc(c.basis)}</p>`).join(""));
  }

  (window.HyperloomPages ||= {}).latency = {
    title: "Latency",
    group: "Inference",
    order: 21,
    icon: '<circle cx="10" cy="11" r="6.5"/><path d="M10 7.5V11l2.5 2M8 2.5h4"/>',
    endpoints: () => [doc(), history("router"), history("engine")],
    ready: () => !!H().state.docs[doc()],
    render() {
      const {esc, card, empty} = H();
      const d = H().state.docs[doc()] || {};
      const by = `<div class="row" role="group" aria-label="Group by">${["model", "route", "caller"].map(b => `<button type="button" class="btn ${view.by === b ? "pri" : ""}" aria-pressed="${view.by === b}" data-page-action="lat-by" data-by="${b}">by ${b}</button>`).join("")}</div>`;
      const range = `<div class="row" role="group" aria-label="Range">${ranges.map(([label, s]) => `<button type="button" class="btn ${view.range === s ? "pri" : ""}" aria-pressed="${view.range === s}" data-page-action="lat-range" data-range="${s}">${esc(label)}</button>`).join("")}</div>`;
      const controls = card("View", `<div class="row between">${by}${range}</div>`);
      if (d.groups == null) return `<div class="grid">${controls}${empty("Latency unavailable", d.why?.groups || "metrics store not open")}</div>`;
      const charts = OBSERVERS.map(([observer]) => {
        const h = H().state.docs[history(observer)];
        if (!h) return "";
        if (h.lines == null) return `<p>${H().display(null, h.why?.lines)}</p>`;
        return window.HyperloomTS.chart({title: `TTFT p50 · ${observer}-observed (s, over 60 s windows)`, unit: "s", step: h.step || 2, cadence: h.tier === "raw" ? h.interval_s || 0 : 0, start: h.start, end: h.end, lines: window.HyperloomTS.lines(h, {label: l => observer === "router" ? `${l.profile || ""} · ${l.model || "router"}` : `${l.replica || "engine"} · ${l.basis === "first_token" ? "first published token" : "queue + prefill (approximation)"}`}), group: "latency", height: 160});
      }).join("");
      const coverage = d.why?.coverage ? `<p class="hint" role="note"><span class="pill p-warn">partial</span> ${H().esc(d.why.coverage)}</p>` : d.coverage ? `<p class="hint">${H().esc(d.coverage.basis)}</p>` : "";
      const groups = coverage + (d.groups.length ? `<div class="grid">${d.groups.map(g => group(g, d.definitions)).join("")}</div>` : `<p>${H().unavailable(d.why?.groups)}</p>`);
      const hist = d.histograms?.length ? estimated(d.histograms) : card("Component histograms", `<p>${H().unavailable(d.why?.histograms)}</p>`);
      return `<div class="grid">${controls}${card("TTFT over time", charts || `<p>${H().unavailable("history not loaded")}</p>`)}${groups}${hist}</div>`;
    },
    after(main) { window.HyperloomTS.bind(main); },
  };

  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action^="lat-"]');
    if (!control) return;
    if (control.dataset.pageAction === "lat-by") view.by = control.dataset.by;
    else view.range = Number(control.dataset.range);
    save(); H().refresh();
  });
})();
