/* Endpoints (F17, F40): other chat-completions-compatible endpoints on an allow-list, each identified by the
   positive signatures of its own endpoints, with a reduced engine panel; and the exposure check of every
   configured engine, the router and every target (bind scope, listener evidence, unauthenticated models list). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const view = {selected: "", form: {label: "", scheme: "http", host: "", port: "", key_name: ""}, busy: "", error: "", notice: ""};
  try { view.selected = JSON.parse(localStorage.getItem("hyperloom-endpoints") || "{}").selected || ""; } catch (_) { /* storage may be disabled */ }
  const remember = () => { try { localStorage.setItem("hyperloom-endpoints", JSON.stringify({selected: view.selected})); } catch (_) {} };
  const docs = () => H().state.docs;
  const targets = () => docs().endpoints?.targets || [];
  const chosen = () => targets().find(t => t.id === view.selected) || null;
  const history = (series, id) => window.HyperloomTS.endpoint(series, {range: 3600, labels: {endpoint: id}});
  const charts = id => [history("endpoint_up", id), history("endpoint_generated_tokens", id), history("endpoint_prompt_tokens", id)];
  const VERDICT = {red: ["err", "exposed"], amber: ["warn", "review"], green: ["ok", "protected"], unknown: ["off", ""]};
  const verdictPill = v => { const [style, word] = VERDICT[v] || VERDICT.unknown; return `<span class="pill p-${style}">${H().esc(VERDICT[v] ? v : "unknown")}${word ? ` · ${H().esc(word)}` : ""}</span>`; };
  const age = t => { const now = docs().endpoints?.server_time; return typeof t === "number" && typeof now === "number" ? `${Math.max(0, Math.round(now - t))} s ago` : null; };
  const blocked = () => view.busy ? `${view.busy} in progress` : !H().csrf() || H().csrf() === "__HYPERLOOM_CSRF__" ? "Launch the portal through hyperloom ui to enable actions" : "";

  function post(action, body, label) {
    view.busy = label; view.error = ""; view.notice = ""; H().render();
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf()},
      body: JSON.stringify(body), signal: AbortSignal.timeout(60000)})
      .then(result => { view.notice = `${label} done`; H().announce(`${label} done`); return result; })
      .catch(error => { view.error = `${label}: ${error.message}`; H().announce(view.error); return null; })
      .finally(() => { view.busy = ""; H().render(); H().refresh(); });
  }

  // Rows that turn into labelled blocks on narrow screens (endpoints.css), so no column hides off-screen.
  const EXPOSURE_HEADERS = ["Endpoint", "Address (from the control machine)", "Configured scope", "Listener (node)", "GET /v1/models without credentials", "Verdict"];
  const TARGET_HEADERS = ["Endpoint", "Address", "Identified as", "State", "Models", "Models list latency", "Last check", ""];
  const stacked = (headers, values) => `<tr>${values.map((v, i) => `<td data-label="${H().esc(headers[i])}"><div class="ep-v">${v}</div></td>`).join("")}</tr>`;

  function exposureCard() {
    const {esc, card, table, cells, display, unavailable} = H();
    const doc = docs()["endpoints/exposure"] || {};
    const summary = doc.summary;
    const counts = summary ? ["red", "amber", "unknown", "green"].map(v => `${verdictPill(v)} <b>${esc(summary[v] ?? 0)}</b>`).join(" ") : unavailable(doc.why?.rows);
    const checked = doc.checked_at != null ? `Checked ${esc(age(doc.checked_at) || "")} · every 60 s while the samplers run.` : "";
    const notes = [doc.why?.stale, doc.why?.configuration].filter(Boolean).map(n => `<p class="hint">${esc(n)}</p>`).join("");
    const rows = (doc.rows || []).map(r => {
      const listener = r.listener ? (r.listener.state === "unavailable" ? unavailable(r.listener.why) : `${esc(r.listener.state)}${r.listener.why ? ` <span class="hint">${esc(r.listener.why)}</span>` : ""}`)
        : unavailable(r.error || "not checked yet");
      const probe = r.unauthenticated?.outcome ? `${esc(r.unauthenticated.outcome.replace("_", " "))} <span class="hint">${esc(r.unauthenticated.detail || "")}</span>` : unavailable(r.unauthenticated?.detail || r.error || "not checked yet");
      const address = r.probe_address ? `${esc(r.probe_address)}:${esc(r.port)}` : unavailable(r.error || "no address");
      const scope = r.configured_scope ? `${esc(r.configured_scope)} · ${r.key_configured ? "key configured" : r.key_supported === false ? "no key support" : "no key"}` : unavailable(r.error);
      return stacked(EXPOSURE_HEADERS, [`<b>${esc(r.label || r.id)}</b><div class="hint mono">${esc(r.id)}</div>`, address, scope, listener, probe, `${verdictPill(r.verdict)}<div class="hint">${esc(r.reason || "")}</div>`]);
    }).join("");
    const body = doc.rows == null ? `<p>${display(null, doc.why?.rows || "loading")}</p>` :
      `<div class="ep-stack">${table(EXPOSURE_HEADERS, rows, "Exposure check")}</div>`;
    return card("Exposure check", `<div class="row ep-counts" role="status">${counts}</div><p class="hint">${checked} Red: an endpoint listening on the LAN answers the models list without a key. Listener evidence comes from each node's socket table.</p>${notes}${body}`, "F40");
  }

  function targetsCard() {
    const {esc, card, table, cells, unavailable, button} = H();
    const doc = docs().endpoints || {};
    const disabled = blocked();
    if (doc.targets == null) return card("Other endpoints", `<p>${unavailable(doc.why?.targets || "loading")}</p>`);
    const rows = targets().map(t => {
      const p = t.probe;
      const up = p ? `<span class="pill p-${p.up ? "ok" : "err"}">${p.up ? "up" : "down"}</span>${p.error ? `<div class="hint">${esc(p.error)}</div>` : ""}` : unavailable(t.why?.probe);
      const kind = p ? `${esc(p.kind)}<div class="hint">${esc(p.kind_text || "")}${p.catch_all ? " · answers any path (catch-all)" : ""}</div>` : unavailable(t.why?.probe);
      const models = p?.models_total != null ? esc(p.models_total) : unavailable(p?.why?.models || t.why?.probe);
      const latency = p?.latency_ms != null ? `${esc(p.latency_ms)} ms` : unavailable(p ? "no models list" : t.why?.probe);
      const actions = `<div class="row">${button("Details", "", {extra: `data-page-action="endpoints-select" data-id="${esc(t.id)}" aria-pressed="${t.id === view.selected}"`})}${button("Remove", "", {style: "dan", extra: `data-page-action="endpoints-remove" data-id="${esc(t.id)}" aria-label="Remove ${esc(t.label)}"`, disabled})}</div>`;
      return stacked(TARGET_HEADERS, [`<b>${esc(t.label)}</b><div class="hint mono">${esc(t.id)}</div>`, `<span class="mono">${esc(t.scheme)}://${esc(t.host)}:${esc(t.port)}</span>${t.host !== t.address && t.address ? `<div class="hint">${esc(t.address)}</div>` : ""}<div class="hint">${t.key_name ? `key: ${esc(t.key_name)}` : "no key"}</div>`,
        kind, up, models, latency, p ? esc(age(p.checked_at) || "") : unavailable(t.why?.probe), actions]);
    }).join("");
    const f = view.form;
    const form = `<form class="ep-form" data-page-action="endpoints-form" novalidate>
<div class="grid ep-fields">
<label class="field" for="ep-label">Label<input class="inp" id="ep-label" name="label" maxlength="64" required value="${esc(f.label)}" placeholder="RTX box"></label>
<label class="field" for="ep-scheme">Scheme<select class="inp" id="ep-scheme" name="scheme">${["http", "https"].map(s => `<option value="${s}" ${f.scheme === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
<label class="field" for="ep-host">Host<input class="inp" id="ep-host" name="host" maxlength="64" required value="${esc(f.host)}" placeholder="private IP or machine id" autocomplete="off"></label>
<label class="field" for="ep-port">Port<input class="inp" id="ep-port" name="port" inputmode="numeric" required value="${esc(f.port)}" placeholder="8080"></label>
<label class="field" for="ep-key">Key name (optional)<input class="inp" id="ep-key" name="key_name" maxlength="40" value="${esc(f.key_name)}" placeholder="secret name" autocomplete="off"></label>
</div><div class="row">${button("Add endpoint", "", {style: "pri", extra: 'data-page-action="endpoints-save"', disabled})}<span class="hint">Only private or loopback addresses and configured machines; names are not resolved. The key itself stays in the secret store.</span></div>
<p class="field-error" id="ep-error" role="alert">${esc(view.error)}</p></form>`;
    const limit = doc.limits?.max_targets ? `${targets().length} of ${doc.limits.max_targets} targets` : "";
    return card("Other endpoints", `<div class="ep-stack">${table(TARGET_HEADERS, rows, "Endpoint targets")}</div><p class="hint">${esc(limit)} · probed every 15 s while the samplers run; identification is repeated every 10 min or on Check now.</p>${form}`, "F17");
  }

  function detailCard() {
    const {esc, card, table, cells, unavailable, display, kv} = H();
    const t = chosen();
    if (!t) return "";
    const p = t.probe;
    if (!p) return card(`${t.label} · engine panel`, `<p>${unavailable(t.why?.probe)}</p>`);
    const models = (p.models || []).map(m => cells([`<span class="mono">${esc(m.id)}</span>`, m.max_model_len != null ? `${esc(m.max_model_len.toLocaleString())} tokens` : unavailable("not reported by this server")])).join("");
    const signatures = p.signatures?.length ? p.signatures.map(s => `<span class="tag">${esc(s)}</span>`).join(" ") : unavailable(p.why?.kind || "no positive signature");
    const panel = p.panel ? kv(p.panel, [["Model", "model"], ["Engine", "engine"], ["State", "state"], ["Context", "context_tokens", "tokens"], ["Uptime", "uptime_seconds", "s"], ["Queue waiting", "waiting"], ["Queue running", "running"], ["Slots busy", "slots_busy"], ["Generated tokens (counter)", "generated_tokens_total", "tokens"], ["Prompt tokens (counter)", "prompt_tokens_total", "tokens"]])
      : `<p>${unavailable(p.kind === "strix" ? "status not answered" : "engine status is only read from our engines; other servers show their models list")}</p>`;
    const [up, generated, prompt] = charts(t.id).map(e => docs()[e]);
    const TS = window.HyperloomTS;
    const tokenLines = [...TS.lines(generated || {}, {label: () => "generated"}).map(l => ({...l, color: "--s1"})), ...TS.lines(prompt || {}, {label: () => "prompt"}).map(l => ({...l, color: "--s2"}))];
    const step = generated?.step || prompt?.step || 15;
    const upChart = up?.lines == null ? `<p>${display(null, up?.why?.lines || "loading")}</p>` : TS.chart({title: "Reachability (state: 1 up, 0 down)", unit: "", lines: TS.lines(up, {label: () => "up"}).map(l => ({...l, color: "--s1"})), step: up.step || 15, cadence: up.tier === "raw" ? up.interval_s || 15 : 0, start: up.start, end: up.end, max: 1, min: 0, group: "endpoint", height: 120});
    const tokenChart = !generated && !prompt ? `<p>${display(null, "loading")}</p>` : !tokenLines.length ? `<p>${display(null, p.why?.counters || generated?.why?.lines || "no token counters recorded for this endpoint")}</p>`
      : TS.chart({title: `Tokens per ${generated?.tier === "raw" ? `${generated.interval_s || 15} s sample` : `${step} s bucket`} (engine counters, measured)`, unit: "tokens", lines: tokenLines, step, cadence: generated?.tier === "raw" ? generated.interval_s || 15 : 0, start: generated?.start ?? prompt?.start, end: generated?.end ?? prompt?.end, group: "endpoint", height: 160});
    const notes = [p.catch_all ? "This server answers 2xx on a random path (catch-all): status codes alone prove nothing; only body signatures count." : "", p.why?.key, p.why?.models].filter(Boolean).map(n => `<p class="hint">${esc(n)}</p>`).join("");
    return card(`${t.label} · engine panel`, `<div class="grid g2"><div><h3 class="ep-h">Identification</h3><p>${esc(p.kind)} · ${esc(p.kind_text || "")}</p><div class="row">${signatures}</div>${notes}<h3 class="ep-h">Models</h3>${table(["Model", "Context"], models, "Models")}</div><div><h3 class="ep-h">Engine</h3>${panel}</div></div><div class="grid">${upChart}${tokenChart}</div>`, "last hour · observer: control machine");
  }

  (window.HyperloomPages ||= {}).endpoints = {
    title: "Endpoints",
    group: "Inference",
    order: 40,
    icon: '<circle cx="5" cy="10" r="2"/><circle cx="15" cy="5" r="2"/><circle cx="15" cy="15" r="2"/><path d="M7 9l6-3M7 11l6 3"/>',
    endpoints: () => ["endpoints", "endpoints/exposure", ...(chosen() ? charts(chosen().id) : [])],
    ready: () => !!docs().endpoints && !!docs()["endpoints/exposure"],
    actions: () => H().button(view.busy === "Check" ? "Checking…" : "Check now", "", {style: "pri", extra: 'data-page-action="endpoints-check"', disabled: blocked()}),
    render() {
      const doc = docs().endpoints || {};
      const collectors = doc.collectors;
      const {esc, unavailable} = H();
      const state = collectors ? Object.entries(collectors).map(([name, s]) => `${esc(name)}: ${s ? (s.error ? `last error · ${esc(s.error)}` : s.last_ok ? `ok ${esc(age(s.last_ok) || "")}` : "waiting for the first run") : "not registered"}`).join(" · ")
        : unavailable(doc.why?.collectors || "samplers not reported");
      const status = `<p class="hint" role="status">Collectors · ${state}${view.notice ? ` · ${esc(view.notice)}` : ""}</p>`;
      return `<div class="grid">${status}${exposureCard()}${targetsCard()}${detailCard()}</div>`;
    },
    after(main) { window.HyperloomTS.bind(main); },
  };

  document.addEventListener("input", event => {
    const input = event.target.closest(".ep-form [name]");
    if (input) view.form[input.name] = input.value;
  });
  function save() {
    if (blocked()) return;
    const f = view.form, port = Number(f.port);
    if (!f.label.trim() || !f.host.trim() || !Number.isInteger(port)) { view.error = "Label, host and an integer port are required"; H().render(); return; }
    const body = {label: f.label.trim(), scheme: f.scheme, host: f.host.trim(), port};
    if (f.key_name.trim()) body.key_name = f.key_name.trim();
    post("endpoints/save", body, "Save").then(result => {
      if (result?.target) { view.form = {label: "", scheme: "http", host: "", port: "", key_name: ""}; view.selected = result.target.id; remember(); }
    });
  }
  document.addEventListener("submit", event => {
    if (event.target.dataset?.pageAction !== "endpoints-form") return;
    event.preventDefault();
    save();
  });
  document.addEventListener("change", event => {
    const input = event.target.closest(".ep-form select[name]");
    if (input) view.form[input.name] = input.value;
  });
  document.addEventListener("click", event => {
    const control = event.target.closest('[data-page-action^="endpoints-"]');
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "endpoints-select") { view.selected = control.dataset.id; remember(); H().refresh(); }
    else if (action === "endpoints-save") save();
    else if (action === "endpoints-remove") {
      if (!window.confirm(`Remove the endpoint ${control.dataset.id}? Its history stays in the store.`)) return;
      post("endpoints/remove", {id: control.dataset.id}, "Remove").then(() => { if (view.selected === control.dataset.id) { view.selected = ""; remember(); } });
    } else if (action === "endpoints-check") post("endpoints/check", {}, "Check");
  });
})();
