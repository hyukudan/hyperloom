/* Machine detail (F2, F3): #/machine?id=<machine>. GPU hero, history (30 min / 1 h / 8 h), memory domains with the
   limiting one named and the engines' own memory reports, GPU processes, CPU, network, disks, engines and events.
   On a machine with several cards: a Combined / per-card switch. Without an id the first machine is shown. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const TS = () => window.HyperloomTS;
  const ranges = [["30 min", 1800], ["1 h", 3600], ["8 h", 28800]];
  const view = {range: 3600, mode: "cards", hiddenIfaces: {}};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-machine") || "{}")); } catch (_) { /* storage may be disabled */ }
  if (!ranges.some(([, s]) => s === view.range)) view.range = 3600;
  const save = () => { try { localStorage.setItem("hyperloom-machine", JSON.stringify(view)); } catch (_) {} };
  const num = v => typeof v === "number" && Number.isFinite(v);
  const fmt = (v, unit) => TS().fmt(v, unit);
  const colors = ["--s1", "--s2", "--s3", "--s4", "--s5"];
  function machineId() {
    const query = new URLSearchParams(location.hash.split("?")[1] || "");
    const wanted = query.get("id");
    if (wanted && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(wanted)) return wanted;
    return H().machines()[0]?.id || H().state.docs.overview?.machines?.[0]?.id || "";
  }
  const docEndpoint = () => machineId() ? `machines/${machineId()}` : null;
  const history = () => {
    const id = machineId();
    if (!id) return {};
    const series = ["node_gpu_busy_percent", "node_gpu_power_watts", "node_gpu_power_limit_watts", "node_gpu_temp_celsius", "node_cpu_busy_percent"];
    return Object.fromEntries(series.map(s => [s, TS().endpoint(s, {range: view.range, labels: {machine: id}})]));
  };
  const v = (value, unit, digits = 0, why = "not reported") => num(value) ? H().esc(`${value.toFixed(digits)}${unit ? " " + unit : ""}`) : H().unavailable(why);
  const levelPill = level => `<span class="pill p-${{ok: "ok", amber: "warn", red: "err", unknown: "off", info: "info"}[level] || "off"}">${H().esc({ok: "ok", amber: "amber", red: "red", unknown: "level unknown", info: "no threshold"}[level] || level)}</span>`;
  function tempPill(c) {
    if (!num(c)) return H().unavailable("no temperature sensor");
    const style = c >= 90 ? "err" : c >= 85 ? "warn" : "ok";
    return `<span class="pill p-${style}">${H().esc(c.toFixed(0))} °C${style === "err" ? " · hot" : style === "warn" ? " · warm" : ""}</span>`;
  }
  function ring(percent, label) {
    const r = 34, c = 2 * Math.PI * r, known = num(percent), p = known ? Math.max(0, Math.min(100, percent)) : 0;
    return `<svg class="mc-ring" viewBox="0 0 84 84" role="img" aria-label="${H().esc(label)}: ${known ? p.toFixed(0) + " %" : "unavailable"}"><circle cx="42" cy="42" r="${r}" fill="none" stroke="var(--border-strong)" stroke-width="9"/>${known ? `<circle cx="42" cy="42" r="${r}" fill="none" stroke="var(--accent)" stroke-width="9" stroke-dasharray="${(c * p / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 42 42)"/>` : ""}<text x="42" y="47" text-anchor="middle" class="mc-ring-t">${known ? H().esc(p.toFixed(0) + "%") : "n/a"}</text></svg>`;
  }
  function meterBar(value, total, cls = "") {
    return `<div class="meter" aria-hidden="true">${num(value) && num(total) && total > 0 ? `<i class="${cls}" data-meter="${Math.min(100, Math.max(0, value / total * 100))}"></i>` : ""}</div>`;
  }
  function hero(g) {
    const {esc, unavailable} = H();
    const temp = Math.max(...(g.temperatures || []).map(t => t.celsius).filter(num), -Infinity);
    const reasons = (g.throttle_reasons || []).join(", ");
    return `<div class="mc-hero"><div>${ring(g.busy_percent, `${g.name || g.device} busy`)}<div class="hint">busy · ${esc(g.observer || "")}</div></div>
      <div class="mc-hero-kv"><b>${esc(g.name || g.device)}</b>${g.excluded ? ' <span class="tag">excluded by policy</span>' : ""}
      <div class="row">Temperature ${num(temp) ? tempPill(temp) : unavailable(g.why?.temperature_c || "no temperature sensor")}</div>
      <div class="mrow"><span>Power${g.power_sensor ? ` · ${esc(g.power_sensor)}` : ""}</span><span class="v">${v(g.power_w, "W", 0, g.why?.power_w)} / ${v(g.power_limit_w, "W", 0, g.why?.power_limit_w || "no readable limit")}</span>${meterBar(g.power_w, g.power_limit_w)}</div>
      <div>Clock ${v(g.clock_mhz, "MHz", 0, g.why?.clock_mhz)}${g.pstate ? ` · ${esc(g.pstate)}` : ""}${g.pcie && num(g.pcie.gen) ? ` · PCIe gen ${esc(g.pcie.gen)} x${esc(g.pcie.width)}` : ""}</div>
      <div>Throttle ${g.throttle ? `<span class="pill ${g.throttle === "none" ? "p-ok" : g.throttle === "thermal" ? "p-err" : "p-warn"}">${esc(g.throttle)}</span>${reasons ? ` <span class="hint">${esc(reasons)}</span>` : ""}` : unavailable(g.why?.throttle || "not reported")}</div></div></div>`;
  }
  function combined(gpus) {
    const busy = gpus.map(g => g.busy_percent).filter(num), power = gpus.map(g => g.power_w).filter(num), limit = gpus.map(g => g.power_limit_w).filter(num);
    const temps = gpus.flatMap(g => (g.temperatures || []).map(t => t.celsius)).filter(num);
    return hero({name: `Combined · ${gpus.length} cards`, device: "combined", observer: "mean busy, summed power, hottest card",
      busy_percent: busy.length === gpus.length ? busy.reduce((a, b) => a + b, 0) / busy.length : null,
      power_w: power.length === gpus.length ? power.reduce((a, b) => a + b, 0) : null,
      power_limit_w: limit.length === gpus.length ? limit.reduce((a, b) => a + b, 0) : null,
      temperatures: temps.length ? [{celsius: Math.max(...temps)}] : [], throttle: gpus.some(g => g.throttle === "thermal") ? "thermal" : gpus.every(g => g.throttle === "none") ? "none" : null,
      why: {throttle: "per-card states differ; see each card", power_limit_w: "a card has no readable limit", power_w: "a card has no power sensor", clock_mhz: "per card only"}});
  }
  function gpuBlock(d) {
    const {card, unavailable, esc} = H(), gpus = d.gpus || [];
    if (!gpus.length) return card("GPU", `<p>${unavailable(d.why?.gpus)}</p>`);
    const switcher = gpus.length > 1 ? `<fieldset class="seg mc-seg"><legend>GPU view</legend>${[["combined", "Combined"], ["cards", "Per card"]].map(([k, l]) => `<label><input type="radio" name="mc-mode" value="${k}" data-page-action="mc-mode"${view.mode === k ? " checked" : ""}>${esc(l)}</label>`).join("")}</fieldset>` : "";
    const body = gpus.length > 1 && view.mode === "combined" ? combined(gpus) : gpus.map(hero).join("");
    return card("GPU", switcher + `<div class="mc-heroes">${body}</div>`, gpus.length > 1 ? "one block per card" : "");
  }
  function lineSet(doc, label, color) {
    return TS().lines(doc, {label}).map((line, i) => ({...line, color: color(line, i)}));
  }
  function charts(d) {
    const {card, esc} = H(), docs = H().state.docs, ep = history();
    const get = name => docs[ep[name]];
    const devices = (d.gpus || []).map(g => g.device);
    const color = labels => colors[Math.max(0, devices.indexOf(labels.device)) % colors.length];
    const busy = get("node_gpu_busy_percent"), power = get("node_gpu_power_watts"), limit = get("node_gpu_power_limit_watts"), temp = get("node_gpu_temp_celsius");
    if (!busy || !power || !limit || !temp) return card("History", `<p class="hint">loading history…</p>`);
    const name = labels => (d.gpus || []).find(g => g.device === labels.device)?.name || labels.device || "";
    const util = [];
    for (const line of busy.lines || []) util.push({label: `${name(line.labels)} · busy`, color: color(line.labels), points: line.points.map(p => [p[0], p[1], p[7] ?? null, p[8] ?? null])});
    const limits = new Map((limit.lines || []).map(line => [line.labels.device, new Map(line.points.map(p => [p[0], p[1]]))]));
    const noLimit = [];
    for (const line of power.lines || []) {
      const lim = limits.get(line.labels.device);
      if (!lim || !lim.size) { noLimit.push(line); continue; }
      util.push({label: `${name(line.labels)} · power % of limit`, color: colors[(devices.indexOf(line.labels.device) + 2) % colors.length],
        points: line.points.filter(p => num(lim.get(p[0])) && lim.get(p[0]) > 0).map(p => [p[0], p[1] / lim.get(p[0]) * 100, p[7] ?? null, p[8] ?? null])});
    }
    const common = {step: busy.step || 2, cadence: busy.tier === "raw" ? busy.interval_s || 0 : 0, start: busy.start, end: busy.end, group: "machine"};
    const ranger = `<div class="row" role="group" aria-label="History range">${ranges.map(([l, s]) => `<button type="button" class="btn sm ${s === view.range ? "pri" : ""}" data-page-action="mc-range" data-range="${s}" aria-pressed="${s === view.range}">${esc(l)}</button>`).join("")}</div>`;
    const parts = [TS().chart({...common, title: "GPU busy and power as % of limit (%)", unit: "%", lines: util, max: 100})];
    parts.push(TS().chart({...common, title: "GPU temperature (°C)", unit: "°C", lines: (temp.lines || []).map(line => ({label: `${name(line.labels)} · ${line.labels.sensor || ""}`, color: color(line.labels), points: line.points.map(p => [p[0], p[1], p[7] ?? null, p[8] ?? null])}))}));
    if (noLimit.length) parts.push(TS().chart({...common, title: "GPU power (W) · no readable power limit", unit: "W", lines: noLimit.map(line => ({label: `${name(line.labels)} · ${line.labels.sensor || "power"}`, color: color(line.labels), points: line.points.map(p => [p[0], p[1], p[7] ?? null, p[8] ?? null])}))}));
    return card("History", ranger + `<div class="mc-charts">${parts.join("")}</div><p class="hint">Gaps are periods without samples (never zeros). Stored in UTC; shown in this browser's time zone.</p>`);
  }
  function memory(d) {
    const {card, table, cells, esc, unavailable} = H(), m = d.memory || {};
    const limit = m.limiting;
    const head = limit ? `<p>Limiting domain: <b>${esc(limit.label)}</b> ${levelPill(limit.level)} · ${esc(fmt(limit.available_bytes, "bytes"))} available</p>` : `<p>Limiting domain: ${unavailable(m.limiting_why)}</p>`;
    const rowsHtml = (m.domains || []).map(x => cells([`${esc(x.label)}${x.excluded ? ' <span class="tag">excluded</span>' : ""}${x.stale ? ' <span class="tag">stale</span>' : ""}<div>${meterBar(x.used_bytes, x.total_bytes, x.level === "red" ? "e" : x.level === "amber" ? "w" : "")}</div>`,
      num(x.used_bytes) ? esc(fmt(x.used_bytes, "bytes")) : unavailable(x.why), `<span class="hide-n">${num(x.total_bytes) ? esc(fmt(x.total_bytes, "bytes")) : unavailable(x.why)}</span>`,
      num(x.available_bytes) ? esc(fmt(x.available_bytes, "bytes")) : unavailable(x.why), levelPill(x.level), `<span class="hide-n">${esc(x.observer || "")}</span>`])).join("");
    const rep = m.engine_report || {};
    let report;
    if (!(rep.reports || []).length) report = `<p class="hint">Engine memory report · ${unavailable(rep.why)}</p>`;
    else {
      const total = rep.used_bytes || rep.reports.reduce((a, r) => a + r.reported_bytes, 0);
      const segs = rep.reports.flatMap(r => Object.entries(r.parts).map(([k, b]) => [`${r.profile} · ${k.replace(/_bytes$/, "")}`, b]));
      const bar = `<div class="mc-stack" role="img" aria-label="Engine memory report">${segs.map(([, b], i) => `<i class="mc-seg-${i % 5}" data-meter="${Math.min(100, b / total * 100)}"></i>`).join("")}${num(rep.unattributed_bytes) ? `<i class="mc-seg-un" data-meter="${Math.min(100, rep.unattributed_bytes / total * 100)}"></i>` : ""}</div>`;
      report = `<h3 class="mc-h3">Engine memory report (as reported)</h3>${bar}<ul class="mc-legend">${segs.map(([l, b], i) => `<li><i class="mc-seg-${i % 5}"></i>${esc(l)} · ${esc(fmt(b, "bytes"))}</li>`).join("")}<li><i class="mc-seg-un"></i>unattributed · ${num(rep.unattributed_bytes) ? esc(fmt(rep.unattributed_bytes, "bytes")) + " · " + esc(rep.label || "") : unavailable(rep.why)}</li></ul>`;
    }
    return card("Memory by domain", head + table(["Domain", "Used", ["Total", "hide-n"], "Available", "Level", ["Observer", "hide-n"]], rowsHtml, "Memory domains") + report, "domains are never merged");
  }
  function processes(d) {
    const {card, table, cells, esc, unavailable} = H();
    if (d.processes == null) return card("Processes holding the GPU", `<p>${unavailable(d.why?.processes)}</p>`);
    return card("Processes holding the GPU", table(["PID", "Process", ["Card", "hide-n"], "GPU memory"], d.processes.map(p => cells([esc(p.pid), p.comm ? esc(p.comm) : unavailable(p.why?.comm || "not reported"), `<span class="hide-n">${esc(p.device || "")}</span>`, num(p.vram_bytes) ? esc(fmt(p.vram_bytes, "bytes")) : unavailable("not reported by the driver")])).join(""), "GPU processes") + (d.why?.processes ? `<p class="hint">${unavailable(d.why.processes)}</p>` : ""));
  }
  function cpu(d) {
    const {card, table, cells, esc, unavailable} = H(), c = d.cpu || {};
    const load = Array.isArray(c.load) ? c.load.map(x => x.toFixed(2)).join(" · ") : null;
    return card("CPU", `<dl class="kv"><dt>Busy</dt><dd>${v(c.busy_percent, "%", 1, c.why?.busy_percent)}</dd><dt>Load (1 · 5 · 15 min)</dt><dd>${load ? esc(load) : unavailable(c.why?.load)}</dd><dt>Cores</dt><dd>${num(c.cores) ? esc(c.cores) : unavailable("not reported")}</dd></dl>` +
      ((c.temperatures || []).length ? table(["Sensor", "Temperature", ["Kind", "hide-n"]], c.temperatures.map(t => cells([esc(t.sensor), v(t.celsius, "°C", 1), `<span class="hide-n">${esc(t.kind)}</span>`])).join(""), "Temperatures") : `<p>Temperatures · ${unavailable(c.why?.temperatures)}</p>`));
  }
  function network(d) {
    const {card, table, cells, esc, unavailable} = H(), id = d.machine?.id;
    const hiddenList = view.hiddenIfaces[id] || [];
    const shown = (d.net || []).filter(n => !hiddenList.includes(n.iface));
    const body = (d.net || []).length ? table(["Interface", "RX", "TX", ["State", "hide-n"], ""], shown.map(n => cells([`${esc(n.iface)}${n.physical ? "" : ' <span class="tag">virtual</span>'}`, num(n.rx_bytes_per_s) ? esc(fmt(n.rx_bytes_per_s, "B/s")) : unavailable(n.why?.rx_bytes_per_s), num(n.tx_bytes_per_s) ? esc(fmt(n.tx_bytes_per_s, "B/s")) : unavailable(n.why?.tx_bytes_per_s), `<span class="hide-n">${esc(n.operstate || "")}</span>`, `<button type="button" class="btn sm" data-page-action="mc-hide-iface" data-iface="${esc(n.iface)}">Hide</button>`])).join(""), "Network interfaces") : `<p>${unavailable(d.why?.net)}</p>`;
    return card("Network", body + (hiddenList.length ? `<button type="button" class="btn sm" data-page-action="mc-show-ifaces">Show hidden interfaces (${hiddenList.length})</button>` : ""), "rates between two committed counter samples");
  }
  function disks(d) {
    const {card, table, cells, esc, unavailable} = H();
    const usage = (d.disks || []).length ? table(["Mount", "Used", "Free", "Size"], d.disks.map(x => cells([`${esc(x.mount)}<div>${meterBar(x.used_bytes, x.total_bytes)}</div>`, esc(fmt(x.used_bytes, "bytes")), esc(fmt(x.free_bytes, "bytes")), esc(fmt(x.total_bytes, "bytes"))])).join(""), "Disks") : `<p>${unavailable(d.why?.disks)}</p>`;
    const io = (d.disk_io || []).length ? table(["Device", "Read", "Write"], d.disk_io.map(x => cells([esc(x.device), num(x.read_bytes_per_s) ? esc(fmt(x.read_bytes_per_s, "B/s")) : unavailable(x.why?.read_bytes_per_s), num(x.write_bytes_per_s) ? esc(fmt(x.write_bytes_per_s, "B/s")) : unavailable(x.why?.write_bytes_per_s)])).join(""), "Disk I/O") : `<p>Disk I/O · ${unavailable(d.why?.disk_io)}</p>`;
    return card("Disks", usage + io);
  }
  function engines(d) {
    const {card, table, cells, esc, unavailable} = H();
    if (!(d.engines || []).length) return card("Engines and services", `<p class="hint">No engine or service of a launched profile on this machine.</p>`);
    return card("Engines and services", table(["Profile", "Role", "Model", ["Context", "hide-n"], "Running / queued", "Completed tok/s"], d.engines.map(e => !e.observer && !e.follower_of ? `<tr><td>${esc(e.profile)}</td><td>${esc(e.role)}</td><td colspan="4">${unavailable(e.why?.model)}</td></tr>` : cells([esc(e.profile), esc(e.role) + (e.follower_of ? `<div class="hint">counted on rank 0 (${esc(e.follower_of)})</div>` : ""), e.model ? esc(e.model) : unavailable(e.why?.model), `<span class="hide-n">${num(e.context_length) ? esc(e.context_length.toLocaleString()) : unavailable(e.why?.context_length)}</span>`, `${num(e.running) ? esc(e.running) : unavailable(e.why?.running)} / ${num(e.queued) ? esc(e.queued) : unavailable(e.why?.queued)}`, num(e.throughput) ? esc(e.throughput.toFixed(1)) + `<div class="hint">${esc(e.throughput_label || "")}</div>` : unavailable(e.why?.throughput)])).join(""), "Engines"));
  }
  function events(d) {
    const {card, table, cells, esc, unavailable, statusPill} = H();
    if (d.events == null) return card("Recent events", `<p>${unavailable(d.why?.events)}</p>`);
    if (!d.events.length) return card("Recent events", `<p class="hint">No events recorded for this machine.</p>`);
    return card("Recent events", table(["Time", "Severity", "Category", "Message"], d.events.map(e => cells([esc(new Date(e.ts * 1000).toLocaleString()), statusPill(e.severity === "critical" ? "error" : e.severity === "warning" ? "warning" : "info"), esc(e.category), esc(e.message)])).join(""), "Events"));
  }

  (window.HyperloomPages ||= {}).machine = {
    title: "Machine",
    group: "Workspace",
    order: 11,
    icon: '<rect x="3" y="3" width="14" height="10" rx="2"/><path d="M7 17h6M10 13v4"/><circle cx="10" cy="8" r="2"/>',
    endpoints: () => docEndpoint() ? [docEndpoint(), ...Object.values(history())] : [],
    ready: () => !docEndpoint() ? !!H().state.docs.devices : !!H().state.docs[docEndpoint()],
    render() {
      const {esc, empty, unavailable, statusPill} = H();
      const id = machineId();
      if (!id) return empty("No machine", "Add a machine to the cluster configuration.");
      const d = H().state.docs[docEndpoint()];
      const ids = d.machines || [id];
      const picker = `<label class="field mc-pick" for="mc-machine">Machine <select class="inp" id="mc-machine" data-page-action="mc-machine">${ids.map(m => `<option value="${esc(m)}"${m === id ? " selected" : ""}>${esc(m)}</option>`).join("")}</select></label>`;
      const up = num(d.uptime_s) ? `${Math.floor(d.uptime_s / 86400)} d ${Math.floor(d.uptime_s % 86400 / 3600)} h` : null;
      const header = `<section class="card"><div class="row between">${picker}<div class="row">${statusPill({online: "online", stale: "stale", offline: "offline", unknown: "no data yet"}[d.status] || d.status)}${d.status_why ? `<span class="hint">${esc(d.status_why)}</span>` : ""}<span class="hint">${esc(d.machine?.os || "")} · ${d.machine?.control ? "control machine" : "node"} · uptime ${up ? esc(up) : unavailable(d.why?.host || "not reported")}</span></div></div>${d.why?.host ? `<p class="hint">${unavailable(d.why.host)}</p>` : ""}</section>`;
      return `${header}${gpuBlock(d)}${charts(d)}<div class="grid g2">${memory(d)}${processes(d)}</div><div class="grid g2">${cpu(d)}${network(d)}</div><div class="grid g2">${disks(d)}${engines(d)}</div>${events(d)}`;
    },
    after(main) { TS().bind(main); },
  };

  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action^="mc-"]');
    if (!control) return;
    const id = machineId(), action = control.dataset.pageAction;
    if (action === "mc-range") view.range = Number(control.dataset.range);
    else if (action === "mc-hide-iface") (view.hiddenIfaces[id] ||= []).includes(control.dataset.iface) || view.hiddenIfaces[id].push(control.dataset.iface);
    else if (action === "mc-show-ifaces") delete view.hiddenIfaces[id];
    else return;
    save(); H().refresh(); H().render();
  });
  document.addEventListener("change", event => {
    const action = event.target.dataset?.pageAction;
    if (action === "mc-machine") { location.hash = `#/machine?id=${encodeURIComponent(event.target.value)}`; return; }
    if (action === "mc-mode") { view.mode = event.target.value; save(); H().render(); }
  });
})();
