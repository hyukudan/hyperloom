/* USB4 links (F14): trained link rate per direction against the cable's full rate, stream state (published
   layout and live checks), host-interface interrupts, kernel evidence of this boot and the engine-measured
   exchange per step. Everything comes from the usb4 collector's stored state; the page never asks a node. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const TS = () => window.HyperloomTS;
  const RANGE = 86400;
  const doc = () => H().state.docs.usb4;
  const ids = () => (doc()?.machines || []).map(m => m.id);
  const history = id => TS().endpoint("usb4_link_gbps", {range: RANGE, labels: {machine: id}});
  const num = v => typeof v === "number" && Number.isFinite(v);
  const ago = (now, t) => !num(t) ? "never" : now - t < 90 ? `${Math.max(0, Math.round(now - t))} s ago` : now - t < 5400 ? `${Math.round((now - t) / 60)} min ago` : `${(Math.round((now - t) / 360) / 10)} h ago`;
  const gbps = v => num(v) ? `${Number(v.toFixed(1))} Gb/s` : null;
  const when = t => num(t) ? new Date(t * 1000).toLocaleString() : null;
  let refreshing = "";

  function un(why) { return H().unavailable(why); }

  function direction(d, why) {
    const {esc} = H();
    if (!d || !num(d.gbps)) return un(why || `sysfs reports ${d?.raw_speed ?? "no speed"} × ${d?.raw_lanes ?? "no lanes"}`);
    return `${esc(Number(d.gbps_per_lane.toFixed(1)))} Gb/s × ${esc(d.lanes)} = <b>${esc(gbps(d.gbps))}</b>`;
  }

  function linkState(link, full) {
    const {statusPill, esc} = H();
    if (link.full_rate === true) return statusPill("full rate");
    if (link.full_rate === false) {
      const parts = (link.below_full_rate || []).map(dir => `${dir} ${gbps(link[dir]?.gbps) || "?"}`).join(", ");
      return `<span class="pill p-warn">below full rate</span> <span class="hint">${esc(parts)} of ${esc(gbps(full.gbps))} (${esc(full.gbps_per_lane)} Gb/s × ${esc(full.lanes)})</span>`;
    }
    return un("trained rate unreadable");
  }

  function changed(link, now) {
    const c = link.changed;
    if (!c) return `<span class="hint">no change seen this boot</span>`;
    const {esc} = H();
    return `<span class="hint">${esc(ago(now, c.at))}: rx ${esc(gbps(c.from.rx) || "?")} → ${esc(gbps(c.to.rx) || "?")}, tx ${esc(gbps(c.from.tx) || "?")} → ${esc(gbps(c.to.tx) || "?")}</span>`;
  }

  function linksTable(m, s, d, now) {
    const {table, cells, esc} = H();
    if (!s.links) return `<p>${un(s.links_why)}</p>`;
    if (!s.links.length) return `<p>${un(s.links_why || "no trained link")}</p>`;
    return table(["Router", "Peer", "RX", "TX", "Trained rate", "Change"], s.links.map(link => cells([
      `<span class="mono">${esc(link.router)}</span><br><span class="hint">domain ${esc(link.domain)} · ${esc(link.nhi || "NHI unknown")}</span>`,
      link.peer ? esc(link.peer) : un("peer name not reported"), direction(link.rx), direction(link.tx),
      linkState(link, d.full_rate), changed(link, now)])).join(""), `${m.id} USB4 links`);
  }

  function flag(value, yes, no, why) {
    if (value === true) return `<span class="pill p-ok">${H().esc(yes)}</span>`;
    if (value === false) return `<span class="pill p-warn">${H().esc(no)}</span>`;
    return un(why);
  }

  function mode(config) {
    const {tag} = H();
    if (!config || !Object.keys(config).length) return un("mode not in the published layout");
    const known = key => typeof config[key] === "number";
    const tags = [known("busy_poll") ? (config.busy_poll ? "busy poll" : "interrupts") : "poll mode not published",
      config.rx_busy_poll ? "rx busy poll" : "",
      known("gpu_direct") ? (config.gpu_direct ? "GPU direct" : "host memory") : "memory path not published",
      config.gpu_poll > 0 ? "GPU poll" : "",
      num(config.ring_size) ? `ring ${config.ring_size}` : "", num(config.throttling) ? `throttling ${config.throttling}` : "",
      config.gpu_quarantine ? "GPU quarantine" : ""].filter(Boolean);
    return tags.map(tag).join(" ");
  }

  function streamsTable(m, s, now) {
    const {table, cells, esc} = H();
    const layout = s.layout || {};
    if (layout.status !== "ok") return `<p>Streams: ${un(layout.why)}</p>`;
    const head = `<p class="hint">Published layout generated ${esc(layout.generated_at || "at an unknown time")} (file written ${esc(ago(now, layout.modified_at))}); “openable” is derived by the layout tool, never by opening a device. Live checks: device node present and service driver binding.</p>`;
    const rows = (s.streams || []).map(st => {
      const p = st.published || {}, live = st.live || {};
      return cells([`<span class="mono">${esc(st.id)}</span><br><span class="hint">${esc(st.device || "no device")} · HopID ${esc(st.in_hopid ?? "?")}/${esc(st.out_hopid ?? "?")}${st.cable ? ` · cable ${esc(st.cable)}` : ""}</span>`,
        `${flag(p.exists, "exists", "missing", "not published")} ${flag(p.attached, "attached", "detached", "not published")} ${flag(p.openable, "openable", "not openable", "not published")}${p.openable_reason ? `<br><span class="hint">${esc(p.openable_reason)}</span>` : ""}`,
        `${flag(live.device_present, "device present", "device missing", live.device_why || (layout.live?.why) || "not checked")} ${flag(live.attached_now, "attached now", "not attached now", layout.live?.why || "not checked")}${live.attached_why ? `<br><span class="hint">${esc(live.attached_why)}</span>` : ""}`,
        mode(st.config)]);
    }).join("");
    const warnings = (layout.warnings || []).map(w => `<p class="hint">Layout note · ${esc(w)}</p>`).join("");
    return head + table(["Stream", "Published", "Live", "Configured mode"], rows, `${m.id} USB4 streams`) + warnings;
  }

  function cablesNote(s) {
    const {esc} = H();
    return (s.cables || []).filter(c => c.layout_differs).map(c => `<p class="hint">Cable ${esc(c.id)}: the layout recorded ${esc(gbps(c.gbps))}; the link trains at ${esc(gbps(c.live_gbps))} now.</p>`).join("");
  }

  function interruptsTable(m, s) {
    const {table, cells, esc} = H();
    const part = s.interrupts || {};
    if (part.status !== "ok") return `<p>Interrupts: ${un(part.why)}</p>`;
    const rows = (part.nhis || []).map(r => cells([`<span class="mono">${esc(r.nhi)}</span>`, esc(r.domain ?? "?"), esc(r.ring),
      esc(r.vectors), esc(Number(r.count).toLocaleString()),
      num(r.rate_per_s) ? `<b>${esc(Math.round(r.rate_per_s).toLocaleString())}</b> interrupts/s${r.uncertain ? ` <span class="hint">${esc(r.why)}</span>` : ""}` : un(r.why)])).join("");
    return table(["Host interface", "Domain", "Rings", "Vectors", "Interrupts since boot", "Rate"], rows, `${m.id} ring interrupts`)
      + `<p class="hint">Observer: node · /proc/interrupts, per host interface (control vectors 0–1, data rings after them). Busy-poll streams hold rings without interrupts, so 0 interrupts/s does not mean idle; interrupts cannot be attributed to a single stream.</p>`;
  }

  function evidence(m, d, now) {
    const {table, cells, esc} = H();
    const j = m.journal;
    if (!j) return `<p>Kernel evidence: ${un("kernel journal not read yet (read every 60 s)")}</p>`;
    if (j.status !== "ok" && !j.totals) return `<p>Kernel evidence: ${un(j.why)}</p>`;
    const stale = j.status !== "ok" ? `<p class="banner warn">Last journal read failed ${esc(ago(now, j.attempted_at))}: ${esc(j.why || "unknown")}. Totals below are from the last good read.</p>` : "";
    const kinds = Object.entries(d.kinds || {}).map(([kind, label]) => cells([esc(label), `<b>${esc((j.totals || {})[kind] ?? 0)}</b>`])).join("");
    const lines = (j.recent || []).slice(-8).reverse().map(r => `<li><span class="hint">${esc(when(r.ts) || "time unknown")}</span> · ${esc(r.message)}</li>`).join("");
    const scope = j.complete ? "complete for this boot" : `lower bounds · ${j.incomplete_why || "scan incomplete"}`;
    return stale + table(["Kernel evidence this boot", "Lines"], kinds, `${m.id} kernel evidence`)
      + `<p class="hint">Read ${esc(ago(now, j.read_at))} with a cursor per boot (${esc(scope)}); last new line ${esc(ago(now, j.last_new_at))}.</p>`
      + (lines ? `<details><summary>Latest lines</summary><ul class="u4-lines">${lines}</ul></details>` : "");
  }

  function parameters(s) {
    const {esc} = H();
    const p = s.parameters || {};
    if (p.status !== "ok") return `<p class="hint">Module parameters: ${un(p.why)}</p>`;
    const blocks = Object.entries(p.modules || {}).filter(([, v]) => v && typeof v === "object").map(([module, v]) => v.status !== "ok"
      ? `<dt>${esc(module)}</dt><dd>${un(v.why)}</dd>`
      : `<dt>${esc(module)}</dt><dd>${Object.entries(v.values || {}).map(([k, val]) => `<span class="tag">${esc(k)} = ${esc(val ?? "unreadable")}</span>`).join(" ")}${(v.write_only || []).length ? ` <span class="hint">write-only (not read): ${esc(v.write_only.join(", "))}</span>` : ""}${(v.unreadable || []).length ? ` <span class="hint">unreadable: ${esc(v.unreadable.join(", "))}</span>` : ""}</dd>`).join("");
    return blocks ? `<details><summary>Module parameters</summary><dl class="kv">${blocks}</dl></details>` : "";
  }

  function sources(m, now) {
    const {esc} = H();
    return `<div class="row u4-sources">${Object.entries(m.sources || {}).map(([name, row]) => {
      const stale = m.stale?.[name];
      const style = row.status === "ok" && !stale ? "p-ok" : stale ? "p-warn" : "p-off";
      const label = `${name} · ${row.status === "ok" && !stale ? "ok" : stale ? "stale" : "unavailable"}`;
      return `<span class="pill ${style}" title="${esc(row.why || `last success ${ago(now, row.last_ok)}`)}">${esc(label)}</span>`;
    }).join("")}</div>`;
  }

  function chart(m) {
    const h = H().state.docs[history(m.id)];
    if (!h) return `<p>${un("loading link history")}</p>`;
    if (h.lines == null) return `<p>${un(h.why?.lines)}</p>`;
    // A colour per link identity (domain of the router, direction), independent of which lines are present.
    const colour = labels => { const domain = parseInt(String(labels.router).split("-")[0], 10) || 0; return TS().colors[(domain * 2 + (labels.direction === "tx" ? 1 : 0)) % TS().colors.length]; };
    const lines = TS().lines(h, {label: l => `${l.router} ${l.direction}`}).map((line, i) => ({...line, color: colour(h.lines[i].labels)}));
    return TS().chart({title: `${m.id} · trained link rate (Gb/s) · last 24 h`, unit: "Gb/s", lines, step: h.step || 10,
      cadence: h.tier === "raw" ? h.interval_s || 10 : 0, start: h.start, end: h.end, min: 0, max: 45, group: "usb4", height: 160})
      + `<p class="hint">A step down is a retrain to a lower rate; gaps are periods without samples. rx and tx of a link overlap when they are equal.</p>`;
  }

  function section(m, names, html, now) {
    const stale = names.filter(name => m.stale?.[name]);
    if (!stale.length) return html;
    const last = Math.min(...stale.map(name => m.sources?.[name]?.last_ok).filter(num));
    return `<div class="u4-stale"><p class="hint"><span class="pill p-warn">stale</span> ${H().esc(stale.join(", "))} · values as of ${H().esc(num(last) ? when(last) : "no successful collection")}</p>${html}</div>`;
  }

  function machineCard(m, d, now) {
    const {card, esc} = H();
    const s = m.summary;
    if (!s) return card(m.id, `<p>${un(m.why?.summary || m.error)}</p>${sources(m, now)}`);
    const error = m.error ? `<p class="banner warn">Last collection failed ${esc(ago(now, m.error_at))}: ${esc(m.error)}</p>` : "";
    return card(m.id, error + sources(m, now) + section(m, ["links"], linksTable(m, s, d, now) + cablesNote(s), now) + chart(m)
      + section(m, ["layout", "live"], streamsTable(m, s, now), now) + section(m, ["interrupts"], interruptsTable(m, s), now)
      + section(m, ["journal"], evidence(m, d, now), now) + section(m, ["parameters"], parameters(s), now),
      `kernel ${s.kernel || "unknown"} · collected ${ago(now, s.collected_at)}`);
  }

  function exchange(d) {
    const {card, table, cells, esc} = H();
    const rows = (d.exchange || []).map(e => {
      const w = e.window || {}, life = e.lifetime || {};
      const asOf = e.stale ? `<br><span class="pill p-warn">stale</span> <span class="hint">as of ${esc(when(e.observed_at) || "unknown")}</span>` : "";
      const us = v => num(v) ? `${(v * 1e6).toFixed(0)} µs` : null;
      const bytes = v => num(v) ? TS().fmt(v, "bytes") : null;
      const why = key => e.why?.[key] || e.why?.window || "no window";
      const row = cells([`<span class="mono">${esc(e.engine)}</span><br><span class="hint">${esc((e.machines || []).join(", ") || `profile ${e.profile || "unknown"}`)} · ${esc(e.group?.transport || "transport unknown")} · ${esc(e.observer)}</span>${asOf}`,
        us(w.seconds_per_step) ? `<b>${esc(us(w.seconds_per_step))}</b>` : un(why("seconds_per_step")),
        bytes(w.bytes_per_step) ? esc(bytes(w.bytes_per_step)) : un(why("bytes_per_step")),
        us(w.seconds_per_exchange) ? esc(us(w.seconds_per_exchange)) : un(why("seconds_per_exchange")),
        num(w.span_s) ? `${esc(Math.round(w.span_s))} s · ${esc(w.steps ?? "?")} steps` : un(why("window")),
        us(life.seconds_per_exchange) ? `${esc(us(life.seconds_per_exchange))} · ${esc(bytes(life.bytes_per_exchange) || "?")}` : un(e.why?.group || "no group counters")]);
      return e.stale ? row.replace("<tr>", '<tr class="u4-stale-row">') : row;
    }).join("");
    const body = rows ? table(["Engine group", "Exchange time per step", "Bytes per step", "Per exchange", "Window", "Since the group formed (per exchange)"], rows, "Engine exchange")
      : `<p>${un(d.why?.exchange)}</p>`;
    const series = (d.exchange_series || []).length ? table(["Series labels", "Seconds per step", "Sampled"], d.exchange_series.map(r => cells([esc(Object.entries(r.labels).map(([k, v]) => `${k}=${v}`).join(" ")), esc(`${(r.seconds_per_step * 1e6).toFixed(0)} µs`), esc(when(r.ts))])).join(""), "Exchange series") : "";
    return card("Engine-measured exchange", body + series + `<p class="hint">Observer: engine (measured, group counters and batch steps between two observations of the same group). Bytes are what this rank sent.</p>`);
  }

  (window.HyperloomPages ||= {}).usb4 = {
    title: "USB4 links",
    group: "Operations",
    order: 40,
    icon: '<path d="M3 10h4M13 10h4"/><rect x="7" y="7" width="6" height="6" rx="1.5"/><path d="M5 6v8M15 6v8"/>',
    endpoints: () => ["usb4", ...ids().map(history)],
    ready: () => !!doc() && ids().every(id => !!H().state.docs[history(id)]),
    render() {
      const d = doc();
      const {esc} = H();
      const now = d.server_time;
      const none = !(d.machines || []).length ? H().empty("No Linux nodes", "USB4 links are read from the Linux nodes of the configuration.") : "";
      const top = d.why?.machines ? `<p class="banner warn">${esc(d.why.machines)}</p>` : "";
      const collector = d.collector ? `<p class="hint">Collector usb4 · every ${esc(d.collector.interval_s)} s · last success ${esc(ago(now, d.collector.last_ok))}${d.collector.error ? ` · last error: ${esc(d.collector.error)}` : ""}. Kernel journal read every 60 s. Full rate: ${esc(d.full_rate.gbps_per_lane)} Gb/s × ${esc(d.full_rate.lanes)} lanes = ${esc(d.full_rate.gbps)} Gb/s per direction.</p>` : "";
      return `<div class="grid u4">${top}${collector}${none}${(d.machines || []).map(m => machineCard(m, d, now)).join("")}${exchange(d)}</div>`;
    },
    actions() {
      const d = doc() || {};
      const token = H().csrf();
      const blocker = refreshing || (!d.samplers ? "Samplers are not running in this portal process" : !token || token === "__HYPERLOOM_CSRF__" ? "Launch the portal through hyperloom ui to enable actions" : "");
      return H().button("Collect now", "", {disabled: blocker, extra: 'data-page-action="usb4-refresh"'});
    },
    after(main) { TS().bind(main); },
  };

  document.addEventListener("click", async event => {
    const control = event.target.closest('[data-page-action="usb4-refresh"]');
    if (!control || control.disabled) return;
    refreshing = "Collection requested";
    H().render();
    try {
      await H().request("usb4/refresh", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf()}, body: "{}"});
      H().announce("USB4 collection requested");
      setTimeout(() => { refreshing = ""; H().refresh(); }, 2500);
    } catch (error) { refreshing = ""; H().announce(error.message); H().render(); }
  });
})();
