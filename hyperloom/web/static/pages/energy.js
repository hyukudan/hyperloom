/* Energy estimate (F18): integrated energy per machine and sensor domain, stacked per hour or day, cost at a
   configured price, energy per 1k generated tokens over covered intervals and the permanent monthly archive.
   Every figure is estimated from sensors, never metered. */
"use strict";
(() => {
  const ranges = [["24 h", "24h"], ["7 d", "7d"], ["30 d", "30d"]];
  const view = {range: "24h", confirmReset: false, draft: {}, error: "", busy: false, before: ""};
  try { const saved = JSON.parse(localStorage.getItem("hyperloom-energy") || "{}"); if (ranges.some(r => r[1] === saved.range)) view.range = saved.range; } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-energy", JSON.stringify({range: view.range})); } catch (_) {} };
  const H = () => window.Hyperloom;
  const endpoint = () => `energy?range=${view.range}`;
  const doc = () => H().state.docs[endpoint()];

  const wh = value => value == null || !Number.isFinite(value) ? null : Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(value >= 100000 ? 0 : 2)} kWh` : `${value.toFixed(value >= 100 ? 0 : 1)} Wh`;
  const watts = value => value == null || !Number.isFinite(value) ? null : `${value.toFixed(value >= 100 ? 0 : 1)} W`;
  const pct = value => value == null || !Number.isFinite(value) ? null : `${Math.round(value * 100)} %`;
  const hours = seconds => seconds >= 3600 ? `${(seconds / 3600).toFixed(seconds >= 36000 ? 0 : 1)} h` : `${Math.round(seconds / 60)} min`;
  const shown = (text, why) => text == null ? H().unavailable(why) : H().esc(text);
  function zoneOpt(d) {
    const name = d?.timezone?.source === "configured" ? d.timezone.name : undefined;
    try { if (name) new Intl.DateTimeFormat(undefined, {timeZone: name}); return name ? {timeZone: name} : {}; } catch (_) { return {}; }
  }
  const when = (t, d, opts = {}) => new Date(t * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...zoneOpt(d), ...opts});
  const colorOf = (d, machine) => `--s${((d.machine_order || []).indexOf(machine) % 5 + 5) % 5 + 1}`;
  const money = (value, currency) => { try { return new Intl.NumberFormat(undefined, {style: "currency", currency}).format(value); } catch (_) { return `${value.toFixed(2)} ${currency}`; } };

  // Bucket labels come from the server in the configured zone: "HH:MM UTC+hh:mm" for hours, an ISO date for days.
  function bucketLabel(b, d) { return d.bucket === "day" ? new Date(`${b.label}T12:00:00Z`).toLocaleDateString(undefined, {month: "short", day: "numeric", timeZone: "UTC"}) : String(b.label || "").split(" ")[0]; }

  function bars(d) {
    const {esc} = H();
    const buckets = d.buckets || [], order = (d.machine_order || []).filter(m => buckets.some(b => b.machines?.[m] != null));
    if (!buckets.some(b => Object.keys(b.machines || {}).length)) return `<p class="muted">No energy integrated in this range (unavailable, not zero).</p>`;
    const W = Math.round(Math.max(300, Math.min(1600, (document.querySelector("main")?.clientWidth || 700) - 64))), Hh = 220, L = 60, R = 8, T = 10, B = 24;
    const totals = buckets.map(b => Object.values(b.machines || {}).reduce((a, v) => a + v, 0));
    const top = Math.max(...totals) * 1.1 || 1;
    const slot = (W - L - R) / buckets.length, bw = Math.max(2, Math.min(48, slot * 0.72));
    const y = v => T + (1 - v / top) * (Hh - T - B);
    let rects = "";
    buckets.forEach((b, i) => {
      let base = 0;
      const x = L + i * slot + (slot - bw) / 2;
      for (const m of order) {
        const v = b.machines?.[m];
        if (v == null) continue;
        const y0 = y(base), y1 = y(base + v);
        rects += `<rect x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0, y0 - y1).toFixed(1)}" fill="var(${colorOf(d, m)})"><title>${esc(`${bucketLabel(b, d)} · ${m}: ${wh(v)}`)}</title></rect>`;
        base += v;
      }
    });
    const grid = [0, .5, 1].map(f => f * top).map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(--grid)"/><text x="${L - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end" class="ts-ax">${esc(wh(v))}</text>`).join("");
    const picks = [0, Math.floor((buckets.length - 1) / 2), buckets.length - 1];
    const xt = picks.map((i, k) => `<text x="${(L + i * slot + slot / 2).toFixed(1)}" y="${Hh - 6}" text-anchor="${["start", "middle", "end"][k]}" class="ts-ax">${esc(bucketLabel(buckets[i], d))}</text>`).join("");
    const legend = order.map(m => `<span class="ts-key"><i style="background:var(${colorOf(d, m)})"></i>${esc(m)}</span>`).join("");
    const head = `<tr><th scope="col">${d.bucket === "day" ? "Day" : "Hour"}</th>${order.map(m => `<th scope="col">${esc(m)}</th>`).join("")}<th scope="col">Total</th><th scope="col">Coverage</th></tr>`;
    const body = buckets.map((b, i) => `<tr><td>${esc(bucketLabel(b, d))}</td>${order.map(m => `<td>${shown(wh(b.machines?.[m]), "no samples")}</td>`).join("")}<td>${Object.keys(b.machines || {}).length ? esc(wh(totals[i])) : H().unavailable("no samples")}</td><td>${shown(pct(b.coverage), "no samples")}</td></tr>`).join("");
    const title = `Energy per ${d.bucket === "day" ? "day" : "hour"}, stacked by machine (Wh, estimated)`;
    return `<figure class="ts en-bars"><figcaption>${esc(title)}</figcaption><div class="ts-legend">${legend}</div>
<svg viewBox="0 0 ${W} ${Hh}" width="${W}" height="${Hh}" role="img" aria-label="${esc(`${title}: ${buckets.length} buckets from ${bucketLabel(buckets[0], d)} to ${bucketLabel(buckets.at(-1), d)}; largest ${wh(Math.max(...totals))}; last ${wh(totals.at(-1)) || "no samples"}`)}">${grid}${rects}${xt}</svg>
<details><summary>View as table</summary><div class="table-wrap" tabindex="0" role="region" aria-label="${esc(title)}"><table class="tbl"><thead>${head}</thead><tbody>${body}</tbody></table></div></details></figure>`;
  }

  function machineCard(m, d) {
    const {esc, card, table} = H();
    const rowsHtml = m.domains.map(x => `<tr><td>${esc(x.label)}<div class="hint">${esc(x.source)}</div></td><td>${x.counted ? `<span class="pill p-ok">added</span>` : `<span class="pill p-off">not added</span><div class="hint">${esc(x.overlap || "")}</div>`}</td><td>${shown(watts(x.latest_w), "no fresh sample")}</td><td>${shown(watts(x.avg_w), "no samples")}</td><td>${shown(wh(x.wh), "no samples")}</td><td>${shown(pct(x.coverage), "no samples")}</td></tr>`).join("");
    const body = m.domains.length ? table(["Domain", "In total", "Now", "Average", "Energy", "Coverage"], rowsHtml, `${m.id} power domains`) : `<p>${H().unavailable(m.why?.wh || "no power reading recorded")}</p>`;
    const head = `<div class="en-mhead"><span class="ts-key"><i style="background:var(${colorOf(d, m.id)})"></i></span><b>${shown(wh(m.wh), m.why?.wh || "no power reading")}</b><span class="muted">coverage ${shown(pct(m.coverage), "no samples")} · average ${shown(watts(m.avg_w), "no samples")}</span></div>`;
    const partial = m.partial?.length && m.wh != null ? `<p class="hint">${esc(m.why?.wh || "")}</p>` : "";
    return card(m.id, head + partial + body + `<p class="hint">Not covered: ${esc(m.uncovered.join(", "))}.</p>`);
  }

  function archive(d) {
    const {esc, table} = H();
    if (!d.archive?.length) return `<p class="muted">No month archived yet. Each quarter hour is archived 15 minutes after it ends.</p>`;
    const order = d.machine_order || [];
    const machines = order.filter(m => d.archive.some(a => a.machines?.[m] != null));
    const body = d.archive.map(a => `<tr><td>${esc(a.month)}${a.open ? ` <span class="tag">open</span>` : ""}</td>${machines.map(m => `<td>${shown(wh(a.machines?.[m]), "no samples")}</td>`).join("")}<td>${esc(wh(a.wh))}</td><td>${a.cost == null ? H().unavailable(a.cost_why || "no price") : esc(money(a.cost, a.currency)) + (a.priced_wh < a.wh ? `<div class="hint">covers ${esc(wh(a.priced_wh))}</div>` : "")}</td><td>${a.wh_per_1k_tokens == null ? H().unavailable("no interval with both observed") : esc(`${a.wh_per_1k_tokens.toFixed(2)} Wh`)}</td></tr>`).join("");
    return table(["Month", ...machines, "Total", "Estimated cost", "Per 1k generated tokens"], body, "Monthly energy archive") + `<p class="hint">Permanent: a reset never removes archived months. Months follow the time zone configured when each quarter hour was archived.</p>`;
  }

  function older(d) {
    const {esc, button, table} = H();
    if (!d.archive_more && !view.before) return "";
    const page = view.before ? H().state.docs[`energy/archive?before=${view.before}`] : null;
    const rowsHtml = (page?.months || []).map(a => `<tr><td>${esc(a.month)}</td><td>${esc(wh(a.wh))}</td><td>${a.cost == null ? H().unavailable(a.cost_why || "no price") : esc(money(a.cost, a.currency))}</td><td>${esc(a.zone || "")}</td></tr>`).join("");
    const listing = view.before ? (page ? table(["Month", "Energy", "Estimated cost", "Zone"], rowsHtml, "Older archived months") : `<p class="muted">Loading older months…</p>`) : "";
    const next = page?.more && page.months?.length ? button("Older months", "", {extra: `data-page-action="energy-older" data-before="${esc(page.months.at(-1).month)}"`}) : "";
    const first = !view.before && d.archive?.length ? button("Older months", "", {extra: `data-page-action="energy-older" data-before="${esc(d.archive.at(-1).month)}"`}) : "";
    return `${listing}<div class="row">${first}${next}${view.before ? button("Newest months only", "", {extra: 'data-page-action="energy-older" data-before=""'}) : ""}</div>`;
  }

  function priceForm(d) {
    const {esc, button} = H();
    const blocker = gate(d, "Saving…");
    const price = view.draft.price ?? (d.price ? String(d.price.price_per_kwh) : "");
    const currency = view.draft.currency ?? (d.price?.currency || "EUR");
    return `<div class="row en-form"><label class="field" for="energy-price">Price per kWh <input class="inp" id="energy-price" inputmode="decimal" value="${esc(price)}" ${blocker ? "disabled" : ""}></label>
<label class="field" for="energy-currency">Currency <input class="inp en-cur" id="energy-currency" maxlength="3" value="${esc(currency)}" ${blocker ? "disabled" : ""}></label>
${button("Save price", "", {style: "pri", disabled: blocker, extra: 'data-page-action="energy-price-save"'})}${d.price ? button("Clear price", "", {disabled: blocker, extra: 'data-page-action="energy-price-clear"'}) : ""}</div>
<p class="field-error" role="alert">${esc(view.error)}</p><p class="hint">${d.price ? `Current: ${esc(money(d.price.price_per_kwh, d.price.currency))} per kWh. ` : "No price configured: costs are unavailable. "}Changes are audited; archived months keep the price they were archived at.</p>`;
  }

  function gate(d, working) {
    if (!d.store) return "metrics store not open in this portal process";
    if (view.busy) return working;
    return H().mutationReason();          // the shell's gate: authoritative state, no other action running
  }

  function resetForm(d) {
    const {button} = H();
    const blocker = gate(d, "Working…");
    const controls = view.confirmReset
      ? `${button("Confirm reset", "", {style: "dan", disabled: blocker, extra: 'data-page-action="energy-reset-confirm"'})}${button("Keep history", "", {extra: 'data-page-action="energy-reset-cancel"'})}`
      : button("Reset energy history…", "", {style: "dan", disabled: blocker, extra: 'data-page-action="energy-reset"'});
    return `<p class="t2">Archives every quarter hour not yet archived, then clears the energy rows shown above; afterwards only samples taken after the reset are integrated. The monthly archive is kept. Audited.</p>${view.confirmReset ? `<p class="t2" role="alert">Confirm with Space or a click; Enter does not confirm.</p>` : ""}<div class="row">${controls}</div>`;
  }

  (window.HyperloomPages ||= {}).energy = {
    title: "Energy",
    group: "Operations",
    order: 40,
    icon: '<path d="M11 2L4 11h5l-1 7 7-9h-5z"/>',
    endpoints: () => [endpoint(), ...(view.before ? [`energy/archive?before=${view.before}`] : [])],
    ready: () => !!doc() && (!view.before || !!H().state.docs[`energy/archive?before=${view.before}`]),
    render() {
      const {esc, card, kpi} = H();
      const d = doc();
      const banner = `<div class="banner info" role="note"><b>${esc(d.banner || "Estimated from sensors, not metered")}.</b><span class="t2">Totals add only disjoint sensor domains and integrate only sampled time; board, fans and disks are never covered.</span></div>`;
      const toggle = `<div class="row" role="group" aria-label="Range">${ranges.map(([label, value]) => `<button type="button" class="btn ${value === view.range ? "pri" : ""}" data-page-action="energy-range" data-range="${value}" aria-pressed="${value === view.range}">${esc(label)}</button>`).join("")}</div>`;
      if (d.machines == null) return `<div class="grid">${banner}${card("Energy", `<div class="row between">${toggle}</div><p>${H().unavailable(d.why?.machines || "no data")}</p>`)}${card("Energy price", priceForm(d))}</div>`;
      const t = d.totals, eff = d.efficiency, cost = d.cost;
      const kpis = `<div class="grid en-kpis">${[
        kpi("Energy (estimated)", shown(t && wh(t.wh), d.why?.totals), t ? `integrated over sampled time${t.since > (d.buckets?.[0]?.start || 0) && d.reset_at ? ` since reset ${esc(t.since_label)}` : ""}${t.partial?.length ? ` · partial: ${esc(t.partial.join(", "))}` : ""}` : ""),
        kpi("Average power", shown(t && watts(t.avg_w), d.why?.totals || d.why?.avg_w || "no samples"), t?.avg_partial?.length ? esc(d.why?.avg_w || "") : "sum of each domain's average over its sampled time"),
        kpi("Peak hour", shown(t?.peak && wh(t.peak.wh), "no samples"), t?.peak ? esc(`starts ${t.peak.label}`) : ""),
        kpi("Per 1k generated tokens", shown(eff && `${eff.wh_per_1k_tokens.toFixed(2)} Wh`, d.why?.efficiency), eff ? esc(`over ${hours(eff.both_s)} with power and tokens both observed (lab-wide)`) : ""),
        kpi("Estimated cost", shown(cost && money(cost.value, cost.currency), d.why?.cost), cost ? esc(`at ${money(cost.price_per_kwh, cost.currency)} per kWh`) : ""),
        kpi("Coverage", shown(t && pct(t.coverage), d.why?.totals || "no samples"), "sampled share of the range over every expected power domain"),
      ].join("")}</div>`;
      const zone = d.timezone ? `Times in ${esc(d.timezone.source === "configured" ? d.timezone.name : "the control machine's local time")} (${esc(d.timezone.offset || "")})${d.timezone.why ? ` · ${esc(d.timezone.why)}` : ""}.` : "";
      const collector = d.collector ? (d.collector.error ? `Collector error: ${esc(d.collector.error)}.` : d.collector.last_ok ? `Recomputed ${esc(when(d.collector.last_ok, d))}.` : "Collector has not completed a run yet.") : "Collector not running in this process; figures are the stored hourly rows.";
      const rule = eff ? `<p class="hint">Energy per 1k generated tokens counts ${esc(eff.rule)}.</p>` : "";
      return `<div class="grid">${banner}<div class="row between">${toggle}<span class="hint">${zone} ${collector}</span></div>${kpis}${rule}
${card("Energy over time", bars(d), d.bucket === "day" ? "per day" : "per hour")}
<div class="grid en-machines">${(d.machines || []).map(m => machineCard(m, d)).join("")}</div>
${card("Monthly archive", archive(d) + older(d))}
<div class="grid g2">${card("Energy price", priceForm(d))}${card("Reset", resetForm(d))}</div></div>`;
    },
  };

  async function post(action, body) {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(body), signal: AbortSignal.timeout(30000)});
  }
  async function act(action, body, done) {
    view.busy = true; view.error = ""; H().render();
    try { await post(action, body); done(); H().announce(`${action.replace("energy/", "Energy ")} saved`); }
    catch (error) { view.error = error.message; H().announce(error.message); }
    finally { view.busy = false; await H().refresh(); }
  }

  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "energy-range") { view.range = control.dataset.range; save(); H().refresh(); }
    else if (action === "energy-older") { view.before = control.dataset.before || ""; H().refresh(); }
    else if (action === "energy-price-save") {
      const price = Number(document.getElementById("energy-price")?.value);
      const currency = (document.getElementById("energy-currency")?.value || "").trim().toUpperCase();
      act("energy/price", {price_per_kwh: price, currency}, () => { view.draft = {}; });
    } else if (action === "energy-price-clear") act("energy/price", {clear: true}, () => { view.draft = {}; });
    else if (action === "energy-reset") { view.confirmReset = true; H().render(); document.querySelector('[data-page-action="energy-reset-cancel"]')?.focus(); }
    else if (action === "energy-reset-cancel") { view.confirmReset = false; H().render(); }
    else if (action === "energy-reset-confirm") act("energy/reset", {confirm: "reset"}, () => { view.confirmReset = false; });
  });
  // Enter never confirms a destructive action (shell rule); Space or a click does.
  document.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.target.closest?.('[data-page-action="energy-reset-confirm"]')) event.preventDefault();
  });
  document.addEventListener("input", event => {
    if (event.target.id === "energy-price") view.draft.price = event.target.value;
    if (event.target.id === "energy-currency") view.draft.currency = event.target.value;
  });
})();
