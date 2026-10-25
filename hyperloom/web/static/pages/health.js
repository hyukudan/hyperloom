/* Health (F20, F23): active findings, rules and the activity log. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const TABS = [["findings", "Findings"], ["rules", "Rules"], ["activity", "Activity log"]];
  const view = {tab: "findings", category: "", machine: "", severity: "", before: null, pages: [], editing: null,
    draft: {}, confirming: null, busy: false, note: ""};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-health") || "{}"), {editing: null, draft: {}, confirming: null, busy: false, note: "", before: null, pages: []}); } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-health", JSON.stringify({tab: view.tab, category: view.category, machine: view.machine, severity: view.severity})); } catch (_) {} };
  const hashTab = () => new URLSearchParams(location.hash.split("?")[1] || "").get("tab");
  const tab = () => { const wanted = hashTab(); return TABS.some(([id]) => id === wanted) ? wanted : TABS.some(([id]) => id === view.tab) ? view.tab : "findings"; };
  const eventsEndpoint = () => {
    const query = new URLSearchParams({limit: "50"});
    for (const key of ["category", "machine", "severity"]) if (view[key]) query.set(key, view[key]);
    if (view.before) query.set("before", String(view.before));
    return "events?" + query;
  };
  const endpointsFor = () => tab() === "rules" ? ["health/rules"] : tab() === "activity" ? [eventsEndpoint()] : ["health"];
  const doc = name => H().state.docs[name];
  const time = ts => typeof ts === "number" ? new Date(ts * 1000).toLocaleString() : null;
  const SEVERITY = {critical: "err", warning: "warn", info: "info"};
  const severity = s => `<span class="pill p-${SEVERITY[s] || "off"}">${H().esc(s || "unknown")}</span>`;
  function duration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
    if (seconds < 60) return `${Math.floor(seconds)} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`;
    return `${Math.floor(seconds / 86400)} d`;
  }
  async function post(action, body) {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token},
      body: JSON.stringify(body), signal: AbortSignal.timeout(30000)});
  }

  function tabs() {
    return `<div class="hl-tabs" role="tablist" aria-label="Health views">${TABS.map(([id, label]) => `<button type="button" role="tab" class="hl-tab" id="health-tab-${id}" aria-selected="${id === tab()}" aria-controls="health-panel" data-page-action="health-tab" data-tab="${id}">${H().esc(label)}</button>`).join("")}</div>`;
  }

  function findings() {
    const {esc, card, table, cells, display, empty} = H();
    const d = doc("health");
    if (d.findings == null) return empty("Health findings unavailable", display(null, d.why?.findings || "health evaluation not running"));
    const now = d.server_time;
    const counts = {critical: 0, warning: 0, info: 0};
    for (const f of d.findings) counts[f.severity] = (counts[f.severity] || 0) + 1;
    const kpis = `<div class="grid g4 hl-kpis">${[["critical", "Critical"], ["warning", "Warning"], ["info", "Info"]].map(([key, label]) => `<div class="card kpi"><div class="lbl">${esc(label)}</div><div class="val">${counts[key]}</div><div class="dl muted">active findings</div></div>`).join("")}<div class="card kpi"><div class="lbl">Evaluated</div><div class="val hl-small">${d.evaluated_at == null ? display(null, d.why?.evaluated_at || "no evaluation yet") : esc(duration(d.evaluation_age_s) + " ago")}</div><div class="dl muted">every 10 s · ${d.pending?.length || 0} pending</div></div></div>`;
    const stale = d.evaluated_at != null && d.stale ? `<div class="banner warn" role="status">Evaluation stale · last run ${esc(time(d.evaluated_at))}. Findings below are the last known state.</div>` : "";
    const rows = d.findings.map(f => cells([severity(f.severity), `<b>${esc(f.title)}</b><div class="hint">${esc(f.detail || "")}</div>`, f.machine ? esc(f.machine) : `<span class="muted">lab</span>`,
      `${esc(time(f.since) || "")}<div class="hint">for ${esc(duration(now - f.since))}</div>`,
      `${f.state === "clearing" ? `<span class="pill p-ok">clearing</span>` : `<span class="pill p-err">firing</span>`}${f.note ? `<div class="hint">${esc(f.note)}</div>` : ""}`,
      `<span class="hide-n">${esc(f.hint || "")}</span>`])).join("");
    const active = d.findings.length ? table(["Severity", "Finding", "Machine", "Since", "State", ["What to do", "hide-n"]], rows, "Active findings")
      : `<p class="hl-ok"><span class="pill p-ok">ok</span> No active findings. Rules whose sources are unavailable are listed on the Rules tab.</p>`;
    const pending = d.pending?.length ? card("Pending", table(["Severity", "Finding", "Machine", "True for"], d.pending.map(f => cells([severity(f.severity), `${esc(f.title)}<div class="hint">${esc(f.detail || "")}</div>`, esc(f.machine || "lab"), esc(duration(now - f.first_true))])).join(""), "Pending findings") + `<p class="hint">A finding fires after its condition has held for the rule's hold time (90 s by default) and clears after it has been absent for its clear time.</p>`, "condition true, hold not yet reached") : "";
    return `${kpis}${stale}${card("Active findings", active, "firing and clearing · critical first")}${pending}`;
  }

  function ruleForm(rule) {
    const {esc, button} = H();
    const draft = view.draft;
    const value = (key, fallback) => esc(draft[key] ?? fallback);
    const params = Object.entries(rule.params || {}).map(([name, current]) => {
      const bounds = rule.bounds?.[name] || {};
      return `<div class="field"><label for="rule-param-${esc(name)}">${esc(name)} (${esc(bounds.unit || "")})</label><input class="inp" type="number" step="any" id="rule-param-${esc(name)}" data-page-action="health-draft" data-key="param:${esc(name)}" min="${esc(bounds.low)}" max="${esc(bounds.high)}" value="${value("param:" + name, current)}" aria-describedby="rule-param-${esc(name)}-hint"><span class="hint" id="rule-param-${esc(name)}-hint">${esc(bounds.low)}..${esc(bounds.high)} · default ${esc(rule.defaults?.params?.[name])}</span></div>`;
    }).join("");
    const enabled = draft.enabled ?? rule.enabled;
    return `<section class="card hl-edit" aria-label="Edit ${esc(rule.title)}"><h2>Edit · ${esc(rule.title)}</h2><div class="grid g2">
      <div class="field"><label class="check"><input type="checkbox" id="rule-enabled" data-page-action="health-draft" data-key="enabled" ${enabled ? "checked" : ""}> Enabled</label><span class="hint">A disabled rule resolves its findings.</span></div>
      <div class="field"><label for="rule-hold">Hold before firing (s)</label><input class="inp" type="number" min="0" max="3600" step="any" id="rule-hold" data-page-action="health-draft" data-key="hold_s" value="${value("hold_s", rule.hold_s)}"><span class="hint">0..3600 · default ${esc(rule.defaults?.hold_s)}</span></div>
      <div class="field"><label for="rule-clear">Hold before clearing (s)</label><input class="inp" type="number" min="0" max="3600" step="any" id="rule-clear" data-page-action="health-draft" data-key="clear_s" value="${value("clear_s", rule.clear_s)}"><span class="hint">0..3600 · default ${esc(rule.defaults?.clear_s)}</span></div>
      ${params}</div>
      ${view.note ? `<p class="field-error" role="alert">${esc(view.note)}</p>` : ""}
      <div class="row">${button("Save rule", "", {style: "pri", extra: `data-page-action="health-rule-save" data-rule="${esc(rule.id)}"`, disabled: view.busy ? "Saving…" : ""})}${button("Reset to defaults", "", {extra: `data-page-action="health-rule-reset" data-rule="${esc(rule.id)}"`, disabled: view.busy ? "Saving…" : rule.overridden ? "" : "Already at the defaults"})}${button("Cancel", "", {extra: `data-page-action="health-rule-cancel"`})}</div>
      <p class="hint">CLI: <span class="mono">hyperloom health set-rule ${esc(rule.id)} --hold ${esc(draft.hold_s ?? rule.hold_s)}</span></p></section>`;
  }

  function rules() {
    const {esc, card, table, cells, display, empty, unavailable, button} = H();
    const d = doc("health/rules");
    if (d.rules == null) return empty("Health rules unavailable", display(null, d.why?.rules || "health evaluation not running"));
    const rows = d.rules.map(rule => {
      const params = Object.entries(rule.params || {}).map(([k, v]) => `${esc(k)} ${esc(v)}${esc(rule.bounds?.[k]?.unit ? " " + rule.bounds[k].unit : "")}`).join("<br>");
      const status = !rule.enabled ? `<span class="pill p-off">disabled</span>` : rule.available === true ? `<span class="pill p-ok">evaluating</span>` : rule.available == null ? `<span class="pill p-off">not yet evaluated</span>` : unavailable(rule.why || "source unavailable");
      return cells([`<b>${esc(rule.title)}</b>${rule.overridden ? ` <span class="tag">changed</span>` : ""}<div class="hint">${esc(rule.description)}</div>`, severity(rule.severity),
        `<span class="hl-nowrap">${esc(rule.hold_s)} s / ${esc(rule.clear_s)} s</span>`, `<span class="hide-n">${params || "—"}</span>`,
        `${status}${rule.notes?.length ? `<div class="hint">${esc(rule.notes.join(" · "))}</div>` : ""}`, `${esc(rule.active)}${rule.pending ? ` <span class="hint">+${esc(rule.pending)} pending</span>` : ""}`,
        button("Edit", "", {extra: `data-page-action="health-rule-edit" data-rule="${esc(rule.id)}" aria-label="Edit ${esc(rule.title)}"`})]);
    }).join("");
    const editing = d.rules.find(rule => rule.id === view.editing);
    return `${editing ? ruleForm(editing) : ""}${card("Rules", table(["Rule", "Severity", "Hold / clear", ["Parameters", "hide-n"], "Status", "Active", ""], rows, "Health rules") + `<p class="hint">Thresholds for memory headroom: ${esc(d.thresholds_source || "defaults (F2)")}. Values are judged only when fresh (within three sampling intervals); a missing or stale value never fires or clears a finding.</p>`, "explainable rules · C10")}`;
  }

  function activity() {
    const {esc, card, table, cells, display, empty, button, tag} = H();
    const d = doc(eventsEndpoint());
    if (d.events == null) return empty("Activity log unavailable", display(null, d.why?.events || "store not open"));
    const select = (key, label, options) => `<div class="field"><label for="events-${key}">${esc(label)}</label><select class="inp" id="events-${key}" data-page-action="health-filter" data-key="${key}"><option value="">All</option>${options.map(o => `<option value="${esc(o)}" ${view[key] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
    const filters = `<div class="row hl-filters">${select("category", "Category", d.categories || [])}${select("machine", "Machine", d.machines || [])}${select("severity", "Severity at least", ["info", "warning", "critical"])}</div>`;
    const clock = ts => typeof ts === "number" ? new Date(ts * 1000).toLocaleTimeString() : "";
    const day = ts => typeof ts === "number" ? new Date(ts * 1000).toLocaleDateString() : "";
    const rows = d.events.map(e => cells([`<span class="mono hl-nowrap">${esc(clock(e.ts))}</span><div class="hint">${esc(day(e.ts))}</div>`, severity(e.severity),
      `${esc(e.message)}<div class="hint">${tag(e.category)}${e.machine ? ` · ${esc(e.machine)}` : ""}</div>`])).join("");
    const paging = `<div class="row">${button("Newest", "", {extra: `data-page-action="health-page" data-to="newest"`, disabled: view.before ? "" : "Showing the newest events"})}${button("Older", "", {extra: `data-page-action="health-page" data-to="older"`, disabled: d.next_before ? "" : "No older events"})}<span class="hint">${esc(d.total)} events kept (at most ${esc(d.retention)})</span></div>`;
    const codes = d.clear?.confirm;
    const options = d.clear?.options || [];
    const labels = d.clear?.labels || {};
    const confirm = view.confirming ? `<div class="banner warn" role="alertdialog" aria-labelledby="clear-question"><p id="clear-question"><b>Clear ${esc(labels[view.confirming] || view.confirming)}?</b> This cannot be undone; the clear itself is logged.</p><div class="row">${button("Confirm clear", "", {style: "dan", extra: `data-page-action="health-clear-confirm" id="health-clear-confirm"`, disabled: view.busy ? "Clearing…" : codes?.[view.confirming] ? "" : "confirmation codes are being renewed; try again in a few seconds"})}${button("Cancel", "", {extra: `data-page-action="health-clear-cancel"`})}</div></div>` : "";
    const clear = `<div class="row">${options.map(o => button(`Clear ${labels[o] || o}`, "", {style: "dan", extra: `data-page-action="health-clear" data-option="${esc(o)}"`, disabled: codes ? "" : d.why?.confirm || "confirmation unavailable"})).join("")}</div>${confirm}${view.note && !view.editing ? `<p class="field-error" role="alert">${esc(view.note)}</p>` : ""}`;
    return `${card("Filters", filters)}${card("Activity log", (d.events.length ? table(["When", "Severity", "Event"], rows, "Activity log") : `<p>No events match these filters.</p>`) + paging, "findings, alerts, jobs, leases, configuration, kernel evidence")}${card("Clear the log", clear, "kept events: 2,000")}`;
  }

  (window.HyperloomPages ||= {}).health = {
    title: "Health",
    group: "Operations",
    order: 10,
    icon: '<path d="M2 10h4l2-5 4 10 2-5h4"/>',
    endpoints: endpointsFor,
    ready: () => endpointsFor().every(e => !!doc(e)),
    render() {
      const body = tab() === "rules" ? rules() : tab() === "activity" ? activity() : findings();
      return `${tabs()}<div id="health-panel" role="tabpanel" aria-labelledby="health-tab-${tab()}">${body}</div>`;
    },
  };

  const rerender = () => { H().state.lastExternal = null; H().render(); };
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled || !document.querySelector("#health-panel")?.contains(control) && control.dataset.pageAction !== "health-tab") return;
    const action = control.dataset.pageAction;
    if (action === "health-tab") {
      view.tab = control.dataset.tab; view.note = ""; view.confirming = null; save();
      if (hashTab()) history.replaceState(null, "", "#/health?tab=" + view.tab);
      H().refresh(); rerender(); return;
    }
    if (action === "health-rule-edit") { view.editing = control.dataset.rule; view.draft = {}; view.note = ""; rerender(); document.getElementById("rule-hold")?.focus(); return; }
    if (action === "health-rule-cancel") { view.editing = null; view.draft = {}; view.note = ""; rerender(); return; }
    if (action === "health-page") {
      const d = doc(eventsEndpoint());
      if (control.dataset.to === "older" && d?.next_before) { view.pages.push(view.before); view.before = d.next_before; }
      else { view.before = null; view.pages = []; }
      H().refresh(); rerender(); return;
    }
    if (action === "health-clear") { view.confirming = control.dataset.option; view.note = ""; rerender(); document.getElementById("health-clear-confirm")?.focus(); return; }
    if (action === "health-clear-cancel") { view.confirming = null; rerender(); return; }
    if (action === "health-rule-save" || action === "health-rule-reset" || action === "health-clear-confirm") {
      view.busy = true; view.note = ""; rerender();
      try {
        if (action === "health-clear-confirm") {
          const option = view.confirming, code = doc(eventsEndpoint())?.clear?.confirm?.[option];
          const result = await post("events/clear", {older_than: option, confirm: code});
          view.confirming = null; view.before = null;
          H().announce(`Activity log cleared · ${result.removed} events removed`);
        } else {
          const rule = (doc("health/rules")?.rules || []).find(r => r.id === control.dataset.rule);
          const body = {rule: control.dataset.rule};
          if (action === "health-rule-reset") body.reset = true;
          else {
            const number = key => { const raw = view.draft[key]; if (raw === undefined || raw === "") return undefined; const n = Number(raw); if (!Number.isFinite(n)) throw new Error(`${key} must be a number`); return n; };
            if (view.draft.enabled !== undefined) body.enabled = !!view.draft.enabled;
            for (const key of ["hold_s", "clear_s"]) { const n = number(key); if (n !== undefined) body[key] = n; }
            const params = {};
            for (const name of Object.keys(rule?.params || {})) { const n = number("param:" + name); if (n !== undefined) params[name] = n; }
            if (Object.keys(params).length) body.params = params;
          }
          await post("health/rules/set", body);
          view.editing = null; view.draft = {};
          H().announce(`Rule ${control.dataset.rule} ${action === "health-rule-reset" ? "reset to its defaults" : "saved"}`);
        }
      } catch (error) { view.note = error.message; H().announce(error.message); }
      finally { view.busy = false; await H().refresh(); rerender(); }
    }
  });
  document.addEventListener("change", event => {
    const control = event.target.closest?.("[data-page-action]");
    if (!control || !document.querySelector("#health-panel")?.contains(control)) return;
    if (control.dataset.pageAction === "health-filter") {
      view[control.dataset.key] = control.value; view.before = null; view.pages = []; save(); H().refresh(); rerender();
    } else if (control.dataset.pageAction === "health-draft") {
      view.draft[control.dataset.key] = control.type === "checkbox" ? control.checked : control.value;
    }
  });
  document.addEventListener("input", event => {
    const control = event.target.closest?.('[data-page-action="health-draft"]');
    if (control && control.type !== "checkbox") view.draft[control.dataset.key] = control.value;
  });
})();
