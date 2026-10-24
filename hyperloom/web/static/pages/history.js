/* Metrics history (F4): any recorded series over 30 min to 30 days, one line per label set, gaps kept. */
"use strict";
(() => {
  const ranges = [["30 min", 1800], ["1 h", 3600], ["8 h", 28800], ["24 h", 86400], ["7 d", 604800], ["30 d", 2592000]];
  const view = {series: "", range: 3600};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-history") || "{}")); } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-history", JSON.stringify(view)); } catch (_) {} };
  const H = () => window.Hyperloom;
  const names = () => H().state.docs.history?.series || [];
  const current = () => view.series && names().includes(view.series) ? view.series : names()[0] || "";
  const seriesEndpoint = () => current() ? window.HyperloomTS.endpoint(current(), {range: view.range}) : null;

  (window.HyperloomPages ||= {}).history = {
    title: "Metrics history",
    group: "Workspace",
    order: 30,
    icon: '<path d="M3 15l4-5 3 3 4-6 3 4"/><path d="M3 3v14h14"/>',
    endpoints: () => ["history", ...(seriesEndpoint() ? [seriesEndpoint()] : [])],
    ready: () => !!H().state.docs.history && (!seriesEndpoint() || !!H().state.docs[seriesEndpoint()]),
    render() {
      const {esc, card, empty, display} = H();
      const index = H().state.docs.history;
      if (!index.series) return empty("Metrics history unavailable", display(null, index.why?.series || "history store not open"));
      if (!index.series.length) return empty("No series recorded yet", "Samplers record a point every few seconds once they run.");
      const doc = H().state.docs[seriesEndpoint()] || {};
      const picker = `<label class="field" for="history-series">Series <select class="inp" id="history-series" data-page-action="history-series">${index.series.map(n => `<option value="${esc(n)}" ${n === current() ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>`;
      const buttons = `<div class="row" role="group" aria-label="Range">${ranges.map(([label, seconds]) => `<button type="button" class="btn ${seconds === view.range ? "pri" : ""}" data-page-action="history-range" data-range="${seconds}" aria-pressed="${seconds === view.range}">${esc(label)}</button>`).join("")}</div>`;
      const chart = doc.lines == null ? `<p>${display(null, doc.why?.lines || "loading")}</p>` : window.HyperloomTS.chart({
        title: `${current()}${doc.unit ? ` (${doc.unit})` : ""} · ${doc.kind || "series"} · ${doc.value || "avg"} per ${doc.step || "?"} s bucket`,
        unit: doc.unit || "", step: doc.step || 2, start: doc.start, end: doc.end,
        lines: window.HyperloomTS.lines(doc), group: "history", height: 220});
      const notes = [doc.why?.truncated, doc.interval_s ? `Sampled every ${doc.interval_s} s; coverage per bucket is in the table.` : "",
        "Stored in UTC; times shown in this browser's time zone. Gaps are periods without samples."].filter(Boolean);
      return `<div class="grid">${card("Series", `<div class="row between">${picker}${buttons}</div>`)}${card("History", chart + notes.map(n => `<p class="hint">${esc(n)}</p>`).join(""))}</div>`;
    },
    after(main) { window.HyperloomTS.bind(main); },
  };

  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action="history-range"]');
    if (!control) return;
    view.range = Number(control.dataset.range); save(); H().refresh();
  });
  document.addEventListener("change", event => {
    if (event.target.dataset?.pageAction !== "history-series") return;
    view.series = event.target.value; save(); H().refresh();
  });
})();
