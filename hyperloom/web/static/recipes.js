/* Recipes & profiles page. Every action is a `hyperloom recipe ...` command line run by the portal (docs/api.md);
   the answers carry that command line (`cli`), shown under each result. UI-SPEC style: disabled controls name their
   reason, refusals are shown verbatim, nothing is invented. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const $ = id => document.getElementById(id);
  const OFFLINE_S = 40, HARDWARE_S = 160;
  const R = {catalog: null, error: null, model: "", profile: "", selected: "", detail: null, detailError: null,
    edits: {}, raw: {}, fieldErrors: {}, parseErrors: {}, editsError: null, explain: null, check: null, simulate: null, tokens: 8192,
    plan: null, planKey: null, diff: null, compare: "", active: null, routes: null, tuning: "", overrides: [],
    source: null, sourceCheck: null, busy: {}, timer: null, lastLoad: 0,
    matrix: null, start: {cls: "", goal: "any", recipe: "", profile: "", ready: null, loading: false, error: null, generation: 0}};
  const esc = value => H().esc(value);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const busy = key => !!R.busy[key];
  const rerender = () => H().render();

  async function get(endpoint) {
    const response = await fetch("/hyperloom/api/" + endpoint, {cache: "no-store", credentials: "same-origin"});
    const body = await response.json();
    if (!body || typeof body !== "object") throw new Error(`HTTP ${response.status}`);
    if (!response.ok && body.exit_code === undefined) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  async function post(endpoint, args, seconds = OFFLINE_S) {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    const response = await fetch("/hyperloom/api/" + endpoint, {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(args), signal: AbortSignal.timeout(seconds * 1000)});
    const body = await response.json();
    if (body.exit_code === undefined && !response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  async function run(key, work) {
    R.busy[key] = true; rerender();
    try { await work(); } catch (error) { H().announce(error.message); R.busy[key + ":error"] = error.message; }
    finally { R.busy[key] = false; rerender(); }
  }

  // --- data -------------------------------------------------------------------------------------------------------
  async function load(force = false) {
    if (!force && Date.now() - R.lastLoad < 10000 && R.catalog) return;
    R.lastLoad = Date.now();
    try {
      const listing = await get("recipes");
      if (!listing.ok) throw new Error(listing.error || "recipe list unavailable");
      R.catalog = listing.result; R.error = null;
      loadMatrix();                                 // not awaited: load's callers continue with the catalog
      const models = modelsWithRecipes();
      // Default: the first model a configured deployment profile serves (what can actually run here).
      if (!R.model || !models.includes(R.model)) R.model = models.find(m => (R.catalog.models[m] || []).length) || models[0] || "";
      const serving = R.catalog.models[R.model] || [];
      if (!serving.includes(R.profile)) R.profile = serving[0] || "";
    } catch (error) { R.error = error.message; }
    rerender();
  }
  async function loadMatrix() {
    try { R.matrix = (await get("recipes/matrix")).result; delete R.busy["matrix:error"]; }
    catch (error) { R.busy["matrix:error"] = error.message; }
    rerender();
  }
  async function poll() {
    if (document.hidden || location.hash.replace(/^#\/?/, "").split(/[?&]/)[0] !== "recipes") return;
    await load();
    try { R.active = (await get("recipes/active")).result; R.routes = (await get("recipes/routes")).result; }
    catch (error) { R.active = R.routes = null; R.busy["poll:error"] = error.message; }
    rerender();
  }
  function modelsWithRecipes() {
    const ids = new Set((R.catalog?.recipes || []).map(r => r.model).filter(Boolean));
    Object.keys(R.catalog?.models || {}).forEach(m => ids.add(m));
    return [...ids].sort();
  }
  const recipesOf = model => (R.catalog?.recipes || []).filter(r => r.model === model || (!r.model && r.error));
  const current = () => (R.catalog?.recipes || []).find(r => r.id === R.selected);
  async function select(id) {
    if (R.selected === id && R.detail) return;
    Object.assign(R, {selected: id, detail: null, detailError: null, edits: {}, raw: {}, fieldErrors: {}, parseErrors: {}, editsError: null,
      explain: null, check: null, simulate: null, plan: null, planKey: null, diff: null, source: null, sourceCheck: null,
      tuning: "", overrides: []});
    rerender();
    try {
      const doc = await get("recipes/" + encodeURIComponent(id));
      if (R.selected !== id) return;               // another recipe was opened meanwhile: its answer wins
      if (!doc.ok) throw new Error(doc.error || "recipe unavailable");
      R.detail = doc.result; R.source = doc.result.text;
    } catch (error) { if (R.selected === id) R.detailError = error.message; }
    rerender();
  }

  // --- requests built from the page state ------------------------------------------------------------------------
  function layerArgs() {
    const out = {};
    if (R.tuning) out.tuning_profile = R.tuning;
    if (R.overrides.length) out.overrides = [...R.overrides];
    return out;
  }
  function editArgs() { return Object.keys(R.edits).length ? {edits: {...R.edits}} : {}; }
  const planKey = () => JSON.stringify([R.selected, R.profile, R.edits, R.raw, R.tuning, R.overrides]);
  const invalid = () => Object.keys(R.parseErrors).length > 0;
  function fieldPathIn(message) {
    const paths = (R.detail?.fields || []).map(f => f.path).filter(p => message.includes(p));
    return paths.sort((a, b) => b.length - a.length)[0] || null;
  }
  let validateTimer = null;
  function scheduleValidate() {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(() => run("edits", async () => {
      if (!Object.keys(R.edits).length) { R.fieldErrors = {}; R.editsError = null; R.explain = null; return; }
      const doc = await post("recipes/explain", {recipe: R.selected, ...layerArgs(), ...editArgs()});
      R.fieldErrors = {}; R.editsError = null; R.explain = doc;
      if (!doc.ok) {
        const path = fieldPathIn(doc.error || "");
        if (path) R.fieldErrors[path] = doc.error; else R.editsError = doc.error;
      }
    }), 600);
  }
  function parse(field, text) {
    if (field.type === "number" || field.type === "integer") {
      if (text.trim() === "") return {error: "a value is required"};
      const value = Number(text);
      if (!Number.isFinite(value)) return {error: "not a number"};
      if (field.type === "integer" && !Number.isInteger(value)) return {error: "an integer is required"};
      if (value < field.min || value > field.max || (field.min_exclusive && value <= field.min)) return {error: `outside ${field.min_exclusive ? "(" : "["}${field.min}, ${field.max}]`};
      return {value};
    }
    if (field.type === "boolean") return {value: text === "true"};
    if (field.type === "enum") return {value: text === "" ? null : text};
    if (field.type === "count") return text === "fill" ? {value: "fill"} : Number.isInteger(Number(text)) && Number(text) >= 0 && text.trim() !== "" ? {value: Number(text)} : {error: "fill or a whole number"};
    if (field.type === "layers" || field.type === "list") { try { return {value: JSON.parse(text)}; } catch (_) { return {error: "JSON array expected, e.g. [1, 2, 3]"}; } }
    return {value: text === "" ? null : text};
  }
  const shown = value => value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);

  // --- rendering --------------------------------------------------------------------------------------------------
  function cli(doc) {
    if (!doc?.cli) return "";
    const files = doc.cli_files ? Object.entries(doc.cli_files).map(([name, text]) => `<details><summary>${esc(name)}</summary><pre class="raw">${esc(text)}</pre></details>`).join("") : "";
    return `<p class="hint cli">CLI: <code class="mono">${esc(doc.cli)}</code></p>${files}`;
  }
  function refusal(doc) {
    if (!doc) return "";
    if (doc.stopped) return `<p class="field-error" role="alert">${esc(doc.error)}</p>`;
    return doc.ok ? "" : `<p class="field-error" role="alert">${esc(doc.error || "refused")}</p>`;
  }
  function basis(value) { return value ? `<span class="tag basis-${esc(value)}">${esc(value.replace("_", " "))}</span>` : ""; }
  function chip(label, item) {
    if (!item) return "";
    return `<span class="chip"><span class="lbl">${esc(label)}</span> <b>${esc(item.value)}${item.unit ? " " + esc(item.unit) : ""}</b> ${basis(item.basis)}${item.inherited ? '<span class="tag">base, not re-measured</span>' : ""}</span>`;
  }
  function headline(row) {
    const h = row.headline || {};
    const quality = Object.entries(h).filter(([k]) => /delta|kl_/.test(k)).map(([, v]) => chip(v.label, v)).join("");
    const parts = [chip("TTFT 1K", h.ttft_1k_s), chip("TTFT 8K", h.ttft_8k_s), chip("TTFT 32K", h.ttft_32k_s), chip(h.decode_tok_s?.label || "decode", h.decode_tok_s), chip(h.memory_gib?.label || "memory", h.memory_gib), quality].join("");
    return parts || `<span class="unavailable">no predictions in this recipe</span>`;
  }
  function statusOf(row) {
    if (row.error) return H().statusPill("error");
    return H().statusPill({builtin: "built-in", pending: "pending", user: "user", configured: "configured"}[row.status] || row.status);
  }
  function header() {
    const models = modelsWithRecipes();
    const serving = R.catalog?.models[R.model] || [];
    const profileControl = serving.length
      ? `<select class="inp" id="recipe-profile" aria-describedby="recipe-profile-hint">${serving.map(p => `<option value="${esc(p)}"${p === R.profile ? " selected" : ""}>${esc(p)}</option>`).join("")}</select>`
      : `<select class="inp" id="recipe-profile" disabled aria-describedby="recipe-profile-hint"><option>none</option></select>`;
    return `<section class="card"><div class="grid g2"><div class="field"><label for="recipe-model">Model</label><select class="inp" id="recipe-model">${models.map(m => `<option value="${esc(m)}"${m === R.model ? " selected" : ""}>${esc(m)}</option>`).join("")}</select></div>
      <div class="field"><label for="recipe-profile">Deployment profile</label>${profileControl}<span class="hint" id="recipe-profile-hint">${serving.length ? "The machines a recipe of this model runs on (cluster configuration)." : "No configured profile serves this model: recipes can be read and edited, not checked or deployed."}</span></div></div>
      <p class="hint">A recipe profile is one recipe file (<span class="mono">model.variant</span>): built-in ones are read-only examples; yours live in <span class="mono">${esc(R.catalog?.user_dir || "")}</span>.</p></section>`;
  }
  function activeCard() {
    const deployments = R.active?.deployments || [];
    if (!R.active) return H().card("Active", H().unavailable(R.busy["poll:error"] || "loading"));
    if (!deployments.length) return H().card("Active", `<p class="t2">Nothing launched by hyperloom is running.</p>`);
    const routes = Object.fromEntries((R.routes?.deployments || []).map(d => [d.profile, d]));
    return deployments.map(d => {
      const r = routes[d.profile];
      const rec = d.record || {};
      const inputs = (d.inputs || []).map(i => `${esc(i.role)} ${esc(i.id || i.original)} · ${esc(i.now)}`).join("<br>");
      const head = `<div class="row">${H().statusPill(d.phase || "running")} ${d.recipe ? H().tag("recipe " + d.recipe) : H().tag("no recipe")} ${rec.production === false ? H().tag("non-production") : ""} ${rec.untuned ? H().tag("untuned") : ""}</div>`;
      if (!d.recipe) return H().card(`Active · ${d.profile}`, head + `<p class="hint">${esc(d.why || "")}</p>`);
      const service = rec.service ? `<p>RTX prefill service: ${rec.service.start ? "started" : "not started · " + esc(rec.service.why || "")}</p>` : "";
      let stats = `<p>${H().unavailable(r?.why?.stats || "route statistics not reported")}</p>`;
      if (r?.totals) {
        const total = (r.totals.rtx || 0) + (r.totals.nodes || 0);
        const share = total ? Math.round(100 * r.totals.rtx / total) : 0;
        stats = `<div class="split" role="img" aria-label="${r.totals.rtx} requests on the RTX, ${r.totals.nodes} on the nodes"><i data-meter="${share}"></i></div><p>RTX <b>${r.totals.rtx}</b> · nodes <b>${r.totals.nodes}</b> · refused <b>${esc(r.totals.refused ?? "unavailable")}</b> · failed hand-offs <b>${esc(r.totals.handoff_failed ?? "unavailable")}</b> <span class="hint">admitted at selection time, router-measured</span></p>`
          + H().table(["Route", "On", "Enabled", "Admitted"], r.routes.map(x => H().cells([esc(x.id), esc(x.on === "rtx" ? "RTX PRO" : "nodes"), esc(x.enabled ? "yes" : "no"), esc(x.admitted ?? "")])).join(""), "Routes of the running recipe")
          + (r.why?.identity ? `<div class="banner warn">${esc(r.why.identity)}</div>` : "")
          + (r.recent ? H().table(["Request", "Tokens (uncached)", "Route", "On", "Outcome", "Status", ["First byte", "hide-n"]], r.recent.slice(-20).reverse().map(x => H().cells([`<span class="mono">${esc(x.id || "")}</span>`, esc(`${x.prompt_tokens ?? ""} (${x.uncached_tokens ?? ""})`), esc(x.route || "-"), esc(x.on), esc(x.outcome || ""), esc(x.status ?? ""), `<span class="hide-n">${esc(x.ttfb_ms != null ? x.ttfb_ms + " ms" : "")}</span>`])).join(""), "Recent requests and their routes") : `<p>${H().unavailable(r.why?.recent || "per-request log not reported")}</p>`);
        if (r.state) stats += `<p class="hint">Route state ${r.state.age_s != null ? `${esc(r.state.age_s)} s old` : "of unknown age"} (the router refuses with a stale state).</p>`;
        else if (r.why?.state) stats += `<p class="hint">${esc(r.why.state)}</p>`;
      }
      return H().card(`Active · ${d.profile}`, head + service + stats + `<details><summary>Effective recipe as launched</summary>${d.effective ? `<pre class="raw">${esc(JSON.stringify(d.effective, null, 1))}</pre>` : H().unavailable(d.why)}</details><details><summary>Inputs</summary><p class="hint">${inputs || "not recorded"}</p></details>`, "what is running now");
    }).join("");
  }
  function profileCards() {
    const rows = recipesOf(R.model);
    if (!rows.length) return H().empty("No recipes for this model", "Write one with Save as my recipe from a similar model, or add a file to the user directory.");
    return `<div class="grid g2 recipe-cards">${rows.map(row => `<article class="card recipe-card${row.id === R.selected ? " sel" : ""}"><div class="row between"><b class="mono">${esc(row.id)}</b>${statusOf(row)}</div>
      <p class="t2">${esc(row.description || row.error || "")}</p>${row.blocker ? `<p class="hint">Waits for: ${esc(row.blocker)}</p>` : ""}<div class="chips">${row.error ? "" : headline(row)}</div>
      <button type="button" class="btn sm" data-recipe-action="open" data-id="${esc(row.id)}" ${row.error ? `disabled title="${esc(row.error)}"` : ""}>${row.id === R.selected ? "Selected" : "Open"}</button></article>`).join("")}</div>`;
  }
  function tradeoffs(row) {
    const items = row.tradeoffs || [];
    if (!items.length) return `<p>${H().unavailable("this recipe states no predictions")}</p>`;
    return H().table(["Route", "Metric", "Value", "At", "Basis", ["Scope", "hide-n"], ["Evidence", "hide-n"]], items.map(t => H().cells([esc(t.route || ""), esc(t.label), `<b>${esc(t.value)}</b> ${esc(t.unit)}`, esc(t.at_prompt_tokens ? `${t.at_prompt_tokens} tokens` : t.concurrency ? `C${t.concurrency}` : t.batch ? `batch ${t.batch}` : ""), basis(t.basis) + (t.inherited ? '<span class="tag">base</span>' : ""), `<span class="hide-n">${esc(t.scope || "")}</span>`, `<span class="hide-n mono">${esc(t.evidence || "")}</span>`])).join(""), "Predicted trade-offs") + `<p class="hint">As stated by the recipe file; basis says how each number was obtained. A copy keeps its base's numbers only as "base, not re-measured".</p>`;
  }
  function fieldInput(field, index) {
    const id = `rf-${index}`, value = Object.hasOwn(R.raw, field.path) ? R.raw[field.path] : shown(Object.hasOwn(R.edits, field.path) ? R.edits[field.path] : field.value);
    const changed = Object.hasOwn(R.edits, field.path), error = R.parseErrors[field.path] || R.fieldErrors[field.path];
    const describe = `${id}-hint${error ? ` ${id}-error` : ""}`;
    const attrs = `id="${id}" data-path="${esc(field.path)}" aria-describedby="${describe}" class="inp${changed ? " chg" : ""}"${R.detail.writable === undefined ? "" : ""}`;
    let control;
    if (field.type === "enum" || field.type === "boolean") {
      const values = field.type === "boolean" ? ["true", "false"] : field.values.map(v => v === null ? "" : String(v));
      control = `<select ${attrs}>${values.map(v => `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(v === "" ? "(none)" : v)}</option>`).join("")}</select>`;
    } else control = `<input ${attrs} type="${field.type === "number" || field.type === "integer" ? "number" : "text"}" ${field.type === "integer" ? 'step="1"' : field.type === "number" ? 'step="any"' : ""} value="${esc(value)}">`;
    const range = field.min !== undefined && field.max !== undefined ? ` · ${field.min_exclusive ? "(" : "["}${field.min}, ${field.max}]` : field.type === "count" ? " · fill or a count" : "";
    const explained = R.explain?.ok ? (R.explain.result.fields || []).find(f => f.path === field.path) : null;
    return `<div class="field"><label for="${id}">${esc(field.path.split(".").slice(-1)[0])}</label>${control}<span class="hint" id="${id}-hint"><span class="mono">${esc(field.path)}</span> · ${field.written ? "written in the recipe" : "default"}${range}${field.pinned ? " · pinned (a tuning profile cannot change it)" : ""}${explained ? ` · now ${esc(explained.layer)} (${esc(explained.origin)})` : ""}</span>${error ? `<span class="field-error" id="${id}-error" role="alert">${esc(error)}</span>` : ""}</div>`;
  }
  function fieldsCard() {
    const fields = R.detail.fields || [];
    const groups = {};
    fields.forEach((field, index) => (groups[field.group] ||= []).push([field, index]));
    const changed = Object.keys(R.edits).length + Object.keys(R.parseErrors).length;
    const body = Object.entries(groups).map(([group, items]) => `<details${items.some(([f]) => Object.hasOwn(R.edits, f.path) || R.fieldErrors[f.path] || R.parseErrors[f.path]) ? " open" : ""}><summary>${esc(group)} <span class="hint">${items.length} fields</span></summary><div class="grid g3">${items.map(([f, i]) => fieldInput(f, i)).join("")}</div></details>`).join("");
    return H().card("Fields", `<p class="hint">Changes are one overrides layer on this recipe (checked as you type, like <span class="mono">--recipe-overrides</span>). They apply to the checks, the preview and the deploy below, never to the file itself.</p><div class="row">${changed ? H().tag(`${changed} changed`) : ""}${busy("edits") ? '<span class="hint">checking…</span>' : ""}<button type="button" class="btn sm" data-recipe-action="reset-edits" ${changed ? "" : 'disabled title="No changes"'}>Reset fields</button></div>${R.editsError ? `<p class="field-error" role="alert">${esc(R.editsError)}</p>` : ""}${body}${R.explain ? cli(R.explain) : ""}`, "edit with inline validation");
  }
  const PROFILE_SCHEMAS = ["strix-stream/recipe-profile-v1", "tierstream/recipe-profile-v1"];
  function layersCard() {
    const name = R.detail.name;
    const files = (R.catalog?.layers || []).filter(l => l.recipe === name && !l.error);
    const isProfile = l => PROFILE_SCHEMAS.includes(l.schema);   // tierstream/* is the renamed spelling of the same kind
    const profiles = files.filter(isProfile), overrides = files.filter(l => !isProfile(l));
    const tuning = `<div class="field"><label for="recipe-tuning">Tuning profile</label><select class="inp" id="recipe-tuning"><option value="">none</option>${profiles.map(l => `<option value="${esc(l.name)}"${l.name === R.tuning ? " selected" : ""}>${esc(l.name)}</option>`).join("")}</select><span class="hint">The autotuner's; refused as stale when the hardware or recipe differ.</span></div>`;
    const list = overrides.length ? `<fieldset class="checks"><legend>Overrides layers (in this order)</legend>${overrides.map((l, i) => `<label><input type="checkbox" data-layer="${esc(l.name)}"${R.overrides.includes(l.name) ? " checked" : ""}> ${esc(l.name)} <span class="hint">${esc(Object.keys(l.set || {}).length)} values</span></label> <button type="button" class="btn sm" data-recipe-action="delete-layer" data-name="${esc(l.name)}" data-sha="${esc(l.sha256)}">Delete</button>`).join("<br>")}</fieldset>` : `<p class="hint">No overrides layers for ${esc(name)} yet.</p>`;
    return H().card("Layers", tuning + list + `<div class="row"><button type="button" class="btn" data-recipe-action="save-layer" ${Object.keys(R.edits).length ? "" : 'disabled title="Change a field first"'}>Save field changes as a layer…</button><button type="button" class="btn" data-recipe-action="add-layer">Add a layer document…</button></div>`, "tuning profile and overrides, from the user layers directory");
  }
  function hardwareCard() {
    const reason = !R.profile ? "No deployment profile serves this model" : "";
    const c = R.check;
    let result = "";
    if (c?.result) {
      const res = c.result;
      const match = (res.match || []).length ? `<p><b>Does not match this hardware:</b></p><ul>${res.match.map(m => `<li>${esc(m)}</li>`).join("")}</ul>` : `<p>${H().statusPill("ok")} Matches the probed hardware.</p>`;
      const decision = res.deploy ? `<p>Decode: <b>${esc(res.deploy.decode?.id || "")}</b></p>${H().table(["Prefill route", "Placed", "Why"], (res.deploy.prefill || []).map(p => H().cells([esc(p.id), esc(p.place === "offload" ? "RTX PRO" : p.place), esc(p.why || "")])).join(""), "Prefill routes")}<p>RTX service: ${res.deploy.service?.start ? "starts" : "not started · " + esc(res.deploy.service?.why || "")}</p>` : "";
      result = match + (res.refused ? `<p class="field-error" role="alert">Refused: ${esc(res.refused)}</p>` : decision) + `<p class="hint">${esc((res.notes || []).join(" · "))}</p>`;
    }
    const sim = R.simulate?.result?.simulated_request;
    const simulation = sim ? `<p>${sim.refused ? `<span class="field-error">Refused: ${esc(sim.refused)}</span>` : `A ${esc(sim.prompt_tokens)}-token request goes to <b>${esc(sim.route)}</b>${sim.handoff_route ? ` then decodes on ${esc(sim.handoff_route)}` : ""}.`}</p>` : "";
    const receipts = R.catalog?.receipts || [];
    return H().card("Hardware check", `<div class="row"><button type="button" class="btn pri" data-recipe-action="check" ${reason || busy("check") ? `disabled title="${esc(reason || "checking")}"` : ""}>Check on ${esc(R.profile || "…")}</button>${busy("check") ? '<span class="hint">probing…</span>' : ""}</div>${reason ? `<p class="hint">${esc(reason)}</p>` : ""}${refusal(c)}${result}${c ? cli(c) : ""}
      <div class="row"><div class="field"><label for="recipe-tokens">Simulate a request of (prompt tokens)</label><input class="inp" id="recipe-tokens" type="number" min="1" step="1" value="${esc(R.tokens)}"></div><button type="button" class="btn" data-recipe-action="simulate" ${reason || busy("simulate") ? `disabled title="${esc(reason || "running")}"` : ""}>Simulate route</button></div>${simulation}${R.simulate ? refusal(R.simulate) + cli(R.simulate) : ""}
      <details><summary>Probes and evidence</summary><p class="hint">Probes run as jobs (one at a time). Reading model headers and measuring disks read the model files.</p><div class="row"><button type="button" class="btn" data-recipe-action="probe" ${reason ? `disabled title="${esc(reason)}"` : ""}>Probe hardware</button><button type="button" class="btn" data-recipe-action="probe-descriptors" ${reason ? `disabled title="${esc(reason)}"` : ""}>Read model headers…</button><button type="button" class="btn" data-recipe-action="add-receipt">Add a disk receipt…</button></div>${receipts.length ? H().table(["Receipt", "Host", "Read GB/s", "Use"], receipts.map(r => H().cells([esc(r.name), esc(r.host || ""), esc(r.disk_read_gbytes_ps ?? r.error ?? ""), `<button type="button" class="btn sm" data-recipe-action="use-receipt" data-name="${esc(r.name)}" data-host="${esc(r.host || "")}" ${reason ? "disabled" : ""}>Probe with it</button>`])).join(""), "Disk receipts") : ""}</details>`, "match, decision and refusal reasons on the probed hardware");
  }
  function previewCard() {
    const reason = !R.profile ? "No deployment profile serves this model" : "";
    const p = R.plan, res = p?.result;
    let body = "";
    if (res) {
      const up = res.up || {};
      const service = res.service || res.preview?.service;
      const plan = service?.plan;
      body = `<p>${up.ok ? H().statusPill("ok") + " Ready to launch (conditional)" : `<span class="field-error" role="alert">up would refuse: ${esc(up.refused || "")}</span>`}</p><p class="hint">${esc(up.conditions || "")}</p>`
        + (res.preview ? `<div class="banner">${esc(res.preview.label)}</div>` : "")
        + (service ? `<p>RTX prefill service: ${service.start ? `starts at VRAM cap <b>${esc(service.vram_cap_gib)}</b> GiB, host <b>${esc(service.host_gib)}</b> GiB` : "not started · " + esc(service.why || "")}</p>` : "")
        + (plan ? `<details open><summary>Planner (vramplan)</summary>${H().table(["Item", "Value"], Object.entries(plan).filter(([k, v]) => typeof v !== "object").map(([k, v]) => H().cells([esc(k.replaceAll("_", " ")), esc(v)])).join(""), "Placement plan")}<pre class="raw">${esc(JSON.stringify(plan, null, 1))}</pre></details>` : "")
        + (res.route_table ? `<details><summary>Router route table</summary><pre class="raw">${esc(JSON.stringify(res.route_table, null, 1))}</pre></details>` : "")
        + `<details><summary>Checked when up starts</summary><ul>${(res.checked_at_start || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>`
        + `<p class="hint">Snapshot <span class="mono">${esc((res.inputs || "").slice(0, 16))}</span> · deploy launches exactly this snapshot (recipe, layers, configuration, profile).</p>`;
    }
    const stale = p && R.planKey !== planKey();
    const planReason = reason || (invalid() ? "Fix the invalid fields first" : "");
    return H().card("Placement preview", `<div class="row"><button type="button" class="btn pri" data-recipe-action="plan" ${planReason || busy("plan") ? `disabled title="${esc(planReason || "planning")}"` : ""}>Preview placement on ${esc(R.profile || "…")}</button>${busy("plan") ? '<span class="hint">planning…</span>' : ""}</div>${stale ? '<div class="banner warn">The fields or layers changed since this preview: preview again before deploying.</div>' : ""}${refusal(p && p.exit_code !== 1 ? p : null)}${body}${p ? cli(p) : ""}`, "what up would decide before any lease");
  }
  function sourceCard() {
    const row = current();
    if (!R.detail) return "";
    if (!row?.writable) return H().card("Source", `<p class="hint">${esc(row?.source === "builtin" || row?.status === "pending" ? "Built-in example: read-only. Save as my recipe to make an editable copy." : "Read-only here.")}</p><details><summary>${esc(R.detail.path)}</summary><pre class="raw">${esc(R.detail.text)}</pre></details>`);
    return H().card("Source", `<div class="field"><label for="recipe-source">${esc(R.detail.path)}</label><textarea class="inp mono" id="recipe-source" rows="16" spellcheck="false" aria-describedby="recipe-source-result">${esc(R.source ?? "")}</textarea></div><div class="row"><button type="button" class="btn" data-recipe-action="check-source">Check</button><button type="button" class="btn pri" data-recipe-action="save-source" ${R.source === R.detail.text ? 'disabled title="No changes"' : ""}>Save</button><button type="button" class="btn dan" data-recipe-action="delete">Delete recipe…</button></div><div id="recipe-source-result">${R.sourceCheck ? (R.sourceCheck.ok ? `<p>${H().statusPill("ok")} valid</p>` : refusal(R.sourceCheck)) + cli(R.sourceCheck) : ""}</div>`, "your recipe (user directory)");
  }
  function compareCard() {
    const others = (R.catalog?.recipes || []).filter(r => !r.error && r.id !== R.selected);
    const d = R.diff?.result;
    return H().card("Compare", `<div class="row"><div class="field"><label for="recipe-compare">With</label><select class="inp" id="recipe-compare"><option value="">choose a recipe</option>${others.map(r => `<option value="${esc(r.id)}"${r.id === R.compare ? " selected" : ""}>${esc(r.id)}</option>`).join("")}</select></div><button type="button" class="btn" data-recipe-action="diff" ${R.compare ? "" : 'disabled title="Choose a recipe"'}>Compare</button></div>${R.diff ? refusal(R.diff) : ""}${d ? (d.same ? "<p>Same effective recipe.</p>" : H().table(["Path", R.selected, R.compare], d.fields.map(f => H().cells([`<span class="mono">${esc(f.path)}</span>`, esc(shown(f.a)), esc(shown(f.b))])).join(""), "Differences")) + cli(R.diff) : ""}`, "field-level difference of the effective recipes");
  }
  function actions() {
    const row = current();
    const deployReason = !R.profile ? "No deployment profile serves this model" : row?.status === "pending" ? `pending: ${row.blocker || "see its header"}` : invalid() ? "Fix the invalid fields first" : Object.keys(R.edits).length ? "Deploy launches only a reviewed snapshot: save the field changes as a layer (Layers), keep it selected, and preview again" : !R.plan ? "Preview the placement first" : R.planKey !== planKey() ? "Preview again: fields or layers changed" : !R.plan.result?.up?.ok ? "The preview refuses: see Placement preview" : H().mutationReason();
    return `<div class="row recipe-actions"><button type="button" class="btn pri" data-recipe-action="deploy" ${deployReason ? `disabled title="${esc(deployReason)}"` : ""}>Deploy…</button><button type="button" class="btn" data-recipe-action="save-copy">Save as my recipe…</button>${deployReason ? `<span class="hint">${esc(deployReason)}</span>` : ""}</div>`;
  }
  function detail() {
    if (!R.selected) return `<p class="hint">Open a recipe profile to see its trade-offs, edit its fields, check it and deploy it.</p>`;
    if (R.detailError) return H().empty("Recipe unavailable", R.detailError);
    if (!R.detail) return `<div class="card skeleton" aria-label="Loading recipe"></div>`;
    const row = current() || {};
    return `<h2 class="section">${esc(R.selected)} <span class="sub">${esc(R.detail.name)} · sha256 ${esc((R.detail.sha256 || "").slice(0, 12))}</span></h2>${actions()}${H().card("Trade-offs", tradeoffs(row), "quality, speed and memory as the recipe states them")}<div class="grid g2">${hardwareCard()}${previewCard()}</div>${fieldsCard()}${layersCard()}${sourceCard()}${compareCard()}`;
  }
  // --- get started: model -> hardware class -> goal -> readiness. Reads recipes/matrix and recipes/ready (offline
  // answers) and the page's own status; it proposes commands and opens the workbench, it never launches anything.
  const GOALS = {any: ["any goal", []], short: ["first token on short prompts", [["ttft_1k_s", 1]]],
    long: ["first token on long prompts", [["ttft_32k_s", 1], ["ttft_8k_s", 1]]], decode: ["decode speed", [["decode_tok_s", -1]]]};
  const STATE_PILL = {ok: "ok", warn: "warn", todo: "info", blocked: "err", skipped: "off"};
  const pill = state => `<span class="pill p-${STATE_PILL[state] || "off"}">${esc(state)}</span>`;
  const readyRow = id => (R.matrix?.cells || []).flatMap(c => c.recipes).find(r => r.id === id) || {profiles: []};
  function score(recipe) {
    // Only measured figures rank; a recipe without one for the goal goes after those that have it.
    for (const [key, sign] of GOALS[R.start.goal][1]) {
      const figure = recipe.figures.find(f => f.key === key && f.measured && typeof f.value === "number");
      if (figure) return sign * figure.value;
    }
    return Infinity;
  }
  function figureTag(f) {
    const prompt = /^ttft_(\d+k)_s$/.exec(f.key || "");
    const text = `${f.label}${prompt ? " " + prompt[1].toUpperCase() : ""} ${f.value} ${f.unit}`;
    const sources = (f.sources || []).map(s => s.ref || `${s.id}: no source`).join("; ");
    const about = f.measured ? "measured · " + sources : (f.basis || "unmeasured") + (sources ? " · " + sources : "");
    return `<span class="tag basis-${esc(f.measured ? "measured" : f.basis || "none")}" title="${esc(about)}">${f.measured ? "" : "estimate · "}${esc(text)}${f.measured ? "" : ` · ${esc(f.basis || "unmeasured")}`}</span>`;
  }
  function readyBlock() {
    const S = R.start, doc = S.ready;
    if (!S.recipe) return "";
    const profiles = readyRow(S.recipe).profiles || [];
    const head = `<h3 class="section">${esc(S.recipe)}${S.profile ? " on " + esc(S.profile) : ""}</h3>` + (profiles.length > 1 ? `<div class="field"><label for="recipe-start-profile">Profile</label><select class="inp" id="recipe-start-profile">${profiles.map(p => `<option value="${esc(p)}"${p === S.profile ? " selected" : ""}>${esc(p)}</option>`).join("")}</select></div>` : "");
    if (S.loading) return head + '<p class="hint">checking…</p>';
    if (S.error) return head + H().unavailable(S.error);
    if (!doc) return head;
    const r = doc.result;
    if (!r) return head + refusal(doc);
    const copy = text => text ? `<div class="row"><code class="mono">${esc(text)}</code><button type="button" class="btn sm" data-recipe-action="copy" data-text="${esc(text)}">Copy</button></div>` : "";
    const steps = r.steps.map(s => H().cells([pill(s.state), esc(s.title), `${esc(s.detail || "")}${s.state !== "ok" ? copy(s.fix) : ""}`])).join("");
    const running = (R.active?.deployments || []).filter(d => d.profile === r.profile);
    const live = running.length ? `<div class="banner info">${esc(r.profile)} is running now: deploying replaces it (the workbench asks first).</div>` : "";
    const next = r.ready
      ? `<p>${H().statusPill("ready")} Next: preview what up decides, then deploy (both in the workbench, through the GPU turn and lease).</p>${copy(r.commands.plan)}${copy(r.commands.up)}<div class="row"><button type="button" class="btn pri" data-recipe-action="start-open" data-id="${esc(r.recipe)}" data-profile="${esc(r.profile || "")}">Open in the workbench</button></div>`
      : `<p>${H().statusPill(r.next ? "warn" : "off")} Not ready${r.next ? `: next step <b>${esc(r.next.step)}</b>` : ""}.</p>`;
    return head + live + next + H().table(["State", "Step", "Detail"], steps, "Readiness") + `<p class="hint">Checked only when up starts: ${esc(r.checked_at_start.join("; "))}.</p>` + cli(doc);
  }
  function recipeTile(r) {
    const status = r.command ? H().tag(r.status) : `<span class="tag basis-target" title="${esc(r.blocker || "")}">pending</span>`;
    // Measured figures and estimates are shown apart; only measured ones rank (score).
    const numeric = r.figures.filter(f => typeof f.value === "number");
    const measured = numeric.filter(f => f.measured).map(figureTag).join("");
    const estimates = numeric.filter(f => !f.measured).map(figureTag).join("");
    const figures = (measured ? `<span class="hint">Measured</span>${measured}` : '<span class="hint">no measured figures</span>') +
      (estimates ? `</div><div class="chips"><span class="hint">Estimates</span>${estimates}` : "");
    const check = r.command ? "" : `disabled title="${esc("pending: " + (r.blocker || "it does not launch"))}"`;
    return `<article class="card recipe-card${r.id === R.start.recipe ? " sel" : ""}"><div class="row between"><b>${esc(r.id)}</b>${status}</div>${r.description ? `<p class="hint">${esc(r.description)}</p>` : ""}<div class="chips">${figures}</div>${r.blocker ? `<p class="hint">Pending: ${esc(r.blocker)}</p>` : ""}<div class="row"><button type="button" class="btn sm pri" data-recipe-action="start-ready" data-id="${esc(r.id)}" ${check}>Check readiness</button><button type="button" class="btn sm" data-recipe-action="open" data-id="${esc(r.id)}">Open</button></div></article>`;
  }
  function startCard() {
    const M = R.matrix, S = R.start;
    const about = "which recipe runs this model on your hardware, and what is still missing";
    if (!M) return H().card("Get started", R.busy["matrix:error"] ? H().unavailable(R.busy["matrix:error"]) : '<div class="skeleton" aria-label="Loading"></div>', about);
    const labels = Object.fromEntries(M.hardware.map(h => [h.class, h.label]));
    const cells = M.cells.filter(c => c.model === R.model);
    const chips = cells.map(c => {
      const runnable = c.recipes.filter(r => r.command).length;
      const why = c.recipes.length ? "" : `disabled title="${esc(`no recipe for ${R.model} on ${labels[c.class]} yet`)}"`;
      return `<button type="button" class="btn sm${S.cls === c.class ? " pri" : ""}" aria-pressed="${S.cls === c.class}" data-recipe-action="start-class" data-class="${esc(c.class)}" ${why}>${esc(labels[c.class])} · ${runnable}/${c.recipes.length}</button>`;
    }).join("");
    const cell = cells.find(c => c.class === S.cls);
    const sorted = cell ? [...cell.recipes].sort((a, b) => (!a.command - !b.command) || (score(a) - score(b)) || a.id.localeCompare(b.id)) : [];
    const goal = `<div class="field"><label for="recipe-goal">Goal</label><select class="inp" id="recipe-goal">${Object.entries(GOALS).map(([k, [label]]) => `<option value="${k}"${k === S.goal ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></div>`;
    return H().card("Get started", `<p class="hint">Model (above), then hardware, goal and readiness. Runnable / all recipes per hardware class. Figures marked measured name their sources (hover); the others show their basis. Nothing here launches: the commands and the workbench do, through the GPU turn and lease.</p><div class="chips" role="group" aria-label="Hardware">${chips}</div>${cell ? `<div class="row">${goal}</div><div class="grid g3 recipe-cards">${sorted.map(recipeTile).join("")}</div>` : ""}${readyBlock()}`, about);
  }
  async function checkReady(id, profile) {
    // Only the newest request may set the answer, its error or the loading state (a slower older one is dropped).
    const profiles = readyRow(id).profiles || [];
    const generation = ++R.start.generation;
    Object.assign(R.start, {recipe: id, ready: null, error: null, loading: true,
      profile: profile ?? (profiles.includes(R.start.profile) ? R.start.profile : profiles[0] || "")});
    rerender();
    const current = () => generation === R.start.generation;
    try {
      const doc = await post("recipes/ready", {recipe: id, ...(R.start.profile ? {profile: R.start.profile} : {})}, HARDWARE_S);
      if (current()) R.start.ready = doc;
    } catch (error) {
      if (current()) R.start.error = error.message;
    } finally {
      if (current()) { R.start.loading = false; rerender(); }
    }
  }

  function render() {
    return header() + startCard() + activeCard() + H().card("Profiles of " + (R.model || "…"), profileCards(), "pick one; built-in ones are read-only examples") + detail();
  }

  // --- dialog ---------------------------------------------------------------------------------------------------
  let dialogState = null;
  function dialog({title, fields = "", effects = "", confirm, destructive = false, submit}) {
    let element = $("recipe-dialog");
    if (!element) {
      element = document.createElement("dialog");
      element.id = "recipe-dialog";
      element.setAttribute("aria-labelledby", "recipe-dialog-title");
      element.innerHTML = `<form id="recipe-dialog-form"><div class="row between"><h2 id="recipe-dialog-title"></h2><button type="button" class="btn" data-recipe-close aria-label="Close dialog">Close</button></div><div id="recipe-dialog-fields"></div><div id="recipe-dialog-effects"></div><p id="recipe-dialog-error" class="field-error" aria-live="polite"></p><div class="row dialog-actions"><button type="button" class="btn" data-recipe-close>Cancel</button><button id="recipe-dialog-confirm" type="submit" class="btn pri"></button></div></form>`;
      document.body.append(element);
      element.addEventListener("cancel", event => { event.preventDefault(); closeDialog(); });
      element.addEventListener("click", event => { if (event.target.closest("[data-recipe-close]")) closeDialog(); });
      element.addEventListener("keydown", event => {
        if (event.key === "Enter" && dialogState?.destructive && event.target.tagName !== "TEXTAREA") { event.preventDefault(); return; }
        if (event.key !== "Tab") return;
        const focusable = [...element.querySelectorAll("button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary")];
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      });
      $("recipe-dialog-form").addEventListener("submit", async event => {
        event.preventDefault();
        const state = dialogState;
        if (!state) return;
        $("recipe-dialog-error").textContent = ""; $("recipe-dialog-confirm").disabled = true;
        try { await state.submit(element); closeDialog(); }
        catch (error) { $("recipe-dialog-error").textContent = error.message; }
        finally { if ($("recipe-dialog-confirm")) $("recipe-dialog-confirm").disabled = false; }
      });
    }
    dialogState = {submit, destructive, returnFocus: document.activeElement};
    $("recipe-dialog-title").textContent = title;
    $("recipe-dialog-fields").innerHTML = fields;
    $("recipe-dialog-effects").innerHTML = effects;
    $("recipe-dialog-error").textContent = "";
    $("recipe-dialog-confirm").textContent = confirm;
    $("recipe-dialog-confirm").className = `btn ${destructive ? "dan" : "pri"}`;
    element.showModal();
    (element.querySelector("input,textarea,select") || $("recipe-dialog-confirm")).focus();
  }
  function closeDialog() {
    const element = $("recipe-dialog");
    if (element?.open) element.close();
    const back = dialogState?.returnFocus;
    dialogState = null;
    if (back && document.contains(back)) back.focus();
  }
  const value = (element, id) => element.querySelector("#" + id).value.trim();
  const tomlSet = () => Object.entries(R.edits).map(([path, v]) => `${path}=${JSON.stringify(v)}`);
  function ensureOk(doc) { if (!doc.ok) throw new Error(doc.error || "refused"); return doc; }

  // --- actions ----------------------------------------------------------------------------------------------------
  const handlers = {
    open: control => select(control.dataset.id),
    "start-class": control => { R.start.generation++; Object.assign(R.start, {cls: control.dataset.class, recipe: "", ready: null, loading: false, error: null}); rerender(); },
    "start-ready": control => checkReady(control.dataset.id),
    "start-open": control => { if (control.dataset.profile) R.profile = control.dataset.profile; select(control.dataset.id); },
    copy: control => { navigator.clipboard?.writeText(control.dataset.text).then(() => H().announce("Copied"), () => H().announce("Copy failed: select the text instead")); },
    "reset-edits": () => { R.edits = {}; R.raw = {}; R.fieldErrors = {}; R.parseErrors = {}; R.editsError = null; R.explain = null; rerender(); },
    check: () => run("check", async () => { R.check = await post("recipes/validate", {recipe: R.selected, profile: R.profile, ...layerArgs(), ...editArgs()}, HARDWARE_S); }),
    simulate: () => run("simulate", async () => { R.simulate = await post("recipes/explain", {recipe: R.selected, profile: R.profile, prompt_tokens: Number(R.tokens) || 0, ...layerArgs(), ...editArgs()}, HARDWARE_S); }),
    plan: () => run("plan", async () => { const key = planKey(); R.plan = await post("recipes/plan", {recipe: R.selected, profile: R.profile, ...layerArgs(), ...editArgs()}, HARDWARE_S); R.planKey = key; }),
    diff: () => run("diff", async () => { R.diff = await post("recipes/diff", {a: R.selected, b: R.compare}); }),
    "check-source": () => run("source", async () => { R.sourceCheck = await post("recipes/check", {text: R.source}); }),
    "save-source": () => run("source", async () => {
      R.sourceCheck = await post("recipes/save", {id: R.selected, text: R.source, replace: R.detail.sha256});
      if (R.sourceCheck.ok) { const id = R.selected; R.selected = ""; await load(true); await select(id); }
    }),
    delete: () => dialog({title: `Delete ${R.selected}`, effects: `<p>Removes <span class="mono">${esc(R.detail.path)}</span> from your recipes. Running deployments keep their snapshot.</p>`, confirm: "Delete recipe", destructive: true,
      submit: async () => { ensureOk(await post("recipes/delete", {id: R.selected, sha256: R.detail.sha256})); R.selected = ""; R.detail = null; await load(true); }}),
    "save-copy": () => dialog({title: `Save ${R.selected} as my recipe`,
      fields: `<div class="field"><label for="copy-id">Recipe id (file name)</label><input class="inp mono" id="copy-id" value="${esc(R.model)}.mine" aria-describedby="recipe-dialog-error"></div><div class="field"><label for="copy-name">Recipe name</label><input class="inp mono" id="copy-name" value="${esc(R.model)}-mine"></div>`,
      effects: `<p>Writes a JSON copy of ${esc(R.selected)} with ${Object.keys(R.edits).length} changed field(s) into your recipe directory. Its predictions stay labelled as the base's (not re-measured); the base's tuning profiles do not apply to it.</p>`, confirm: "Save copy",
      submit: async element => {
        const id = value(element, "copy-id"), name = value(element, "copy-name");
        ensureOk(await post("recipes/save", {id, name, base: R.selected, base_sha256: R.detail.sha256, ...(Object.keys(R.edits).length ? {set: tomlSet()} : {})}));
        await load(true); await select(id);
      }}),
    "save-layer": () => dialog({title: "Save field changes as a layer",
      fields: `<div class="field"><label for="layer-name">Layer name</label><input class="inp mono" id="layer-name" value="${esc(R.selected)}.edits"></div>`,
      effects: `<p>Writes an overrides layer (${Object.keys(R.edits).length} values) for ${esc(R.detail.name)} into the user layers directory; select it under Layers to use it.</p>`, confirm: "Save layer",
      submit: async element => { const name = value(element, "layer-name"); ensureOk(await post("recipes/save-layer", {name, recipe: R.selected, set: tomlSet()})); await load(true); R.overrides = [...R.overrides, name + ".json"]; R.edits = {}; R.raw = {}; rerender(); }}),
    "add-layer": () => dialog({title: "Add a layer document",
      fields: `<div class="field"><label for="layer-add-name">Layer name</label><input class="inp mono" id="layer-add-name"></div><div class="field"><label for="layer-add-text">Overrides or tuning-profile document (TOML or JSON)</label><textarea class="inp mono" id="layer-add-text" rows="10"></textarea></div>`,
      effects: `<p>Checked against its recipe before it is written (schema, recipe name and sha256).</p>`, confirm: "Add layer",
      submit: async element => { ensureOk(await post("recipes/save-layer", {name: value(element, "layer-add-name"), text: element.querySelector("#layer-add-text").value})); await load(true); }}),
    "delete-layer": control => dialog({title: `Delete layer ${control.dataset.name}`, confirm: "Delete layer", destructive: true, effects: "<p>Removes the layer file from the user layers directory.</p>",
      submit: async () => { ensureOk(await post("recipes/delete-layer", {name: control.dataset.name, sha256: control.dataset.sha})); R.overrides = R.overrides.filter(n => n !== control.dataset.name); await load(true); }}),
    "add-receipt": () => dialog({title: "Add a disk receipt",
      fields: `<div class="field"><label for="receipt-name">Receipt name</label><input class="inp mono" id="receipt-name"></div><div class="field"><label for="receipt-text">Receipt JSON (disk_read_gbytes_ps, method "direct", host, path)</label><textarea class="inp mono" id="receipt-text" rows="6"></textarea></div>`, confirm: "Store receipt",
      submit: async element => { ensureOk(await post("recipes/save-receipt", {name: value(element, "receipt-name"), text: element.querySelector("#receipt-text").value})); await load(true); }}),
    probe: () => H().mutate("recipes/probe", {profile: R.profile}).catch(error => H().announce(error.message)),
    "probe-descriptors": () => dialog({title: "Read the model's file headers", effects: `<p>Reads the header of ${esc(R.profile)}'s model file on this machine (not the weights) and saves it for planning. Runs as a job.</p>`, confirm: "Read headers",
      submit: async () => { await H().mutate("recipes/probe", {profile: R.profile, descriptors: true}); }}),
    "use-receipt": control => H().mutate("recipes/probe", {profile: R.profile, evidence: [`${control.dataset.host}=${control.dataset.name}`]}).catch(error => H().announce(error.message)),
    deploy: () => {
      const running = (R.active?.deployments || []).map(d => d.profile);
      const res = R.plan.result;
      dialog({title: `${running.length ? "Switch to" : "Deploy"} ${R.selected} on ${R.profile}`, destructive: running.length > 0,
        effects: `<ul>${running.map(p => `<li>Stops <b>${esc(p)}</b> first.</li>`).join("")}<li>Starts <b>${esc(R.profile)}</b> with recipe <b>${esc(R.selected)}</b>${R.tuning ? ` + tuning ${esc(R.tuning)}` : ""}${R.overrides.length ? ` + layers ${esc(R.overrides.join(", "))}` : ""}${Object.keys(R.edits).length ? ` ` : ""}.</li><li>RTX prefill service: ${res.service?.start ? "starts" : "not started · " + esc(res.service?.why || "")}.</li><li>Launches exactly the reviewed snapshot; if anything differs (running set, files, configuration) or a check refuses, nothing is stopped.</li></ul><p class="hint">Checked when it starts: ${esc((res.checked_at_start || []).join("; "))}.</p>`,
        confirm: running.length ? "Stop and deploy" : "Deploy",
        submit: async () => {
          // Exactly the snapshot this dialog describes; anything else needs its own preview and review.
          if (R.planKey !== planKey()) throw new Error("The fields or layers changed since the preview: preview again.");
          const digest = res.inputs;
          await H().mutate("recipes/deploy", {inputs: digest, stop: running});
        }});
    },
  };

  document.addEventListener("click", event => {
    const control = event.target.closest("[data-recipe-action]");
    if (!control || control.disabled) return;
    const handler = handlers[control.dataset.recipeAction];
    if (handler) handler(control);
  });
  document.addEventListener("change", event => {
    const t = event.target;
    if (t.id === "recipe-model") { R.model = t.value; R.profile = (R.catalog.models[R.model] || [])[0] || ""; R.selected = ""; R.detail = null; R.start.generation++; Object.assign(R.start, {cls: "", recipe: "", ready: null, loading: false, error: null}); rerender(); }
    else if (t.id === "recipe-goal") { R.start.goal = t.value; rerender(); }
    else if (t.id === "recipe-start-profile") checkReady(R.start.recipe, t.value);
    else if (t.id === "recipe-profile") { R.profile = t.value; R.check = R.simulate = null; rerender(); }
    else if (t.id === "recipe-tuning") { R.tuning = t.value; scheduleValidate(); rerender(); }
    else if (t.id === "recipe-compare") { R.compare = t.value; R.diff = null; rerender(); }
    else if (t.dataset?.layer) { R.overrides = t.checked ? [...R.overrides, t.dataset.layer] : R.overrides.filter(n => n !== t.dataset.layer); scheduleValidate(); rerender(); }
    else if (t.dataset?.path && t.tagName === "SELECT") editField(t);
  });
  document.addEventListener("input", event => {
    const t = event.target;
    if (t.id === "recipe-tokens") R.tokens = t.value;
    else if (t.id === "recipe-source") { R.source = t.value; R.sourceCheck = null; }
    else if (t.dataset?.path) editField(t);
  });
  function editField(input) {
    const field = R.detail.fields.find(f => f.path === input.dataset.path);
    R.raw[field.path] = input.value;
    const parsed = parse(field, input.value);
    delete R.fieldErrors[field.path];
    delete R.parseErrors[field.path];
    // A value that does not parse stays an error (and blocks preview and deploy) until it is fixed or reset.
    if (parsed.error) { R.parseErrors[field.path] = parsed.error; delete R.edits[field.path]; }
    else if (JSON.stringify(parsed.value) === JSON.stringify(field.value)) delete R.edits[field.path];
    else R.edits[field.path] = parsed.value;
    if (!Object.hasOwn(R.edits, field.path) && !parsed.error) delete R.raw[field.path];
    scheduleValidate();
    rerender();
  }

  window.HyperloomRecipes = {
    render, ready: () => !!R.catalog, error: () => R.catalog ? null : R.error, refresh: () => load(true),
  };
  addEventListener("hashchange", () => { if (location.hash.includes("recipes")) poll(); });
  setInterval(poll, 2000);
  poll();
})();
