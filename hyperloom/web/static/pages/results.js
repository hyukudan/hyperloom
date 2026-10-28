/* Published measurements and cached observations, separately labelled. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  let selected = "";
  const fmt = (v, unit = "", why = "not published for this condition") => v == null ? H().unavailable(why) : H().esc(String(v) + (unit ? " " + unit : ""));
  // A published table cell without a value means the README gives none for that condition, not "unavailable".
  const num = (v, unit) => v == null ? '<span class="hint" title="not published for this condition">—</span>' : H().esc(String(v) + " " + unit);
  function live(p) {
    const {esc, unavailable} = H();
    return `<section class="results-live"><h3>Live now · ${esc(p.profile)}</h3>${p.model == null ? `<p>${unavailable(p.why)}</p>` : ""}${p.values.map(v => `<div class="results-value"><b>${fmt(v.value, v.unit, v.why)}</b><div class="hint">${esc(v.label)}</div><div class="hint">${v.replica ? esc(v.replica) + " · " : ""}${v.group ? esc(v.group) + " · " : ""}${v.n == null ? "" : "n=" + esc(v.n) + " · "}window ${Number.isFinite(v.window_s) ? esc(Math.round(v.window_s) + " s") : unavailable("window unavailable")}</div></div>`).join("")}</section>`;
  }
  function modelCard(m, doc) {
    const {esc, card, unavailable} = H(), source = doc.published.source;
    const profiles = doc.live.filter(p => p.model === m.id);
    const rows = m.rows.map(r => `<tr><th scope="row">${esc(r.mode)}<div class="hint">${esc(r.metric.replaceAll("_", " "))}</div></th><td>${esc(r.hardware)}</td><td>${esc(r.prompt)}<div class="hint">${esc(r.note)}</div></td><td>${esc(Array.isArray(r.concurrency) ? r.concurrency.join(" / ") : r.concurrency)}</td><td class="results-num">${num(r.best, r.unit)}</td><td class="results-num">${num(r.typical, r.unit)}</td><td><span class="pill">published · ${esc(source.ref)} · ${esc(r.date)}</span></td><td class="mono">${esc(r.build)}</td></tr>`).join("");
    const table = m.rows.length ? `<div class="results-scroll" role="region" aria-label="Published measurements for ${esc(m.name)}" tabindex="0"><table><caption>Published measurements · ${esc(m.name)}</caption><thead><tr>${["Mode / metric", "Hardware", "Prompt / conditions", "Concurrency", "Best", "Typical", "Date / source", "Build"].map(h => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>` : `<p>${unavailable(m.status)}</p>`;
    return card(m.name, `<p>${esc(m.quant)} · ${esc(m.hardware)}</p><p class="hint">${esc(m.status)}</p><div class="results-layout"><div class="results-published">${table}</div><aside aria-label="Live now for ${esc(m.name)}">${profiles.length ? profiles.map(live).join("") : "<p>No profile is running for this model</p>"}</aside></div>`);
  }
  (window.HyperloomPages ||= {}).results = {
    title: "Measured results", group: "Benchmarks", order: 15,
    icon: '<path d="M3 3v14h14M6 12l3-4 3 2 4-6"/><circle cx="6" cy="12" r="1"/><circle cx="12" cy="10" r="1"/>',
    endpoints: () => ["results"],
    render() {
      const {esc, card, unavailable} = H(), d = H().state.docs.results;
      if (!d) return card("Measured results", unavailable(H().state.errors.results || "loading"));
      const models = d.published?.models || [];
      if (!selected && models.length) selected = d.live.find(p => models.some(m => m.id === p.model))?.model || models[0].id;
      const selector = models.length ? `<div class="field"><label for="results-model">Model / identity</label><select class="inp" id="results-model">${models.map(m => `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></div>` : "";
      const url = "https://github.com/hyukudan/strix-halo-engine-lab/blob/62d7b0ac/";
      return `<div class="results"><div class="card">${selector}<p class="hint">Published numbers describe the named workload, hardware, build and numerical identity. Live observations have their own observer and window; compare matching conditions.</p><p><a href="${url}README.md">Source README · 62d7b0ac</a> · <a href="${url}docs/performance.md">Performance methodology</a></p>${d.why?.live ? `<p>${unavailable(d.why.live)}</p>` : !d.live.length ? "<p>No profile is running</p>" : ""}</div>${d.published == null ? card("Published", unavailable(d.why?.published)) : models.filter(m => m.id === selected).map(m => modelCard(m, d)).join("")}${d.live.filter(p => p.model == null || !models.some(m => m.id === p.model)).map(live).join("")}</div>`;
    },
  };
  document.addEventListener("change", e => {
    if (e.target.id === "results-model") { selected = e.target.value; H().render(); }
  });
})();
