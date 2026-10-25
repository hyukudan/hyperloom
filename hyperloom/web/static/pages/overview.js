/* Fleet overview (F1, F2): the lab's KPI strip, then one card per machine. Every number names its observer;
   missing values read "unavailable · why". Tiles can be hidden one by one (kept in this browser). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const KEY = "hyperloom-overview-hidden";
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem(KEY) || "[]"); if (!Array.isArray(hidden)) hidden = []; } catch (_) { hidden = []; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(hidden)); } catch (_) { /* storage may be disabled */ } };
  const fmt = (value, unit) => window.HyperloomTS.fmt(value, unit);
  const num = value => typeof value === "number" && Number.isFinite(value);
  const levelPill = level => {
    const style = {ok: "ok", amber: "warn", red: "err", unknown: "off", info: "info"}[level] || "off";
    const text = {ok: "headroom ok", amber: "amber", red: "red", unknown: "level unknown", info: "no threshold"}[level] || String(level);
    return `<span class="pill p-${style}">${H().esc(text)}</span>`;
  };
  const statusText = {online: "online", stale: "stale", offline: "offline", unknown: "no data yet"};
  const dot = status => `<span class="fl-dot fl-${H().esc(status)}" aria-hidden="true"></span>`;
  function value(owner, key, unit, digits) {
    const {unavailable} = H();
    const v = owner?.[key];
    if (!num(v)) return unavailable(owner?.why?.[key] || "not reported");
    return H().esc(unit === "bytes" ? fmt(v, "bytes") : `${digits == null ? fmt(v, "") : v.toFixed(digits)}${unit ? ` ${unit}` : ""}`);
  }
  function uptime(seconds) {
    if (!num(seconds)) return null;
    const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
    return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
  }
  function hide(id, label) {
    return `<button type="button" class="btn sm fl-hide" data-page-action="ov-hide" data-tile="${H().esc(id)}" aria-label="Hide ${H().esc(label)}">Hide</button>`;
  }
  function tile(id, label, valueHtml, note) {
    if (hidden.includes(id)) return "";
    return `<section class="card kpi fl-kpi${id === "kpi:memory" ? " fl-wide" : ""}"><div class="fl-kpi-head"><div class="lbl">${H().esc(label)}</div>${hide(id, label)}</div><div class="val">${valueHtml}</div><div class="dl muted">${note}</div></section>`;
  }
  function kpis(d) {
    const {esc, unavailable} = H(), k = d.kpis || {};
    const t = k.throughput || {}, p = k.power || {}, tok = k.generated_24h || {}, on = k.machines_online || {};
    const memory = (k.memory?.domains || []).map(m => `<div class="fl-mem"><span>${esc(m.label)}</span><b>${esc(fmt(m.used_bytes, "bytes"))} / ${esc(fmt(m.total_bytes, "bytes"))}</b></div>`).join("");
    return `<div class="grid fl-kpis">${[
      tile("kpi:throughput", "Lab throughput · completed", num(t.value) ? `${esc(t.value.toFixed(1))}<small>tok/s</small>` : unavailable(t.why), `${esc(t.label || "")} · observer: ${esc(t.observer || "engine")}${t.partial ? ` · ${esc(t.partial)}` : ""}`),
      tile("kpi:power", "Lab power", num(p.value) ? `${esc(p.value.toFixed(0))}<small>W</small>` : unavailable(p.why), `${esc(p.label || "")} · ${esc((p.covered || []).length)} sensor domains · not covered: ${esc((p.not_covered || []).join("; "))}`),
      tile("kpi:memory", "Memory in use by domain", memory || unavailable(k.memory?.why), "never merged across domains · observer: node and control machine"),
      tile("kpi:tokens", "Tokens · last 24 h", num(tok.value) ? `${esc(fmt(tok.value, ""))}<small>tokens</small>` : unavailable(tok.why), `${esc(tok.label || "generated tokens")} · observer: engine`),
      tile("kpi:online", "Machines online", num(on.value) ? `${esc(on.value)}<small>of ${esc(on.total)}</small>` : unavailable("not reported"), "collected every 5 s (nodes) / 2 s (control machine)"),
    ].join("")}</div>`;
  }
  function headroom(c) {
    const {esc, unavailable} = H(), limit = c.headroom?.limiting;
    if (!limit) return `<div class="fl-head"><span class="lbl">Limiting memory</span> ${unavailable(c.headroom?.why || "not reported")}</div>`;
    const used = num(limit.total_bytes) && num(limit.available_bytes) ? limit.total_bytes - limit.available_bytes : null;
    const pct = used != null && limit.total_bytes > 0 ? Math.min(100, Math.max(0, used / limit.total_bytes * 100)) : null;
    return `<div class="fl-head"><div class="row between"><span class="lbl">Limiting memory · ${esc(limit.label)}</span>${levelPill(limit.level)}</div>
      <div class="meter" aria-hidden="true">${pct == null ? "" : `<i class="${limit.level === "red" ? "e" : limit.level === "amber" ? "w" : ""}" data-meter="${pct}"></i>`}</div>
      <div class="hint">${esc(fmt(limit.available_bytes, "bytes"))} available of ${esc(fmt(limit.total_bytes, "bytes"))}</div></div>`;
  }
  function gpus(c) {
    const {esc, unavailable} = H();
    if (!(c.gpus || []).length) return `<p class="hint">GPU · ${unavailable(c.why?.gpus || "not reported")}</p>`;
    return `<div class="fl-gpus">${c.gpus.map(g => `<div class="fl-gpu"><b>${esc(g.name || g.device)}</b>${g.excluded ? ' <span class="tag">excluded by policy</span>' : ""}
      <span>busy ${num(g.busy_percent) ? esc(g.busy_percent.toFixed(0) + " %") : unavailable("not reported")}</span>
      <span>${num(g.temperature_c) ? esc(g.temperature_c.toFixed(0) + " °C") : unavailable("no temperature")}</span>
      <span>${num(g.power_w) ? esc(g.power_w.toFixed(0) + " W") : unavailable("no power sensor")}${num(g.power_limit_w) ? esc(" / " + g.power_limit_w.toFixed(0) + " W") : ""}</span>
      ${g.throttle && g.throttle !== "none" ? `<span class="pill ${g.throttle === "thermal" ? "p-err" : "p-warn"}">throttle · ${esc(g.throttle)}</span>` : ""}</div>`).join("")}</div>`;
  }
  function engines(c) {
    const {esc, unavailable} = H();
    if (!(c.engines || []).length) return "";
    return `<div class="fl-engines">${c.engines.map(e => {
      if (e.follower_of) return `<div class="fl-engine"><b>${esc(e.profile)}</b> · rank 1 of the pair · counted on rank 0 (${esc(e.follower_of)})</div>`;
      if (["router", "prefill service"].includes(e.role)) return `<div class="fl-engine"><b>${esc(e.profile)}</b> · ${esc(e.role)}</div>`;
      if (!e.observer) return `<div class="fl-engine"><b>${esc(e.profile)}</b> · ${esc(e.role)} · ${unavailable(e.why?.model)}</div>`;
      const ctx = num(e.context_length) ? `${esc(e.context_length.toLocaleString())} ctx` : unavailable(e.why?.context_length);
      return `<div class="fl-engine"><div><b>${esc(e.profile)}</b> · ${esc(e.role)} · ${e.model ? esc(e.model) : unavailable(e.why?.model)} · ${ctx}</div>
        <div class="hint">${num(e.throughput) ? `${esc(e.throughput.toFixed(1))} tok/s · ${esc(e.throughput_label || "completed")}` : `throughput ${unavailable(e.why?.throughput)}`}</div>
        <div class="hint">running ${num(e.running) ? esc(e.running) : unavailable(e.why?.running)} · queued ${num(e.queued) ? esc(e.queued) : unavailable(e.why?.queued)} · emitted rate ${unavailable(c.why?.emitted_rate)}${e.observer ? ` · ${esc(e.observer)}` : ""}</div></div>`;
    }).join("")}</div>`;
  }
  function machineCard(c) {
    const {esc, unavailable, tag} = H(), id = "machine:" + c.id;
    if (hidden.includes(id)) return "";
    const roles = (c.roles || []).map(r => tag(`${r.profile ? r.profile + " · " : ""}${r.role}`)).join(" ") + (c.idle ? " " + tag("idle") : "");
    const up = uptime(c.uptime_s);
    const holder = c.access === "ssh" ? `<p class="hint">GPU turn · ${c.turn_holder ? esc(c.turn_holder) : unavailable(c.why?.turn_holder)}</p>` : "";
    return `<section class="card fl-card ${c.status !== "online" ? "fl-off" : ""}"><div class="row between"><h2>${dot(c.status)}<a href="#/machine?id=${encodeURIComponent(c.id)}">${esc(c.id)}</a><span class="sub">${esc(c.os)} · ${esc(c.access === "local" ? "control machine" : "node")}</span></h2>${hide(id, c.id)}</div>
      <div class="row">${H().statusPill(statusText[c.status] || c.status)}${c.status_why ? `<span class="hint">${esc(c.status_why)}</span>` : ""}<span class="hint">uptime ${up ? esc(up) : unavailable(c.why?.uptime_s)}</span></div>
      <div class="row fl-roles">${roles}</div>${headroom(c)}${gpus(c)}${engines(c)}${holder}</section>`;
  }

  (window.HyperloomPages ||= {}).overview = {
    title: "Overview",
    group: "Workspace",
    order: 10,
    home: true,
    icon: '<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="11" y="3" width="6" height="6" rx="1"/><rect x="3" y="11" width="6" height="6" rx="1"/><rect x="11" y="11" width="6" height="6" rx="1"/>',
    endpoints: () => ["overview"],
    actions: () => hidden.length ? `<button type="button" class="btn" data-page-action="ov-show">Show hidden tiles (${hidden.length})</button>` : "",
    render() {
      const {card, unavailable, esc} = H();
      const d = H().state.docs.overview;
      const cards = (d.machines || []).map(machineCard).join("");
      return `${kpis(d)}${d.why?.machines ? `<div class="banner">${unavailable(d.why.machines)}</div>` : ""}<div class="grid fl-grid">${cards}</div>${(d.machines || []).length ? "" : card("No machines", `<p>${esc("Add a machine to the cluster configuration.")}</p>`)}`;
    },
  };

  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action^="ov-"]');
    if (!control) return;
    if (control.dataset.pageAction === "ov-hide" && control.dataset.tile && !hidden.includes(control.dataset.tile)) hidden.push(control.dataset.tile);
    if (control.dataset.pageAction === "ov-show") hidden = [];
    save(); H().render();
  });
})();
