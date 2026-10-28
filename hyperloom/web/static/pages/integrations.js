/* Integrations (F43-F46): MQTT publishing settings and state, the live stream, the exporter and CSV/JSON export. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const doc = () => H().state.docs.integrations || {};
  const draft = {loaded: null, values: {}, password: "", clear: false, busy: "", result: null, error: "",
    target: "", share: false, range: "", exportBusy: false, exportResult: ""};
  const fields = ["enabled", "host", "port", "tls", "topic_prefix", "username", "client_id", "interval_s"];

  function syncDraft() {
    const settings = doc().mqtt?.settings;
    if (!settings) return;
    // Take the saved settings as the form's starting point once per saved revision (never over unsaved edits).
    if (draft.loaded !== settings.revision) {
      draft.loaded = settings.revision;
      draft.values = Object.fromEntries(fields.map(k => [k, settings[k] ?? (k === "port" ? "" : null)]));
      draft.values.port = settings.port ?? "";
    }
  }
  const blocker = () => {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") return "Launch the portal through hyperloom ui to enable actions";
    if (!doc().mqtt) return doc().why?.mqtt || "settings store unavailable";
    return draft.busy ? `${draft.busy} in progress` : "";
  };
  const input = (id, label, value, {type = "text", hint = "", extra = ""} = {}) =>
    `<div class="field"><label for="${id}">${H().esc(label)}</label><input class="inp" id="${id}" type="${type}" value="${H().esc(value ?? "")}" data-page-action="integrations-field" ${extra}>${hint ? `<span class="hint">${H().esc(hint)}</span>` : ""}</div>`;
  const check = (id, label, checked, extra = "") =>
    `<label class="check" for="${id}"><input type="checkbox" id="${id}" data-page-action="integrations-field" ${checked ? "checked" : ""} ${extra}> ${H().esc(label)}</label>`;
  const when = t => typeof t === "number" ? new Date(t * 1000).toLocaleString() : null;

  function publisherCard(m) {
    const {esc, card, kv, statusPill, unavailable, display} = H();
    const p = m.publisher;
    const pill = (text, style) => `<span class="pill p-${style}">${esc(text)}</span>`;
    const state = !m.settings.enabled ? pill("disabled", "off") : p ? (p.connected ? pill("connected", "ok") : pill("disconnected", "warn")) : unavailable(doc().why?.publisher || "publisher not running");
    const figures = p ? kv({...p, last_publish: when(p.last_publish) ?? "none yet", last_connect: when(p.last_connect) ?? "not connected yet", last_error: p.last_error ?? "none", queued: `${p.queued} / ${p.limit}`}, [
      ["Queued", "queued"], ["Published", "published"], ["Dropped (queue full)", "dropped"], ["Expired before sending", "expired"],
      ["Events lost", "events_lost"], ["Last publish", "last_publish"], ["Last connect", "last_connect"], ["Last error", "last_error"]])
      : `<p>${display(null, doc().why?.publisher || "publisher not running")}</p>`;
    return card("Publisher", `<div class="row between"><span>State</span>${state}</div>${figures}<p class="hint">${esc(m.publishing_here ? "This portal process publishes (it holds the publisher lease)." : "Publishing needs this portal's samplers and the publisher lease; one portal process publishes at a time.")}</p>`, "Best effort · bounded queue");
  }

  function settingsCard(m) {
    const {esc, card, button} = H();
    const v = draft.values, why = blocker();
    const saved = m.settings.credential_saved;
    const form = `<div class="intg-form">
      ${check("intg-enabled", "Publish to the broker", v.enabled)}
      <p class="hint">Broker: <span class="mono">${esc(m.settings.host ? `${m.settings.host}:${m.settings.port}` : "not configured")}</span> · ${m.settings.tls ? "TLS (certificate and host name verified)" : "no TLS (a password is refused)"} · set in the configuration file (pc.mqtt_broker)</p>
      ${input("intg-prefix", "Topic prefix", v.topic_prefix, {hint: "1-4 segments of letters, digits, _ or -"})}
      ${input("intg-interval", "Metrics every (s)", v.interval_s, {type: "number", extra: 'min="5" max="300"'})}
      ${input("intg-username", "User name", v.username, {hint: "Optional"})}
      ${input("intg-password", "Password", "", {type: "password", hint: saved ? "A password is saved; leave empty to keep it" : "Stored in the secret store, never shown", extra: 'autocomplete="new-password"'})}
      ${saved ? check("intg-clear", "Remove the saved password", draft.clear) : ""}
      ${input("intg-client", "Client id", v.client_id, {hint: "Up to 21 letters, digits, _ or -; empty = generated"})}
    </div>`;
    const buttons = `<div class="row">${button("Save settings", "", {disabled: why, style: "pri", extra: 'data-page-action="integrations-save"'})}${button("Send test message", "", {disabled: why || (!m.settings.host ? "Save a broker host first" : ""), extra: 'data-page-action="integrations-test"'})}</div>`;
    const result = draft.error ? `<p class="field-error" role="alert">${esc(draft.error)}</p>` : draft.result ? `<p class="hint" role="status">${esc(draft.result)}</p>` : `<p class="hint" role="status"></p>`;
    return card("MQTT broker", form + buttons + result, "Settings are audited; the password goes to the secret store");
  }

  function topicsCard(m) {
    const {esc, card, table, cells} = H();
    return card("Topics", table(["Topic", "QoS", "Retained", "Payload"], m.topics.map(t => cells([`<span class="mono">${esc(t.topic)}</span>`, esc(t.qos), t.retained ? "yes" : "no", esc(t.payload)])).join(""), "MQTT topics"), "Consumers treat retained values as current only while the portal status is online");
  }

  function streamCard(s) {
    const {esc, card, kv} = H();
    return card("Live stream", kv(s, [["Endpoint", "endpoint"], ["Clients now", "clients"], ["Client limit", "max_clients"], ["Interval", "interval_s", "s"], ["Longest connection", "max_connection_s", "s"]]) +
      `<p class="hint">${esc("Server-sent events on this portal's own origin: one snapshot event when the machines, profiles, engine figures or overview change, a comment every 15 s otherwise. Same host check as every page; read-only.")}</p>`, "Snapshot projection · " + esc((s.documents || []).join(", ")));
  }

  function exporterCard(e) {
    const {esc, card, kv} = H();
    return card("Prometheus exporter", kv(e, [["Run", "command"], ["Listens on", "listen"], ["Node and USB4 series", "store_series"], ["Token ledger", "ledger"], ["Findings", "findings"], ["Passed through", "passthrough"]]) +
      `<p class="hint">${esc("Unknown values are left out, never written as zero. The exporter reads this portal's telemetry store read-only.")}</p>`, "Loopback only");
  }

  function exportCard(x) {
    const {esc, card, button} = H();
    const targets = x.targets || [];
    if (!targets.some(t => t.document === draft.target)) draft.target = targets[0]?.document || "";
    const target = targets.find(t => t.document === draft.target);
    const takesRange = (target?.query || []).includes("range");
    const picker = `<div class="field"><label for="intg-target">Document</label><select class="inp" id="intg-target" data-page-action="integrations-export-field">${targets.map(t => `<option value="${esc(t.document)}" ${t.document === draft.target ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select></div>`;
    const range = takesRange ? input("intg-range", "Range", draft.range, {hint: target.document.startsWith("history/") ? "Seconds, e.g. 3600" : "As the document accepts it, e.g. 24h or 86400"}) : "";
    const why = !target ? "No exportable document in this portal" : draft.exportBusy ? "Export in progress" : "";
    return card("Export", `<div class="intg-form">${picker}${range}</div>${check("intg-share", "CSV share mode (only fields cleared for sharing; JSON is always the full document)", draft.share, 'data-share="1"')}
      <div class="row">${button("Download CSV", "", {disabled: why, style: "pri", extra: 'data-page-action="integrations-csv"'})}${button("Download JSON", "", {disabled: why, extra: 'data-page-action="integrations-json"'})}</div>
      <p class="hint" role="status">${esc(draft.exportResult || "CSV cells that start with = + - @ are prefixed with ' so spreadsheets never run them.")}</p>`, "CSV built by the portal from its own documents; JSON as served");
  }

  (window.HyperloomPages ||= {}).integrations = {
    title: "Integrations",
    group: "Operations",
    order: 80,
    icon: '<path d="M7 3v4M13 3v4M5 7h10v3a5 5 0 0 1-10 0z"/><path d="M10 15v2"/>',
    endpoints: () => ["integrations"],
    render() {
      const {card, empty} = H();
      const d = doc();
      syncDraft();
      const mqttPart = d.mqtt ? `<div class="grid g2">${settingsCard(d.mqtt)}${publisherCard(d.mqtt)}</div>${topicsCard(d.mqtt)}`
        : card("MQTT publishing", `<p>${H().display(null, d.why?.mqtt || "settings store unavailable")}</p>`);
      if (!d.stream) return empty("Integrations unavailable", "The integrations document did not load.");
      return `<div class="grid">${mqttPart}<div class="grid g2">${streamCard(d.stream)}${exporterCard(d.exporter || {})}</div>${exportCard(d.export || {})}</div>`;
    },
    after(main) {
      const password = main.querySelector("#intg-password");
      if (password) password.value = draft.password;
    },
  };

  function readField(el) {
    const map = {"intg-prefix": "topic_prefix", "intg-interval": "interval_s", "intg-username": "username", "intg-client": "client_id"};
    if (el.id === "intg-enabled") draft.values.enabled = el.checked;
    else if (el.id === "intg-password") draft.password = el.value;
    else if (el.id === "intg-clear") draft.clear = el.checked;
    else if (map[el.id]) draft.values[map[el.id]] = el.value;
  }
  function settingsArguments() {
    const v = draft.values, text = value => (value == null || String(value).trim() === "" ? null : String(value).trim());
    const number = value => text(value) == null ? null : Number(value);
    const args = {enabled: !!v.enabled, topic_prefix: text(v.topic_prefix) || "hyperloom",
      username: text(v.username), client_id: text(v.client_id), interval_s: number(v.interval_s) ?? 10};
    if (draft.password) args.password = draft.password;
    else if (draft.clear) args.clear_password = true;
    return args;
  }
  async function post(action, args) {
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf()}, body: JSON.stringify(args), signal: AbortSignal.timeout(20000)});
  }
  async function run(kind, work) {
    draft.busy = kind; draft.error = ""; draft.result = null; H().render();
    try { await work(); } catch (error) { draft.error = error.message; } finally { draft.busy = ""; H().render(); H().refresh(); }
  }

  document.addEventListener("input", event => {
    if (event.target.dataset?.pageAction === "integrations-field") readField(event.target);
    if (event.target.id === "intg-range") draft.range = event.target.value;
  });
  document.addEventListener("change", event => {
    const el = event.target;
    if (el.dataset?.pageAction === "integrations-field") { readField(el); if (el.type === "checkbox") H().render(); }
    if (el.id === "intg-target") { draft.target = el.value; draft.exportResult = ""; H().render(); }
    if (el.id === "intg-share") { draft.share = el.checked; H().render(); }
  });
  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action^="integrations-"]');
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "integrations-save") {
      run("Save", async () => {
        const result = await post("integrations/mqtt/save", settingsArguments());
        draft.password = ""; draft.clear = false; draft.loaded = null;
        draft.result = `Saved (password ${result.credential}).`;
        H().announce("MQTT settings saved");
      });
    } else if (action === "integrations-test") {
      run("Test", async () => {
        const result = await post("integrations/mqtt/test", {});
        if (result.ok) draft.result = `${result.detail} in ${result.elapsed_s} s.`; else draft.error = `Test failed: ${result.detail}`;
      });
    } else if (action === "integrations-csv" || action === "integrations-json") {
      const target = (doc().export?.targets || []).find(t => t.document === draft.target);
      if (!target) return;
      const query = new URLSearchParams();
      if ((target.query || []).includes("range") && draft.range.trim()) query.set("range", draft.range.trim());
      draft.exportBusy = true; draft.exportResult = ""; H().render();
      (async () => {
        try {
          if (action === "integrations-csv") {
            if (draft.share) query.set("share", "1");
            const result = await H().request(`integrations/export/${target.document}.csv${query.size ? "?" + query : ""}`);
            H().download(result.filename, result.csv, "text/csv;charset=utf-8");
            draft.exportResult = `${result.rows} rows exported${result.share ? " (share mode)" : ""}.`;
          } else {
            const result = await H().request(`${target.document}${query.size ? "?" + query : ""}`);
            H().download(`hyperloom-${target.document.replace(/[^a-z0-9_-]+/gi, "-")}.json`, JSON.stringify(result, null, 2));
            draft.exportResult = "JSON document downloaded.";
          }
        } catch (error) { draft.exportResult = `Export failed: ${error.message}`; }
        finally { draft.exportBusy = false; H().render(); }
      })();
    }
  });
})();
