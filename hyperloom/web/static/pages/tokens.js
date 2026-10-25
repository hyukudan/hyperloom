/* Token ledger (F11): hour records of the engines' own token counters per model, machine and kind. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const ranges = [["1 h", "3600"], ["24 h", "86400"], ["7 d", "604800"], ["30 d", "2592000"], ["All", "all"]];
  const groups = [["Kind", "kind"], ["Model", "model"], ["Machine", "machine"]];
  const kindColors = {generated: "--s1", computed: "--s2", cached: "--s3", imported: "--s4"};
  const kindNames = {generated: "generated", computed: "computed prefill", cached: "cache-restored", imported: "RTX-imported"};
  const view = {range: "86400", by: "kind", sort: "total", desc: true, confirm: false, message: "", pending: ""};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-tokens") || "{}"), {confirm: false, message: ""}); } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-tokens", JSON.stringify({range: view.range, by: view.by, sort: view.sort, desc: view.desc})); } catch (_) {} };
  const endpoint = () => `ledger?${new URLSearchParams({range: view.range, by: view.by})}`;
  const num = (value, digits = 0) => value == null || !Number.isFinite(value) ? null : value.toLocaleString(undefined, {maximumFractionDigits: digits, minimumFractionDigits: digits});
  const compact = value => {
    if (value == null || !Number.isFinite(value)) return null;
    for (const [unit, size] of [["B", 1e9], ["M", 1e6], ["k", 1e3]]) if (Math.abs(value) >= size) return `${(value / size).toFixed(1)} ${unit}`;
    return value.toFixed(0);
  };
  const pct = value => value == null || !Number.isFinite(value) ? null : `${(value * 100).toFixed(1)} %`;
  const tag = (text, kind) => `<span class="lg-tag ${kind || ""}">${H().esc(text)}</span>`;
  function shown(value, why, suffix = "") {
    return value == null ? H().unavailable(why) : `${H().esc(value)}${suffix ? ` <small>${H().esc(suffix)}</small>` : ""}`;
  }
  function kpis(doc) {
    const {kpi, esc} = H(), k = doc.kpis, why = doc.why || {};
    const observer = "control machine · engine counters";
    const busiest = k.busiest_day ? `<span class="lg-day">${esc(k.busiest_day.day)}</span><br><small>${esc(compact(k.busiest_day.tokens))} tokens</small>` : H().unavailable(why["kpis.busiest_day"]);
    return `<div class="lg-kpis">${[
      kpi("Total tokens", shown(compact(k.total)), `${esc(compact(k.input))} input · ${esc(compact(k.generated))} generated · ${observer}`),
      kpi("Generated share", shown(pct(k.generated_share), why["kpis.generated_share"]), "generated / (input + generated)"),
      kpi("Cache-restored", shown(pct(k.cached_share), why["kpis.cached_share"]), k.tokens_saved == null ? esc(why["kpis.tokens_saved"] || "") : `${esc(compact(k.tokens_saved))} tokens of prefill saved${k.cached_tag && k.cached_tag !== "measured" ? " · partial" : ""}`),
      kpi("Computed prefill", shown(compact(k.computed)), `${esc(k.computed_tag)}${k.computed_unavailable_input ? ` · unavailable for ${esc(compact(k.computed_unavailable_input))} input tokens` : ""}`),
      kpi("Output : input", shown(k.output_input == null ? null : k.output_input.toFixed(2), why["kpis.output_input"]), "generated tokens per input token"),
      kpi("Average per day", shown(compact(k.avg_per_day), why["kpis.avg_per_day"]), `over ${esc(k.observed_days)} observed day(s) in ${esc(doc.timezone)}`),
      kpi("Busiest day", busiest, "local day of the configured zone"),
    ].join("")}</div>`;
  }
  function colorOf(doc, key) {
    if (doc.by === "kind") return kindColors[key] || "--s5";
    // A model or machine keeps its colour across ranges and refreshes: chosen from its name, not its position.
    let hash = 0;
    for (const char of String(key)) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    return window.HyperloomTS.colors[hash % window.HyperloomTS.colors.length];
  }
  function bars(doc) {
    const {esc} = H();
    const buckets = doc.buckets || [];
    if (!buckets.length) return `<p class="muted">No engine was listed in this range: nothing was expected or counted (not zero).</p>`;
    const keys = doc.by === "kind" ? ["generated", "computed", "cached", "imported"] : [...new Set(buckets.flatMap(b => Object.keys(b.values)))].sort();
    const W = Math.round(Math.max(300, Math.min(1600, (document.querySelector("main")?.clientWidth || 700) - 64))), Hh = 200, L = 56, R = 8, T = 8, B = 30;
    // Bars sit at their real time, so a period without any bucket stays empty space.
    const t0 = Math.min(doc.start, ...buckets.map(b => b.start)), t1 = Math.max(doc.end, ...buckets.map(b => b.end));
    const x = t => L + (t - t0) / Math.max(1, t1 - t0) * (W - L - R);
    const totals = buckets.map(b => keys.reduce((sum, key) => sum + (b.values[key] || 0), 0));
    const top = Math.max(1, ...totals) * 1.1;
    const y = v => T + (1 - v / top) * (Hh - T - B);
    let rects = "";
    buckets.forEach((b, i) => {
      const left = x(b.start), width = Math.max(1.5, (x(b.end) - left) * 0.8);
      let base = 0;
      if (b.state === "expected, not observed") rects += `<rect class="lg-none" x="${left.toFixed(1)}" y="${T}" width="${width.toFixed(1)}" height="${Hh - T - B}"><title>${esc(b.label)}: an engine was listed but not observed</title></rect>`;
      else if (b.state === "observed" && !totals[i]) rects += `<rect x="${left.toFixed(1)}" y="${(y(0) - 1.5).toFixed(1)}" width="${width.toFixed(1)}" height="1.5" fill="var(--text-2)"><title>${esc(b.label)}: observed, 0 tokens</title></rect>`;
      for (const key of keys) {
        const value = b.values[key] || 0;
        if (!value) continue;
        rects += `<rect x="${left.toFixed(1)}" y="${y(base + value).toFixed(1)}" width="${width.toFixed(1)}" height="${(y(base) - y(base + value)).toFixed(1)}" fill="var(${colorOf(doc, key)})"><title>${esc(b.label)} · ${esc(kindNames[key] || key)}: ${esc(num(value))}</title></rect>`;
        base += value;
      }
      const coverage = b.coverage == null ? null : Math.max(0, Math.min(1, b.coverage));
      if (coverage != null) rects += `<rect x="${left.toFixed(1)}" y="${Hh - B + 3}" width="${(width * coverage).toFixed(1)}" height="3" fill="var(--text-3)"><title>${esc(b.label)} coverage ${Math.round(coverage * 100)} %</title></rect>`;
    });
    const ticks = [0, .5, 1].map(f => f * top / 1.1).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${L - 4}" y="${y(v) + 3}" text-anchor="end" class="lg-ax">${esc(compact(v))}</text>`).join("");
    const count = Math.max(2, Math.floor((W - L) / 110));
    const labels = Array.from({length: count + 1}, (_, i) => t0 + i * (t1 - t0) / count).map((t, i) => {
      const date = new Date(t * 1000);
      const text = doc.bucket === "hour" ? date.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", timeZone: doc.timezone_iana || undefined}) : date.toLocaleDateString(undefined, {month: "short", day: "numeric"});
      return `<text x="${x(t).toFixed(1)}" y="${Hh - 6}" text-anchor="${i === 0 ? "start" : i === count ? "end" : "middle"}" class="lg-ax">${esc(text)}</text>`;
    }).join("");
    const legend = keys.map(key => `<span><i style="background:var(${colorOf(doc, key)})"></i>${esc(kindNames[key] || key)}</span>`).join("") + `<span><i style="background:var(--text-3);height:3px"></i>coverage</span><span><i class="lg-hatch-key"></i>listed, not observed</span>`;
    const cell = (b, key) => (b.unavailable || []).includes(key) || !(key in b.values)
      ? H().unavailable(key === "computed" ? "input missing for part of the record, or inconsistent counters" : doc.by === "kind" ? "not reported in this " + doc.bucket : "no record of it in this " + doc.bucket)
      : esc(num(b.values[key])) + ((b.partial_series || []).includes(key) ? `<span class="lg-tag e">partial</span>` : "");
    const rows = buckets.map(b => `<tr><td>${esc(b.label)}</td>${keys.map(key => `<td class="lg-num">${cell(b, key)}</td>`).join("")}<td>${esc(b.state)}${b.partial ? " · partly reported" : ""}</td><td class="lg-num">${b.coverage == null ? H().unavailable("nothing expected") : esc(Math.round(b.coverage * 100) + " %")}</td><td class="lg-num">${esc(num(b.uncertain))}</td></tr>`).join("");
    return `<figure class="lg-bars" style="margin:0"><figcaption class="muted">Tokens per ${esc(doc.bucket)} (${esc(doc.timezone)}) by ${esc(doc.by)} · hatched: an engine was listed but not observed · thin line: observed, 0 tokens · empty space: no engine listed</figcaption>
<div class="lg-legend">${legend}</div>
<svg viewBox="0 0 ${W} ${Hh}" width="${W}" height="${Hh}" role="img" aria-label="Stacked tokens per ${esc(doc.bucket)}"><defs><pattern id="lg-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="var(--text-3)" stroke-width="1"/></pattern></defs>${ticks}${rects}${labels}</svg>
<details><summary>View as table</summary><div class="table-wrap" tabindex="0" role="region" aria-label="Tokens per ${esc(doc.bucket)}"><table><thead><tr><th scope="col">${esc(doc.bucket)}</th>${keys.map(key => `<th scope="col" class="lg-num">${esc(kindNames[key] || key)}</th>`).join("")}<th scope="col">State</th><th scope="col" class="lg-num">Coverage</th><th scope="col" class="lg-num">Approximate hour</th></tr></thead><tbody>${rows}</tbody></table></div></details></figure>`;
  }
  const columns = [["model", "Model"], ["input", "Input"], ["computed", "Computed"], ["cached", "Cache-restored"], ["imported", "Imported"], ["generated", "Generated"], ["total", "Total"], ["cached_share", "Cache share"], ["output_input", "Out : in"]];
  function models(doc) {
    const {esc, unavailable} = H();
    const rows = [...(doc.models || [])].sort((a, b) => {
      const x = a[view.sort], y = b[view.sort];
      const order = typeof x === "string" ? String(x).localeCompare(String(y)) : (x ?? -1) - (y ?? -1);
      return view.desc ? -order : order;
    });
    const head = columns.map(([key, label]) => `<th scope="col" class="${key === "model" ? "" : "lg-num"}" aria-sort="${view.sort === key ? (view.desc ? "descending" : "ascending") : "none"}"><button type="button" class="lg-sort" data-page-action="tokens-sort" data-key="${key}">${esc(label)}${view.sort === key ? (view.desc ? " ↓" : " ↑") : ""}</button></th>`).join("");
    const cell = (m, key) => {
      if (key === "model") return `${esc(m.model)}<div class="muted">${esc(m.machines.join(", "))}</div>`;
      if (key === "cached") return m.cached == null ? unavailable("not reported by this model's engines") : esc(num(m.cached)) + (m.cached_tag === "partial" ? tag("partial", "e") : "");
      if (key === "computed") return esc(num(m.computed)) + (m.computed_tag !== "measured" ? tag("partly estimated", "e") : "");
      if (key === "cached_share") return m.cached_share == null ? unavailable("no whole-record cache counts") : esc(pct(m.cached_share));
      if (key === "output_input") return m.output_input == null ? unavailable("no input") : esc(m.output_input.toFixed(2));
      return esc(num(m[key]));
    };
    const body = rows.map(m => `<tr>${columns.map(([key]) => `<td class="${key === "model" ? "" : "lg-num"}">${cell(m, key)}</td>`).join("")}</tr>`).join("");
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Tokens by model"><table><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${columns.length}">No models in this range.</td></tr>`}</tbody></table></div>`;
  }
  function counts(map) {
    if (map === null || typeof map !== "object" || Array.isArray(map)) return H().unavailable("not reported");
    const entries = Object.entries(map).filter(([, v]) => typeof v === "number" && Number.isFinite(v));
    return entries.length ? H().esc(entries.map(([k, v]) => `${k} ${num(v)}`).join(" · ")) : H().unavailable("no counter value for this interval");
  }
  function gaps(doc) {
    const {esc} = H();
    const when = t => t == null ? "unknown" : new Date(t * 1000).toLocaleString();
    const rows = (doc.gaps || []).map(g => `<tr><td>${esc(when(g.start))} → ${g.end == null ? "now" : esc(when(g.end))}</td><td>${esc(g.profile || "")} · ${esc(g.machine || "")}</td><td class="lg-wrap">${esc(g.reason)}</td><td class="lg-num">${counts(g.counts)}</td></tr>`).join("");
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Unobserved intervals"><table><thead><tr><th scope="col">Interval</th><th scope="col">Engine</th><th scope="col">Why</th><th scope="col" class="lg-num">Tokens counted by the engine there</th></tr></thead><tbody>${rows || `<tr><td colspan="4">No unobserved interval in this range.</td></tr>`}</tbody></table></div>${doc.gap_count > (doc.gaps || []).length ? `<p class="hint">${esc(doc.gap_count)} intervals in range; the newest ${esc((doc.gaps || []).length)} are listed.</p>` : ""}`;
  }
  function sources(doc) {
    const {esc, statusPill} = H();
    const rows = (doc.sources || []).map(s => `<tr><td>${esc(s.profile)} · ${esc(s.replica)}</td><td>${esc(s.machine)}</td><td>${esc(s.model || "")}</td><td>${s.error ? statusPill("error") + ` <span class="lg-wrap">${esc(s.error)}</span>` : statusPill("ok")}</td><td class="lg-wrap">${esc((s.reported || []).join(", ") || "none")}${(s.limits || []).map(l => `<div class="hint">${esc(l)}</div>`).join("")}</td><td class="lg-wrap">${esc(s.identity || "not established yet")}</td></tr>`).join("");
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="Counted engines"><table><thead><tr><th scope="col">Engine</th><th scope="col">Machine</th><th scope="col">Model</th><th scope="col">Last read</th><th scope="col">Counters reported</th><th scope="col">Instance identity</th></tr></thead><tbody>${rows || `<tr><td colspan="6">No engine counted yet.</td></tr>`}</tbody></table></div>`;
  }
  function controls(doc) {
    const {esc, button} = H();
    const seg = (list, current, action) => `<div class="row" role="group">${list.map(([label, value]) => `<button type="button" class="btn ${value === current ? "pri" : ""}" data-page-action="${action}" data-value="${value}" aria-pressed="${value === current}">${esc(label)}</button>`).join("")}</div>`;
    const blocker = doc.kpis == null ? (doc.why?.kpis || "ledger unavailable") : "";
    const gate = H().mutationReason() || view.pending;
    const reset = view.confirm ? `<div class="lg-confirm" id="tokens-reset-dialog" role="alertdialog" aria-modal="true" aria-labelledby="tokens-reset-title" aria-describedby="tokens-reset-text"><p><b id="tokens-reset-title">Reset the token ledger?</b> <span id="tokens-reset-text">Every hour record and every disclosed interval is deleted for all time. Counting continues from the next sample. This cannot be undone. Enter does not confirm: use the button.</span></p><div class="row">${button("Confirm reset", "", {style: "dan", id: "tokens-reset-confirm", disabled: gate, extra: 'data-page-action="tokens-reset-confirm"'})}${button("Cancel", "", {id: "tokens-reset-cancel", extra: 'data-page-action="tokens-reset-cancel"'})}</div></div>` : "";
    return `<div class="lg-controls"><div class="row"><span class="muted">Range</span>${seg(ranges, view.range, "tokens-range")}</div><div class="row"><span class="muted">Bars by</span>${seg(groups, view.by, "tokens-by")}</div><div class="row">${button("Export CSV", "", {disabled: blocker, extra: 'data-page-action="tokens-export"'})}${button("Reset ledger…", "", {style: "dan", id: "tokens-reset-open", disabled: blocker || gate || (view.confirm ? "confirmation open below" : ""), extra: 'data-page-action="tokens-reset"'})}</div></div>${reset}${view.message ? `<p role="status" class="hint">${esc(view.message)}</p>` : ""}`;
  }

  (window.HyperloomPages ||= {}).tokens = {
    title: "Token ledger",
    group: "Inference",
    order: 40,
    icon: '<path d="M3 5h14M3 10h14M3 15h9"/><circle cx="15.5" cy="15" r="2"/>',
    endpoints: () => [endpoint()],
    ready: () => !!H().state.docs[endpoint()],
    render() {
      const {card, esc, empty} = H();
      const doc = H().state.docs[endpoint()];
      const head = card("Ledger", controls(doc) + `<p class="hint">${esc(doc.range_note || "")} · times in ${esc(doc.timezone)} (${esc(doc.timezone_source)}) · observer: ${esc(doc.observer)}</p>${(doc.notes || []).map(n => `<p class="hint">${esc(n)}</p>`).join("")}`);
      if (doc.kpis == null) return `<div class="grid">${head}${empty("Token ledger unavailable", doc.why?.kpis || "no ledger records")}${doc.sources ? card("Counted engines", sources(doc)) : ""}</div>`;
      return `<div class="grid">${head}${kpis(doc)}${card("Tokens over time", bars(doc))}${card("By model", models(doc), "click a column to sort")}${card("Insights", `<ul class="lg-insights">${(doc.insights || []).map(t => `<li>${esc(t)}</li>`).join("")}</ul>`)}${card("Unobserved intervals", gaps(doc), "first observations, restarts, unreachable engines, resets")}${card("Counted engines", sources(doc))}${card("Coverage", `<p class="hint">${esc(doc.coverage?.note || "")}</p>`)}</div>`;
    },
  };

  function closeConfirm() {
    view.confirm = false; H().render(); document.getElementById("tokens-reset-open")?.focus();
  }
  // The confirmation keeps focus inside itself, closes with Escape (focus back on its opener) and never confirms on
  // Enter: only an explicit click or Space on "Confirm reset" sends the reset.
  document.addEventListener("keydown", event => {
    const dialog = document.getElementById("tokens-reset-dialog");
    if (!view.confirm || !dialog) return;
    if (event.key === "Escape") { event.preventDefault(); closeConfirm(); return; }
    if (event.key === "Enter" && dialog.contains(event.target)) { event.preventDefault(); return; }
    if (event.key === "Tab") {
      const focusable = [...dialog.querySelectorAll("button:not(:disabled)")];
      if (!focusable.length) return;
      const first = focusable[0], last = focusable.at(-1);
      if (!dialog.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  async function post(action, body) {
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify(body)});
  }
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action^='tokens-']");
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "tokens-range") { view.range = control.dataset.value; save(); H().refresh(); return; }
    if (action === "tokens-by") { view.by = control.dataset.value; save(); H().refresh(); return; }
    if (action === "tokens-sort") {
      const key = control.dataset.key;
      view.desc = view.sort === key ? !view.desc : key !== "model";
      view.sort = key; save(); H().render(); return;
    }
    if (action === "tokens-reset") { view.confirm = true; view.message = ""; H().render(); document.getElementById("tokens-reset-cancel")?.focus(); return; }
    if (action === "tokens-reset-cancel") { closeConfirm(); return; }
    if (action === "tokens-reset-confirm") {
      const blocker = H().mutationReason() || view.pending;
      if (blocker) { view.message = `Reset not sent: ${blocker}`; H().render(); return; }
      view.confirm = false; view.pending = "the reset is being sent"; H().state.busy = true; H().render();
      try { const result = await post("ledger/reset", {confirm: "reset"}); view.message = `Ledger reset: ${result.records_deleted} records deleted. ${result.note}`; }
      catch (error) { view.message = `Reset refused: ${error.message}`; }
      finally { view.pending = ""; H().state.busy = false; H().state.fresh = false; }
      H().announce(view.message); await H().refresh(); document.getElementById("tokens-reset-open")?.focus(); return;
    }
    if (action === "tokens-export") {
      try {
        const doc = await H().request(`ledger/export?${new URLSearchParams({range: view.range})}`);
        H().download(doc.filename || "hyperloom-token-ledger.csv", doc.csv, "text/csv;charset=utf-8");
        if (doc.truncated_rows) H().announce(doc.why.csv);
      } catch (error) { H().announce(`Export failed: ${error.message}`); }
    }
  });
})();
