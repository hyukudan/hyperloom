/* Operations: node tool status and update (F38), power actions (F39), machine configuration (F41) and collectors
   (F42). Every control names its blocker; anything that powers a machine off or removes it asks to type its id. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const UI = () => window.HyperloomOpsUI;
  const tabs = [["nodes", "Node tool"], ["power", "Power"], ["config", "Machines"], ["collectors", "Collectors"]];
  const endpointOf = {nodes: "ops/nodes", power: "ops/power", config: "ops/config", collectors: "ops/collectors"};
  const intervals = {};
  let notice = "";
  const tell = text => { notice = text; H().render(); };

  function tab() {
    const query = new URLSearchParams(location.hash.split("?")[1] || "");
    const value = query.get("tab");
    return tabs.some(([id]) => id === value) ? value : "nodes";
  }
  const doc = () => H().state.docs[endpointOf[tab()]] || {};
  const short = digest => typeof digest === "string" ? digest.slice(0, 12) : null;
  // A row action; a blocker shared by the whole page (waiting for state, another job) is named once above the table,
  // so such a button only carries it as its title; a blocker of its own is shown beside it.
  function act(label, action, data, {disabled = "", style = ""} = {}) {
    const extra = `data-page-action="${action}" ${Object.entries(data).map(([k, v]) => `data-${k}="${H().esc(v)}"`).join(" ")}`;
    if (disabled && disabled === H().mutationReason()) return `<button type="button" class="btn ${style}" disabled title="${H().esc(disabled)}" ${extra}>${H().esc(label)}</button>`;
    return H().button(label, "", {disabled, style, extra});
  }
  const pageBlocker = () => H().mutationReason() ? `<p class="ops-note" role="status">Actions wait: ${H().esc(H().mutationReason())}</p>` : "";

  function nodesView() {
    const {table, cells, esc, statusPill, unavailable, card} = H(), d = doc();
    const rows = (d.machines || []).map(m => cells([
      esc(m.machine), m.status ? statusPill(m.status) : unavailable(m.why?.deployed),
      m.deployed ? `<span class="ops-mono">${esc(short(m.deployed))}</span>` : unavailable(m.why?.deployed || "not reported"),
      `<span class="ops-mono">${esc(short(m.expected))}</span>`, UI().show(UI().time(m.checked_at), "not checked yet"),
      m.actions?.length ? `<div class="ops-actions">${m.actions.map(a => act(`Update (${a.profile})`, "ops-update", {profile: a.profile, machine: a.machine}, {disabled: H().mutationReason() || a.blocked || ""})).join("")}</div>`
        : `<span class="ops-note">${esc(m.why?.actions || "—")}</span>`])).join("");
    return card("Node tool", `${pageBlocker()}${table(["Machine", "Status", "Deployed", "This checkout", "Checked", "Update"], rows, "Node tool per machine")}<p class="ops-note">${esc(d.rule || "")}</p><p class="ops-note">${esc(d.limitation || "")}</p>${d.why?.machines ? `<p>${unavailable(d.why.machines)}</p>` : ""}`, "digest of the deployed node tool vs this checkout");
  }

  function powerView() {
    const {table, cells, esc, statusPill, unavailable, card} = H(), d = doc();
    const rows = (d.machines || []).map(m => cells([
      `${esc(m.machine)} <span class="ops-note">${esc(m.os)} · ${esc(m.access)}</span>`,
      m.stop?.length ? `<div class="ops-actions">${m.stop.map(p => act(`Stop ${p.profile}`, "ops-stop", {profile: p.profile}, {style: "dan"})).join("")}</div>`
        : `<span class="ops-note">${m.profiles?.length ? "no engine running" : "no profile uses it"}</span>`,
      `${act("Wake", "ops-wake", {machine: m.machine}, {disabled: m.wake?.blocked || ""})}${m.wake?.macs?.length ? `<div class="ops-note">${esc(m.wake.macs.join(", "))} · ${esc(m.wake.source)}</div>` : ""}`,
      act("Shut down…", "ops-shutdown", {machine: m.machine}, {style: "dan", disabled: H().mutationReason() || m.shutdown?.blocked || ""}),
      UI().show(UI().time(m.last_seen), "never answered")])).join("");
    return card("Power", `${pageBlocker()}${table(["Machine", "Engines", "Wake-on-LAN", "Shutdown", "Last answer"], rows, "Power actions")}${d.why?.shutdown_helper ? `<p>${unavailable(d.why.shutdown_helper)}</p>` : `<p class="ops-note">Shutdown helper: <span class="ops-mono">${esc(d.shutdown_helper || "")}</span></p>`}<p class="ops-note">${esc(d.rule || "")}</p>`, "stop engines, wake or power off machines");
  }

  function configView() {
    const {table, cells, esc, statusPill, unavailable, card, button} = H(), d = doc();
    if (d.machines == null) return card("Machines", unavailable(d.why?.machines));
    const all = d.machines, count = all.length, busy = H().mutationReason();
    const rows = all.map((m, i) => {
      const e = m.entry || {};
      const ssh = e.access === "ssh" ? `<div class="ops-mono">${esc(e.ssh_target || "")}:${esc(e.ssh_port ?? 22)}</div>${H().tag(m.pinned ? "host key pinned" : "host key not pinned")}` : "";
      return cells([esc(i + 1), `<b class="ops-mono">${esc(m.id)}</b>${m.router ? "<div>" + H().tag("router") + "</div>" : ""}`,
        `<div>${esc(e.os)} · ${esc(e.access)}</div><div class="ops-mono">${esc(e.http_address || "")}</div>${ssh}`,
        `<div class="ops-mono">${esc(e.install_root || "")}</div>${e.lab_dir ? `<div class="ops-mono ops-note">lab ${esc(e.lab_dir)}</div>` : ""}`,
        esc((e.devices || []).map(x => x.id).join(", ") || "—"),
        m.frozen ? `${statusPill("in use")}<div class="ops-note">${esc(m.frozen)}</div>` : statusPill("editable"),
        `<div class="ops-actions">${act("Edit…", "ops-edit", {id: m.id}, {disabled: busy})}${act("↑", "ops-move", {id: m.id, index: i - 1}, {disabled: busy || (i === 0 ? "already first" : "")})}${act("↓", "ops-move", {id: m.id, index: i + 1}, {disabled: busy || (i === count - 1 ? "already last" : "")})}${act("Extras…", "ops-extras", {id: m.id})}${e.access === "ssh" ? act("Host key…", "ops-scan", {id: m.id}) : ""}${act("Test", "ops-test", {id: m.id}, {disabled: busy})}${act("Remove…", "ops-remove", {id: m.id}, {style: "dan", disabled: busy || (m.frozen ? "in use" : m.profiles?.length ? "used by " + m.profiles.join(", ") : "")})}</div>`]);
    }).join("");
    const job = H().state.job;
    const test = job?.action === "ops/machine-test" && job.result?.steps ? card(`Test of ${job.result.machine}`, `<ul class="ops-steps">${job.result.steps.map(s => `<li>${statusPill(s.status === "pass" ? "ok" : s.status === "fail" ? "failed" : "skipped")} <b>${esc(s.step)}</b> <span class="ops-note">${esc(s.detail)}</span></li>`).join("")}</ul>`, job.result.passed ? "passed" : "failed") : "";
    return `${card("Machines", `<div class="row between"><span class="ops-note">${esc(d.path || "")} · version <span class="ops-mono">${esc(short(d.version))}</span></span>${act("Add machine…", "ops-add", {}, {style: "pri", disabled: busy})}</div>${pageBlocker()}${table(["#", "Id", "Connection", "Paths", "Devices", "State", "Actions"], rows, "Configured machines")}<p class="ops-note">${esc(d.rule || "")}</p>${d.why?.configuration ? `<p>${unavailable(d.why.configuration)}</p>` : ""}`, "configuration order")}${test}`;
  }

  function collectorsView() {
    const {table, cells, esc, statusPill, unavailable, card} = H(), d = doc();
    const rows = (d.collectors || []).map(c => {
      const s = c.state || {}, value = intervals[c.collector] ?? c.interval_s ?? c.default_s;
      const health = !c.state ? unavailable(c.blocked) : s.error ? `${statusPill("error")} <span class="ops-note">${esc(s.error)}</span>` : statusPill(s.last_ok ? "ok" : "starting");
      return cells([`<b>${esc(c.collector)}</b>`, health, UI().show(UI().time(s.last_ok), "no success yet"),
        `<div class="row"><label class="field" for="ops-int-${esc(c.collector)}"><span class="ops-note">${esc(c.min_s)}..${esc(c.max_s)} s (default ${esc(c.default_s)})</span><input class="inp" type="number" id="ops-int-${esc(c.collector)}" data-page-action="ops-interval-value" data-collector="${esc(c.collector)}" min="${esc(c.min_s)}" max="${esc(c.max_s)}" step="0.5" value="${esc(value)}" ${c.blocked ? "disabled" : ""}></label>${act("Set", "ops-interval", {collector: c.collector}, {disabled: c.blocked || ""})}</div>`,
        act("Refresh now", "ops-refresh", {collector: c.collector}, {disabled: c.blocked || ""})]);
    }).join("");
    return card("Collectors", `${table(["Collector", "State", "Last success", "Interval", ""], rows, "Collectors")}<p class="ops-note">${esc(d.rule || "")}</p>${d.why?.state ? `<p>${unavailable(d.why.state)}</p>` : ""}`, "refresh now and poll intervals");
  }

  (window.HyperloomPages ||= {}).ops = {
    title: "Operations",
    group: "Operations",
    order: 42,
    icon: '<path d="M4 6h12M4 10h12M4 14h12"/><circle cx="7" cy="6" r="1.6"/><circle cx="13" cy="10" r="1.6"/><circle cx="9" cy="14" r="1.6"/>',
    endpoints: () => [endpointOf[tab()]],
    render() {
      const current = tab();
      const nav = `<nav class="ops-tabs" aria-label="Operations sections">${tabs.map(([id, label]) => `<a href="#/ops?tab=${id}" ${id === current ? 'aria-current="page"' : ""}>${H().esc(label)}</a>`).join("")}</nav>`;
      return nav + `<p class="ops-note" role="status">${H().esc(notice)}</p>` + {nodes: nodesView, power: powerView, config: configView, collectors: collectorsView}[current]();
    },
  };

  // -- machine editor -------------------------------------------------------------------------------------------
  function machineForm(entry, frozen) {
    const esc = H().esc, e = entry || {}, lock = key => frozen && ["id", "os", "access", "http_address", "ssh_target", "ssh_port", "install_root", "lab_dir", "lab_mode", "peer_ssh", "devices"].includes(key) ? `disabled title="${esc("frozen: " + frozen)}"` : "";
    const text = (key, label, placeholder = "") => `<label class="field" for="ops-f-${key}">${esc(label)}<input class="inp" id="ops-f-${key}" value="${esc(e[key] ?? "")}" placeholder="${esc(placeholder)}" spellcheck="false" ${lock(key)}></label>`;
    const select = (key, label, values) => `<label class="field" for="ops-f-${key}">${esc(label)}<select class="inp" id="ops-f-${key}" ${lock(key)}>${values.map(v => `<option ${e[key] === v ? "selected" : ""}>${v}</option>`).join("")}</select></label>`;
    return `${frozen ? `<p class="ops-note">In use (${esc(frozen)}): its identity fields are frozen.</p>` : ""}
      <div class="two">${text("id", "Id")}${text("http_address", "Address (private IP)")}</div>
      <div class="two">${select("os", "System", ["linux", "windows"])}${select("access", "Access", ["ssh", "local"])}</div>
      <div class="two">${text("ssh_target", "SSH target", "user@host or alias")}${text("ssh_port", "SSH port", "22")}</div>
      <div class="two">${text("install_root", "Install root (absolute)")}${text("lab_dir", "Lab directory (absolute, optional)")}</div>
      <label class="check" for="ops-f-lab_mode"><input type="checkbox" id="ops-f-lab_mode" ${e.lab_mode !== false ? "checked" : ""} ${lock("lab_mode")}>Lab mode (GPU turns through the lab's turn tool)</label>
      <label class="field" for="ops-f-devices">Devices (JSON list of {id, type?, uuid?, excluded?})<textarea class="inp" id="ops-f-devices" spellcheck="false" ${lock("devices")}>${esc(JSON.stringify(e.devices || [], null, 1))}</textarea></label>
      <label class="field" for="ops-f-peer_ssh">Peer ssh aliases (JSON object {machine id: alias})<textarea class="inp" id="ops-f-peer_ssh" spellcheck="false" ${lock("peer_ssh")}>${esc(JSON.stringify(e.peer_ssh || {}, null, 1))}</textarea></label>
      <p class="ops-note" id="ops-check-result" aria-live="polite"></p>`;
  }

  function readMachine() {
    const value = id => document.getElementById("ops-f-" + id).value.trim();
    const entry = {id: value("id"), os: value("os"), access: value("access"), http_address: value("http_address"), install_root: value("install_root"),
      lab_mode: document.getElementById("ops-f-lab_mode").checked};
    if (value("ssh_target")) entry.ssh_target = value("ssh_target");
    if (value("ssh_port")) entry.ssh_port = Number(value("ssh_port"));
    if (value("lab_dir")) entry.lab_dir = value("lab_dir");
    for (const key of ["devices", "peer_ssh"]) {
      try { const parsed = JSON.parse(value(key) || (key === "devices" ? "[]" : "{}")); if ((Array.isArray(parsed) && parsed.length) || (!Array.isArray(parsed) && Object.keys(parsed).length)) entry[key] = parsed; }
      catch (_) { throw new Error(`${key === "devices" ? "Devices" : "Peer ssh aliases"}: not valid JSON`); }
    }
    return entry;
  }

  async function saveEdit(edit) {
    const check = await UI().post("ops/config/validate", edit);
    if (!check.valid) throw new Error(check.error);
    await UI().post("ops/config/save", {...edit, version: doc().version});
    tell("Configuration saved.");
  }

  function openMachine(id) {
    const current = (doc().machines || []).find(m => m.id === id);
    UI().open({
      title: current ? `Edit ${id}` : "Add a machine", confirm: current ? "Save" : "Add",
      body: machineForm(current?.entry || {os: "linux", access: "ssh", ssh_port: 22, lab_mode: true}, current?.frozen),
      submit: () => saveEdit(current ? {op: "edit", id, machine: readMachine()} : {op: "add", machine: readMachine()}),
    });
  }

  function openExtras(id) {
    const m = (doc().machines || []).find(x => x.id === id), x = m?.extras || {}, esc = H().esc;
    UI().open({
      title: `Extras of ${id}`, confirm: "Save",
      body: `<div class="two"><label class="field" for="ops-x-mac">MAC for Wake-on-LAN<input class="inp" id="ops-x-mac" value="${esc(x.mac || "")}" placeholder="aa:bb:cc:dd:ee:ff" spellcheck="false"></label>
        <label class="field" for="ops-x-bc">Directed broadcast (optional)<input class="inp" id="ops-x-bc" value="${esc(x.wol_broadcast || "")}" placeholder="private IPv4 broadcast" spellcheck="false"></label></div>
        <label class="field" for="ops-x-disks">Hidden disks (one per line)<textarea class="inp" id="ops-x-disks" spellcheck="false">${esc((x.hide_disks || []).join("\n"))}</textarea></label>
        <label class="field" for="ops-x-ifaces">Hidden network interfaces (one per line)<textarea class="inp" id="ops-x-ifaces" spellcheck="false">${esc((x.hide_interfaces || []).join("\n"))}</textarea></label>`,
      submit: () => {
        const lines = el => document.getElementById(el).value.split("\n").map(s => s.trim()).filter(Boolean);
        return UI().post("ops/machine-extras", {machine: id, mac: document.getElementById("ops-x-mac").value.trim() || null,
          wol_broadcast: document.getElementById("ops-x-bc").value.trim() || null, hide_disks: lines("ops-x-disks"), hide_interfaces: lines("ops-x-ifaces")});
      },
    });
  }

  async function openScan(id) {
    const esc = H().esc;
    let scan;
    try { scan = await UI().post("ops/hostkey/scan", {machine: id}); }
    catch (error) { H().announce(`Host-key scan of ${id} failed: ${error.message}`); return; }
    const list = scan.candidates.map(c => `<li><span class="ops-mono">${esc(c.algorithm)} ${esc(c.fingerprint)}</span></li>`).join("");
    UI().open({
      title: `Host key of ${id}`, confirm: "Pin",
      body: `<p class="ops-note">Scanned at ${esc(scan.address)} (nothing is pinned yet). ${esc(scan.next)}.</p><ul class="ops-steps">${list}</ul>
        <label class="field" for="ops-fingerprint">Fingerprint read on the machine itself<input class="inp" id="ops-fingerprint" placeholder="SHA256:..." spellcheck="false" autocomplete="off"></label>`,
      submit: async () => {
        const fingerprint = document.getElementById("ops-fingerprint").value.trim();
        if (!scan.candidates.some(c => c.fingerprint === fingerprint)) throw new Error("That fingerprint is not one of the scanned keys.");
        await H().mutate("hosts/pin", {machine: id, fingerprint});
      },
    });
  }

  document.addEventListener("input", event => {
    if (event.target.dataset?.pageAction === "ops-interval-value") intervals[event.target.dataset.collector] = event.target.value;
  });
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled || !control.dataset.pageAction.startsWith("ops-")) return;
    const data = control.dataset, action = data.pageAction, esc = H().esc;
    try {
      if (action === "ops-update") {
        UI().open({title: `Update the node tool (${data.profile})`, confirm: "Update",
          body: `<p>Copies this checkout's node tool to <b>${esc(data.machine)}</b> through the profile <b>${esc(data.profile)}</b>, without force. It is refused while a lease is live there or its state cannot be read; an update already running on a machine cannot be interrupted.</p>`,
          submit: () => H().mutate("ops/nodes-update", {profile: data.profile, machine: data.machine})});
      } else if (action === "ops-stop") {
        await H().mutate("down", {profile: data.profile}, true);
      } else if (action === "ops-wake") {
        const result = await UI().post("ops/wake", {machine: data.machine});
        tell(`Magic packet sent to ${result.machine} (${result.sent.length} packet(s)).`);
      } else if (action === "ops-shutdown") {
        UI().open({title: `Shut down ${data.machine}`, confirm: "Shut down", danger: true, expect: data.machine,
          body: `<p>Runs the configured shutdown helper for <b>${esc(data.machine)}</b>. Refused while a lease is live there, a profile uses it, or its state cannot be read.</p>`,
          submit: () => H().mutate("ops/shutdown", {machine: data.machine, confirm: document.getElementById("ops-confirm-id").value})});
      } else if (action === "ops-add") {
        openMachine(null);
      } else if (action === "ops-edit") {
        openMachine(data.id);
      } else if (action === "ops-extras") {
        openExtras(data.id);
      } else if (action === "ops-move") {
        await UI().post("ops/config/save", {op: "move", id: data.id, index: Number(data.index), version: doc().version});
        H().refresh();
      } else if (action === "ops-remove") {
        UI().open({title: `Remove ${data.id}`, confirm: "Remove", danger: true, expect: data.id,
          body: `<p>Removes <b>${esc(data.id)}</b> from the configuration (validated first; refused while it is in use or its state cannot be read).</p>`,
          submit: () => saveEdit({op: "remove", id: data.id})});
      } else if (action === "ops-scan") {
        await openScan(data.id);
      } else if (action === "ops-test") {
        await H().mutate("ops/machine-test", {machine: data.id});
      } else if (action === "ops-refresh") {
        const result = await UI().post("telemetry/refresh", {collector: data.collector});
        tell(`${data.collector}: ${result.note}.`);
      } else if (action === "ops-interval") {
        const seconds = Number(intervals[data.collector] ?? document.getElementById("ops-int-" + data.collector)?.value);
        await UI().post("telemetry/interval", {collector: data.collector, seconds});
        delete intervals[data.collector];
        tell(`${data.collector}: interval set to ${seconds} s.`);
        H().refresh();
      }
    } catch (error) { H().announce(error.message); }
  });
})();
