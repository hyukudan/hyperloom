/* Local portal: source and capability rules are docs/ui/UI-SPEC.md sections 9–11. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const routes = {devices: "Devices", topology: "Topology & links", models: "Models & placement", engines: "Engines", stats: "Live stats", logs: "Logs", settings: "Settings & pairing"};
  const navIcons = {
    devices: '<rect x="3" y="4" width="14" height="10" rx="2"/><path d="M7 17h6"/>',
    topology: '<circle cx="5" cy="5" r="2.2"/><circle cx="15" cy="5" r="2.2"/><circle cx="10" cy="15" r="2.2"/><path d="M7 5h6M6.2 7l2.6 6M13.8 7l-2.6 6"/>',
    models: '<path d="M10 2l7 4v8l-7 4-7-4V6z"/><path d="M3 6l7 4 7-4M10 10v8"/>',
    engines: '<path d="M5 4l10 6-10 6z"/>',
    stats: '<path d="M3 16V9M8 16V4M13 16v-6M18 16v-9"/>',
    logs: '<path d="M4 4h12M4 8h12M4 12h8M4 16h10"/>',
    settings: '<circle cx="10" cy="10" r="3"/><path d="M10 2v3M10 15v3M2 10h3M15 10h3M4.3 4.3l2 2M13.7 13.7l2 2M4.3 15.7l2-2M13.7 6.3l2-2"/>'
  };
  const endpoints = ["devices", "topology", "models", "profiles", "stats", "logs", "settings"];
  const api = "/hyperloom/api/";
  const state = {docs: {}, fetched: {}, errors: {}, fresh: false, busy: false, job: null, selected: "", dialog: null, returnFocus: null, logProcess: "all", logLevel: "warnings", logText: "", range: 900, warnings: new Set(), generation: 0, operation: 0, switching: false};
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
    return all.find(p => (p.id || p.name || p.profile) === state.selected) || all.find(p => (p.id || p.name || p.profile) === "qwen-replicas") || all[0];
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
  function card(title, content, subtitle = "") {
    return `<section class="card"><h2>${esc(title)}${subtitle ? `<span class="sub">${esc(subtitle)}</span>` : ""}</h2>${content}</section>`;
  }
  function table(headers, body, label = "Data") {
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="${esc(label)}"><table><thead><tr>${headers.map(h => `<th scope="col"${Array.isArray(h) ? ` class="${h[1]}"` : ""}>${esc(Array.isArray(h) ? h[0] : h)}</th>`).join("")}</tr></thead><tbody>${body || `<tr><td colspan="${headers.length}">No entries reported.</td></tr>`}</tbody></table></div>`;
  }
  const cells = values => `<tr>${values.map(v => `<td${/class="[^"]*\bhide-n\b/.test(v) ? ' class="hide-n"' : ""}>${v}</td>`).join("")}</tr>`;
  function kv(owner, entries) {
    return `<dl class="kv">${entries.map(([label, key, unit]) => `<dt>${esc(label)}</dt><dd>${field(owner, key, unit)}</dd>`).join("")}</dl>`;
  }
  function empty(title, detail, action = "") {
    return `<section class="card empty"><h2>${esc(title)}</h2><p class="t2">${esc(detail)}</p>${action}</section>`;
  }
  const mutationReason = () => !state.fresh ? "Waiting for authoritative state" : state.busy || state.switching || activeJob() ? "Another action is running" : "";
  const activeJob = () => state.job && !["done", "completed", "succeeded", "failed", "error", "cancelled", "canceled", "stopped"].includes(state.job.state || state.job.status);
  function meter(label, value, total, unit, why, offline = false) {
    value = unwrap(value); total = unwrap(total);
    const known = typeof value === "number" && typeof total === "number" && total > 0 && !offline;
    return `<div class="mrow"><span>${esc(label)}</span><span class="v">${offline ? unavailable("machine offline") : display(value, why, unit)}${total != null && !offline ? ` / ${display(total, why, unit)}` : ""}</span><div class="meter" aria-hidden="true">${known ? `<i data-meter="${Math.min(100, Math.max(0, value / total * 100))}"></i>` : ""}</div></div>`;
  }
  function kpi(label, value, note, history) {
    return `<div class="card kpi"><div class="lbl">${esc(label)}</div><div class="val">${value}</div><div class="dl muted">${note || ""}</div>${history?.some(v => typeof v === "number") ? `<div class="spk">${spark(history, label)}</div>` : ""}</div>`;
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
    const kpis = `<div class="grid g4 kpis">${kpi("Machines online", field(summary, "online", "", reason(d, "machines")), `${all.length} configured · ${discovered.length} discovered`)}${kpi("Accelerators in use", field(summary, "accelerators_in_use"), field(summary, "excluded_reason"))}${kpi("GPU memory headroom", field(summary, "memory_free_bytes", "bytes"), `Minimum per device: ${field(summary, "minimum_headroom_bytes", "bytes")}`)}${kpi("Links", field(summary, "links_healthy"), field(summary, "links_summary"))}</div>`;
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
        return `<div class="accel"><div class="an">${name ? esc(name) : unavailable(reason(g, "name"))}${excluded ? statusPill("Excluded") : ""}</div>${unpaired ? "" : excluded ? `<p class="hint">${field(g, "policy_reason")}</p>` : `<div class="hint">${esc(g.memory_kind || "VRAM")}</div>${meter("GPU memory", g.memory_used_bytes, g.memory_total_bytes, "bytes", reason(g, "memory_used_bytes"), offline)}${meter("GPU", g.utilization_pct, 100, "%", reason(g, "utilization_pct"), offline)}${kv(g, [["Temperature", "temperature_c", "°C"], ["Power", "power_w", "W"], ...(!m.rocm ? [["Driver", "driver"]] : [])])}`}</div>`;
      }).join("") : unavailable(reason(m, "accelerators"))}
      ${unpaired ? later("Pair", "later phase · TensorMeld agent · TM-R10..R13") : `${m.rocm ? `<p class="hint runtime">${esc(m.rocm)}</p><details class="packages"><summary>ROCm packages</summary><pre class="raw">${esc(JSON.stringify(m.rocm_packages, null, 2))}</pre></details>` : ""}${!/win/i.test(m.os || "") ? `<p class="hint">${field(m, "kernel_summary")}</p>` : ""}${usb ? `<div class="usb-summary"><p>${esc(usb.summary)}</p>${rows(usb.cables).map(c => `<p class="hint">${esc(c.summary)} · ${esc(c.health)}</p>`).join("")}</div>` : ""}${m.release ? kv(m, [["Release", "release"]]) : ""}${rows(m.leases).map(l => leaseView(l, d.server_time)).join("")}`}
      ${rows(m.warnings).map(w => `<div class="banner warn">Warning · ${display(w.message ?? w.value ?? w)}</div>`).join("")}${health === "degraded" && !rows(m.warnings).length ? `<div class="banner warn">Degraded · ${display(m.why)}. Available passive readings remain visible.</div>` : ""}${offline ? `<div class="banner">Offline · ${display(m.why)}. Roles are unreachable.</div>` : ""}${m.error ? `<div class="banner">${display(m.error)} <a href="#/logs">Logs and failures</a></div>` : ""}</section>`;
  }
  function renderTopology() {
    const d = doc("topology"), links = rows(d.links);
    return `<section class="card topo-map"><h2>Cluster map <span class="sub">Machines, accelerators, roles and links</span></h2>${topologyMap(rows(d.machines), links)}</section>`
      + card("Links", links.length ? table(["Link", "Kind", "Link speed", "Bandwidth", "RTT p50", ["p99", "hide-n"], "Source", "Health"], links.map(l => cells([display(l.name ?? `${l.from || ""} ↔ ${l.to || ""}`), field(l, "kind"), field(l, "gbps", "Gb/s"), field(l, "bytes_per_s", "bytes/s"), field(l, "rtt_us_p50", "µs"), `<span class="hide-n">${field(l, "rtt_us_p99", "µs")}</span>`, `${field(l, "source")}<div class="hint">${l.source === "driver status" ? "Current negotiated speed · throughput needs R18" : l.source === "interface status" ? "Negotiated speed · no link-probe receipt" : "Dated receipt · RTT"}</div>`, `${statusPill(l.health ?? l.state, reason(l, "health"))}${rows(l.warnings).map(w => `<p class="hint">${esc(w)}</p>`).join("")}`])).join(""), "Links and dated measurements") + `<div class="topo-link-list">${links.map(l => `<article class="accel"><b>${display(l.name)}</b><div class="row between"><span>${field(l, "kind")} · ${field(l, "gbps", "Gb/s")}</span>${statusPill(l.health)}</div><p class="hint">${field(l, "source")} · negotiated speed</p><p class="hint">Bandwidth: ${field(l, "bytes_per_s", "bytes/s")}<br>RTT p50: ${field(l, "rtt_us_p50", "µs")}</p>${rows(l.warnings).map(w => `<p class="hint">${esc(w)}</p>`).join("")}</article>`).join("")}</div>` : unavailable(reason(d, "links", "no link-probe receipt")), "Negotiated link speed is distinct from measured bandwidth")
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
    if ((p.configuration?.preset || profileId(p)) !== "qwen-replicas") return p.blocker || "later phase · Phase 2 / 3a / R14";
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
    const identity = `<dl class="kv"><dt>Started</dt><dd>${p.started_at == null ? unavailable(reason(p, "started_at", "profile has not started")) : esc(new Date(timestamp(p.started_at) * 1000).toLocaleString())}</dd></dl>` + kv(p, [["Release", "release"], ["Qualified receipt", "receipt"], ["Identity", "identity"], ["Router runs on", "router"], ["Linked machines", "nodes"], ["Linked devices", "devices"]]);
    return card(p.label || profileId(p), choice + `<div class="row">${statusPill(p.state, reason(p, "state"))} ${profileBlocker(p) ? `<span class="hint">${esc(profileBlocker(p))}</span>` : ""}</div>${identity}${rows(p.leases).map(l => leaseView(l, d.server_time)).join("")}${p.error ? `<p class="field-error">${display(p.error)}</p><a class="btn" href="#/logs">View failure and logs</a>` : ""}`)
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
      const tiles = `<div class="grid stats-kpis">${kpi("Throughput · completed", field(e, "throughput", "tok/s"), "10 s windows · reset-aware engine counter", series("throughput"))}${kpi("TTFT p50 · engine-side", field(e, "ttft_p50", "ms"), `queue + prefill · p95 ${field(e, "ttft_p95", "ms")} · n=${display(e.ttft_p50?.n)}`, series("ttft_p50"))}${kpi("Queue", field(metrics, "waiting", "waiting"), `${field(metrics, "running")} running · ${field(status.slots, "total")} slots`)}${kpi("Draft acceptance", field(e, "draft_acceptance"), "Windowed accepted / proposed")}${kpi("Prefill placement", unavailable("router counts shown below"), "Router source")}${kpi("Errors · stalls", field(metrics, "failed"), `engine cumulative failures · ${unavailable("stall counter unavailable")}`)}</div>`;
      return card(e.id, `${tiles}<div class="grid g2">${chart("Throughput", [{name: "completed", data: series("throughput"), color: "--s1"}], history.map(s => s.time), "tok/s")}${chart("Time to first token · engine-side", [{name: "p50", data: series("ttft_p50"), color: "--s1"}, {name: "p95", data: series("ttft_p95"), color: "--s4"}], history.map(s => s.time), "ms")}</div><div class="grid g2">${card("Memory headroom", kv(status.memory, [["Free", "device_free_bytes", "bytes"], ["Total", "device_total_bytes", "bytes"]]), "Engine-side · GPU-visible memory")}${card("USB4 exchange mean", `<p>${field(e, "usb4_exchange_mean_us", "µs")}</p>`, "Engine exchange totals · mean over sampling window")}</div>`, e.profile || "Engine telemetry");
    }).join("");
    const requests = rows(d.recent_requests), correlated = requests.some(r => r.route != null);
    return range + (engines.length ? perEngine : empty("No engine telemetry", "Start a qualified profile to collect completed throughput, engine-side TTFT and queue samples.", `<a class="btn" href="#/engines">Open engines</a>`))
      + card("Router queue & prefill placement", rows(d.routers).length ? rows(d.routers).map(r => `<div class="accel"><b>${esc(r.profile || r.id)}</b>${kv(r, [["Waiting", "waiting"]])}${kv(r.prefill, [["Placement counters", "placements"]])}</div>`).join("") : unavailable(reason(d, "routers", "router not running")), "Latest router status · placements are counters")
      + card("Recent requests", `<div class="row between"><p class="hint">Completed · engine-side TTFT = queue + prefill · route needs R7 correlation</p>${button("Export CSV", "export-csv", {disabled: requests.length ? "" : "No completed requests"})}</div>${requests.length ? table(["Time", ...(correlated ? ["Route"] : []), "Engine", "Prompt", ["Cached", "hide-n"], "TTFT · ms", "Decode · tok/s", ["Output", "hide-n"], "Status"], requests.map(r => cells([field(r, "time"), ...(correlated ? [field(r, "route")] : []), field(r, "engine"), field(r, "prompt_tokens"), `<span class="hide-n">${field(r, "reused_tokens")}</span>`, field(r, "engine_ttft_ms"), field(r, "decode_tok_s"), `<span class="hide-n">${field(r, "output_tokens")}</span>`, statusPill(r.status, reason(r, "status"))])).join("")) : unavailable(reason(d, "recent_requests", "no completed requests"))}`);
  }
  function chart(title, series, times, unit) {
    const values = series.flatMap(s => s.data).filter(v => typeof v === "number" && Number.isFinite(v));
    if (!values.length) return card(title, `<div class="chart">${unavailable("requires telemetry samples")}</div>`, unit);
    const max = Math.max(...values, 1) * 1.15;
    const legend = `<div class="legend">${series.map(s => `<span><i class="series-${esc(s.color.slice(3))}"></i>${esc(s.name)}</span>`).join("")}</div>`;
    const summary = `${title}, ${unit}. ${series.map(s => `${s.name}: last ${displayText(s.data.at(-1))}`).join("; ")}. Range ${Math.min(...values).toFixed(2)} to ${Math.max(...values).toFixed(2)} ${unit}.`;
    return card(title, `<div class="chart">${legend}${lineChart(series, {max, unit, summary, times})}<details><summary>View as table</summary>${table(["Time", ...series.map(s => `${s.name} · ${unit}`)], times.map((time, index) => cells([esc(clockTime(time)), ...series.map(s => display(s.data[index], "no sample"))])).join(""), `${title} values`)}</details></div>`, unit);
  }
  function displayText(value) { return typeof value === "number" ? value.toFixed(2) : "unavailable"; }
  function clockTime(value) {
    const n = timestamp(value);
    return Number.isFinite(n) ? new Date(n * 1000).toLocaleTimeString() : "unavailable";
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
    return `<div class="grid g2">${card("Add by address", `<p>Declare an SSH machine in the cluster configuration, then compare and pin its host-key fingerprint.</p>${button("Add by address", "add", {disabled: mutationReason(), style: "pri"})}<h2>Pairing wizard</h2><ol><li>New machine displays a one-time code and fingerprint.</li><li>Compare the fingerprint and choose GPU worker or observer.</li><li>Exchange keys to grant access.</li></ol>${later("Pair machine", "later phase · TensorMeld agent · TM-R10..R13")}<p class="hint">Discovery only announces; it never grants access.</p>`)}${card("Trusted machines", trusted.length ? table(["Machine", "Pinned", "Host key", "Access"], trusted.map(m => cells([display(m.machine ?? m.id), field(m, "paired_at"), `${field(m, "algorithm")}<div class="mono">${field(m, "fingerprint")}</div>`, "SSH · pinned key"])).join("")) + later("Disconnect", "unavailable · uninstall / unpin endpoint not exposed") + later("Revoke enrollment", "later phase · TensorMeld agent") : unavailable(reason(d, "trusted_machines", "no pinned host keys")))}</div>
      <div class="grid g2">${card("Secrets", kv(d.secrets, [["Storage", "storage"], ["Values", "values"]]) + button("Rotate engine key…", "rotate-dialog", {disabled: mutationReason()}) + `<p class="hint">Restart required after rotation. Reservations, approvals, pairings and secrets cannot be rolled back as configuration.</p>`)}${card("Lab rules", kv(d.lab_rules, [["GPU-turn labels", "label_prefix"], ["Turn cap", "turn_cap_s", "s"], ["Drain margin", "drain_margin_s", "s"], ["USB4 layout", "usb4_layout"], ["Excluded", "excluded"], ["Extended turn", "extended_turn"]]))}</div>
      ${card("Configuration", `<p class="mono">${field(d, "config_path")}</p>${d.configuration != null ? `<details><summary>View redacted configuration</summary><pre class="raw">${esc(JSON.stringify(d.configuration, null, 2))}</pre></details>${button("Export (redacted)", "export-config")}` : unavailable(reason(d, "configuration"))}${later("Import configuration", "unavailable · validation / import endpoint not exposed")}`, "hyperloom/cluster-v1 · versioned configuration")}`;
  }
  function route() {
    const name = location.hash.replace(/^#\/?/, "").split(/[?&]/)[0];
    return Object.hasOwn(routes, name) ? name : "devices";
  }
  function render() {
    const page = route(), source = page === "engines" ? "profiles" : page;
    document.title = `Hyperloom · ${routes[page]}`;
    document.body.dataset.page = page;
    $("page-title").textContent = routes[page];
    if (!$("navigation").childElementCount) $("navigation").innerHTML = Object.entries(routes).map(([key, title]) => `<a href="#/${key}"><svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${navIcons[key]}</svg>${esc(title)}</a>`).join("");
    $("navigation").querySelectorAll("a").forEach(link => {
      const active = link.getAttribute("href") === `#/${page}`;
      link.classList.toggle("on", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const current = selected(), disabled = mutationReason();
    const controls = page === "devices" ? button("Add by address", "add", {id: "add-machine", style: "pri", disabled}) + button("Doctor…", "doctor-dialog", {id: "doctor-devices", disabled}) : page === "engines" ? button("Start profile…", "start-dialog", {id: "start-profile", style: "pri", disabled: disabled || profileBlocker(current)}) + button("Install…", "install-dialog", {id: "install-profile", disabled: disabled || profileBlocker(current)}) + button("Doctor…", "doctor-dialog", {id: "doctor-profile", disabled}) + button("Stop profile", "stop", {id: "stop-profile", style: "dan", disabled: current ? "" : "No profile configured"}) : page === "topology" ? button("Measure Ethernet links", "", {disabled: "later phase · Phase 2"}) : page === "logs" ? button("Download redacted log", "export-log", {disabled: logEntries().length ? "" : "No log entries"}) : "";
    if ($("page-actions").innerHTML !== controls) $("page-actions").innerHTML = controls;
    const running = profiles().find(p => ["running", "starting", "stopping", "degraded", "error", "failed"].includes(p.state));
    const global = running?.state || (profiles().length ? "stopped" : state.docs.profiles ? "not configured" : "loading");
    $("global-status").outerHTML = `<span id="global-status">${statusPill(global)}</span>`;
    $("mobile-status").outerHTML = `<span id="mobile-status">${statusPill(global)}</span>`;
    const local = machines().find(m => m.access === "local");
    $("control-machine").textContent = local ? `Control · ${local.name || local.id}` : "Control · this machine";
    $("version").textContent = doc("settings").version ? `Hyperloom ${doc("settings").version}` : "Hyperloom · local portal";
    const renderers = {devices: renderDevices, topology: renderTopology, models: renderModels, engines: renderEngines, stats: renderStats, logs: renderLogs, settings: renderSettings};
    if (state.docs[source]) {
      const focused = document.activeElement;
      // Keep a keyboard user's focus stable across polls and filters.
      const focusId = $("main").contains(focused) ? focused.id : "";
      const selection = focused?.selectionStart;
      const scroll = $("main").querySelector(".log")?.scrollTop;
      const openDetails = [...$("main").querySelectorAll("details")].map(el => el.open);
      $("main").innerHTML = renderers[page]();
      $("main").querySelectorAll("button,input,select,a,summary,[tabindex]").forEach((el, index) => { if (!el.id) el.id = `route-${page}-control-${index}`; });
      $("main").querySelectorAll("[data-meter]").forEach(el => { el.style.width = `${el.dataset.meter}%`; });
      [...$("main").querySelectorAll("details")].forEach((el, i) => { el.open = openDetails[i] || false; });
      if (focusId && $(focusId)) {
        $(focusId).focus({preventScroll: true});
        if (selection != null && typeof $(focusId).setSelectionRange === "function" && ["text", "search"].includes($(focusId).type)) $(focusId).setSelectionRange(selection, selection);
      }
      if (scroll != null && $("main").querySelector(".log")) $("main").querySelector(".log").scrollTop = scroll;
    } else if (state.errors[source]) {
      $("main").innerHTML = empty("Portal data unavailable", state.errors[source], button("Retry", "refresh"));
    }
    $("main").setAttribute("aria-busy", state.docs[source] ? "false" : "true");
    renderConnection();
    renderJob();
    updateDialogGate();
  }
  function renderConnection() {
    const oldest = Math.min(...Object.values(state.fetched), Date.now());
    const stale = Date.now() - oldest > 6000 || Object.keys(state.errors).length > 0;
    document.body.classList.toggle("stale", stale);
    const message = document.hidden ? "Polling paused in background. Controls resume after authoritative state is fetched." : !navigator.onLine ? "Offline. Displaying the last received state." : stale ? `Stale · as of ${new Date(oldest).toLocaleTimeString()}. ${Object.values(state.errors).join(" · ")}` : !state.fresh ? "Refreshing authoritative state before enabling actions…" : "";
    $("connection").hidden = !message;
    $("connection").textContent = message;
  }
  function renderJob() {
    const panel = $("job-panel"), job = state.job;
    panel.hidden = !job;
    if (!job) return;
    panel.innerHTML = `<div class="row"><b>${esc(job.action || "Action")} · ${statusPill(job.state || job.status)}</b><span class="mono">${esc(job.id || job.job || "")}</span>${activeJob() && job.action === "up" ? button("Cancel start now", "cancel") : ""}${activeJob() ? button("Stop profile now", "stop", {style: "dan"}) : ""}</div><p>${display(job.progress, "waiting for progress")}</p>${job.error ? `<p class="field-error">${display(job.error)}</p><a class="btn" href="#/logs">View logs</a>` : ""}${job.result != null ? `<details><summary>Result (redacted)</summary><pre class="raw">${esc(JSON.stringify(job.result, null, 2))}</pre></details>` : ""}`;
  }
  async function request(endpoint, options = {}) {
    const response = await fetch(api + endpoint, {cache: "no-store", credentials: "same-origin", ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  async function refresh() {
    if (document.hidden || !navigator.onLine) { state.fresh = false; renderConnection(); return; }
    if (inFlight) return inFlight;
    const generation = state.generation;
    inFlight = (async () => {
      const names = [...endpoints, "jobs"];
      pollAbort = new AbortController();
      const timeout = setTimeout(() => pollAbort?.abort(), 30000);
      const results = await Promise.allSettled(names.map(name => {
        const query = name === "logs" && state.logProcess !== "all" && state.logProcess !== "portal" && selected() ? `?${new URLSearchParams({profile: profileId(selected()), process: state.logProcess, tail: "200"})}` : "";
        return request(name + query, {signal: pollAbort.signal});
      }));
      clearTimeout(timeout);
      if (generation !== state.generation || document.hidden) return;
      for (let i = 0; i < results.length; i++) {
        const name = names[i], result = results[i];
        if (result.status === "fulfilled") {
          if (name === "jobs") {
            const jobs = rows(result.value.jobs), active = jobs.find(j => (j.id || j.job) === result.value.active);
            state.job = active || jobs.find(j => (j.id || j.job) === (state.job?.id || state.job?.job)) || state.job;
            if (state.job?.error && !state.warnings.has(state.job.id)) { state.warnings.add(state.job.id); announce(state.job.error); }
          } else { state.docs[name] = result.value; state.fetched[name] = Date.now(); }
          delete state.errors[name];
        } else state.errors[name] = `${name}: ${result.reason.message}`;
      }
      state.fresh = results.every(r => r.status === "fulfilled");
      const warningMessages = machines().flatMap(m => rows(m.warnings).map(w => `${m.id}: ${typeof w === "string" ? w : w.message || w.value || JSON.stringify(w)}`));
      for (const warning of warningMessages) if (!state.warnings.has(warning)) { state.warnings.add(warning); announce(`New warning · ${warning}`); }
      render();
    })().finally(() => { inFlight = null; });
    return inFlight;
  }
  function announce(message) { $("alerts").textContent = message; $("alerts").classList.toggle("warning", message.startsWith("New warning")); }
  function resetPolling() {
    clearInterval(polling);
    polling = null;
    if (!document.hidden) { refresh(); polling = setInterval(refresh, 2000); }
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
      state.fresh = false;
    } catch (error) { state.fresh = false; refresh(); throw error; }
    finally { state.busy = false; render(); }
    await refresh();
  }
  async function stop(cancel = false) {
    state.operation++; state.switching = false;
    const current = state.dialog?.profile || state.job?.arguments?.profile || profileId(selected());
    if (state.dialog) closeDialog();
    if (!current && !state.job?.id) { announce("No profile selected to stop."); return; }
    try { await mutate(cancel ? "cancel" : "down", cancel && state.job?.action === "up" ? {job: state.job.id || state.job.job} : {profile: current}, true); }
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
    if (action === "refresh") { refresh(); return; }
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
    if (event.target.id === "profile-select") { state.selected = event.target.value; render(); }
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
    $("main").innerHTML = "";
    render(); refresh(); $("main").focus({preventScroll: true});
  });
  document.addEventListener("visibilitychange", () => { state.generation++; state.fresh = false; if (document.hidden) pollAbort?.abort(); render(); resetPolling(); });
  addEventListener("online", () => { state.fresh = false; render(); resetPolling(); });
  addEventListener("offline", () => { state.generation++; state.fresh = false; pollAbort?.abort(); render(); });
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  let appearance = "system";
  try { appearance = localStorage.getItem("hyperloom-theme") || "system"; } catch (_) { /* Storage may be disabled. */ }
  if (!["light", "dark", "system"].includes(appearance)) appearance = "system";
  function setTheme() { document.documentElement.dataset.theme = appearance === "system" ? systemTheme.matches ? "dark" : "light" : appearance; }
  $("theme").value = appearance; setTheme();
  systemTheme.addEventListener("change", setTheme);
  $("theme").addEventListener("change", () => { appearance = $("theme").value; try { localStorage.setItem("hyperloom-theme", appearance); } catch (_) {} setTheme(); });
  setInterval(() => {
    if (Object.values(state.fetched).some(time => Date.now() - time > 6000) && state.fresh) { state.fresh = false; render(); }
  }, 1000);
  render(); resetPolling();
})();
