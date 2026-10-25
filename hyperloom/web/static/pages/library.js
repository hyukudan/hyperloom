/* Model library (F36): model units per machine and verification against their manifests. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const view = {busy: false, accept: false};
  const doc = () => H().state.docs.library;
  const when = t => typeof t === "number" ? new Date(t * 1000).toLocaleString() : null;
  const size = (value, why) => H().display(value, why, "bytes");
  const verifiable = status => !["none", "unknown"].includes(status);
  const statusClass = status => ({complete: "ok", partial: "warn", missing: "err", conflict: "err", none: "off", unknown: "off"})[status] || "off";
  const pill = (text, cls) => `<span class="pill p-${cls}">${H().esc(text)}</span>`;
  const act = (label, action, data = {}, {disabled = "", style = "sm"} = {}) => H().button(label, "", {disabled, style,
    extra: `data-page-action="${action}" ${Object.entries(data).map(([k, v]) => `data-${k}="${H().esc(v)}"`).join(" ")}`});
  const runningJobs = () => (doc()?.jobs || []).filter(j => j.state === "running");

  function verifyCell(machine, root, name) {
    const result = doc()?.verify?.[`${machine}|${root}|${name}`];
    if (!result) return H().unavailable("not verified here yet");
    if (result.state !== "succeeded") return `${pill("failed", "err")} <span class="hint">${H().esc(result.reason || "")}</span>`;
    const counts = result.result?.counts || {};
    const ok = result.result?.ok;
    return `${pill(ok ? "verified" : "mismatch", ok ? "ok" : "err")} <span class="hint">${H().esc(`${counts.ok ?? 0}/${result.result?.entries ?? "?"} files · ${when(result.at)}`)}</span>`;
  }

  function unitRows(machine, root) {
    const {esc, cells, display} = H();
    // Copies between machines are not part of this release: the control is shown disabled with the reason.
    const copyWhy = doc()?.copy?.available ? "" : doc()?.copy?.why || "copies between machines are not available";
    return (root.units || []).map(unit => {
      const manifest = unit.manifest || {};
      const status = manifest.status || "unknown";
      const busy = view.busy ? "Another library request is being sent" : "";
      const verifyBlocker = busy || (verifiable(status) ? "" : status === "none" ? "No manifest entries to verify against" : "The scan reached a bound in this unit");
      return cells([
        `<span class="mono lib-unit">${esc(unit.name)}</span>`,
        `<span class="hide-n">${esc(unit.kind || "?")}</span>`,
        size(unit.bytes, manifest.why || "not measured"),
        `<span class="hide-n">${display(unit.files, manifest.why || "not counted")}</span>`,
        `<span class="hide-n">${unit.newest_mtime ? esc(when(unit.newest_mtime)) : H().unavailable(manifest.why || "no files")}</span>`,
        `${pill(status, statusClass(status))}${manifest.unlisted ? ` <span class="hint">${esc(manifest.unlisted)} not listed</span>` : ""}${manifest.missing ? ` <span class="hint">${esc(manifest.missing)} missing</span>` : ""}`,
        `<span class="hide-n">${verifyCell(machine.id, root.path, unit.name)}</span>`,
        `<div class="row lib-actions">${act("Verify", "library-verify", {machine: machine.id, root: root.path, name: unit.name}, {disabled: verifyBlocker})}${act("Copy to…", "library-copy", {machine: machine.id, root: root.path, name: unit.name}, {disabled: copyWhy})}</div>`]);
    }).join("");
  }

  function machineCard(machine) {
    const {esc, card, table, tag, unavailable} = H();
    const inventory = machine.inventory;
    const scanning = (doc()?.scanning || []).includes(machine.id);
    const subtitle = inventory?.scanned_at ? `scanned ${when(inventory.scanned_at)}` : "";
    const head = `<div class="row">${tag(machine.os)}${tag(machine.access)}${machine.lab_mode ? tag("lab mode") : ""}${scanning ? H().statusPill("scanning") : ""}${act("Scan", "library-scan", {machine: machine.id}, {disabled: scanning ? "A scan of this machine is running" : !machine.roots.length ? "No model roots configured" : ""})}</div>`;
    let body;
    if (!machine.roots.length) body = `<p>${unavailable(machine.why?.roots)}</p>`;
    else if (!inventory) body = `<p>${unavailable(machine.why?.inventory || "not scanned yet")}</p>`;
    else if (inventory.error) body = `<p>${unavailable(inventory.error)}</p>`;
    else body = (inventory.roots || []).map(root => {
      const notes = [root.error ? `unavailable · ${root.error}` : "", root.truncated?.length ? `scan bounds reached: ${root.truncated.join(", ")} (affected units show unknown)` : "",
        root.other_files ? `${root.other_files} other files at the top level` : ""].filter(Boolean);
      return `<h3 class="lib-root"><span class="mono">${esc(root.path)}</span> ${tag(root.source || "config")} <span class="hint">free ${size(root.free_bytes, "not reported")}</span></h3>${notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}${root.error ? "" : table(["Unit", ["Kind", "hide-n"], "Size", ["Files", "hide-n"], ["Newest file", "hide-n"], "Manifest", ["Last verify", "hide-n"], "Actions"], unitRows(machine, root), `Model units in ${root.path}`)}`;
    }).join("");
    return card(machine.id, head + body, subtitle);
  }

  function jobsCard() {
    const {esc, card, table, cells} = H();
    const jobs = doc()?.jobs || [];
    const body = jobs.map(job => {
      const what = job.kind === "verify" ? `${job.name} on ${job.machine}` : esc(job.kind);
      const node = job.node || {};
      // A pause is shown only while the job is still running; a finished or unresolved job shows its own state
      // and reason (a pause reason left in its last node record is history).
      const paused = job.state === "running" ? node.paused?.reason : null;
      const machine = job.machine;
      // The portal job (a verification running here, or the watcher of a node job) is cancelled through
      // the portal; a node job started elsewhere (no portal job here) through library/cancel.
      const blocker = view.busy ? "A request is being sent" : "";
      // An unresolved job (its watcher could not read the node) is cancelled on the node directly.
      const cancel = job.state === "unresolved" && job.node_job ? act("Cancel on the node", "library-cancel", {machine, job: job.node_job}, {disabled: blocker})
        : job.state !== "running" ? "" : job.portal_job
        ? act("Cancel", "library-cancel-portal", {job: job.portal_job}, {disabled: blocker})
        : job.node_job ? act("Cancel", "library-cancel", {machine, job: job.node_job}, {disabled: blocker})
        : H().unavailable("cancel · no job id reported yet");
      const state = paused ? "paused" : job.state;
      return cells([esc(job.kind), esc(what), pill(state, ({succeeded: "ok", running: "info", paused: "warn", unresolved: "warn", failed: "err"})[state] || "off"), esc(paused ? `paused: ${paused}` : job.reason || job.progress || ""), esc(when(job.started) || ""), cancel]);
    }).join("");
    return card("Library jobs", jobs.length ? table(["Kind", "What", "State", "Progress", "Started", ""], body, "Library jobs") : `<p class="t2">No verifications started from this portal yet.</p>`,
      "verifications; node jobs keep running when this page closes");
  }

  function rootsCard() {
    const {esc, card} = H();
    const machines = doc()?.machines || [];
    const body = machines.map(m => {
      const config = m.roots.filter(r => r.source === "config");
      const prefs = m.roots.filter(r => r.source === "prefs").map(r => r.path);
      return `<div class="lib-roots"><h3>${esc(m.id)}</h3><p class="hint">From the configuration: ${config.length ? config.map(r => `<span class="mono">${esc(r.path)}</span>`).join(", ") : "none"}</p>
        <div class="field"><label for="library-roots-${esc(m.id)}">Extra roots on ${esc(m.id)} (one absolute directory per line)</label><textarea class="inp mono" rows="2" id="library-roots-${esc(m.id)}">${esc(prefs.join("\n"))}</textarea></div>
        ${act("Save roots", "library-roots", {machine: m.id}, {disabled: view.busy ? "A request is being sent" : ""})}</div>`;
    }).join("");
    return card("Model roots", `<div class="grid g2">${body}</div>`, "extra roots per machine, kept in the portal preferences");
  }

  function ledgerCard() {
    const {esc, card, unavailable} = H();
    const ledger = doc()?.ledger || {};
    let body;
    if (!ledger.configured) body = `<p>${unavailable(ledger.why || "not configured")}</p>`;
    else if (!ledger.baseline) body = `<p>${unavailable(ledger.why || "no accepted baseline")}</p>`;
    else body = `<p class="t2">Accepted ${esc(when(ledger.baseline.accepted_at))}${ledger.baseline.advanced_at ? ` · last checked growth ${esc(when(ledger.baseline.advanced_at))}` : ""} · ${esc(ledger.baseline.size)} bytes covered.</p>`;
    const button = !ledger.configured ? "" : view.accept
      ? `<div class="row">${act("Confirm: no RTX PRO big job is hidden by a rotation", "library-accept-confirm", {}, {style: "dan"})}${act("Keep the current baseline", "library-accept-cancel", {}, {style: ""})}</div>`
      : act("Accept the current ledger", "library-accept", {}, {style: "", disabled: view.busy ? "A request is being sent" : ""});
    return card("RTX big-job guard", body + `<p class="hint">Verification on the control machine pauses while the coordination ledger has an open PC-BIG start; a replaced or rewritten ledger blocks it until it is accepted again.</p>` + button);
  }

  function kpis() {
    const machines = doc()?.machines || [];
    const scanned = machines.filter(m => m.inventory && !m.inventory.error);
    const units = scanned.flatMap(m => (m.inventory.roots || []).flatMap(r => r.units || []));
    const known = units.filter(u => typeof u.bytes === "number");
    const total = known.reduce((sum, u) => sum + u.bytes, 0);
    const complete = units.filter(u => u.manifest?.status === "complete").length;
    const {kpi, esc, unavailable} = H();
    return `<div class="grid g4 kpis">${kpi("Machines scanned", `${scanned.length}<small>/ ${machines.length}</small>`, "inventory from the last scan")}${kpi("Model units", scanned.length ? esc(units.length) : unavailable("not scanned"), "directories and model files")}${kpi("Total size", known.length ? size(total) : unavailable("not scanned"), units.length > known.length ? `${units.length - known.length} ${units.length - known.length === 1 ? "unit" : "units"} not measured (scan bound)` : "measured by the node")}${kpi("Complete manifests", scanned.length ? `${complete}<small>/ ${units.length}</small>` : unavailable("not scanned"), "*.sha256 lists every file")}</div>`;
  }

  (window.HyperloomPages ||= {}).library = {
    title: "Model library",
    group: "Operations",
    order: 40,
    icon: '<path d="M3 4h4v12H3z"/><path d="M8 4h4v12H8z"/><path d="M13.5 4.5l3.5 1-3 11-3.5-1z"/>',
    endpoints: () => ["library"],
    ready: () => !!doc(),
    actions: () => act("Scan all", "library-scan", {}, {style: "", disabled: (doc()?.scanning || []).length ? "A scan is running" : view.busy ? "A request is being sent" : ""}),
    render() {
      const library = doc();
      const {esc} = H();
      const store = library.why?.inventory ? `<p class="hint">${esc(library.why.inventory)}</p>` : "";
      return `<div class="grid">${kpis()}${store}${(library.machines || []).map(machineCard).join("")}${jobsCard()}<div class="grid g2">${rootsCard()}${ledgerCard()}</div></div>`;
    },
  };

  async function post(endpoint, body, done) {
    view.busy = true; H().render();
    try {
      const result = await H().request(endpoint, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify(body)});
      if (done) done(result);
    } catch (error) {
      H().announce(error.message);
    } finally {
      view.busy = false; H().render(); H().refresh();
    }
  }

  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action^='library-']");
    if (!control || control.disabled) return;
    const d = control.dataset;
    switch (d.pageAction) {
      case "library-scan": post("library/scan", d.machine ? {machine: d.machine} : {}, () => H().announce("Scan started")); break;
      case "library-verify": post("library/verify", {machine: d.machine, model_root: d.root, name: d.name}, () => H().announce(`Verification of ${d.name} started`)); break;
      case "library-cancel": post("library/cancel", {machine: d.machine, job: d.job}, () => H().announce("Cancel requested")); break;
      case "library-cancel-portal": post("cancel", {job: d.job}, () => H().announce("Cancel requested")); break;
      case "library-roots": {
        const text = document.getElementById(`library-roots-${d.machine}`)?.value || "";
        post("library/roots", {machine: d.machine, roots: text.split("\n").map(s => s.trim()).filter(Boolean)}, () => H().announce(`Roots of ${d.machine} saved`));
        break;
      }
      case "library-accept": view.accept = true; H().render(); break;
      case "library-accept-cancel": view.accept = false; H().render(); break;
      case "library-accept-confirm": view.accept = false; post("library/ledger-accept", {}, () => H().announce("Ledger accepted as the new baseline")); break;
    }
  });
})();
