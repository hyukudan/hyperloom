/* Local portal: source and capability rules are docs/ui/UI-SPEC.md sections 9–11. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const routes = {devices: "Devices", topology: "Topology & links", models: "Models & placement", recipes: "Recipes & profiles", engines: "Engines", stats: "Live stats", logs: "Logs", settings: "Settings & pairing"};
  const navIcons = {
    devices: '<rect x="3" y="4" width="14" height="10" rx="2"/><path d="M7 17h6"/>',
    topology: '<circle cx="5" cy="5" r="2.2"/><circle cx="15" cy="5" r="2.2"/><circle cx="10" cy="15" r="2.2"/><path d="M7 5h6M6.2 7l2.6 6M13.8 7l-2.6 6"/>',
    models: '<path d="M10 2l7 4v8l-7 4-7-4V6z"/><path d="M3 6l7 4 7-4M10 10v8"/>',
    recipes: '<path d="M5 3h8l3 3v11H5z"/><path d="M8 8h5M8 11h5M8 14h3"/>',
    engines: '<path d="M5 4l10 6-10 6z"/>',
    stats: '<path d="M3 16V9M8 16V4M13 16v-6M18 16v-9"/>',
    logs: '<path d="M4 4h12M4 8h12M4 12h8M4 16h10"/>',
    settings: '<circle cx="10" cy="10" r="3"/><path d="M10 2v3M10 15v3M2 10h3M15 10h3M4.3 4.3l2 2M13.7 13.7l2 2M4.3 15.7l2-2M13.7 6.3l2-2"/>'
  };
  const endpoints = ["devices", "topology", "models", "profiles", "stats", "logs", "settings"];
  // Feature pages (static/pages/*.js) register here before this file runs:
  //   (window.HyperloomPages ||= {}).id = {title, icon, group, order, home, endpoints() -> ["doc?query"...],
  //     render() -> html, ready()?, error()?, actions()? -> header html, after(main)?, refresh()?}
  // Their documents are fetched with the core ones while the page is open (state.docs[endpoint]).
  // Controls with data-page-action are handled by the page itself.
  const pages = window.HyperloomPages || {};
  const coreOrder = Object.keys(routes);
  // Sidebar groups (F7): core screens sit in the same groups as feature pages, ordered by `order` within a group.
  const groupOrder = ["Workspace", "Inference", "Benchmarks", "Operations"];
  const coreNav = {devices: ["Workspace", 20], topology: ["Workspace", 25], models: ["Inference", 20], recipes: ["Inference", 25],
    engines: ["Inference", 30], stats: ["Inference", 40], logs: ["Operations", 60], settings: ["Operations", 90]};
  for (const [id, page] of Object.entries(pages).sort((a, b) => (a[1].order ?? 100) - (b[1].order ?? 100))) {
    if (Object.hasOwn(routes, id) || !/^[a-z][a-z0-9-]*$/.test(id) || typeof page?.render !== "function") continue;
    routes[id] = page.title || id;
    navIcons[id] = page.icon || '<circle cx="10" cy="10" r="6"/>';
  }
  // Shell strings (F51): pages/shell-i18n.js loads before this file; without it every string is its English fallback.
  const i18n = window.HyperloomI18n || {lang: "en", t: (key, vars, fallback) => fallback ?? key, add() {}, languages: ["en"]};
  const t = (key, vars, fallback) => i18n.t(key, vars, fallback);
  const titleOf = id => coreOrder.includes(id) ? t("route." + id, null, routes[id]) : routes[id];
  const homeRoute = Object.keys(pages).find(id => pages[id]?.home && Object.hasOwn(routes, id)) || "devices";
  const pageOf = name => coreOrder.includes(name) ? null : pages[name] || null;
  const pageEndpoints = name => { try { return (pageOf(name)?.endpoints?.() || []).filter(e => typeof e === "string" && e); } catch (_) { return []; } };
  const api = "/hyperloom/api/";
  const state = {docs: {}, fetched: {}, errors: {}, fresh: false, busy: false, job: null, selected: "", dialog: null, returnFocus: null, logProcess: "all", logLevel: "warnings", logText: "", range: 900, warnings: new Set(), generation: 0, operation: 0, switching: false,
    optional: {}, absent: {}, filters: {}, prefs: null};
  let polling = null;
  let inFlight = null;
  let pollAbort = null;
  const esc = value => String(value).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const unwrap = value => object(value) && value.status === "unavailable" ? null : object(value) && Object.hasOwn(value, "value") ? value.value : value;
  function reason(owner, key, fallback = "source not reported") {
    const value = owner?.[key];
    if (object(value) && value.why) return value.why;
    if (object(owner?.why)) return owner.why[key] || fallback;
    return owner?.[`${key}_why`] || (typeof owner?.why === "string" ? owner.why : fallback);
  }
  const unavailable = why => `<span class="unavailable">unavailable · ${esc(why || "source not reported")}</span>`;
  function display(value, why, unit = "") {
    const raw = unwrap(value);
    if (raw === null || raw === undefined) return unavailable(value?.why || why);
    if (Array.isArray(raw)) return raw.map(v => display(v, why)).join(" · ");
    if (object(raw)) return Object.entries(raw).filter(([key]) => key !== "why").map(([key, value]) => `${esc(key.replaceAll("_", " "))}: ${display(value, reason(raw, key, why), key.endsWith("_bytes") ? "bytes" : "")}`).join(" · ");
    if (typeof raw === "number" && unit === "bytes") return `${Number((raw / 1e9).toFixed(2)).toLocaleString()} GB`;
    if (typeof raw === "number" && unit === "bytes/s") return `${Number((raw / 1e6).toFixed(2)).toLocaleString()} MB/s`;
    if (typeof raw === "number" && unit === "°C") return esc(temperature(raw));
    return esc(typeof raw === "number" ? Number(raw.toFixed(3)).toLocaleString() : raw) + (unit ? ` ${esc(unit)}` : "");
  }
  const field = (owner, key, unit = "", fallback) => display(owner?.[key], reason(owner, key, fallback), unit);
  function rows(value) {
    value = unwrap(value);
    if (Array.isArray(value)) return value;
    if (object(value)) return Object.entries(value).map(([id, row]) => object(row) ? {id, ...row} : {id, value: row});
    return [];
  }
  const doc = key => state.docs[key] || {};
  const machines = () => rows(doc("devices").machines);
  const profiles = () => rows(doc("profiles").profiles);
  function selected() {
    const all = profiles();
    return all.find(p => (p.id || p.name || p.profile) === state.selected) || all.find(p => p.family === "qwen-replicas") || all[0];
  }
  const profileId = p => p?.id || p?.name || p?.profile || "";
  function statusPill(status, why) {
    status = unwrap(status);
    if (status == null) return unavailable(why);
    const text = String(status);
    const style = /^(online|ready|running|verified|ok|up|done)$/i.test(text) ? "ok" : /error|failed|poisoned|mismatch/i.test(text) ? "err" : /degraded|warn|starting|stopping|draining|queued|size.only/i.test(text) ? "warn" : /unpaired|ready to start/i.test(text) ? "info" : "off";
    return `<span class="pill p-${style}">${esc(text)}</span>`;
  }
  const tag = value => `<span class="tag">${esc(value)}</span>`;
  function button(label, action, {id = "", disabled = "", style = "", extra = ""} = {}) {
    const control = `<button type="button" class="btn ${style}" ${id ? `id="${esc(id)}"` : ""} ${action ? `data-action="${esc(action)}"` : ""} ${disabled ? `disabled title="${esc(disabled)}"` : ""} ${extra}>${esc(label)}</button>`;
    return disabled ? `<span class="disabled-control">${control}<span class="hint">${esc(disabled)}</span></span>` : control;
  }
  const later = (label, blocker) => `<div class="row">${button(label, "", {disabled: blocker})}</div>`;
  function card(title, content, subtitle = "", id = "", about = "") {
    return `<section class="card"${id ? ` id="${esc(id)}"` : ""}><h2>${esc(title)}${about ? info(about) : ""}${subtitle ? `<span class="sub">${esc(subtitle)}</span>` : ""}</h2>${content}</section>`;
  }
  function table(headers, body, label = "Data") {
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="${esc(label)}"><table><thead><tr>${headers.map(h => `<th scope="col"${Array.isArray(h) ? ` class="${h[1]}"` : ""}>${esc(Array.isArray(h) ? h[0] : h)}</th>`).join("")}</tr></thead><tbody>${body || `<tr><td colspan="${headers.length}">No entries reported.</td></tr>`}</tbody></table></div>`;
  }
  const cells = values => `<tr>${values.map(v => `<td${/class="[^"]*\bhide-n\b/.test(v) ? ' class="hide-n"' : ""}>${v}</td>`).join("")}</tr>`;
  function kv(owner, entries) {
    return `<dl class="kv">${entries.map(([label, key, unit, about]) => `<dt>${esc(label)}${about ? info(about) : ""}</dt><dd>${field(owner, key, unit)}</dd>`).join("")}</dl>`;
  }
  function empty(title, detail, action = "") {
    return `<section class="card empty"><h2>${esc(title)}</h2><p class="t2">${esc(detail)}</p>${action}</section>`;
  }
  const mutationReason = () => !state.fresh || documentsAged() ? "Waiting for authoritative state" : state.busy || state.switching || activeJob() ? "Another action is running" : "";
  const activeJob = () => state.job && !["done", "completed", "succeeded", "failed", "error", "cancelled", "canceled", "stopped"].includes(state.job.state || state.job.status);
  function meter(label, value, total, unit, why, offline = false, about = "") {
    value = unwrap(value); total = unwrap(total);
    const known = typeof value === "number" && typeof total === "number" && total > 0 && !offline;
    return `<div class="mrow"><span>${esc(label)}${about ? info(about) : ""}</span><span class="v">${offline ? unavailable("machine offline") : display(value, why, unit)}${total != null && !offline ? ` / ${display(total, why, unit)}` : ""}</span><div class="meter" aria-hidden="true">${known ? `<i data-meter="${Math.min(100, Math.max(0, value / total * 100))}"></i>` : ""}</div></div>`;
  }
  function kpi(label, value, note, history, about) {
    return `<div class="card kpi"><div class="lbl">${esc(label)}${about ? info(about) : ""}</div><div class="val">${value}</div><div class="dl muted">${note || ""}</div>${history?.some(v => typeof v === "number") ? `<div class="spk">${spark(history, label)}</div>` : ""}</div>`;
  }
  function leaseView(lease, serverTime) {
    if (!lease) return "";
    const seconds = timestamp(lease.deadline) - timestamp(serverTime);
    const left = Number.isFinite(seconds) ? `${Math.max(0, Math.floor(seconds / 60))}:${String(Math.max(0, Math.floor(seconds % 60))).padStart(2, "0")} left` : "";
    return `<div class="accel"><div class="row between"><span>GPU turn ${display(lease.label ?? lease.lease, "lease label not reported")}</span><b>${left || unavailable(reason(lease, "deadline", "server time or lease deadline unavailable"))}</b></div>${kv(lease, [["Cap", "cap_s", "s"], ["Drain margin", "drain_margin_s", "s"], ["Approval", "approval"]])}<p class="hint">Extension needs an approver's ACK. Countdown uses the lease deadline and server time.</p></div>`;
  }
  function timestamp(value) {
    value = unwrap(value);
    return typeof value === "number" ? value : typeof value === "string" ? Date.parse(value) / 1000 : NaN;
  }
  function renderDevices() {
    const d = doc("devices"), all = machines();
    const discovered = rows(d.discovered);
    const summary = d.summary || {};
    const kpis = `<div class="grid g4 kpis">${kpi("Machines online", field(summary, "online", "", reason(d, "machines")), `${all.length} configured · ${discovered.length} discovered`, null, t("info.machines_online"))}${kpi("Accelerators in use", field(summary, "accelerators_in_use"), field(summary, "excluded_reason"), null, t("info.accelerators"))}${kpi("GPU memory headroom", field(summary, "memory_free_bytes", "bytes"), `Minimum per device: ${field(summary, "minimum_headroom_bytes", "bytes")}`, null, t("info.headroom"))}${kpi("Links", field(summary, "links_healthy"), field(summary, "links_summary"), null, t("info.links"))}</div>`;
    const cards = all.map(m => deviceCard(m, d)).join("");
    return `${discovered.length ? `<div class="banner info">${discovered.length} discovered, unpaired. Discovery only announces; it never grants access. ${later("Pair", "later phase · TensorMeld agent")}</div>` : ""}${kpis}${all.length ? `<div class="grid g3">${cards}</div>` : empty("Add the first machine", "Add a configured machine by address and confirm its SSH host-key fingerprint.", button("Add by address", "add", {style: "pri"}))}${card("Discovered, not paired", discovered.length ? table(["Name", "OS", "Accelerators", "Fingerprint", "Action"], discovered.map(m => cells([field(m, "name"), field(m, "os"), field(m, "accelerators"), field(m, "fingerprint"), later("Pair", "later phase · TensorMeld agent")])).join("")) : unavailable(reason(d, "discovered", "later phase · TM-R13")), "later phase · TensorMeld agent")}`;
  }
  function deviceCard(m, d) {
    const health = m.health, offline = health === "offline", unpaired = health === "unpaired";
    const accelerators = rows(m.accelerators), usb = m.usb4;
    const last = m.last_seen == null ? unavailable(reason(m, "last_seen")) : `${Math.max(0, Math.floor(d.server_time - m.last_seen))} s ago · ${esc(clockTime(m.last_seen))}`;
    const roles = rows(m.roles).map(r => tag(`${r.profile ? r.profile + " · " : ""}${r.role || r.name || r.id}${offline ? " · unreachable" : ""}`)).join(" ");
    return `<section class="card dev ${offline ? "offline" : ""}"><div class="hd"><div class="osi" aria-hidden="true">${/win/i.test(String(unwrap(m.os))) ? "WIN" : "LNX"}</div><div><div class="row between"><span class="nm">${esc(m.name || m.id)}</span>${statusPill(health, reason(m, "health"))}</div><div class="meta">${field(m, "os")}${!unpaired ? ` · ${field(m, "cpu_model")} · last seen ${last}` : ""}</div></div></div>${unpaired ? "" : `<div class="row">${roles}</div>${kv(m, [["Host RAM", "ram_summary"], ["Disk free", "disk_free_bytes", "bytes"], ["NICs", "nics_summary"]])}`}
      ${accelerators.length ? accelerators.map(g => {
        const name = g.name || g.id, excluded = g.excluded;
        return `<div class="accel"><div class="an">${name ? esc(name) : unavailable(reason(g, "name"))}${excluded ? statusPill("Excluded") : ""}</div>${unpaired ? "" : excluded ? `<p class="hint">${field(g, "policy_reason")}</p>` : `<div class="hint">${esc(g.memory_kind || "VRAM")}</div>${meter("GPU memory", g.memory_used_bytes, g.memory_total_bytes, "bytes", reason(g, "memory_used_bytes"), offline, t("info.gpu_memory"))}${meter("GPU", g.utilization_pct, 100, "%", reason(g, "utilization_pct"), offline, t("info.gpu_busy"))}${kv(g, [["Temperature", "temperature_c", "°C", t("info.temperature")], ["Power", "power_w", "W", t("info.power")], ...(!m.rocm ? [["Driver", "driver"]] : [])])}`}</div>`;
      }).join("") : unavailable(reason(m, "accelerators"))}
      ${unpaired ? later("Pair", "later phase · TensorMeld agent · TM-R10..R13") : `${m.rocm ? `<p class="hint runtime">${esc(m.rocm)}</p><details class="packages"><summary>ROCm packages</summary><pre class="raw">${esc(JSON.stringify(m.rocm_packages, null, 2))}</pre></details>` : ""}${!/win/i.test(m.os || "") ? `<p class="hint">${field(m, "kernel_summary")}</p>` : ""}${usb ? `<div class="usb-summary"><p>${esc(usb.summary)}</p>${rows(usb.cables).map(c => `<p class="hint">${esc(c.summary)} · ${esc(c.health)}</p>`).join("")}</div>` : ""}${m.release ? kv(m, [["Release", "release"]]) : ""}${rows(m.leases).map(l => leaseView(l, d.server_time)).join("")}`}
      ${rows(m.warnings).map(w => `<div class="banner warn">Warning · ${display(w.message ?? w.value ?? w)}</div>`).join("")}${health === "degraded" && !rows(m.warnings).length ? `<div class="banner warn">Degraded · ${display(m.why)}. Available passive readings remain visible.</div>` : ""}${offline ? `<div class="banner">Offline · ${display(m.why)}. Roles are unreachable.</div>` : ""}${m.error ? `<div class="banner">${display(m.error)} <a href="#/logs">Logs and failures</a></div>` : ""}</section>`;
  }
  function renderTopology() {
    const d = doc("topology"), links = rows(d.links);
    return `<section class="card topo-map"><h2>Cluster map <span class="sub">Machines, accelerators, roles and links</span></h2>${topologyMap(rows(d.machines), links)}</section>`
      + card("Links", links.length ? table(["Link", "Kind", "Link speed", "Bandwidth", "RTT p50", ["p99", "hide-n"], "Source", "Health"], links.map(l => cells([display(l.name ?? `${l.from || ""} ↔ ${l.to || ""}`), field(l, "kind"), field(l, "gbps", "Gb/s"), field(l, "bytes_per_s", "bytes/s"), field(l, "rtt_us_p50", "µs"), `<span class="hide-n">${field(l, "rtt_us_p99", "µs")}</span>`, `${field(l, "source")}<div class="hint">${l.source === "driver status" ? "Current negotiated speed · throughput needs R18" : l.source === "interface status" ? "Negotiated speed · no link-probe receipt" : "Dated receipt · RTT"}</div>`, `${statusPill(l.health ?? l.state, reason(l, "health"))}${rows(l.warnings).map(w => `<p class="hint">${esc(w)}</p>`).join("")}`])).join(""), "Links and dated measurements") + `<div class="topo-link-list">${links.map(l => `<article class="accel"><b>${display(l.name)}</b><div class="row between"><span>${field(l, "kind")} · ${field(l, "gbps", "Gb/s")}</span>${statusPill(l.health)}</div><p class="hint">${field(l, "source")} · negotiated speed</p><p class="hint">Bandwidth: ${field(l, "bytes_per_s", "bytes/s")}<br>RTT p50: ${field(l, "rtt_us_p50", "µs")}</p>${rows(l.warnings).map(w => `<p class="hint">${esc(w)}</p>`).join("")}</article>`).join("")}</div>` : unavailable(reason(d, "links", "no link-probe receipt")), "Negotiated link speed is distinct from measured bandwidth", "", t("info.link_table"))
      + card("USB4 layout", `${rows(d.usb4).map(row => `<div class="accel"><b>${esc(row.machine)}</b><p>${esc(row.summary)}</p>${rows(row.cables).map(c => `<p class="hint">${esc(c.summary)} · ${esc(c.health)}</p>`).join("")}${rows(row.warnings).map(w => `<div class="banner warn">${esc(w)}</div>`).join("")}</div>`).join("") || "No USB4 links reported."}<p class="hint">Read-only driver state. The portal never changes the USB4 layout. Per-cable throughput, latency and retrain history need R18.</p>`);
  }
  function topologyMap(all, links) {
    if (!all.length) return unavailable("no machines configured");
    const columns = Math.min(all.length, 3), width = columns * 350 + 20;
    const height = Math.ceil(all.length / columns) * 310 + 100;
    const positions = new Map(all.map((m, i) => [m.id, {x: 20 + (i % columns) * 350, y: 110 + Math.floor(i / columns) * 310}]));
    let svg = `<svg class="topo" viewBox="0 0 ${width} ${height}" role="img" aria-label="Cluster map: ${esc(all.map(m => m.name || m.id).join(", "))}. Link details follow in the table."><title>Machines and current links</title>`;
    svg += `<rect class="box" x="${width / 2 - 95}" y="14" width="190" height="40" rx="8"/><text class="t" x="${width / 2}" y="39" text-anchor="middle">Configured LAN</text>`;
    for (const m of all) {
      const p = positions.get(m.id);
      svg += `<path class="eth" d="M${width / 2} 54 L${p.x + 150} ${p.y}"/><text class="lbl" x="${p.x + 150}" y="${p.y - 12}" text-anchor="middle">${esc(rows(m.nics_summary).length ? rows(m.nics_summary).map(n => n.value || n).join(" · ") : "Configured · rate not reported")}</text>`;
    }
    const drawn = new Set();
    for (const l of [...links].sort((a, b) => Number(/warn/.test(b.health)) - Number(/warn/.test(a.health)))) {
      const a = positions.get(l.from), b = positions.get(l.to);
      if (!a || !b) continue;
      const key = [l.from, l.to].sort().join("/") + "/" + l.id;
      if (drawn.has(key)) continue;
      drawn.add(key);
      const y = Math.min(a.y, b.y) + 150 + (String(l.id) === "B" ? 55 : 0);
      const left = a.x < b.x ? a : b, right = a.x < b.x ? b : a;
      svg += `<path class="${/warn|down|error/.test(l.health) ? "usbw" : "usb"}" d="M${left.x + 300} ${y} L${right.x} ${y}"/><text class="lbl" x="${(left.x + 300 + right.x) / 2}" y="${y - 10}" text-anchor="middle">${esc(l.id)}</text>`;
    }
    for (const m of all) {
      const p = positions.get(m.id), devices = rows(m.accelerators);
      const label = (text, limit = 36) => esc(String(text || "").length > limit ? String(text).slice(0, limit - 1) + "…" : text || "");
      svg += `<g><title>${esc(m.name || m.id)} · ${esc(m.os)} · ${esc(m.health)}</title><rect class="box ${m.access === "local" ? "on" : ""}" x="${p.x}" y="${p.y}" width="300" height="270" rx="12"/><text class="t" x="${p.x + 16}" y="${p.y + 28}">${label(m.name || m.id)}</text><text class="s" x="${p.x + 16}" y="${p.y + 49}">${label(m.os)} · ${label(m.health)}</text>`;
      devices.slice(0, 2).forEach((g, i) => {
        const y = p.y + 65 + i * 62;
        svg += `<rect class="gpu" x="${p.x + 14}" y="${y}" width="272" height="54" rx="8"/><text class="gt" x="${p.x + 24}" y="${y + 22}">${label(g.name || g.id, 34)}</text><text class="s" x="${p.x + 24}" y="${y + 42}">${g.excluded ? "Excluded by policy" : `${g.memory_total_bytes == null ? "Memory not reported" : `${(g.memory_total_bytes / 1e9).toFixed(1)} GB · ${esc(g.memory_kind || "VRAM")}`}`}</text>`;
      });
      svg += `<text class="rt" x="${p.x + 16}" y="${p.y + 222}">${label(rows(m.roles).map(r => r.role).join(" · ") || "No active roles")}</text><text class="s" x="${p.x + 16}" y="${p.y + 247}">${label(m.usb4?.summary || rows(m.nics_summary).join(" · "), 42)}</text></g>`;
    }
    return svg + "</svg>";
  }
  function renderModels() {
    const d = doc("models"), models = rows(d.models ?? d.assets);
    const library = models.length ? table(["Model / asset", "Declared size", "Machine", "Verification", ["Declared identity", "hide-n"]], models.map(m => cells([display(m.name ?? m.id), field(m.manifest, "bytes", "bytes"), field(m, "machine"), statusPill(m.presence, reason(m, "presence")), `<span class="mono hide-n">${field(m.manifest, "sha256")} · ${field(m.manifest, "path")}</span>`])).join("")) + `<p class="hint">Manifest sizes and hashes declare identity; they do not prove file presence. Size only means SHA-256 has not been verified. Hashing runs outside measurement turns.</p>${later("Schedule hash", "unavailable · no hash scheduling endpoint in Phase 1b")}` : empty("No model files registered", d.models == null ? reason(d, "models") : "Register asset paths in the cluster configuration; then install a qualified profile.");
    const choices = profiles().map(p => `<div class="accel"><div class="row between"><b>${esc(p.label || profileId(p))}</b>${statusPill(p.state, reason(p, "state"))}</div>${kv({...p.configuration, preset: p.configuration?.preset || profileId(p), receipt: p.receipt, nodes: p.nodes}, [["Preset", "preset"], ["Receipt", "receipt"], ["Machines", "nodes"]])}${button("Start / switch…", "start-dialog", {disabled: profileBlocker(p) || mutationReason() || capability("up"), extra: `data-profile="${esc(profileId(p))}"`})}</div>`).join("");
    return card("Model library", library, "Files are never downloaded or converted automatically") + card("What can run on the connected machines", `<div class="grid g2">${choices}</div><p>${unavailable(reason(d, "proposals", "later phase · Phase 2.5 · R17 / TM-R15"))}</p>`, "Qualified profiles · placement proposals arrive in Phase 2.5");
  }
  function profileBlocker(p) {
    if (!p) return "No configured profile";
    if (p.state == null) return reason(p, "state", "Profile state unavailable");
    // The engine family (from the API), not the preset name: qwen-replicas-w5 starts like qwen-replicas.
    if (p.family !== "qwen-replicas") return p.blocker || "later phase · Phase 2 / 3a / R14";
    return p.available === false ? p.blocker || "Profile not available" : "";
  }
  function capability(action) {
    const entry = doc("settings").capabilities?.[action];
    return entry?.enabled === false ? entry.why || "Capability not available" : "";
  }
  function renderEngines() {
    const d = doc("profiles"), p = selected();
    if (!p) return empty("No profiles configured", d.profiles == null ? reason(d, "profiles") : "Add a qualified qwen-replicas profile to the cluster configuration.", `<a class="btn" href="#/settings">Open settings</a>`);
    const processRows = rows(p.processes ?? d.processes);
    const choice = `<div class="field"><label for="profile-select">Profile</label><select class="inp" id="profile-select">${profiles().map(item => `<option value="${esc(profileId(item))}"${profileId(item) === profileId(p) ? " selected" : ""}>${esc(item.label || profileId(item))}</option>`).join("")}</select></div>`;
    const identity = `<dl class="kv"><dt>Started</dt><dd>${p.started_at == null ? unavailable(reason(p, "started_at", "profile has not started")) : esc(formatTime(timestamp(p.started_at), true))}</dd></dl>` + kv(p, [["Release", "release"], ["Qualified receipt", "receipt"], ["Identity", "identity"], ["Router runs on", "router"], ["Linked machines", "nodes"], ["Linked devices", "devices"]]);
    return card(p.label || profileId(p), choice + (state.launchErrors?.launch ? '<p class="hint">Launch timeline unavailable</p>' : "") + (window.HyperloomLaunch?.render(rows(state.docs.launch?.launches).find(l => l.profile === profileId(p) && l.job_state === "running")) || "") + `<div class="row">${statusPill(p.state, reason(p, "state"))} ${profileBlocker(p) ? `<span class="hint">${esc(profileBlocker(p))}</span>` : ""}</div>${identity}${rows(p.leases).map(l => leaseView(l, d.server_time)).join("")}${p.error ? `<p class="field-error">${display(p.error)}</p><a class="btn" href="#/logs">View failure and logs</a>` : ""}`)
      + card("Processes", processRows.length ? table(["Process", "Machine", "State", ["Ready after", "hide-n"], ["Endpoint", "hide-n"], ["Binary SHA", "hide-n"], "Logs"], processRows.map(proc => {
        const lease = rows(p.leases).find(l => l.lease === proc.lease);
        const health = proc.state || (lease && lease.state !== "ready" ? lease.state : proc.ready === true ? "ready" : proc.ready === false ? "starting" : null);
        return cells([display(proc.role ?? proc.name ?? proc.id), field(proc, "machine"), `${statusPill(health, reason(proc, "state"))}${proc.reason || lease?.reason ? `<p class="hint">${esc(proc.reason || lease.reason)}</p>` : ""}${proc.start_time != null && health === "starting" ? `<p class="hint">${display(Math.max(0, d.server_time - proc.start_time), "start time missing", "s elapsed")}</p>` : ""}`, `<span class="hide-n">${field(proc, "ready_after_s", "s")}</span>`, `<span class="mono hide-n">${field(proc, "endpoint")}</span>`, `<span class="mono hide-n">${field(proc, "binary_sha256")}</span>`, `<a class="btn sm" data-log-process="${esc(proc.machine === "local" ? "router" : proc.machine || proc.name || proc.id)}" href="#/logs">Logs</a>`]);
      }).join("")) : `<p>${unavailable(reason(p, "processes", "profile stopped"))}</p>`, "supervise · starting → ready / draining → stopped")
      + `<div class="grid g2">${card("Parameters", renderParameters(p), "Apply on next start · production values and qualified ranges")}${card("Effective configuration", p.effective_configuration == null ? unavailable(reason(p, "effective_configuration")) : renderEffective(p.effective_configuration), "Rendered argv / env and preset provenance")}</div>${card("Change history", renderHistory(p.history ?? d.history), "Only configuration versions are rollbackable")}`;
  }
  function renderParameters(p) {
    const parameters = rows(p.parameters);
    return (parameters.length ? `<div class="grid g2">${parameters.map((v, i) => `<div class="field"><label for="parameter-${i}">${esc(v.label || v.id || v.name)}</label><input class="inp ${v.value !== v.production ? "chg" : ""}" id="parameter-${i}" value="${esc(unwrap(v.value) ?? "")}" readonly><span class="hint">Production ${field(v, "production")} · qualified ${field(v, "qualified")}</span></div>`).join("")}</div>` : `<p>${unavailable(reason(p, "parameters", "capacity schema not reported"))}</p>`) + `<div class="banner"><b>Advanced locked</b><span class="hint">Needs per-pin qualification. Numerics, model identity, transport and layout are never editable here.</span></div><p class="hint">${esc(reason(p, "parameter_save", "configuration save endpoint unavailable"))}</p>${later("Save · restart required", reason(p, "parameter_save", "configuration save endpoint unavailable"))}`;
  }
  function renderEffective(config) {
    if (unwrap(config) == null) return unavailable(config?.why || "effective config not reported");
    return table(["Setting", "Value", "Source"], rows(config).map(v => {
      const value = v.value ?? (v.argv || v.env ? v : null);
      const summary = Array.isArray(value) ? `${value.length} arguments` : object(value) ? `${Object.keys(value).length} settings` : "";
      const rendered = summary ? `<details><summary>${esc(summary)} · view values</summary><pre class="raw">${esc(JSON.stringify(value, null, 2))}</pre></details>` : display(value, v.why);
      return cells([esc(v.setting || v.id || v.name), rendered, display(v.source, "provenance not reported")]);
    }).join(""));
  }
  function renderHistory(history) {
    return rows(history).length ? table(["When", "Action", "Result"], rows(history).map(h => cells([display(h.when ?? h.time), display(h.action ?? h.change), display(h.result ?? h.state, h.why)])).join("")) + later("Roll back", "configuration restore endpoint unavailable · permissions and leases must be revalidated") : unavailable(history?.why || "configuration history not reported");
  }
  function renderStats() {
    const d = doc("stats"), engines = rows(d.engines);
    const history = rows(d.history).filter(sample => d.server_time - sample.time <= state.range);
    const range = `<fieldset class="seg"><legend>Chart range</legend>${[[300, "5 min"], [900, "15 min"], [3600, "1 h"]].map(([n, label]) => `<label><input type="radio" name="range" value="${n}"${state.range === n ? " checked" : ""}>${label}</label>`).join("")}</fieldset>`;
    const perEngine = engines.map(e => {
      const status = e.status || {}, metrics = e.metrics || {};
      const h = history.map(s => rows(s.engines).find(item => item.id === e.id) || {});
      const series = key => h.map(s => unwrap(s[key]));
      const tiles = `<div class="grid stats-kpis">${kpi("Throughput · completed", field(e, "throughput", "tok/s"), "10 s windows · reset-aware engine counter", series("throughput"), t("info.throughput"))}${kpi("TTFT p50 · engine-side", field(e, "ttft_p50", "ms"), `queue + prefill · p95 ${field(e, "ttft_p95", "ms")} · n=${display(e.ttft_p50?.n)}`, series("ttft_p50"), t("info.ttft"))}${kpi("Queue", field(metrics, "waiting", "waiting"), `${field(metrics, "running")} running · ${field(status.slots, "total")} slots`, null, t("info.queue"))}${kpi("Draft acceptance", field(e, "draft_acceptance"), "Windowed accepted / proposed", null, t("info.draft"))}${kpi("Prefill placement", unavailable("router counts shown below"), "Router source")}${kpi("Errors · stalls", field(metrics, "failed"), `engine cumulative failures · ${unavailable("stall counter unavailable")}`)}</div>`;
      return card(e.id, `${tiles}<div class="grid g2">${chart("Throughput", [{name: "completed", data: series("throughput"), color: "--s1"}], history.map(s => s.time), "tok/s", t("info.throughput"))}${chart("Time to first token · engine-side", [{name: "p50", data: series("ttft_p50"), color: "--s1"}, {name: "p95", data: series("ttft_p95"), color: "--s4"}], history.map(s => s.time), "ms", t("info.ttft"))}</div><div class="grid g2">${card("Memory headroom", kv(status.memory, [["Free", "device_free_bytes", "bytes"], ["Total", "device_total_bytes", "bytes"]]), "Engine-side · GPU-visible memory", "", t("info.engine_memory"))}${card("USB4 exchange mean", `<p>${field(e, "usb4_exchange_mean_us", "µs")}</p>`, "Engine exchange totals · mean over sampling window", "", t("info.usb4_exchange"))}</div>`, e.profile || "Engine telemetry");
    }).join("");
    const requests = rows(d.recent_requests), correlated = requests.some(r => r.route != null);
    return range + (engines.length ? perEngine : empty("No engine telemetry", "Start a qualified profile to collect completed throughput, engine-side TTFT and queue samples.", `<a class="btn" href="#/engines">Open engines</a>`))
      + card("Router queue & prefill placement", rows(d.routers).length ? rows(d.routers).map(r => `<div class="accel"><b>${esc(r.profile || r.id)}</b>${kv(r, [["Waiting", "waiting"]])}${kv(r.prefill, [["Placement counters", "placements"]])}</div>`).join("") : unavailable(reason(d, "routers", "router not running")), "Latest router status · placements are counters")
      + card("Recent requests", `<div class="row between"><p class="hint">Completed · engine-side TTFT = queue + prefill · route needs R7 correlation</p>${button("Export CSV", "export-csv", {disabled: requests.length ? "" : "No completed requests"})}</div>${requests.length ? table(["Time", ...(correlated ? ["Route"] : []), "Engine", "Prompt", ["Cached", "hide-n"], "TTFT · ms", "Decode · tok/s", ["Output", "hide-n"], "Status"], requests.map(r => cells([field(r, "time"), ...(correlated ? [field(r, "route")] : []), field(r, "engine"), field(r, "prompt_tokens"), `<span class="hide-n">${field(r, "reused_tokens")}</span>`, field(r, "engine_ttft_ms"), field(r, "decode_tok_s"), `<span class="hide-n">${field(r, "output_tokens")}</span>`, statusPill(r.status, reason(r, "status"))])).join("")) : unavailable(reason(d, "recent_requests", "no completed requests"))}`);
  }
  function chart(title, series, times, unit, about = "") {
    const values = series.flatMap(s => s.data).filter(v => typeof v === "number" && Number.isFinite(v));
    if (!values.length) return card(title, `<div class="chart">${unavailable("requires telemetry samples")}</div>`, unit, "", about);
    const max = Math.max(...values, 1) * 1.15;
    const legend = `<div class="legend">${series.map(s => `<span><i class="series-${esc(s.color.slice(3))}"></i>${esc(s.name)}</span>`).join("")}</div>`;
    const summary = `${title}, ${unit}. ${series.map(s => `${s.name}: last ${displayText(s.data.at(-1))}`).join("; ")}. Range ${Math.min(...values).toFixed(2)} to ${Math.max(...values).toFixed(2)} ${unit}.`;
    return card(title, `<div class="chart">${legend}${lineChart(series, {max, unit, summary, times})}<details><summary>View as table</summary>${table(["Time", ...series.map(s => `${s.name} · ${unit}`)], times.map((time, index) => cells([esc(clockTime(time)), ...series.map(s => display(s.data[index], "no sample"))])).join(""), `${title} values`)}</details></div>`, unit, "", about);
  }
  function displayText(value) { return typeof value === "number" ? value.toFixed(2) : "unavailable"; }
  function clockTime(value) {
    const n = timestamp(value);
    return Number.isFinite(n) ? formatTime(n) : "unavailable";
  }
  // The shell.js SVG approach, with gaps for missing samples and no invented data.
  function lineChart(series, {max, unit = "", summary, times = [], height = 180, sparkline = false}) {
    const viewport = document.documentElement.clientWidth;
    const W = sparkline ? 600 : Math.max(240, viewport < 760 ? viewport - 112 : Math.min(600, (viewport - 340) / 2));
    const H = height, pl = sparkline ? 0 : 66, pr = 12, pt = 12, pb = sparkline ? 0 : 26;
    const n = Math.max(2, ...series.map(s => s.data.length));
    const x = i => pl + (W - pl - pr) * i / (n - 1), y = v => pt + (H - pt - pb) * (1 - v / max);
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(summary)}"><title>${esc(summary)}</title>`;
    if (!sparkline) {
      for (let i = 0; i < 5; i++) {
        const v = max * i / 4;
        svg += `<line x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${pl - 6}" y="${y(v) + 4}" text-anchor="end">${esc(Number(v.toFixed(1)))} ${esc(unit)}</text>`;
      }
      const indices = [...new Set([0, Math.floor((times.length - 1) / 2), times.length - 1])].filter(i => i >= 0);
      for (const i of indices) svg += `<text x="${x(i)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === times.length - 1 ? "end" : "middle"}">${esc(clockTime(times[i]))}</text>`;
    }
    for (const s of series) {
      let path = "", gap = true;
      s.data.forEach((value, i) => {
        if (typeof value !== "number" || !Number.isFinite(value)) { gap = true; return; }
        path += `${gap ? "M" : "L"}${x(i).toFixed(2)},${y(value).toFixed(2)} `; gap = false;
        if (s.data.length === 1) svg += `<circle cx="${x(i)}" cy="${y(value)}" r="3" fill="var(${s.color})"/>`;
      });
      svg += `<path d="${path}" fill="none" stroke="var(${s.color})" stroke-width="2" stroke-linejoin="round"/>`;
    }
    return svg + "</svg>";
  }
  function spark(data, label) { return lineChart([{data, color: "--s1"}], {max: Math.max(0.001, ...data.filter(v => typeof v === "number")) * 1.15, summary: `${label} recent samples; last ${displayText(data.at(-1))}`, height: 38, sparkline: true}); }
  function logEntries() {
    const entries = doc("logs").entries;
    if (object(entries) && ("stdout" in entries || "stderr" in entries)) return ["stdout", "stderr"].flatMap(stream => String(entries[stream] || "").split("\n").filter(Boolean).map(message => ({process: state.logProcess, message, stream})));
    return rows(entries);
  }
  function renderLogs() {
    const d = doc("logs"), all = logEntries();
    const options = ["all", "portal", "router", ...machines().map(m => m.id)];
    if (!options.includes(state.logProcess)) options.push(state.logProcess);
    const filters = `<div class="row"><div class="field"><label for="log-process">Process</label><select class="inp" id="log-process">${options.map(p => `<option value="${esc(p)}"${p === state.logProcess ? " selected" : ""}>${esc(p)}</option>`).join("")}</select></div><div class="field"><label for="log-level">Level</label><select class="inp" id="log-level">${[["events", "Events"], ["warnings", "Events + warnings"], ["everything", "Everything"]].map(([k, v]) => `<option value="${k}"${state.logLevel === k ? " selected" : ""}>${v}</option>`).join("")}</select></div><div class="field"><label for="log-text">Filter text</label><input class="inp" id="log-text" type="search" value="${esc(state.logText)}"></div></div>`;
    const entries = all.filter(row => {
      const message = typeof row === "string" ? row : row.message || row.text || JSON.stringify(row);
      const event = /READY|POISONED|STOPPED|error|failed|lease|action/i.test(message), warning = /warn/i.test(message);
      return message.toLowerCase().includes(state.logText.toLowerCase()) && (state.logLevel === "everything" || event || state.logLevel === "warnings" && warning);
    });
    const content = entries.map(row => {
      const message = typeof row === "string" ? row : row.message || row.text || JSON.stringify(row);
      const style = /POISONED|error|failed/i.test(message) ? "er" : /warn/i.test(message) ? "wn" : /READY/.test(message) ? "ev" : "";
      return `<div class="${style}">${row.when || row.time ? `<span class="ts">${display(row.when || row.time)}</span> ` : ""}${esc(row.process || row.action || "")} ${esc(message)}</div>`;
    }).join("");
    const failures = rows(d.failures);
    return filters + card("Live log", `<div class="log" role="region" aria-label="Redacted process log" tabindex="0">${content || (all.length ? "No entries match these filters." : unavailable(reason(d, "entries", "no log entries yet")))}</div>`, "supervise stdout / stderr and portal actions · secrets are redacted")
      + card("Recent failures", failures.length ? table(["When", "Process", "Event", "Reason", "Actions"], failures.map((f, i) => cells([display(f.when ?? f.time), field(f, "process"), statusPill(f.event ?? f.state), field(f, "reason"), `<div class="failure-actions"><a class="btn" href="#/logs" data-log-process="${esc(f.process || "all")}">Logs</a>${button("Open redacted bundle", "failure-bundle", {extra: `data-index="${i}"`, disabled: f.bundle == null ? "redacted bundle unavailable" : ""})}</div>`])).join("")) : unavailable(reason(d, "failures", "no recorded failures")), "Persistent across restarts");
  }
  function renderSettings() {
    const d = doc("settings"), trusted = rows(d.trusted_machines);
    return preferencesSection() + `<div class="grid g2">${card("Add by address", `<p>Declare an SSH machine in the cluster configuration, then compare and pin its host-key fingerprint.</p>${button("Add by address", "add", {disabled: mutationReason(), style: "pri"})}<h2>Pairing wizard</h2><ol><li>New machine displays a one-time code and fingerprint.</li><li>Compare the fingerprint and choose GPU worker or observer.</li><li>Exchange keys to grant access.</li></ol>${later("Pair machine", "later phase · TensorMeld agent · TM-R10..R13")}<p class="hint">Discovery only announces; it never grants access.</p>`)}${card("Trusted machines", trusted.length ? table(["Machine", "Pinned", "Host key", "Access"], trusted.map(m => cells([display(m.machine ?? m.id), field(m, "paired_at"), `${field(m, "algorithm")}<div class="mono">${field(m, "fingerprint")}</div>`, "SSH · pinned key"])).join("")) + later("Disconnect", "unavailable · uninstall / unpin endpoint not exposed") + later("Revoke enrollment", "later phase · TensorMeld agent") : unavailable(reason(d, "trusted_machines", "no pinned host keys")))}</div>
      <div class="grid g2">${card("Secrets", kv(d.secrets, [["Storage", "storage"], ["Values", "values"]]) + button("Rotate engine key…", "rotate-dialog", {disabled: mutationReason()}) + `<p class="hint">Restart required after rotation. Reservations, approvals, pairings and secrets cannot be rolled back as configuration.</p>`)}${card("Lab rules", kv(d.lab_rules, [["GPU-turn labels", "label_prefix"], ["Turn cap", "turn_cap_s", "s"], ["Drain margin", "drain_margin_s", "s"], ["USB4 layout", "usb4_layout"], ["Excluded", "excluded"], ["Extended turn", "extended_turn"]]))}</div>
      ${card("Configuration", `<p class="mono">${field(d, "config_path")}</p>${d.configuration != null ? `<details><summary>View redacted configuration</summary><pre class="raw">${esc(JSON.stringify(d.configuration, null, 2))}</pre></details>${button("Export (redacted)", "export-config")}` : unavailable(reason(d, "configuration"))}${later("Import configuration", "unavailable · validation / import endpoint not exposed")}`, "hyperloom/cluster-v1 · versioned configuration")}`;
  }
  const hashQuery = () => new URLSearchParams(location.hash.split("?").slice(1).join("?"));
  function route() {
    const name = location.hash.replace(/^#\/?/, "").split(/[?&]/)[0];
    return Object.hasOwn(routes, name) ? name : homeRoute;
  }
  function render() {
    const page = route(), source = page === "engines" ? "profiles" : page;
    infoCount = 0;
    // #/engines?profile=<id> selects that profile (initial load, reload and history navigation).
    const linked = page === "engines" ? hashQuery().get("profile") : null;
    if (linked && linked !== state.selected && profiles().some(p => profileId(p) === linked)) state.selected = linked;
    const active = activeCount();
    document.title = `${active ? `(${active}${summaryStale() ? "?" : ""}) ` : summaryStale() ? "(?) " : ""}Hyperloom · ${titleOf(page)}`;
    document.body.dataset.page = page;
    $("page-title").textContent = titleOf(page);
    renderNav(page);
    $("navigation").querySelectorAll("a").forEach(link => {
      const active = link.getAttribute("href") === `#/${page}`;
      link.classList.toggle("on", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const current = selected(), disabled = mutationReason();
    const controls = page === "devices" ? button("Add by address", "add", {id: "add-machine", style: "pri", disabled}) + button("Doctor…", "doctor-dialog", {id: "doctor-devices", disabled}) : page === "engines" ? button("Start profile…", "start-dialog", {id: "start-profile", style: "pri", disabled: disabled || profileBlocker(current)}) + button("Install…", "install-dialog", {id: "install-profile", disabled: disabled || profileBlocker(current)}) + button("Doctor…", "doctor-dialog", {id: "doctor-profile", disabled}) + button("Stop profile", "stop", {id: "stop-profile", style: "dan", disabled: current ? "" : "No profile configured"}) : page === "topology" ? button("Measure Ethernet links", "", {disabled: "later phase · Phase 2"}) : page === "logs" ? button("Download redacted log", "export-log", {disabled: logEntries().length ? "" : "No log entries"}) : "";
    const pageControls = pageOf(page)?.actions ? (() => { try { return pageOf(page).actions(); } catch (_) { return ""; } })() : controls;
    if ($("page-actions").innerHTML !== pageControls) $("page-actions").innerHTML = pageControls;
    const running = profiles().find(p => ["running", "starting", "stopping", "degraded", "error", "failed"].includes(p.state));
    const global = running?.state || (profiles().length ? "stopped" : state.docs.profiles ? "not configured" : "loading");
    $("global-status").outerHTML = `<span id="global-status">${statusPill(global)}</span>`;
    $("mobile-status").outerHTML = `<span id="mobile-status">${statusPill(global)}</span>`;
    const local = machines().find(m => m.access === "local");
    $("control-machine").textContent = local ? `Control · ${local.name || local.id}` : "Control · this machine";
    $("version").textContent = doc("settings").version ? `Hyperloom ${doc("settings").version}` : "Hyperloom · local portal";
    renderShell(page);
    const renderers = {devices: renderDevices, topology: renderTopology, models: renderModels, engines: renderEngines, stats: renderStats, logs: renderLogs, settings: renderSettings};
    // The recipes page (recipes.js) and feature pages own their documents; they are rendered with the same
    // focus/details preservation.
    const feature = pageOf(page) && page !== "recipes" ? pageOf(page) : null;
    const external = page === "recipes" ? window.HyperloomRecipes : feature ? {
      render: feature.render,
      ready: () => feature.ready ? feature.ready() : pageEndpoints(page).every(e => state.docs[e]),
      error: () => feature.error ? feature.error() : pageEndpoints(page).map(e => state.errors[e]).filter(Boolean).join(" · "),
    } : null;
    if (external) renderers[page] = external.render;
    if (external ? external.ready() : state.docs[source]) {
      const focused = document.activeElement;
      // Keep a keyboard user's focus stable across polls and filters.
      const focusId = $("main").contains(focused) ? focused.id : "";
      const selection = focused?.selectionStart;
      const scroll = $("main").querySelector(".log")?.scrollTop;
      const openDetails = [...$("main").querySelectorAll("details")].map(el => el.open);
      const html = renderers[page]();
      if (external && html === state.lastExternal) { renderConnection(); renderJob(); updateDialogGate(); return; }
      state.lastExternal = external ? html : null;
      $("main").innerHTML = html;
      $("main").querySelectorAll("button,input,select,a,summary,[tabindex]").forEach((el, index) => { if (!el.id) el.id = `route-${page}-control-${index}`; });
      $("main").querySelectorAll("[data-meter]").forEach(el => { el.style.width = `${el.dataset.meter}%`; });
      [...$("main").querySelectorAll("details")].forEach((el, i) => { el.open = openDetails[i] || false; });
      applyTips();
      if (focusId && $(focusId)) {
        $(focusId).focus({preventScroll: true});
        if (selection != null && typeof $(focusId).setSelectionRange === "function" && ["text", "search", "textarea"].includes($(focusId).type)) $(focusId).setSelectionRange(selection, selection);
      }
      if (scroll != null && $("main").querySelector(".log")) $("main").querySelector(".log").scrollTop = scroll;
      if (feature?.after) { try { feature.after($("main")); } catch (error) { console.error(error); } }
      deepLink(page);
    } else if (external && external.error()) {
      state.lastExternal = null;       // the next successful render must replace this, even with unchanged content
      $("main").innerHTML = problem({title: feature ? `${titleOf(page)} ${t("problem.data_unavailable", null, "data unavailable")}` : t("problem.recipes", null, "Recipe data unavailable"), why: external.error(), next: {label: t("problem.open_logs", null, "Open logs"), href: "#/logs"}});
    } else if (state.errors[source]) {
      state.lastExternal = null;
      $("main").innerHTML = problem({title: t("problem.portal", null, "Portal data unavailable"), why: state.errors[source], next: {label: t("problem.open_settings", null, "Open settings"), href: "#/settings"}});
    }
    $("main").setAttribute("aria-busy", (external ? external.ready() : state.docs[source]) ? "false" : "true");
    renderConnection();
    renderJob();
    updateDialogGate();
  }
  function renderConnection() {
    const current = currentSet(), errors = Object.entries(state.errors).filter(([name]) => current.includes(name)).map(([, error]) => error);
    const oldest = Math.min(...current.map(name => state.fetched[name]).filter(Boolean), Date.now());
    const stale = Date.now() - oldest > staleAfter() || errors.length > 0;
    document.body.classList.toggle("stale", stale);
    const message = document.hidden ? t("conn.paused", null, "Polling paused in background. Controls resume after authoritative state is fetched.") : !navigator.onLine ? t("conn.offline", null, "Offline. Displaying the last received state.") : stale ? `${t("conn.stale", {time: formatTime(oldest / 1000)}, `Stale · as of ${formatTime(oldest / 1000)}.`)} ${errors.join(" · ")}` : !state.fresh ? t("conn.refreshing", null, "Refreshing authoritative state before enabling actions…") : "";
    $("connection").hidden = !message;
    $("connection").textContent = message;
    renderHeartbeat(stale);
  }
  function renderJob() {
    const panel = $("job-panel"), job = state.launchRefusal || state.job, active = !state.launchRefusal && activeJob();
    panel.hidden = !job;
    if (!job) return;
    panel.innerHTML = `<div class="row"><b>${esc(job.action || "Action")} · ${statusPill(job.state || job.status)}</b><span class="mono">${esc(job.id || job.job || "")}</span>${active && ["up", "recipes/deploy"].includes(job.action) ? button("Cancel start now", "cancel") : ""}${active ? button("Stop profile now", "stop", {style: "dan"}) : ""}</div><p>${display(job.progress, "waiting for progress")}</p>${["up", "recipes/deploy"].includes(job.action) && (job.id || job.job) ? window.HyperloomLaunch?.render(state.docs["launch/" + (job.id || job.job)]) || (state.launchErrors?.["launch/" + (job.id || job.job)] ? '<p class="hint">Launch timeline unavailable</p>' : "") : ""}${job.error ? `<p class="field-error" role="alert">${display(job.error)}</p><a class="btn" href="#/logs">View logs</a>` : ""}${job.result != null ? `<details><summary>Result (redacted)</summary><pre class="raw">${esc(JSON.stringify(job.result, null, 2))}</pre></details>` : ""}`;
  }
  async function request(endpoint, options = {}) {
    const response = await fetch(api + endpoint, {cache: "no-store", credentials: "same-origin", ...options});
    const body = await response.json();
    if (!response.ok) throw Object.assign(new Error(body.error || `HTTP ${response.status}`), {status: response.status});
    return body;
  }
  async function refresh() {
    if (document.hidden || !navigator.onLine) { state.fresh = false; renderConnection(); return; }
    if (inFlight) return inFlight;
    const generation = state.generation, accepted = state.accepted;
    inFlight = (async () => {
      const own = [...endpoints, ...pageEndpoints(route()).filter(e => !endpoints.includes(e))];
      const launchId = ["up", "recipes/deploy"].includes(state.job?.action) ? state.job.id || state.job.job : null;
      const names = [...own, "jobs"];
      refreshLaunch(generation, launchId);
      const prefsEpoch = prefsSync.epoch;
      pollAbort = new AbortController();
      const timeout = setTimeout(() => pollAbort?.abort(), 30000);
      // Each answer is stamped when it arrives (freshness is the age of what was received, not of the batch).
      const arrived = {}, seq = sequence(names);
      const results = await Promise.allSettled(names.map(name => fetchDocument(name, pollAbort.signal).finally(() => { arrived[name] = Date.now(); })));
      clearTimeout(timeout);
      if (generation !== state.generation || document.hidden) return;
      state.current = names;
      // Only the documents of the current page (core + its endpoints + jobs) keep a fetch time or an error: those of
      // pages left behind, or of a page's previous query, must not age the view.
      for (const key of Object.keys(state.fetched)) if (!names.includes(key)) delete state.fetched[key];
      for (const key of Object.keys(state.errors)) if (!names.includes(key)) delete state.errors[key];
      for (let i = 0; i < results.length; i++) {
        const name = names[i], result = results[i];
        if (OPTIONAL.includes(name) && !optionalResult(name, result, prefsEpoch, arrived[name], seq[name])) continue;
        if (result.status === "fulfilled") {
          if (name === "jobs") {
            state.lastJobs = result.value.jobs;
            const jobs = rows(result.value.jobs).filter(j => !state.expiredLaunches?.has(j.id || j.job)), active = jobs.find(j => (j.id || j.job) === result.value.active);
            const listed = jobs.find(j => (j.id || j.job) === (state.job?.id || state.job?.job));
            // A roster requested before the latest mutation was accepted cannot know that job: it only refreshes the
            // job it lists. A newer roster is authoritative: a job it no longer lists (portal restart, eviction) is gone.
            if (accepted !== state.accepted) { if (listed) state.job = listed; }
            else state.job = active || listed || null;
            if (state.job?.error && !state.warnings.has(state.job.id)) { state.warnings.add(state.job.id); announce(state.job.error); }
          } else { state.docs[name] = result.value; state.fetched[name] = arrived[name]; }
          delete state.errors[name];
        } else state.errors[name] = `${name}: ${result.reason.message}`;
      }
      state.fresh = results.every(r => r.status === "fulfilled") && !documentsAged();
      const warningMessages = machines().flatMap(m => rows(m.warnings).map(w => `${m.id}: ${typeof w === "string" ? w : w.message || w.value || JSON.stringify(w)}`));
      for (const warning of warningMessages) if (!state.warnings.has(warning)) { state.warnings.add(warning); announce(`New warning · ${warning}`); }
      render();
      // The shell's optional documents follow on their own: never delaying the screen's documents, never making
      // the view stale, one round in flight at a time.
      refreshOptional(generation, prefsEpoch, own);
    })().finally(() => { inFlight = null; });
    return inFlight;
  }
  let launchAbort = null;
  function refreshLaunch(generation, launchId) {
    launchAbort?.abort();
    launchAbort = new AbortController();
    const signal = launchAbort.signal;
    const names = [...(route() === "engines" ? ["launch"] : []), ...(launchId ? ["launch/" + encodeURIComponent(launchId)] : [])];
    state.launchErrors ||= {};
    // Same polling cadence, independent promises: timeline availability cannot gate ordinary actions.
    names.forEach(async name => {
      try {
        const value = await fetchDocument(name, signal);
        if (generation !== state.generation || signal.aborted) return;
        state.docs[name] = value;
        delete state.launchErrors[name];
      } catch (error) {
        if (generation !== state.generation || signal.aborted) return;
        delete state.docs[name];
        if (error.status === 404 && error.message === "unknown launch job") {
          delete state.launchErrors[name];
          if (name.startsWith("launch/")) {
            (state.expiredLaunches ||= new Set()).add(decodeURIComponent(name.slice(7)));
          }
          if (name === "launch/" + encodeURIComponent(state.job?.id || state.job?.job)) state.job = null;
        } else state.launchErrors[name] = "Launch timeline unavailable";
      }
      render();
    });
  }
  function fetchDocument(name, signal) {
    const query = name === "logs" && state.logProcess !== "all" && state.logProcess !== "portal" && selected() ? `?${new URLSearchParams({profile: profileId(selected()), process: state.logProcess, tail: "200"})}` : "";
    return request(name + query, {signal});
  }
  let optionalInFlight = null;
  function refreshOptional(generation, prefsEpoch, own) {
    if (optionalInFlight) return;
    const extra = optionalDue().filter(name => !own.includes(name));
    if (!extra.length) return;
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
    optionalInFlight = (async () => {
      const arrived = {}, seq = sequence(extra);
      const results = await Promise.allSettled(extra.map(name => fetchDocument(name, abort.signal).finally(() => { arrived[name] = Date.now(); })));
      if (generation !== state.generation || document.hidden) return;
      extra.forEach((name, i) => optionalResult(name, results[i], prefsEpoch, arrived[name], seq[name]));
      render();
    })().finally(() => { clearTimeout(timer); optionalInFlight = null; });
  }
  function announce(message) { $("alerts").textContent = message; $("alerts").classList.toggle("warning", message.startsWith("New warning")); }
  function resetPolling() {
    clearInterval(polling);
    polling = null;
    if (!document.hidden) { refresh(); polling = setInterval(refresh, pollMs()); }
  }
  function paired(m) {
    return m.access === "local" || rows(doc("settings").trusted_machines).some(t => (t.machine || t.id) === m.id);
  }
  const reachable = m => ["online", "degraded"].includes(m.health || m.state);
  const machineLabel = m => `${m.name || m.id}${!paired(m) ? " · host key not pinned" : !reachable(m) ? " · offline" : ""}`;
  function openDialog(kind, p = selected()) {
    if (mutationReason()) { announce(mutationReason()); return; }
    state.returnFocus = document.activeElement;
    state.dialog = {kind, profile: profileId(p), destructive: kind === "rotate", effects: [], action: ""};
    let title, fields = "", effects = [], action, confirm;
    const all = machines();
    const selector = (id, label, list, defaultId = "") => `<div class="field"><label for="${id}">${esc(label)}</label><select class="inp" id="${id}" aria-describedby="dialog-error">${list.map(m => `<option value="${esc(m.id)}"${m.id === defaultId ? " selected" : ""}>${esc(machineLabel(m))}</option>`).join("")}</select></div>`;
    if (kind === "start") {
      const active = profiles().filter(item => ["running", "starting", "stopping", "degraded"].includes(item.state));
      state.dialog.stopProfiles = active.map(profileId);
      state.dialog.destructive = active.length > 0;
      title = `${active.length ? "Switch to" : "Start"} ${profileId(p)}`; action = "up"; confirm = active.length ? "Stop and start profile" : "Start profile";
      const config = p?.configuration || {}, declared = Array.isArray(config.nodes) ? config.nodes : [];
      const declaredDevices = Array.isArray(config.devices) ? config.devices : [];
      fields = `<div class="field"><label for="router-location">Router runs on</label><select class="inp" id="router-location" aria-describedby="launch-error dialog-error"><option value="local">This machine (default)</option>${all.filter(m => m.access !== "local" && paired(m)).map(m => `<option value="${esc(m.id)}"${!reachable(m) ? " disabled" : ""}>${esc(machineLabel(m))}</option>`).join("")}</select></div><fieldset><legend>Machines to link</legend>${all.filter(m => m.access !== "local").map(m => `<label class="check"><input type="checkbox" name="node" value="${esc(m.id)}"${declared.includes(m.id) ? " checked" : ""}${!paired(m) ? " disabled" : ""} aria-describedby="launch-error dialog-error"><span>${esc(machineLabel(m))}</span></label>`).join("") || "No worker machines declared."}${declared.filter(id => !all.some(m => m.id === id)).map(id => `<p class="start-blocker">${esc(id)} · declared machine unavailable</p>`).join("")}</fieldset><fieldset><legend>Devices to link</legend>${all.flatMap(m => rows(m.declared_devices).map(g => {
        const id = `${m.id}:${g.id || g.name || g.value}`;
        return `<label class="check"><input type="checkbox" name="device" value="${esc(id)}"${!g.excluded && (declaredDevices.includes(id) || declared.includes(m.id)) ? " checked" : ""}${!paired(m) || g.excluded ? " disabled" : ""}><span>${esc(id)}${g.excluded ? " · excluded by policy" : ""}</span></label>`;
      })).join("") || "No devices declared."}</fieldset><p id="launch-error" class="field-error"></p>`;
      effects = [...(active.length ? [`Will stop: ${active.map(item => `${profileId(item)} (${rows(item.processes).length} reported processes)`).join(", ")}. Stop the router before the engines and release their turns.`] : []), "Start the selected qualified profile; validate assets and reachability before taking a lease.", `Request GPU turns for the checked workers · cap ${doc("settings").lab_rules?.turn_cap_s ?? "unavailable"} s · drain margin ${doc("settings").lab_rules?.drain_margin_s ?? "unavailable"} s.`, "Run the router on the selected machine and link only the selected declared devices.", "Record each action in the audit log. If starting fails, inspect the reported failure before retrying."];
    } else if (kind === "install") {
      title = `Install ${profileId(p)}`; action = "install"; confirm = "Install profile";
      fields = selector("build-on", "Build on", all.filter(m => m.access !== "local"), p?.configuration?.nodes?.[0]);
      effects = ["Run the pinned install recipe and host checks on the selected configured worker.", "Write a release into the marked install root; retain model assets.", "Build only outside measurement turns. Record the action and result."];
    } else if (kind === "pin") {
      title = "Add by address · pin SSH host key"; action = "hosts/pin"; confirm = "Pin confirmed host key";
      fields = `<p>First declare the address, SSH target and install root in the cluster configuration. Compare the full fingerprint with <code>ssh-keygen -lf</code> on that machine.</p>${selector("pin-machine", "Configured machine / address", all.filter(m => m.access === "ssh"))}<div id="pin-address" class="raw"></div><div class="field"><label for="host-fingerprint">Confirmed SHA-256 fingerprint</label><input class="inp mono" id="host-fingerprint" required pattern="SHA256:[A-Za-z0-9+/=]+" placeholder="SHA256:…" autocomplete="off" spellcheck="false" aria-describedby="pin-hint dialog-error"><span class="hint" id="pin-hint">The server scans the candidate keys and pins only the exact matching fingerprint. A mismatch writes nothing.</span></div>`;
      effects = ["Trust the exact key matching this fingerprint in Hyperloom's own known_hosts file.", "Record the pin in the audit log. Run Doctor afterwards to check the machine."];
    } else if (kind === "doctor") {
      title = "Run Doctor"; action = "doctor"; confirm = "Run checks";
      fields = selector("doctor-machine", "Machine", [{id: "local", name: "This machine", access: "local", health: "online"}, ...all.filter(m => m.access !== "local")]);
      effects = ["Run passive host checks for the selected configured machine.", "Record the command and redacted result; GPU work is not started."];
    } else {
      title = "Rotate engine key"; action = "keys/rotate"; confirm = "Rotate key";
      fields = `<div class="field"><label for="key-name">Engine key name</label><input id="key-name" class="inp" required pattern="[A-Za-z0-9_.-]+" autocomplete="off" aria-describedby="key-hint dialog-error"><span class="hint" id="key-hint">Use the key name from your engine configuration. Key values are never shown.</span></div>`;
      effects = ["Replace the named engine key in protected storage.", "A separate explicit restart is required for processes to use it.", "Secrets cannot be restored through configuration rollback. Record the action."];
    }
    state.dialog.action = action; state.dialog.effects = effects;
    $("dialog-title").textContent = title;
    $("dialog-fields").innerHTML = fields;
    $("dialog-effects").innerHTML = `<div class="effects"><b>Effects</b><ul>${effects.map(e => `<li>${esc(e)}</li>`).join("")}</ul></div>${p ? `<div class="row emergency-actions">${button("Stop profile now", "stop", {style: "dan"})}${activeJob() && state.job.action === "up" ? button("Cancel start now", "cancel") : ""}</div>` : ""}`;
    $("dialog-error").textContent = "";
    $("dialog-confirm").textContent = confirm;
    $("dialog-confirm").className = `btn ${state.dialog.destructive ? "dan" : "pri"}`;
    $("dialog-confirm").disabled = false;
    $("action-dialog").showModal();
    updatePinAddress();
    updateDialogGate();
    ($( "dialog-fields").querySelector("input,select,button") || $("action-dialog").querySelector("[data-close]")).focus();
  }
  function updatePinAddress() {
    if (!$("pin-address")) return;
    const machine = doc("settings").configuration?.machines?.find(m => m.id === $("pin-machine").value);
    $("pin-address").textContent = machine ? `${machine.id} · ${machine.http_address} · SSH port ${machine.ssh_port || 22}` : "No configured SSH machine. Add its address to the cluster configuration first.";
  }
  function launchBlocker() {
    const p = profiles().find(item => profileId(item) === state.dialog?.profile);
    const declared = p?.configuration?.nodes || [];
    const inputs = [...$("action-form").querySelectorAll('input[name="node"]:checked')];
    const missing = declared.filter(id => !machines().some(m => m.id === id));
    const unreachable = inputs.filter(input => {
      const m = machines().find(machine => machine.id === input.value);
      return !m || !paired(m) || !reachable(m);
    }).map(input => input.value);
    const deviceInputs = [...$("action-form").querySelectorAll('input[name="device"]:checked')];
    for (const input of deviceInputs) {
      const [mid] = input.value.split(":"), m = machines().find(machine => machine.id === mid);
      if (!m || !paired(m) || !reachable(m)) unreachable.push(input.value);
    }
    const router = $("router-location")?.value;
    if (router && router !== "local") {
      const m = machines().find(machine => machine.id === router);
      if (!m || !paired(m) || !reachable(m)) unreachable.push(router);
    }
    return profileBlocker(p) || (missing.length || unreachable.length ? `Cannot start · unreachable declared selection: ${[...new Set([...missing, ...unreachable])].join(", ")}` : !inputs.length ? "Select at least one paired worker machine." : "");
  }
  function updateDialogGate() {
    if (!state.dialog) return;
    let blocker = mutationReason();
    if (state.dialog.kind === "start") {
      const launch = launchBlocker();
      if ($("launch-error")) $("launch-error").textContent = launch;
      blocker ||= launch;
    } else if (state.dialog.kind === "pin" && !$("pin-machine")?.value) blocker ||= "No configured SSH machines";
    else if (state.dialog.kind === "install" && !$("build-on")?.value) blocker ||= "No configured worker machines";
    $("dialog-confirm").disabled = Boolean(blocker);
    $("dialog-confirm").title = blocker;
  }
  function closeDialog() {
    $("action-dialog").close();
    state.dialog = null;
    const original = state.returnFocus;
    const replacement = original?.id ? $(original.id) : null;
    if (original?.isConnected) original.focus();
    else if (replacement) replacement.focus();
    else $("main").focus({preventScroll: true});
  }
  function dialogArguments() {
    const dialog = state.dialog;
    if (dialog.kind === "start") {
      const args = {profile: dialog.profile, router: $("router-location").value, node: [...$("action-form").querySelectorAll('input[name="node"]:checked')].map(input => input.value)};
      const devices = [...$("action-form").querySelectorAll('input[name="device"]:checked')].map(input => input.value);
      if (devices.length) args.device = devices;
      return args;
    }
    if (dialog.kind === "install") return {profile: dialog.profile, build_on: $("build-on").value};
    if (dialog.kind === "pin") return {machine: $("pin-machine").value, fingerprint: $("host-fingerprint").value.trim()};
    if (dialog.kind === "doctor") return $("doctor-machine").value === "local" ? {} : {machine: $("doctor-machine").value};
    return {name: $("key-name").value.trim()};
  }
  async function mutate(action, args, priority = false) {
    if (!priority && mutationReason()) throw new Error(mutationReason());
    if (!priority && capability(action)) throw new Error(capability(action));
    const token = document.querySelector('meta[name="hyperloom-csrf"]')?.content;
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    state.busy = !priority;
    render();
    try {
      state.job = await request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(args), signal: AbortSignal.timeout(30000)});
      state.accepted = (state.accepted || 0) + 1;
      state.launchRefusal = null;
      state.fresh = false;
    } catch (error) {
      if (error.status === 400 && ["up", "recipes/deploy"].includes(action)) {
        state.launchRefusal = {action, state: "refused", error: error.message, progress: "Launch refused before a job was created."};
      }
      state.fresh = false; refresh(); throw error;
    }
    finally { state.busy = false; render(); }
    await refresh();
  }
  async function stop(cancel = false) {
    state.operation++; state.switching = false;
    const current = state.dialog?.profile || state.job?.arguments?.profile || profileId(selected());
    if (state.dialog) closeDialog();
    if (!current && !state.job?.id) { announce("No profile selected to stop."); return; }
    try { await mutate(cancel ? "cancel" : "down", cancel && ["up", "recipes/deploy"].includes(state.job?.action) ? {job: state.job.id || state.job.job} : {profile: current}, true); }
    catch (error) { announce(error.message); }
  }
  function download(name, text, mime = "application/json") {
    const url = URL.createObjectURL(new Blob([text], {type: mime}));
    const link = document.createElement("a"); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportCSV() {
    const data = rows(doc("stats").recent_requests), headers = ["time", "engine", "prompt_tokens", "reused_tokens", "engine_ttft_ms", "decode_tok_s", "output_tokens", "status"];
    if (data.some(r => r.route != null)) headers.push("route");
    const quote = value => `"${String(value ?? "unavailable").replace(/^[=+@-]/, "'$&").replace(/"/g, '""')}"`;
    download("hyperloom-requests.csv", [headers.map(quote).join(","), ...data.map(r => headers.map(key => quote(r[key])).join(","))].join("\n"), "text/csv;charset=utf-8");
  }
  document.addEventListener("click", event => {
    const log = event.target.closest("[data-log-process]");
    if (log) { state.logProcess = log.dataset.logProcess; refresh(); }
    if (event.target.closest("[data-close]")) { closeDialog(); return; }
    const control = event.target.closest("[data-action]");
    if (!control || control.disabled) return;
    const action = control.dataset.action;
    if (action === "stop" || action === "cancel") { stop(action === "cancel"); return; }
    if (action === "refresh") { refresh(); window.HyperloomRecipes?.refresh(); pageOf(route())?.refresh?.(); return; }
    if (control.dataset.recipeAction || control.dataset.pageAction) return;      // recipes.js and feature pages handle their own controls
    if (action === "export-csv") { exportCSV(); return; }
    if (action === "export-config") { download("hyperloom-config-redacted.json", JSON.stringify(doc("settings").configuration, null, 2)); return; }
    if (action === "export-log") { download("hyperloom-log-redacted.json", JSON.stringify(doc("logs"), null, 2)); return; }
    if (action === "failure-bundle") {
      download("hyperloom-failure-redacted.json", JSON.stringify(rows(doc("logs").failures)[Number(control.dataset.index)].bundle, null, 2)); return;
    }
    const p = profiles().find(p => profileId(p) === control.dataset.profile) || selected();
    if (control.dataset.profile) state.selected = control.dataset.profile;
    openDialog(({add: "pin", "start-dialog": "start", "install-dialog": "install", "doctor-dialog": "doctor", "rotate-dialog": "rotate"})[action], p);
  });
  $("action-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.dialog || $("dialog-confirm").disabled) return;
    $("dialog-error").textContent = "";
    const action = state.dialog.action;
    let operation = state.operation;
    try {
      // Revalidate the selected machines against a current server snapshot.
      state.fresh = false; updateDialogGate();
      await refresh();
      updateDialogGate();
      if ($("dialog-confirm").disabled) throw new Error($("dialog-confirm").title);
      if (action === "up") {
        const currentActive = profiles().filter(item => ["running", "starting", "stopping", "degraded"].includes(item.state)).map(profileId).sort();
        if (JSON.stringify(currentActive) !== JSON.stringify([...(state.dialog.stopProfiles || [])].sort())) throw new Error("Running profiles changed. Reopen this dialog to review the current effects.");
      }
      const args = dialogArguments(), stopProfiles = state.dialog.stopProfiles || [];
      closeDialog();
      if (action === "up" && stopProfiles.length) {
        operation = ++state.operation;
        state.switching = true;
        try {
          for (const profile of stopProfiles) {
            await mutate("down", {profile}, true);
            const jobId = state.job.id || state.job.job;
            let job = state.job;
            while (job.state === "running") {
              await new Promise(resolve => setTimeout(resolve, 200));
              if (operation !== state.operation) return;
              job = await request(`jobs/${encodeURIComponent(jobId)}`);
              state.job = job; renderJob();
            }
            if (job.state !== "succeeded") throw new Error(job.error || "Stopping the previous profile failed.");
          }
          if (operation !== state.operation) return;
        } finally { state.switching = false; }
        await refresh();
      }
      if (action === "up" && operation !== state.operation) return;
      await mutate(action, args);
    } catch (error) {
      if (state.dialog) { $("dialog-error").textContent = error.message; $("dialog-confirm").focus(); }
      else announce(error.message);
    }
  });
  $("action-dialog").addEventListener("cancel", event => { event.preventDefault(); closeDialog(); });
  $("action-dialog").addEventListener("keydown", event => {
    if (event.key === "Enter" && state.dialog?.destructive) { event.preventDefault(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...$("action-dialog").querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href],summary,[tabindex="0"]')].filter(el => !el.hidden);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  $("action-form").addEventListener("input", updateDialogGate);
  $("action-form").addEventListener("change", () => { updatePinAddress(); updateDialogGate(); });
  document.addEventListener("change", event => {
    if (event.target.id === "profile-select") {
      state.selected = event.target.value;
      history.replaceState(null, "", `#/engines?profile=${encodeURIComponent(state.selected)}`);
      render();
    }
    if (event.target.id === "log-process") { state.logProcess = event.target.value; refresh(); }
    if (event.target.id === "log-level") { state.logLevel = event.target.value; render(); }
    if (event.target.name === "range") { state.range = Number(event.target.value); render(); }
  });
  document.addEventListener("input", event => { if (event.target.id === "log-text") { state.logText = event.target.value; render(); } });
  $("menu").addEventListener("click", () => {
    const expanded = $("menu").getAttribute("aria-expanded") !== "true";
    $("menu").setAttribute("aria-expanded", String(expanded));
    $("menu").setAttribute("aria-label", `${expanded ? "Close" : "Open"} navigation menu`);
    $("sidebar").classList.toggle("open", expanded);
    if (expanded) $("navigation").querySelector("a")?.focus();
  });
  document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); $("main").focus(); });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && $("sidebar").classList.contains("open")) { $("menu").click(); $("menu").focus(); }
  });
  addEventListener("hashchange", () => {
    $("sidebar").classList.remove("open"); $("menu").setAttribute("aria-expanded", "false"); $("menu").setAttribute("aria-label", "Open navigation menu");
    $("main").innerHTML = ""; state.lastExternal = null; linkDone = "";
    render(); refresh(); $("main").focus({preventScroll: true});
  });
  document.addEventListener("visibilitychange", () => { state.generation++; state.fresh = false; if (document.hidden) pollAbort?.abort(); render(); resetPolling(); });
  addEventListener("online", () => { state.fresh = false; render(); resetPolling(); });
  addEventListener("offline", () => { state.generation++; state.fresh = false; pollAbort?.abort(); render(); });
  setInterval(() => {
    if (currentSet().some(name => state.fetched[name] && Date.now() - state.fetched[name] > staleAfter()) && state.fresh) { state.fresh = false; render(); }
  }, 1000);
  // ---- Portal shell (docs/ui/UI-SPEC.md section 12): preferences, sidebar, palette, exceptions, installable shell.
  const store = {
    get(key, fallback = null) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch (_) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* storage may be disabled */ } },
  };
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  const PREF_DEFAULTS = {refresh_s: 2, temperature_unit: "C", timezone: "local", language: "en", theme: "system", density: "comfortable", hidden_panels: []};
  const PREF_CHOICES = {refresh_s: [1, 2, 5, 10, 30], temperature_unit: ["C", "F"], language: ["en", "es"], theme: ["system", "light", "dark", "black"], density: ["comfortable", "compact"]};
  function cleanPrefs(values) {
    const out = {...PREF_DEFAULTS};
    for (const [key, value] of Object.entries(object(values) ? values : {})) {
      if (key === "hidden_panels") { if (Array.isArray(value)) out[key] = value.filter(v => typeof v === "string" && v !== "settings"); }
      else if (key === "timezone") { if (typeof value === "string" && value) out[key] = value; }
      else if (PREF_CHOICES[key]?.includes(value)) out[key] = value;
    }
    return out;
  }
  // Per-viewer mirror for the first paint ({values, local: [fields saved in this browser only]}); the server's
  // prefs document is authoritative for every field that is neither pending nor browser-only.
  const mirror = store.get("hyperloom-prefs") || (() => { try { const theme = localStorage.getItem("hyperloom-theme"); return theme ? {values: {theme}} : {}; } catch (_) { return {}; } })();
  state.prefs = cleanPrefs(object(mirror.values) ? mirror.values : mirror);
  const prefs = () => state.prefs || PREF_DEFAULTS;
  const prefsStatus = {message: "", error: false};
  // Saves are serialized (one POST in flight, later changes merged into one queued POST); `epoch` changes whenever
  // a save starts or ends, so a prefs GET that began before is ignored.
  const prefsSync = {epoch: 0, inflight: null, queued: null, pending: new Set(), rev: {}, local: new Set(Array.isArray(mirror.local) ? mirror.local.filter(k => Object.hasOwn(PREF_DEFAULTS, k)) : [])};
  const keepMirror = () => store.set("hyperloom-prefs", {values: state.prefs, local: [...prefsSync.local]});
  const pollMs = () => prefs().refresh_s * 1000;
  // Freshness counts the documents of the current poll set only (pages left behind no longer age the view).
  const currentSet = () => state.current || endpoints;
  const staleAfter = () => Math.max(6000, 3 * pollMs());
  function temperature(celsius) {
    return prefs().temperature_unit === "F" ? `${Number((celsius * 9 / 5 + 32).toFixed(1)).toLocaleString()} °F` : `${Number(celsius.toFixed(1)).toLocaleString()} °C`;
  }
  // "local" is the control machine's zone (the prefs document reports its offset); the browser's own zone is used
  // when its offset is the same, else that fixed offset.
  const browserZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) { return ""; } };
  const zoneSupport = new Map();
  function formats(zoneName) {
    if (!zoneSupport.has(zoneName)) {
      let ok = true;
      try { new Intl.DateTimeFormat("en-US", {timeZone: zoneName}).format(0); } catch (_) { ok = false; }
      zoneSupport.set(zoneName, ok);
    }
    return zoneSupport.get(zoneName);
  }
  function offsetIn(zoneName, epoch) {
    try {
      const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {timeZone: zoneName, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric"}).formatToParts(new Date(epoch * 1000)).map(p => [p.type, p.value]));
      return (Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) / 1000) - Math.floor(epoch);
    } catch (_) { return null; }
  }
  function zone() {
    const chosen = prefs().timezone;
    if (chosen && chosen !== "local") return formats(chosen) ? {name: chosen, source: "preference"} : {name: "UTC", source: `${chosen} cannot be shown by this browser; UTC used`};
    const local = state.optional.prefs?.local_zone;
    if (local?.iana && formats(local.iana)) return {name: local.iana, source: "control machine"};
    const own = browserZone(), at = timestamp(state.optional.prefs?.server_time);
    if (!local || typeof local.utc_offset_s !== "number" || !Number.isFinite(at) || (own && offsetIn(own, at) === local.utc_offset_s))
      return {name: own || "local", source: local ? "inferred: this browser's zone has the control machine's current offset" : "this browser"};
    return {name: local.name || "local", source: "control machine offset", offset: local.utc_offset_s};
  }
  function formatTime(epoch, withDate = false) {
    const z = zone();
    let date = new Date(epoch * 1000), options = {};
    if (z.offset != null) { date = new Date((epoch + z.offset) * 1000); options = {timeZone: "UTC"}; }
    else if (z.name && z.name !== "local") options = {timeZone: z.name};
    try { return withDate ? date.toLocaleString(i18n.lang, options) : date.toLocaleTimeString(i18n.lang, options); }
    catch (_) { return withDate ? date.toLocaleString() : date.toLocaleTimeString(); }
  }
  let lastPollMs = pollMs();
  function applyPrefs() {
    const p = prefs(), root = document.documentElement;
    const theme = p.theme === "system" ? (systemTheme.matches ? "dark" : "light") : p.theme === "black" ? "dark" : p.theme;
    root.dataset.theme = theme;
    if (p.theme === "black") root.dataset.shade = "black"; else delete root.dataset.shade;
    root.dataset.density = p.density;
    if ($("theme") && $("theme").value !== p.theme) $("theme").value = p.theme;
    if (i18n.lang !== p.language && i18n.set) i18n.set(p.language);
    root.lang = i18n.lang;
    if (lastPollMs !== pollMs()) { lastPollMs = pollMs(); resetPolling(); }
  }
  function setPref(changes) {
    const clean = cleanPrefs({...prefs(), ...changes});
    changes = Object.fromEntries(Object.keys(changes).filter(k => Object.hasOwn(PREF_DEFAULTS, k)).map(k => [k, clean[k]]));
    state.prefs = clean;
    for (const key of Object.keys(changes)) { prefsSync.pending.add(key); prefsSync.rev[key] = (prefsSync.rev[key] || 0) + 1; }
    keepMirror(); applyPrefs(); state.lastExternal = null; render();
    prefsSync.queued = {...(prefsSync.queued || {}), ...changes};
    return pumpPrefs();
  }
  async function pumpPrefs() {
    if (prefsSync.inflight || !prefsSync.queued) return;
    const changes = prefsSync.queued, keys = Object.keys(changes);
    prefsSync.queued = null;
    // Per-field revisions: an answer only settles a field no newer local change has touched since it was sent.
    const sent = Object.fromEntries(keys.map(key => [key, prefsSync.rev[key]]));
    const current = key => prefsSync.rev[key] === sent[key];
    const token = document.querySelector('meta[name="hyperloom-csrf"]')?.content;
    prefsSync.inflight = changes; prefsSync.epoch++;
    prefsStatus.message = t("prefs.saving", null, "Saving…"); prefsStatus.error = false; render();
    try {
      if (!token || token === "__HYPERLOOM_CSRF__") throw new Error(t("prefs.no_portal", null, "page not served by the portal"));
      const send = () => request("prefs/save", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(changes), signal: AbortSignal.timeout(15000)});
      // A refused connection (no answer at all) is retried once: the write is idempotent.
      const body = await send().catch(async error => {
        if (!(error instanceof TypeError)) throw error;
        await new Promise(resolve => setTimeout(resolve, 400));
        return send();
      });
      state.optional.prefs = body;
      const saved = cleanPrefs(body.values);
      for (const key of keys) if (current(key)) { prefsSync.local.delete(key); state.prefs = {...state.prefs, [key]: saved[key]}; }
      prefsStatus.message = t("prefs.saved", null, "Saved"); prefsStatus.error = false;
    } catch (error) {
      for (const key of keys) if (current(key)) prefsSync.local.add(key);
      prefsStatus.message = t("prefs.local_only", {why: error.message}, `Saved in this browser only · ${error.message}`); prefsStatus.error = true;
    } finally {
      for (const key of keys) if (current(key)) prefsSync.pending.delete(key);
      prefsSync.inflight = null; prefsSync.epoch++;
      keepMirror(); applyPrefs(); state.lastExternal = null; render();
    }
    return pumpPrefs();
  }
  function serverPrefs(document, epoch) {
    // A poll that began before a save started or ended, or a server without a preferences store (defaults only),
    // never overrides this browser's choices; pending and browser-only fields keep their local value.
    if (!document || epoch !== prefsSync.epoch || prefsSync.inflight || document.why?.values) return;
    const next = cleanPrefs(document.values);
    let changed = false;
    for (const key of Object.keys(PREF_DEFAULTS)) {
      if (prefsSync.pending.has(key) || prefsSync.local.has(key) || JSON.stringify(next[key]) === JSON.stringify(prefs()[key])) continue;
      state.prefs = {...state.prefs, [key]: next[key]}; changed = true;
    }
    if (changed) { keepMirror(); applyPrefs(); state.lastExternal = null; }
  }

  // Optional shell documents: 404 = that feature is not installed (probed again every 60 s, not every poll).
  const OPTIONAL = ["prefs", "health/summary", "overview"];
  function optionalDue() { return OPTIONAL.filter(name => !state.absent[name] || Date.now() - state.absent[name] > 60000); }
  // Requests are numbered when sent; for one document, an answer older than one already applied (success, error
  // or 404) is dropped before it touches any snapshot or notification.
  let requestSeq = 0;
  const appliedSeq = {};
  function sequence(names) { return Object.fromEntries(names.map(name => [name, ++requestSeq])); }
  function documentsAged() {
    const now = Date.now();
    return currentSet().some(name => name !== "jobs" && state.fetched[name] && now - state.fetched[name] > staleAfter());
  }
  function optionalResult(name, result, epoch, at = Date.now(), seq = ++requestSeq) {
    if (seq <= (appliedSeq[name] || 0)) return false;
    appliedSeq[name] = seq;
    if (result.status === "fulfilled") {
      delete state.absent[name]; delete state.optional[name + ":error"];
      state.optional[name] = result.value; state.optional[name + ":at"] = at;
      if (Object.hasOwn(state.docs, name)) {
        state.docs[name] = result.value;
        if (currentSet().includes(name)) state.fetched[name] = state.optional[name + ":at"];   // the open page's own copy only
      }
      if (name === "prefs") serverPrefs(result.value, epoch);
      if (name === "health/summary") summaryArrived(result.value);
    } else if (result.reason?.status === 404) {
      // Not installed (any more): the shell's snapshot and an open page's copy go together.
      state.absent[name] = Date.now();
      for (const key of [name, name + ":at", name + ":error"]) delete state.optional[key];
      delete state.docs[name]; delete state.fetched[name];
    }
    else state.optional[name + ":error"] = result.reason?.message || "request failed";
    return true;
  }

  // F53: info tooltips and problem states.
  let infoCount = 0;
  // Kept by tooltip id across re-renders (a poll replaces the elements, and with them the browser's hover state):
  // opened by a click, or by the pointer resting on it; dismissed by Escape.
  const tips = {open: new Set(), dismissed: new Set(), hover: null};
  function applyTips() {
    document.querySelectorAll(".info").forEach(box => {
      const id = box.querySelector("[role=tooltip]")?.id;
      box.classList.toggle("open", tips.open.has(id) || tips.hover === id);
      box.classList.toggle("dismissed", tips.dismissed.has(id));
    });
  }
  function hash(text) { let h = 2166136261; for (const ch of String(text)) h = Math.imul(h ^ ch.codePointAt(0), 16777619); return (h >>> 0).toString(36); }
  function info(text, {observer, basis} = {}) {
    const body = [text, observer ? t("info.observer", {observer}, `Observer: ${observer}`) : "", basis ? t("info.basis", {basis}, `Basis: ${basis}`) : ""].filter(Boolean).join(" · ");
    const id = `info-${hash(body)}-${infoCount++}`;
    return `<span class="info"><button type="button" class="info-btn" aria-label="${esc(t("info.label", null, "About this value"))}" aria-describedby="${id}" data-info><span aria-hidden="true">i</span></button><span role="tooltip" id="${id}" class="info-tip">${esc(body)}</span></span>`;
  }
  function problem({title, why, retry = true, next} = {}) {
    return `<section class="card empty problem"><h2>${esc(title || t("problem.title", null, "Data unavailable"))}</h2><p class="t2">${esc(why || t("problem.no_reason", null, "source not reported"))}</p><div class="row">${retry ? button(t("retry", null, "Retry"), "refresh", {style: "pri"}) : ""}${next?.href ? `<a class="btn" href="${esc(next.href)}">${esc(next.label || next.href)}</a>` : ""}</div></section>`;
  }
  // F6: filter boxes for pages; the value survives re-renders (render() keeps focus and caret by id).
  function filter(id, {label, placeholder = "", statuses} = {}) {
    const value = state.filters[id] || {text: "", status: ""};
    const safe = String(id).replace(/[^a-z0-9-]/gi, "");
    return `<div class="row filter-box"><div class="field"><label for="filter-${safe}">${esc(label || t("filter.label", null, "Filter"))}</label><input class="inp" type="search" id="filter-${safe}" data-filter="${esc(id)}" placeholder="${esc(placeholder)}" value="${esc(value.text)}" autocomplete="off"></div>${statuses ? `<div class="field"><label for="filter-${safe}-status">${esc(t("filter.status", null, "Status"))}</label><select class="inp" id="filter-${safe}-status" data-filter-status="${esc(id)}"><option value="">${esc(t("filter.any", null, "Any"))}</option>${statuses.map(s => `<option value="${esc(s)}"${s === value.status ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></div>` : ""}</div>`;
  }
  const filterValue = id => ({text: "", status: "", ...(state.filters[id] || {})});
  const fold = text => String(text ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  function matches(text, query) {
    query = fold(query).trim(); if (!query) return true;
    const hay = fold(text); let i = 0;
    for (const ch of query.replace(/\s+/g, "")) { i = hay.indexOf(ch, i); if (i < 0) return false; i++; }
    return true;
  }
  function score(text, query) {
    const hay = fold(text), q = fold(query).trim();
    if (!q) return 1;
    if (hay.startsWith(q)) return 4;
    if (hay.split(/[\s·/:-]+/).some(word => word.startsWith(q))) return 3;
    if (hay.includes(q)) return 2;
    return matches(text, q) ? 1 : 0;
  }

  // F7: grouped sidebar with remembered groups, hidden panels, machines and the heartbeat.
  const collapsedGroups = new Set(store.get("hyperloom-nav-groups", []));
  const groupOf = key => coreNav[key]?.[0] || pageOf(key)?.group || "";
  const orderOf = key => coreNav[key]?.[1] ?? pageOf(key)?.order ?? 100;
  const visible = key => key === "settings" || !prefs().hidden_panels.includes(key);
  let navKey = "";
  function navGroups() {
    const groups = new Map(groupOrder.map(g => [g, []]));
    for (const key of Object.keys(routes)) { const g = groupOf(key) || "Other"; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(key); }
    for (const keys of groups.values()) keys.sort((a, b) => orderOf(a) - orderOf(b));
    return [...groups].filter(([, keys]) => keys.length);
  }
  function renderNav(page) {
    const current = groupOf(page) || "Other";
    const key = JSON.stringify([i18n.lang, prefs().hidden_panels, [...collapsedGroups], current, Object.keys(routes)]);
    if (key === navKey && $("navigation").childElementCount) return;
    navKey = key;
    const link = id => `<a href="#/${id}" title="${esc(titleOf(id))}"><svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${navIcons[id]}</svg><span class="lbl">${esc(titleOf(id))}</span></a>`;
    $("navigation").innerHTML = navGroups().map(([group, keys]) => {
      const shown = keys.filter(visible);
      if (!shown.length) return "";
      const open = group === current || !collapsedGroups.has(group), gid = `nav-group-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
      return `<div class="nav-section"><button type="button" class="nav-group" data-shell="group" data-group="${esc(group)}" aria-expanded="${open}" aria-controls="${gid}"><span class="lbl">${esc(t("group." + group.toLowerCase(), null, group))}</span><span class="chev" aria-hidden="true"></span></button><div id="${gid}" class="nav-links"${open ? "" : " hidden"}>${shown.map(link).join("")}</div></div>`;
    }).join("");
  }
  // The fleet overview document (exact fields read): machines[] = {id, status, engines: [{profile, role ("rank 0",
  // "rank 1", "replica", "router", "prefill service"), throughput (completed tok/s, number or null), follower_of
  // (rank 0's machine, rank 1 rows only)}]}. Rates add up over the machine's own serving rows (rank 0, replica);
  // rank 1 rows name their rank 0 instead; a missing rate is omitted, never 0.
  function overviewSummary(id) {
    const d = state.optional.overview;
    const row = rows(d?.machines).find(item => item.id === id);
    if (!row) return null;
    const engines = rows(row.engines);
    const serving = engines.filter(e => ["rank 0", "replica"].includes(e.role) && typeof unwrap(e.throughput) === "number" && Number.isFinite(unwrap(e.throughput)));
    const followers = engines.filter(e => e.role === "rank 1").map(e => t("nav.follower", {profile: e.profile ?? "", leader: e.follower_of ?? "?"}, `rank 1 of ${e.profile} · rank 0 on ${e.follower_of}`));
    return {status: typeof row.status === "string" ? row.status : "", followers,
      rate: serving.length ? serving.reduce((sum, e) => sum + unwrap(e.throughput), 0) : null};
  }
  function renderMachinesNav() {
    const all = machines();
    const target = id => pages.machine && Object.hasOwn(routes, "machine") ? `#/machine?id=${encodeURIComponent(id)}` : "#/devices";
    const html = all.length ? `<h2 class="nav-title"><span class="lbl">${esc(t("nav.machines", null, "Machines"))}</span></h2>${all.map(m => {
      const fleet = overviewSummary(m.id) || {}, health = String(fleet.status || unwrap(m.health) || "unknown");
      const style = /^(online|ok|ready|up)$/i.test(health) ? "ok" : /degraded|warn|stale/i.test(health) ? "warn" : /offline|error|down/i.test(health) ? "err" : "off";
      const note = [...(typeof fleet.rate === "number" ? [`${Number(fleet.rate.toFixed(1)).toLocaleString()} tok/s`] : []), ...(fleet.followers || [])].join(" · ");
      return `<a class="nav-machine" href="${target(m.id)}" title="${esc(`${m.name || m.id} · ${health}`)}"><i class="dot dot-${style}" aria-hidden="true"></i><span class="lbl"><span class="nm">${esc(m.name || m.id)}</span><span class="hint">${esc(health)}${note ? ` · ${esc(note)}` : ""}</span></span></a>`;
    }).join("")}` : "";
    if ($("nav-machines").innerHTML !== html) $("nav-machines").innerHTML = html;
  }
  function renderHeartbeat(stale) {
    const el = $("heartbeat");
    if (!el) return;
    const paused = document.hidden || !navigator.onLine;
    const text = paused ? t("beat.paused", null, "Paused") : stale || !state.fetched.devices ? t("beat.stale", null, "Stale") : t("beat.live", {s: prefs().refresh_s}, `Live · ${prefs().refresh_s} s`);
    const style = paused ? "off" : stale || !state.fetched.devices ? "warn" : "ok";
    if (el.textContent !== text) el.textContent = text;
    el.className = `pill p-${style} heartbeat`;
  }

  // F5 / F22: exceptions strip, throttle banner, tab badge, favicon dot, desktop notifications.
  const RED = /^(critical|error|err|fatal)$/i, AMBER = /^(warning|warn)$/i;
  const severityRank = s => RED.test(s) ? 0 : AMBER.test(s) ? 1 : 2;
  const summary = () => state.optional["health/summary"] || null;
  // Stale when the producer says so, when its evaluation is older than 90 s (3 x its 10 s interval, plus a poll),
  // or when the summary could not be fetched for longer than the view's own stale limit: last known values stay
  // visible, marked; no notification is raised from them.
  const HEALTH_MAX_AGE_S = 90;
  function summaryStale() {
    const s = summary();
    if (!s) return false;
    const at = state.optional["health/summary:at"] || 0;
    if (Date.now() - at > staleAfter()) return true;
    const serverNow = timestamp(s.server_time) + (Date.now() - at) / 1000, evaluated = timestamp(s.evaluated_at);
    return s.stale === true || (Number.isFinite(serverNow) && Number.isFinite(evaluated) && serverNow - evaluated > HEALTH_MAX_AGE_S);
  }
  function activeCount() {
    const s = summary();
    if (!s) return 0;
    const counts = object(s.counts) ? Object.values(s.counts).filter(n => typeof n === "number" && n > 0) : [];
    return counts.length ? counts.reduce((a, b) => a + b, 0) : rows(s.strip).length;
  }
  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    if (seconds < 60) return t("dur.s", {n: Math.floor(seconds)}, `${Math.floor(seconds)} s`);
    if (seconds < 3600) return t("dur.min", {n: Math.floor(seconds / 60)}, `${Math.floor(seconds / 60)} min`);
    if (seconds < 86400) return t("dur.h", {h: Math.floor(seconds / 3600), m: Math.floor(seconds % 3600 / 60)}, `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`);
    return t("dur.d", {n: Math.floor(seconds / 86400)}, `${Math.floor(seconds / 86400)} d`);
  }
  let stripOpen = true;
  function renderStrip() {
    const s = summary(), error = state.optional["health/summary:error"], el = $("exceptions");
    let html = "";
    if (s) {
      const items = rows(s.strip).slice().sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || (timestamp(a.since) || 0) - (timestamp(b.since) || 0));
      const now = timestamp(s.server_time);
      if (items.length) {
        const shown = items.slice(0, 6), health = Object.hasOwn(routes, "health");
        const evaluated = timestamp(s.evaluated_at);
        const staleNote = summaryStale() ? ` <span class="pill p-warn">${esc(Number.isFinite(evaluated) ? t("strip.stale_since", {time: formatTime(evaluated)}, `evaluation stale since ${formatTime(evaluated)}`) : t("strip.stale", null, "evaluation stale"))}</span>` : "";
        html = `<div class="row between"><b>${esc(t("strip.title", {n: items.length}, `Exceptions · ${items.length} active`))}${staleNote}</b><button type="button" class="btn sm" data-shell="strip" aria-expanded="${stripOpen}" aria-controls="exception-list">${esc(stripOpen ? t("strip.hide", null, "Hide") : t("strip.show", null, "Show"))}</button></div>`
          + `<ul id="exception-list" class="exception-list"${stripOpen ? "" : " hidden"}>${shown.map(item => {
            const sev = String(item.severity || "info"), style = RED.test(sev) ? "err" : AMBER.test(sev) ? "warn" : "info";
            const since = timestamp(item.since), age = Number.isFinite(now) && Number.isFinite(since) ? duration(now - since) : "";
            return `<li class="exception" data-id="${esc(item.id ?? "")}"><span class="pill p-${style}">${esc(sev)}</span><span class="ex-title">${esc(item.title ?? item.id ?? "")}</span>${item.machine ? tag(item.machine) : ""}<span class="hint">${age ? esc(t("strip.for", {d: age}, `for ${age}`)) : unavailable(t("strip.no_since", null, "start time not reported"))}</span></li>`;
          }).join("")}</ul>${items.length > shown.length || health ? `<p class="hint">${items.length > shown.length ? esc(t("strip.more", {n: items.length - shown.length}, `+${items.length - shown.length} more`)) + " · " : ""}${health ? `<a href="#/health">${esc(t("strip.open", null, "Open health"))}</a>` : ""}</p>` : ""}`;
      }
      else if (summaryStale()) {
        // No findings in the last evaluation, but that evaluation is no longer current: say so.
        const evaluated = timestamp(s.evaluated_at);
        html = `<p><span class="pill p-warn">${esc(Number.isFinite(evaluated) ? t("strip.stale_since", {time: formatTime(evaluated)}, `evaluation stale since ${formatTime(evaluated)}`) : t("strip.stale", null, "evaluation stale"))}</span> ${esc(t("strip.none_known", null, "No findings in the last known evaluation."))}${error ? ` ${unavailable(error)}` : ""}</p>`;
      }
    } else if (error && !state.absent["health/summary"]) html = `<p>${esc(t("strip.unavailable", null, "Exceptions"))} ${unavailable(error)}</p>`;
    if (el.innerHTML !== html) el.innerHTML = html;
    el.hidden = !html;
    const banner = s && typeof s.banner === "string" && s.banner ? (summaryStale() ? `${t("strip.last_known", null, "Last known:")} ${s.banner}` : s.banner) : null;
    $("throttle-banner").hidden = !banner;
    if ($("throttle-banner").textContent !== (banner || "")) $("throttle-banner").textContent = banner || "";
    // The banner is its own alert region (text changes only when the banner does): announced once, not repeated.
  }
  let favicon = "";
  function renderFavicon() {
    const items = rows(summary()?.strip);
    const level = items.some(i => RED.test(i.severity)) ? "red" : items.some(i => AMBER.test(i.severity)) || activeCount() ? "amber" : "";
    if (level === favicon) return;
    favicon = level;
    const link = $("favicon");
    if (!link) return;
    if (!level) { link.href = "/assets/icon.svg"; return; }
    const dot = `<circle cx='24' cy='8' r='7' fill='${level === "red" ? "#d32f2f" : "#e69100"}' stroke='#ffffff' stroke-width='2'/>`;
    link.href = "data:image/svg+xml," + encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='#2f6fde'/><path d='M9 7v18M9 17c0-5 8-5 8 0v8M22 7v18' fill='none' stroke='#ffffff' stroke-width='3' stroke-linecap='round'/>${dot}</svg>`);
  }
  const seenFindings = new Set();
  let summarySeen = false;
  const notifySupported = () => "Notification" in window;
  const notifyOn = () => notifySupported() && store.get("hyperloom-notify", false) && Notification.permission === "granted";
  function summaryArrived(s) {
    // Baseline: the first fresh summary of this page load marks its findings as known (nothing is notified for
    // what was already there when the page opened). After that, a finding not seen in a fresh summary before is
    // notified once; a stale summary neither notifies nor marks anything seen, so a finding that appeared while
    // stale is notified when a fresh evaluation still reports it.
    if (!s || summaryStale()) return;
    const items = rows(s.strip), fresh = [];
    for (const item of items) {
      const id = String(item.id ?? item.title ?? "");
      if (!seenFindings.has(id)) { seenFindings.add(id); if (summarySeen && severityRank(item.severity) < 2) fresh.push(item); }
    }
    summarySeen = true;
    if (!notifyOn() || document.hidden) return;
    for (const item of fresh.slice(0, 3)) {
      try { new Notification(`Hyperloom · ${item.title ?? item.id}`, {body: [item.machine, item.severity].filter(Boolean).join(" · "), tag: String(item.id ?? item.title), icon: "/assets/icon-192.png"}); }
      catch (_) { /* some browsers refuse page notifications; the strip still shows it */ }
    }
  }
  async function toggleNotifications() {
    if (!notifySupported()) return;
    if (store.get("hyperloom-notify", false)) { store.set("hyperloom-notify", false); render(); return; }
    const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    store.set("hyperloom-notify", permission === "granted");
    render();
  }

  // F49: bottom tab bar on narrow screens.
  function renderTabbar(page) {
    // Overview, Machines, Stats, Settings; without an overview page the fourth tab is Logs.
    const overview = Object.hasOwn(routes, "overview") && visible("overview");
    const tabs = [...(overview ? [["overview", t("tab.overview", null, "Overview")]] : []), ["devices", t("tab.machines", null, "Machines")],
      ["stats", t("tab.stats", null, "Stats")], ...(overview ? [] : [["logs", t("tab.logs", null, "Logs")]]), ["settings", t("tab.settings", null, "Settings")]];
    const html = tabs.map(([id, label]) => `<a href="#/${id}"${id === page ? ' aria-current="page" class="on"' : ""}><svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${navIcons[id]}</svg><span>${esc(label)}</span></a>`).join("");
    if ($("tabbar").innerHTML !== html) $("tabbar").innerHTML = html;
  }
  function renderShell(page) {
    renderMachinesNav();
    renderStrip();
    renderFavicon();
    renderTabbar(page);
    const labels = {"palette-open": t("palette.open", null, "Search"), "sidebar-toggle": sidebarCollapsed() ? t("sidebar.expand", null, "Expand sidebar") : t("sidebar.collapse", null, "Collapse sidebar")};
    for (const [id, label] of Object.entries(labels)) if ($(id) && $(id).getAttribute("aria-label") !== label) $(id).setAttribute("aria-label", label);
    if ($("sidebar-toggle")) $("sidebar-toggle").setAttribute("aria-pressed", String(sidebarCollapsed()));
    const strings = {"menu": t("menu", null, "Menu"), "theme-label": t("prefs.theme", null, "Appearance"), "skip-link": t("skip", null, "Skip to content")};
    for (const [id, text] of Object.entries(strings)) if ($(id) && $(id).firstChild?.nodeType === 3 && $(id).firstChild.textContent.trim() !== text) $(id).firstChild.textContent = text;
    const paletteText = {"palette-label": t("palette.open", null, "Search"), "palette-keys": t("palette.keys", null, "")};
    for (const [id, text] of Object.entries(paletteText)) if ($(id).textContent !== text) $(id).textContent = text;
    const searchLabel = $("palette-open").querySelector("span.lbl");
    if (searchLabel.textContent !== t("palette.button")) searchLabel.textContent = t("palette.button");
    if ($("palette-input").placeholder !== t("palette.placeholder")) $("palette-input").placeholder = t("palette.placeholder");
    if ($("palette").getAttribute("aria-label") !== t("palette.label")) $("palette").setAttribute("aria-label", t("palette.label"));
    if ($("palette-mobile").getAttribute("aria-label") !== t("palette.open")) $("palette-mobile").setAttribute("aria-label", t("palette.open"));
    for (const option of $("theme").options) { const text = t("theme." + option.value, null, option.textContent); if (option.textContent !== text) option.textContent = text; }
  }

  // F52: the preferences section of the settings page. Zone choices are the ones the portal accepts.
  const zoneList = {names: null, loading: false, why: ""};
  async function loadZones() {
    zoneList.loading = true;
    try { const body = await request("prefs/timezones"); zoneList.names = Array.isArray(body.names) ? body.names : []; zoneList.why = body.why?.names || ""; }
    catch (error) { zoneList.names = []; zoneList.why = error.message; }
    finally { zoneList.loading = false; state.lastExternal = null; render(); }
  }
  function preferencesSection() {
    const p = prefs(), d = state.optional.prefs, why = d?.why || {};
    const select = (key, label, options, hint = "") => `<div class="field"><label for="pref-${key}">${esc(label)}</label><select class="inp" id="pref-${key}" data-pref="${key}">${options.map(([value, text]) => `<option value="${esc(value)}"${String(p[key]) === String(value) ? " selected" : ""}>${esc(text)}</option>`).join("")}</select>${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
    const zones = ["local", "UTC", ...(Array.isArray(zoneList.names) ? zoneList.names.filter(z => z !== "UTC" && formats(z)) : [])];
    if (!zoneList.names && !zoneList.loading && d) loadZones();
    if (!zones.includes(p.timezone)) zones.push(p.timezone);
    const status = prefsStatus.message ? `<p class="${prefsStatus.error ? "field-error" : "hint"}" role="status">${esc(prefsStatus.message)}</p>` : `<p class="hint" role="status">${d ? (why.values ? unavailable(why.values) : esc(t("prefs.stored", null, "Stored by the portal for every browser on this machine."))) : unavailable(state.absent.prefs ? t("prefs.no_feature", null, "preferences feature not installed; saved in this browser only") : state.optional["prefs:error"] || t("prefs.loading", null, "loading"))}</p>`;
    const appearance = select("theme", t("prefs.theme", null, "Appearance"), PREF_CHOICES.theme.map(v => [v, t("theme." + v, null, v)]))
      + select("density", t("prefs.density", null, "Density"), PREF_CHOICES.density.map(v => [v, t("density." + v, null, v)]))
      + select("language", t("prefs.language", null, "Language"), [["en", "English"], ["es", "Español"]]);
    const data = select("refresh_s", t("prefs.refresh", null, "Refresh every"), PREF_CHOICES.refresh_s.map(v => [v, `${v} s`]), esc(t("prefs.refresh_hint", null, "How often this page asks the portal for new state.")))
      + select("temperature_unit", t("prefs.temperature", null, "Temperature unit"), [["C", "°C"], ["F", "°F"]])
      + select("timezone", t("prefs.timezone", null, "Time zone"), zones.map(z => [z, z === "local" ? t("prefs.tz_local", null, "Control machine (local)") : z]), why.timezone || zoneList.why ? unavailable(why.timezone || zoneList.why) : `${esc(t("prefs.tz_hint", null, "Used for clock times and for day and hour boundaries."))} ${esc(t("prefs.tz_now", {zone: zone().name, source: zone().source}, `Now: ${zone().name} (${zone().source}).`))}`);
    const panels = navGroups().flatMap(([, keys]) => keys).map(id => `<label class="check"><input type="checkbox" data-pref-panel="${esc(id)}" id="pref-panel-${esc(id)}"${visible(id) ? " checked" : ""}${id === "settings" ? ` disabled aria-describedby="pref-panel-note"` : ""}><span>${esc(titleOf(id))}</span></label>`).join("");
    const permission = notifySupported() ? Notification.permission : "unsupported";
    const blocker = !notifySupported() ? t("notify.unsupported", null, "this browser has no desktop notifications") : permission === "denied" ? t("notify.denied", null, "notifications are blocked for this page in the browser settings") : !Object.hasOwn(routes, "health") && !summary() ? t("notify.no_health", null, "needs the health feature") : "";
    const notify = `<p>${esc(t("notify.text", null, "Show a desktop notification when a new warning or critical finding appears while this page is open."))}</p>${button(store.get("hyperloom-notify", false) && permission === "granted" ? t("notify.off", null, "Turn off notifications") : t("notify.on", null, "Turn on notifications"), "", {id: "pref-notify", disabled: blocker, extra: 'data-shell="notify"'})}<p class="hint">${esc(t("notify.state", {state: permission}, `Browser permission: ${permission}`))}</p>`;
    const related = d ? rows(d.related).map(r => `<li>${esc(t("related." + r.id, null, r.label))} · ${r.available ? (Object.hasOwn(routes, r.page) ? `<a href="#/${esc(r.page)}">${esc(t("related.open", {page: titleOf(r.page)}, `Open ${titleOf(r.page)}`))}</a>` : `<span class="mono">${esc(r.endpoint)}</span>`) : unavailable(r.why)}</li>`).join("") : "";
    return `<h2 class="section" id="preferences">${esc(t("prefs.title", null, "Portal preferences"))}</h2>${status}<div class="grid g2 prefs">${card(t("prefs.appearance", null, "Appearance and language"), appearance, "", "prefs-appearance")}${card(t("prefs.data", null, "Data and time"), data, "", "prefs-data")}${card(t("prefs.panels", null, "Panels in the sidebar"), `${panels}<p class="hint" id="pref-panel-note">${esc(t("prefs.panels_hint", null, "Hidden pages stay reachable from search (Ctrl K) and links. Settings is always shown."))}</p>`, "", "prefs-panels")}${card(t("prefs.notifications", null, "Desktop notifications"), notify, "", "prefs-notifications")}</div>${card(t("prefs.related", null, "Settings kept by other features"), related ? `<ul class="related">${related}</ul>` : unavailable(state.absent.prefs ? t("prefs.no_feature", null, "preferences feature not installed; saved in this browser only") : t("prefs.loading", null, "loading")), t("prefs.related_sub", null, "energy price, alerts, MQTT, retention, intervals"), "prefs-related")}<h2 class="section">${esc(t("prefs.pairing", null, "Pairing, secrets and configuration"))}</h2>`;
  }

  // F6: command palette.
  const palette = {items: [], index: 0, recipes: null, recipesAt: 0, returnFocus: null};
  function paletteEntries() {
    const out = [];
    const go = href => () => { location.hash = href; };
    for (const [group, keys] of navGroups()) for (const id of keys)
      out.push({kind: t("palette.kind.page", null, "Page"), label: titleOf(id), keywords: `${id} ${group} ${t("group." + group.toLowerCase(), null, group)}${visible(id) ? "" : " hidden"}`, hint: visible(id) ? t("group." + group.toLowerCase(), null, group) : t("palette.hidden", null, "hidden from sidebar"), run: go(`#/${id}`)});
    for (const m of machines()) out.push({kind: t("palette.kind.machine", null, "Machine"), label: m.name || m.id, keywords: `${m.id} ${unwrap(m.os) ?? ""} ${unwrap(m.health) ?? ""}`, hint: String(unwrap(m.health) ?? ""), run: go(pages.machine && Object.hasOwn(routes, "machine") ? `#/machine?id=${encodeURIComponent(m.id)}` : "#/devices")});
    for (const p of profiles()) out.push({kind: t("palette.kind.profile", null, "Profile"), label: p.label || profileId(p), keywords: `${profileId(p)} engine ${p.state ?? ""}`, hint: String(p.state ?? ""), run: () => { location.hash = `#/engines?profile=${encodeURIComponent(profileId(p))}`; }});
    for (const r of rows(palette.recipes)) out.push({kind: t("palette.kind.recipe", null, "Recipe"), label: r.name || r.id, keywords: `${r.id} ${r.model ?? ""}`, hint: r.model || "", run: () => { location.hash = `#/recipes?recipe=${encodeURIComponent(r.id)}`; }});
    const blockedStart = mutationReason() || profileBlocker(selected()) || capability("up");
    const actions = [
      [t("act.refresh", null, "Refresh now"), "", () => { refresh(); window.HyperloomRecipes?.refresh(); pageOf(route())?.refresh?.(); }],
      [t("act.sidebar", null, "Toggle sidebar"), "", toggleSidebar, "Ctrl B"],
      [t("act.theme", null, "Theme: light / dark / true black / system"), "", () => setPref({theme: PREF_CHOICES.theme[(PREF_CHOICES.theme.indexOf(prefs().theme) + 1) % PREF_CHOICES.theme.length]})],
      [t("act.density", null, "Density: compact / comfortable"), "", () => setPref({density: prefs().density === "compact" ? "comfortable" : "compact"})],
      [t("act.language", null, "Language: English / Español"), "", () => setPref({language: prefs().language === "es" ? "en" : "es"})],
      [t("act.add", null, "Add machine by address…"), mutationReason(), () => { location.hash = "#/devices"; openDialog("pin"); }],
      [t("act.doctor", null, "Run Doctor…"), mutationReason(), () => openDialog("doctor")],
      [t("act.start", null, "Start profile…"), blockedStart, () => { location.hash = "#/engines"; openDialog("start"); }],
      [t("act.rotate", null, "Rotate engine key…"), mutationReason(), () => { location.hash = "#/settings"; openDialog("rotate"); }],
      [t("act.preferences", null, "Portal preferences"), "", () => { location.hash = "#/settings?section=preferences"; }],
    ];
    for (const [label, blocked, run, hint] of actions) out.push({kind: t("palette.kind.action", null, "Action"), label, keywords: "action command", hint: blocked || hint || "", blocked, run});
    return out;
  }
  // Deep links (F6): #/settings?section=<name> focuses that section; #/recipes?recipe=<id> opens that recipe (through
  // the recipes page's select() when it has one, else its open control once listed). Once per navigation.
  const SECTIONS = {preferences: "preferences", appearance: "prefs-appearance", data: "prefs-data", panels: "prefs-panels", notifications: "prefs-notifications", related: "prefs-related"};
  let linkDone = "";
  function deepLink(page) {
    if (linkDone === location.hash) return;
    const query = hashQuery();
    if (page === "settings" && SECTIONS[query.get("section")]) {
      const el = $(SECTIONS[query.get("section")]);
      if (!el) return;
      linkDone = location.hash;
      el.setAttribute("tabindex", "-1");
      setTimeout(() => { el.scrollIntoView({block: "start"}); el.focus({preventScroll: true}); }, 0);
    } else if (page === "recipes" && query.get("recipe")) {
      const id = query.get("recipe"), recipes = window.HyperloomRecipes;
      if (!recipes?.ready?.()) return;
      if (typeof recipes.select === "function") { linkDone = location.hash; recipes.select(id); return; }
      const control = [...document.querySelectorAll("[data-recipe-action=open]")].find(el => el.dataset.id === id);
      if (control) { linkDone = location.hash; control.click(); }
    } else linkDone = location.hash;
  }
  async function loadRecipes() {
    if (Date.now() - palette.recipesAt < 60000) return;
    palette.recipesAt = Date.now();
    try { const body = await request("recipes"); palette.recipes = body?.result?.recipes || []; if ($("palette").open) paletteRender(); }
    catch (_) { palette.recipes = palette.recipes || []; }
  }
  function openPalette() {
    if (document.querySelector("dialog[open]")) return;
    palette.returnFocus = document.activeElement;
    $("palette-input").value = "";
    palette.index = 0;
    $("palette").showModal();
    paletteRender();
    $("palette-input").focus();
    loadRecipes();
  }
  function closePalette(restore = true) {
    if (!$("palette").open) return;
    $("palette").close();
    if (restore && palette.returnFocus?.isConnected) palette.returnFocus.focus();
  }
  function paletteRender() {
    const query = $("palette-input").value;
    palette.items = paletteEntries().map(item => ({item, s: Math.max(score(item.label, query) * 2, score(`${item.label} ${item.keywords} ${item.kind}`, query))}))
      .filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 50).map(x => x.item);
    palette.index = Math.min(palette.index, Math.max(0, palette.items.length - 1));
    $("palette-list").innerHTML = palette.items.length ? palette.items.map((item, i) => `<li role="option" id="palette-option-${i}" data-index="${i}" aria-selected="${i === palette.index}"${item.blocked ? ' aria-disabled="true"' : ""} class="${i === palette.index ? "on" : ""}"><span class="tag">${esc(item.kind)}</span><span class="pl">${esc(item.label)}</span><span class="hint">${esc(item.hint || "")}</span></li>`).join("") : `<li class="hint" role="presentation">${esc(t("palette.none", null, "No matches"))}</li>`;
    $("palette-input").setAttribute("aria-activedescendant", palette.items.length ? `palette-option-${palette.index}` : "");
    $("palette-list").querySelector("li.on")?.scrollIntoView({block: "nearest"});
  }
  function paletteRun(index = palette.index) {
    const item = palette.items[index];
    if (!item) return;
    if (item.blocked) { $("palette-status").textContent = t("palette.blocked", {why: item.blocked}, `Unavailable · ${item.blocked}`); return; }
    closePalette(false);
    item.run();
  }
  $("palette-input").addEventListener("input", () => { palette.index = 0; $("palette-status").textContent = ""; paletteRender(); });
  $("palette-input").addEventListener("keydown", event => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const n = palette.items.length; if (!n) return;
      palette.index = (palette.index + (event.key === "ArrowDown" ? 1 : n - 1)) % n; paletteRender();
    } else if (event.key === "Enter") { event.preventDefault(); paletteRun(); }
  });
  $("palette-list").addEventListener("click", event => { const li = event.target.closest("li[data-index]"); if (li) paletteRun(Number(li.dataset.index)); });
  $("palette").addEventListener("cancel", event => { event.preventDefault(); closePalette(); });
  $("palette").addEventListener("click", event => { if (event.target === $("palette")) closePalette(); });

  // Sidebar collapse (Ctrl/Cmd-B) and shortcuts.
  const narrow = matchMedia("(max-width: 760px)");
  const sidebarCollapsed = () => document.querySelector(".app").classList.contains("collapsed");
  function toggleSidebar() {
    if (narrow.matches) { $("menu").click(); return; }
    const collapsed = !sidebarCollapsed();
    document.querySelector(".app").classList.toggle("collapsed", collapsed);
    store.set("hyperloom-sidebar", collapsed ? "collapsed" : "open");
    render();
  }
  if (store.get("hyperloom-sidebar") === "collapsed") document.querySelector(".app").classList.add("collapsed");
  document.addEventListener("keydown", event => {
    const mod = (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey;
    if (mod && event.key.toLowerCase() === "k") { event.preventDefault(); if ($("palette").open) closePalette(); else openPalette(); return; }
    const typing = event.target.closest?.("input,textarea,select,[contenteditable]");
    if (mod && event.key.toLowerCase() === "b" && !typing && !document.querySelector("dialog[open]")) { event.preventDefault(); toggleSidebar(); return; }
    if (event.key === "Escape" && event.target.classList?.contains("info-btn")) {
      const id = event.target.getAttribute("aria-describedby");
      tips.open.delete(id); tips.dismissed.add(id); applyTips();
    }
  });
  // Focus leaving the button (not a re-render, which puts focus back on the same id) or the pointer leaving the
  // box ends the click-open and Escape-dismissed states.
  document.addEventListener("focusout", event => {
    if (!event.target.classList?.contains("info-btn")) return;
    const id = event.target.getAttribute("aria-describedby");
    setTimeout(() => {
      if (document.activeElement?.getAttribute?.("aria-describedby") === id) return;     // a re-render kept focus here
      tips.open.delete(id); tips.dismissed.delete(id); applyTips();
    }, 0);
  });
  document.addEventListener("mouseover", event => {
    const id = event.target.closest?.(".info")?.querySelector("[role=tooltip]")?.id;
    if (id && tips.hover !== id) { tips.hover = id; applyTips(); }
  });
  document.addEventListener("mouseout", event => {
    const box = event.target.closest?.(".info");
    if (!box || box.contains(event.relatedTarget)) return;
    const id = box.querySelector("[role=tooltip]")?.id;
    if (tips.hover === id) tips.hover = null;
    if (document.activeElement?.getAttribute?.("aria-describedby") !== id) tips.dismissed.delete(id);
    applyTips();
  });
  document.addEventListener("click", event => {
    if (tips.open.size && !event.target.closest(".info")) { tips.open.clear(); applyTips(); }
    const tip = event.target.closest("[data-info]");
    if (tip) {
      const id = tip.getAttribute("aria-describedby");
      if (tips.open.has(id)) tips.open.delete(id); else tips.open.add(id);
      tips.dismissed.delete(id); applyTips(); return;
    }
    const shell = event.target.closest("[data-shell]");
    if (!shell || shell.disabled) return;
    const kind = shell.dataset.shell;
    if (kind === "group") {
      const group = shell.dataset.group;
      if (shell.getAttribute("aria-expanded") === "true") collapsedGroups.add(group); else collapsedGroups.delete(group);
      store.set("hyperloom-nav-groups", [...collapsedGroups]);
      const open = shell.getAttribute("aria-expanded") !== "true";
      shell.setAttribute("aria-expanded", String(open));
      $(shell.getAttribute("aria-controls")).hidden = !open;
      navKey = JSON.stringify([i18n.lang, prefs().hidden_panels, [...collapsedGroups], groupOf(route()) || "Other", Object.keys(routes)]);
    } else if (kind === "strip") { stripOpen = !stripOpen; renderStrip(); $("exceptions").querySelector("[data-shell=strip]")?.focus(); }
    else if (kind === "notify") toggleNotifications();
    else if (kind === "palette") openPalette();
    else if (kind === "sidebar") toggleSidebar();
  });
  document.addEventListener("change", event => {
    const el = event.target;
    if (el.dataset?.pref) setPref({[el.dataset.pref]: el.dataset.pref === "refresh_s" ? Number(el.value) : el.value});
    else if (el.dataset?.prefPanel) {
      const hidden = new Set(prefs().hidden_panels);
      if (el.checked) hidden.delete(el.dataset.prefPanel); else hidden.add(el.dataset.prefPanel);
      setPref({hidden_panels: [...hidden].sort()});
    } else if (el.dataset?.filterStatus) { state.filters[el.dataset.filterStatus] = {...filterValue(el.dataset.filterStatus), status: el.value}; state.lastExternal = null; render(); }
  });
  document.addEventListener("input", event => {
    const el = event.target;
    if (el.dataset?.filter) { state.filters[el.dataset.filter] = {...filterValue(el.dataset.filter), text: el.value}; state.lastExternal = null; render(); }
  });
  i18n.onchange?.(() => { navKey = ""; state.lastExternal = null; });

  // F50: installable shell. The worker caches only static scripts, styles and icons; it is removed when the
  // portal stops serving it.
  async function removeWorker() {
    try {
      const mine = new URL("/sw.js", location.origin).href;
      for (const registration of await navigator.serviceWorker.getRegistrations()) {
        const worker = registration.active || registration.waiting || registration.installing;
        if (worker?.scriptURL !== mine) continue;
        for (const each of [registration.active, registration.waiting, registration.installing]) each?.postMessage("hyperloom-shell-retire");
        await registration.unregister();
      }
      const drop = async () => { for (const name of await caches.keys()) if (name.startsWith("hyperloom-shell-")) await caches.delete(name); };
      await drop();
      // A request the old worker was still answering may have written the cache again: drop once more.
      setTimeout(() => drop().catch(() => {}), 1500);
    } catch (_) { /* nothing registered */ }
  }
  async function installWorker() {
    const token = document.querySelector('meta[name="hyperloom-csrf"]')?.content;
    if (!("serviceWorker" in navigator) || !window.isSecureContext || !token || token === "__HYPERLOOM_CSRF__") return;
    // A refused connection (the page's first burst of requests) is retried a few times before giving up until the
    // next load.
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const response = await fetch("/sw.js", {method: "HEAD", cache: "no-store", credentials: "same-origin"});
        if (response.status === 404 || response.status === 410) { await removeWorker(); return; }
        if (response.ok) await navigator.serviceWorker.register("/sw.js", {scope: "/"});
        return;
      } catch (_) { await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1))); }
    }
  }
  applyPrefs();
  addEventListener("load", installWorker);
  systemTheme.addEventListener("change", applyPrefs);
  $("theme").addEventListener("change", () => setPref({theme: $("theme").value}));
  // Shared with recipes.js (the Recipes & profiles page): the same rendering helpers, request path and job flow.
  // Benchmark-class jobs started from this page stay alive only while it renews them (abandoned otherwise).
  const owned = new Set();
  function own(jobId) { if (jobId) { owned.add(jobId); renewOwned(); } }
  async function renewOwned() {
    if (!owned.size) return;
    try {
      const answer = await request("jobs/renew", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": document.querySelector('meta[name="hyperloom-csrf"]')?.content || ""}, body: JSON.stringify({jobs: [...owned].slice(0, 32)})});
      // Only the server's answer ends ownership (the job is no longer running); a stale poll never does.
      for (const id of rows(answer.not_running)) owned.delete(id);
    } catch (_) { /* the next renewal retries; the server cancels a job only after two minutes without one */ }
  }
  setInterval(renewOwned, 15000);
  window.Hyperloom = {esc, display, unavailable, statusPill, tag, button, card, table, cells, kv, empty, rows, own,
    request, mutate, refresh, render, announce, mutationReason, capability, profiles, profileId, state, activeJob,
    doc, machines, meter, kpi, chart, lineChart, spark, field, route, routes, pages, download,
    info, problem, filter, filterValue, matches, prefs, setPref, temperature, time: formatTime, zone, t, i18n,
    palette: openPalette, toggleSidebar,
    csrf: () => document.querySelector('meta[name="hyperloom-csrf"]')?.content};
  render(); resetPolling();
})();
