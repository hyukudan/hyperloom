/* Thermals (F19): temperature history of every GPU, CPU and other sensor over a day or a week, one chart per
   machine, with the warn/critical thresholds and the periods a GPU reported throttling. */
"use strict";
(() => {
  const ranges = [["Day", "day"], ["Week", "week"]];
  const kinds = [["gpu", "GPU"], ["cpu", "CPU"], ["sensor", "Other sensors"]];
  const view = {range: "day", draft: {}, error: "", busy: false};
  try { const saved = JSON.parse(localStorage.getItem("hyperloom-thermals") || "{}"); if (ranges.some(r => r[1] === saved.range)) view.range = saved.range; } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-thermals", JSON.stringify({range: view.range})); } catch (_) {} };
  const H = () => window.Hyperloom;
  const endpoint = () => `thermals?range=${view.range}`;
  const deg = v => v == null || !Number.isFinite(v) ? null : `${v.toFixed(1)} °C`;
  const shown = (text, why) => text == null ? H().unavailable(why) : H().esc(text);
  // Times are shown in the zone the server uses (configured or the control machine's): the document carries that
  // zone's UTC offsets over its range, so no browser zone is ever assumed.
  function zoned(t, offsets, opts) {
    let offset = 0;
    for (const [from, value] of offsets || []) if (from <= t) offset = value; else break;
    return new Date((t + offset) * 1000).toLocaleString(undefined, {...opts, timeZone: "UTC"});
  }
  let offsets = [];
  const setZone = d => { offsets = d?.offsets || []; };
  const when = (t, span) => zoned(t, offsets, span > 2 * 86400 ? {month: "short", day: "numeric", hour: "2-digit"} : {hour: "2-digit", minute: "2-digit"});
  const stamp = t => zoned(t, offsets, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  // A sensor's colour and dash come from its identity (server side), so it keeps its look as others come and go.
  const color = l => `--s${(l.colour ?? 0) % 5 + 1}`;
  const dashes = ["", ' stroke-dasharray="6 3"', ' stroke-dasharray="2 3"'];
  const dash = l => dashes[l.dash || 0];

  function chart(m, d) {
    const {esc} = H();
    const lines = m.lines || [];
    const all = lines.flatMap(l => l.points);
    if (!all.length) return `<p class="muted">No temperature samples in this range (unavailable, not zero).</p>`;
    const t0 = d.start, t1 = d.end, span = Math.max(1, t1 - t0);
    const present = new Set(lines.map(l => l.kind));
    const limits = kinds.filter(([k]) => present.has(k) && d.thresholds?.[k]).map(([k, label]) => [k, label, d.thresholds[k]]);
    const values = all.map(p => p[1]);
    const top = Math.max(...values, ...limits.map(l => l[2].crit)) + 5, bottom = Math.max(0, Math.min(...values) - 5);
    const W = Math.round(Math.max(300, Math.min(1600, (document.querySelector("main")?.clientWidth || 700) - 64))), Hh = 240, L = 52, R = 8, T = 8, B = 22;
    const x = t => L + (t - t0) / span * (W - L - R), y = v => T + (1 - (v - bottom) / ((top - bottom) || 1)) * (Hh - T - B);
    const gap = (d.step || 60) * 2.5;
    const bands = (m.throttle || []).map(p => `<rect x="${x(Math.max(p.start, t0)).toFixed(1)}" y="${T}" width="${Math.max(1.5, x(Math.min(p.end, t1)) - x(Math.max(p.start, t0))).toFixed(1)}" height="${Hh - T - B}" fill="var(--err-soft)" class="th-band"><title>${esc(`throttled${p.device ? ` (device ${p.device})` : ""}: ${stamp(p.start)} to ${stamp(p.end)}`)}</title></rect>`).join("");
    const paths = lines.map((l, i) => {
      let path = "", dots = "";
      l.points.forEach(([t, v], k) => {
        const before = k > 0 && t - l.points[k - 1][0] <= gap, after = k < l.points.length - 1 && l.points[k + 1][0] - t <= gap;
        path += `${before ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
        if (!before && !after) dots += `<circle cx="${x(t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.2" fill="var(${color(l)})"/>`;
      });
      return `<path d="${path}" fill="none" stroke="var(${color(l)})" stroke-width="1.6"${dash(l)} vector-effect="non-scaling-stroke"/>${dots}`;
    }).join("");
    const marks = limits.flatMap(([k, label, lim]) => [["warn", lim.warn, "--warn"], ["critical", lim.crit, "--err"]].map(([name, v, c]) => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(${c})" stroke-dasharray="6 4"/><text x="${W - R - 2}" y="${(y(v) - 3).toFixed(1)}" text-anchor="end" class="ts-ax th-mark">${esc(`${label} ${name} ${v} °C`)}</text>`)).join("");
    const ticks = [0, .25, .5, .75, 1].map(f => bottom + f * (top - bottom)).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(--grid)"/><text x="${L - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end" class="ts-ax">${esc(`${v.toFixed(0)} °C`)}</text>`).join("");
    const xt = [0, .5, 1].map(f => t0 + f * span).map((t, i) => `<text x="${x(t).toFixed(1)}" y="${Hh - 6}" text-anchor="${["start", "middle", "end"][i]}" class="ts-ax">${esc(when(t, span))}</text>`).join("");
    const legend = lines.map(l => `<span class="ts-key th-sensor" data-sensor="${esc(l.label)}"><svg class="th-swatch" width="18" height="6" aria-hidden="true"><line x1="0" x2="18" y1="3" y2="3" stroke="var(${color(l)})" stroke-width="2"${dash(l)}/></svg>${esc(l.label)} <b>${esc(deg(l.last) || "")}</b></span>`).join("") + (m.throttle?.length ? `<span class="ts-key"><i class="th-key"></i>throttled</span>` : "");
    const title = `${m.id}: temperature (°C, ${view.range === "day" ? "5-minute" : "15-minute"} average)`;
    const rowsHtml = lines.flatMap(l => l.points.map(p => `<tr><td>${esc(l.label)}</td><td>${esc(stamp(p[0]))}</td><td>${esc(deg(p[1]))}</td><td>${esc(deg(p[2]))}</td></tr>`)).join("");
    return `<figure class="ts"><figcaption>${esc(title)}</figcaption><div class="ts-legend">${legend}</div>
<svg viewBox="0 0 ${W} ${Hh}" width="${W}" height="${Hh}" role="img" aria-label="${esc(`${title}: ${lines.length} sensors from ${when(t0, span)} to ${when(t1, span)}; ${lines.map(l => `${l.label} last ${deg(l.last)}, max ${deg(l.max)}`).join("; ")}${m.throttle?.length ? `; ${m.throttle.length} throttle periods` : ""}`)}">${ticks}${bands}${marks}${paths}${xt}</svg>
<details><summary>View as table</summary><div class="table-wrap" tabindex="0" role="region" aria-label="${esc(title)}"><table class="tbl"><thead><tr><th scope="col">Sensor</th><th scope="col">Time</th><th scope="col">Average</th><th scope="col">Max</th></tr></thead><tbody>${rowsHtml}</tbody></table></div></details></figure>`;
  }

  function sensors(m, d) {
    const {esc, table} = H();
    const body = (m.lines || []).map(l => {
      const lim = d.thresholds?.[l.kind];
      const state = lim && l.max >= lim.crit ? `<span class="pill p-err">reached critical</span>` : lim && l.max >= lim.warn ? `<span class="pill p-warn">reached warn</span>` : lim ? `<span class="pill p-ok">below warn</span>` : `<span class="muted">no threshold</span>`;
      return `<tr><td>${esc(l.label)}</td><td>${shown(deg(l.last), "no sample")}</td><td>${shown(deg(l.max), "no sample")}</td><td>${l.warn_buckets == null ? H().unavailable("no threshold set") : esc(`${l.warn_buckets} of ${l.sampled_buckets} sampled (${l.buckets} in range)`)}</td><td>${state}</td></tr>`;
    }).join("");
    const throttle = (m.throttle || []).length ? `<p class="t2">Throttled: ${m.throttle.map(p => esc(`${stamp(p.start)} – ${stamp(p.end)}${p.device ? ` (device ${p.device})` : ""}`)).join("; ")}${m.throttle_more ? esc(`; ${m.throttle_more} earlier periods not listed`) : ""}.</p><p class="hint">${esc(d.notes?.throttle || "")}</p>` : (d.throttle_read === false ? `<p>${H().unavailable(`throttle history · ${d.why?.node_gpu_throttled || "not read"}`)}</p>` : `<p class="hint">No throttle period reported in this range.</p>`);
    return table(["Sensor", "Last", "Max", `${(d.step || 300) / 60}-minute buckets reaching warn`, "Threshold"], body, `${m.id} sensors`) + `<p class="hint">${esc(d.notes?.warn || "")}</p>` + throttle;
  }

  function form(d) {
    const {esc, button} = H();
    const blocker = d.machines === null && d.why?.machines?.includes("not open") ? "metrics store not open in this portal process" : view.busy ? "Saving…" : H().mutationReason();
    const value = (k, which) => view.draft[`${k}.${which}`] ?? (d.thresholds?.[k] ? String(d.thresholds[k][which]) : "");
    const inputs = kinds.map(([k, label]) => `<fieldset class="th-set"><legend>${esc(label)}</legend>${["warn", "crit"].map(w => `<label class="field" for="th-${k}-${w}">${w === "warn" ? "Warn" : "Critical"} (°C) <input class="inp th-inp" id="th-${k}-${w}" inputmode="decimal" data-th="${k}.${w}" value="${esc(value(k, w))}" ${view.error ? 'aria-invalid="true" aria-describedby="th-error"' : ""} ${blocker ? "disabled" : ""}></label>`).join("")}</fieldset>`).join("");
    return `<div class="row th-form">${inputs}</div><div class="row">${button("Save thresholds", "", {style: "pri", disabled: blocker, extra: 'data-page-action="thermals-save"'})}</div><p class="field-error" id="th-error" role="alert">${esc(view.error)}</p><p class="hint">GPU thresholds are required (default 85 / 90 °C); leave CPU or other sensors empty for no threshold. Changes are audited.</p>`;
  }

  (window.HyperloomPages ||= {}).thermals = {
    title: "Thermals",
    group: "Operations",
    order: 41,
    icon: '<path d="M8 3a2 2 0 014 0v8.5a4 4 0 11-4 0z"/><path d="M10 9v5"/>',
    endpoints: () => [endpoint()],
    render() {
      const {esc, card} = H();
      const d = H().state.docs[endpoint()];
      setZone(d);
      const toggle = `<div class="row" role="group" aria-label="Range">${ranges.map(([label, value]) => `<button type="button" class="btn ${value === view.range ? "pri" : ""}" data-page-action="thermals-range" data-range="${value}" aria-pressed="${value === view.range}">${esc(label)}</button>`).join("")}</div>`;
      const head = `<div class="row between">${toggle}<span class="hint">${esc(d.observer || "node sensors")}${d.timezone ? ` · times in ${esc(d.timezone.source === "configured" ? d.timezone.name : "the control machine's local time")} (${esc(d.timezone.offset || "")})` : ""} · thresholds dashed (warn amber, critical red) · throttle periods shaded.</span></div>`;
      const body = d.machines == null ? card("Thermals", `<p>${H().unavailable(d.why?.machines || "no data")}</p>`)
        : !d.machines.length ? card("Thermals", `<p>${H().unavailable(d.why?.machines || "no temperature samples recorded")}</p>`)
        : d.machines.map(m => card(m.id, chart(m, d) + sensors(m, d) + (d.why?.[m.id] ? `<p class="hint">${esc(d.why[m.id])}</p>` : ""))).join("");
      const series = Object.entries(d.why || {}).filter(([k]) => k.startsWith("node_")).map(([k, v]) => `<p>${H().unavailable(`${k} · ${v}`)}</p>`).join("");
      return `<div class="grid">${head}${series}${body}${card("Thresholds", form(d))}</div>`;
    },
  };

  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "thermals-range") { view.range = control.dataset.range; save(); H().refresh(); return; }
    if (action !== "thermals-save") return;
    const read = key => { const raw = (document.querySelector(`[data-th="${key}"]`)?.value || "").trim(); return raw === "" ? null : Number(raw); };
    const body = {};
    for (const [k] of kinds) {
      const warn = read(`${k}.warn`), crit = read(`${k}.crit`);
      body[k] = warn == null && crit == null && k !== "gpu" ? null : {warn, crit};
    }
    const token = H().csrf();
    const blocker = H().mutationReason();
    if (blocker || view.busy) { H().announce(blocker || "Another action is running"); return; }
    view.busy = true; H().state.busy = true; view.error = ""; H().render();
    try {
      if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
      await H().request("energy/thresholds", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(body), signal: AbortSignal.timeout(30000)});
      view.draft = {}; H().announce("Thermal thresholds saved");
    } catch (error) { view.error = error.message; H().announce(error.message); }
    finally {
      // The first refresh may be a poll that started before the write; the second one surely reads after it.
      try { H().state.fresh = false; await H().refresh(); await H().refresh(); }
      finally { view.busy = false; H().state.busy = false; H().render(); }
    }
  });
  document.addEventListener("input", event => { if (event.target.dataset?.th) view.draft[event.target.dataset.th] = event.target.value; });
})();
