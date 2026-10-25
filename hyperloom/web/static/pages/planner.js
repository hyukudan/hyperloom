/* Recipe planner (F33 what-if, F34 placement preview). Every run is a command line the portal executes
   (`hyperloom whatif ...`, `hyperloom placement ...`); answers carry it. Nothing here deploys anything. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const POST_S = 170;
  const V = {view: "whatif", catalog: null, catalogError: null, catalogAt: 0, shown: {}, shownError: {},
    recipe: "", profile: "", facts: "", fields: [{path: "", values: ""}], tokens: "", busy: false, error: null, answer: null,
    save: {}, p: {model: "", facts: "", prompt: 8192, output: 256, slots: 1, objective: "latency", top_k: 5,
      busy: false, error: null, answer: null}};
  try { V.view = localStorage.getItem("hyperloom-planner-view") === "placement" ? "placement" : "whatif"; } catch (_) { /* storage may be disabled */ }
  const esc = value => H().esc(value);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const doc = () => H().state.docs.planner || {};
  const rerender = () => H().render();
  const num = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? Number(value.toFixed(digits)).toLocaleString() : null;
  const gib = (value, why) => num(value) == null ? H().unavailable(why || "not reported") : `${num(value)} GiB`;
  const bytesGib = (value, why) => typeof value === "number" ? gib(value / 1073741824) : H().unavailable(why || "not reported");
  const pageButton = (label, action, {disabled = "", style = "", extra = ""} = {}) =>
    H().button(label, "", {disabled, style, extra: `data-page-action="${esc(action)}" ${extra}`});

  async function post(endpoint, args) {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    const response = await fetch("/hyperloom/api/" + endpoint, {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(args), signal: AbortSignal.timeout(POST_S * 1000)});
    const body = await response.json();
    if (!response.ok && body.exit_code === undefined) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  async function get(endpoint) {
    const response = await fetch("/hyperloom/api/" + endpoint, {cache: "no-store", credentials: "same-origin"});
    const body = await response.json();
    if (!response.ok && body.exit_code === undefined) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  async function loadCatalog(force = false) {
    if (!force && (V.catalog || V.catalogError) && Date.now() - V.catalogAt < 10000) return;
    V.catalogAt = Date.now();
    try {
      const listing = await get("recipes");
      if (!listing.ok) throw new Error(listing.error || "recipe list unavailable");
      V.catalog = listing.result; V.catalogError = null;
    } catch (error) { V.catalogError = error.message; }
    defaults(); rerender();
  }
  async function loadShown(id) {
    if (!id || V.shown[id] || V.shownError[id] === "loading") return;
    V.shownError[id] = "loading";
    try {
      const answer = await get("recipes/" + encodeURIComponent(id));
      if (!answer.ok) throw new Error(answer.error || "recipe unavailable");
      V.shown[id] = answer.result; delete V.shownError[id];
    } catch (error) { V.shownError[id] = error.message; }
    rerender();
  }

  // --- what-if -------------------------------------------------------------------------------------------------------
  const recipes = () => (V.catalog?.recipes || []).filter(r => !r.error && r.status !== "pending" && r.model);
  const profilesOf = model => (doc().profiles || []).filter(p => p.model === model).map(p => p.id);
  const factsOf = profiles => (doc().facts || []).filter(f => profiles.includes(f.profile));
  const currentRecipe = () => recipes().find(r => r.id === V.recipe);
  function defaults() {
    const usable = recipes().filter(r => profilesOf(r.model).length);
    if (!recipes().some(r => r.id === V.recipe)) V.recipe = (usable[0] || recipes()[0] || {}).id || "";
    const serving = profilesOf(currentRecipe()?.model);
    if (!serving.includes(V.profile)) V.profile = serving[0] || "";
    if (V.facts && !factsOf([V.profile]).some(f => f.name === V.facts)) V.facts = "";
    const models = Object.entries(doc().placement?.models || {});
    if (!models.some(([m]) => m === V.p.model)) V.p.model = (models.find(([, s]) => s.available) || models[0] || [""])[0];
    if (V.p.facts && !factsOf(profilesOf(V.p.model)).some(f => f.name === V.p.facts)) V.p.facts = "";
  }
  function splitValues(text) {
    const out = []; let depth = 0, quote = null, current = "";
    for (const char of text) {
      if (quote) { current += char; if (char === quote) quote = null; continue; }
      if (char === '"' || char === "'") quote = char;
      else if (char === "[" || char === "{") depth++;
      else if (char === "]" || char === "}") depth = Math.max(0, depth - 1);
      else if (char === "," && !depth) { out.push(current.trim()); current = ""; continue; }
      current += char;
    }
    out.push(current.trim());
    return out.filter(v => v !== "");
  }
  function parseValue(text) {
    try { return JSON.parse(text); } catch (_) { return text.replace(/^'(.*)'$/, "$1"); }
  }
  function vary() {
    const out = {};
    for (const row of V.fields) if (row.path && splitValues(row.values).length) out[row.path] = splitValues(row.values).map(parseValue);
    return out;
  }
  function variantCount() {
    const lists = Object.values(vary());
    return lists.length ? 1 + lists.reduce((n, l) => n * l.length, 1) : 1;
  }
  function whatifBlocker() {
    const limit = doc().limits?.variants || 12;
    if (V.catalogError) return `recipe catalog unavailable: ${V.catalogError}`;
    if (!V.recipe) return "no runnable recipe in the catalog";
    if (!V.profile) return `no configured deployment profile serves ${currentRecipe()?.model || "this model"}`;
    if (!factsOf([V.profile]).length) return `no saved hardware facts for profile ${V.profile}: run a probe with an output name on the Recipes page`;
    if (new Set(V.fields.filter(r => r.path).map(r => r.path)).size !== V.fields.filter(r => r.path).length) return "a field is listed twice";
    if (variantCount() > limit) return `${variantCount()} variants (the base included); at most ${limit}`;
    if (V.tokens !== "" && !(Number.isInteger(Number(V.tokens)) && Number(V.tokens) >= 1)) return "prompt tokens must be a positive whole number";
    if (V.busy) return "running";
    return "";
  }
  function fieldOptions(selectedPath) {
    const shown = V.shown[V.recipe];
    if (!shown) return `<option value="">${esc(V.shownError[V.recipe] && V.shownError[V.recipe] !== "loading" ? "fields unavailable" : "loading fields…")}</option>`;
    const groups = {};
    for (const f of shown.fields || []) (groups[f.group || "fields"] ||= []).push(f);
    return `<option value="">choose a field</option>` + Object.entries(groups).map(([group, list]) => `<optgroup label="${esc(group)}">${list.map(f =>
      `<option value="${esc(f.path)}"${f.path === selectedPath ? " selected" : ""}>${esc(f.path.split(".").slice(-2).join("."))} = ${esc(JSON.stringify(f.value))}${f.pinned ? " (pinned)" : ""}</option>`).join("")}</optgroup>`).join("");
  }
  function fieldHint(path) {
    const f = (V.shown[V.recipe]?.fields || []).find(x => x.path === path);
    if (!f) return "Values separated by commas, e.g. 16384, 32768 · true, false.";
    const range = f.type === "enum" ? `one of ${(f.values || []).map(v => JSON.stringify(v)).join(", ")}` : f.min != null ? `${f.type} ${f.min}…${f.max}` : f.type;
    return `${f.path} · ${range} · base ${JSON.stringify(f.value)}${f.pinned ? " · pinned by the recipe (a change is refused)" : ""}`;
  }
  function whatifForm() {
    const rows = recipes(), serving = profilesOf(currentRecipe()?.model), facts = factsOf([V.profile]);
    const recipeSelect = rows.length ? `<select class="inp" id="planner-recipe" data-page-action="planner-input" data-key="recipe">${rows.map(r =>
      `<option value="${esc(r.id)}"${r.id === V.recipe ? " selected" : ""}>${esc(r.id)} · ${esc(r.source)}</option>`).join("")}</select>` :
      `<select class="inp" id="planner-recipe" disabled><option>${esc(V.catalogError ? "catalog unavailable" : "loading…")}</option></select>`;
    const profileSelect = serving.length ? `<select class="inp" id="planner-profile" data-page-action="planner-input" data-key="profile">${serving.map(p =>
      `<option value="${esc(p)}"${p === V.profile ? " selected" : ""}>${esc(p)}</option>`).join("")}</select>` :
      `<select class="inp" id="planner-profile" disabled><option>none serves this model</option></select>`;
    const factsSelect = `<select class="inp" id="planner-facts" data-page-action="planner-input" data-key="facts"${facts.length ? "" : " disabled"}>${facts.length
      ? `<option value="">last saved (${esc(facts[0].name)}, ${esc(when(facts[0].observed_at))})</option>` + facts.map(f => `<option value="${esc(f.name)}"${f.name === V.facts ? " selected" : ""}>${esc(f.name)} · ${esc(when(f.observed_at))}</option>`).join("")
      : "<option>no saved facts for this profile</option>"}</select>`;
    const fieldRows = V.fields.map((row, i) => `<div class="planner-vary" role="group" aria-label="Varied field ${i + 1}">
      <div class="field"><label for="planner-path-${i}">Field ${i + 1}</label><select class="inp" id="planner-path-${i}" data-page-action="planner-path" data-index="${i}">${fieldOptions(row.path)}</select></div>
      <div class="field"><label for="planner-values-${i}">Values</label><input class="inp" id="planner-values-${i}" data-page-action="planner-values" data-index="${i}" value="${esc(row.values)}" placeholder="v1, v2" aria-describedby="planner-hint-${i}" autocomplete="off"><span class="hint" id="planner-hint-${i}">${esc(fieldHint(row.path))}</span></div>
      ${pageButton("Remove", "planner-remove", {extra: `data-index="${i}" aria-label="Remove field ${i + 1}"`, style: "sm"})}</div>`).join("");
    const blocker = whatifBlocker(), limit = doc().limits?.variants || 12;
    return H().card("What to compare", `<div class="grid g3">
      <div class="field"><label for="planner-recipe">Recipe</label>${recipeSelect}</div>
      <div class="field"><label for="planner-profile">Deployment profile</label>${profileSelect}</div>
      <div class="field"><label for="planner-facts">Hardware facts</label>${factsSelect}<span class="hint">Saved observations, not live. A fresh probe is the Recipes page's probe job.</span></div></div>
      ${fieldRows}
      <div class="row">${pageButton("Add field", "planner-add", {disabled: V.fields.length >= 8 ? "at most 8 varied fields" : ""})}
        <div class="field planner-tokens"><label for="planner-tokens">Simulate a request of (prompt tokens, optional)</label><input class="inp" id="planner-tokens" type="number" min="1" step="1" data-page-action="planner-input" data-key="tokens" value="${esc(V.tokens)}"></div></div>
      <div class="row between"><span class="hint" aria-live="off">${esc(variantCount())} of at most ${esc(limit)} variants (the base and every combination of the values).</span>
      ${pageButton(V.busy ? "Running…" : "Run what-if", "planner-run", {style: "pri", disabled: blocker})}</div>
      ${V.error ? `<p class="field-error" role="alert">${esc(V.error)}</p>` : ""}`, "nothing is deployed");
  }
  function when(epoch) {
    if (typeof epoch !== "number") return "unrecorded time";
    const age = (Date.now() / 1000) - epoch;
    const ago = age < 3600 ? `${Math.max(0, Math.round(age / 60))} min ago` : age < 172800 ? `${Math.round(age / 3600)} h ago` : `${Math.round(age / 86400)} d ago`;
    return `${new Date(epoch * 1000).toLocaleString()} (${ago})`;
  }
  function settled(variant, path) {
    const f = variant.fields?.[path];
    if (!f || f.value === undefined || f.value === null && f.why) return H().unavailable(f?.why || "not reported");
    const applied = !variant.set?.[path] && variant.set?.[path] !== false && variant.set?.[path] !== 0 ? "" :
      f.layer === "override" ? "" : ` <span class="tag">not applied: ${esc(f.layer)}</span>`;
    return `${esc(JSON.stringify(f.value))} <span class="hint">${esc(f.layer || "")}</span>${applied}`;
  }
  const missing = value => object(value) && value.value === null && "why" in value;
  function routeCell(variant, id) {
    const r = (variant.routes || []).find(x => x.id === id);
    if (!r) return H().unavailable("not in this variant");
    const parts = [];
    if (r.enabled_in_recipe === false) parts.push(H().tag("disabled in the recipe"));
    if (missing(r.decision)) parts.push(H().unavailable(r.decision.why));
    else if (r.phase === "decode") parts.push(r.deployed ? H().statusPill("deployed") : H().tag("not chosen"));
    else if (r.admitted !== undefined) parts.push(r.admitted ? `${H().statusPill("ok")} ${esc(r.place)}` : `${H().statusPill("refused")} <span class="t2">${esc(r.why || "")}</span>`);
    if (r.phase === "prefill" && r.table) parts.push(missing(r.table) ? `<span class="hint">router table: ${esc(r.table.why)}</span>` : `<span class="hint">router table: ${r.table.enabled ? "enabled" : "disabled"}</span>`);
    return parts.join(" ") || H().unavailable("not reported");
  }
  function chargeCell(variant) {
    const charges = variant.resources?.charges;
    if (missing(charges)) return H().unavailable(charges.why);
    if (!Array.isArray(charges) || !charges.length) return H().unavailable("no charges reported");
    const totals = {};
    for (const c of charges) {
      const key = `${c.route} · ${c.id}`;
      totals[key] ||= {host: 0, disk: 0};
      totals[key].host += c.host_bytes || 0; totals[key].disk += c.disk_bytes || 0;
    }
    return Object.entries(totals).map(([k, b]) => `${esc(k)}: ${bytesGib(b.host)}${b.disk ? ` + disk ${bytesGib(b.disk)}` : ""}`).join("<br>") + `<br><span class="hint">${esc(variant.resources.charges_label || "")}</span>`;
  }
  function budgetCell(variant) {
    const budgets = variant.resources?.budgets || {};
    const rows = Object.entries(budgets).flatMap(([route, sets]) => Object.entries(sets).map(([set, b]) =>
      `${esc(route)} · ${esc(set)}: device ${b.device_gib == null ? "match minimum" : gib(b.device_gib)}, pinned ${gib(b.host_pinned_gib)}, context ${esc(num(b.max_context_tokens, 0) ?? "?")}, batch ${esc(num(b.max_batch, 0) ?? "?")}${b.tier_mode ? `, tier ${esc(b.tier_mode)}` : ""}`));
    return rows.length ? rows.join("<br>") : H().unavailable(variant.explain?.error || "explain did not report the placement fields");
  }
  function serviceCell(variant) {
    const s = variant.resources?.service;
    if (!s || missing(s)) return H().unavailable(s?.why || "not reported");
    const head = s.start ? H().statusPill("ok") + " starts" : `${H().tag("not started")} <span class="t2">${esc(s.why || "")}</span>`;
    const plan = s.plan ? `<br>resident ${gib(s.plan.resident_gib)} · pinned host ${gib(s.plan.pinned_host_gib)} · disk ${gib(s.plan.disk_gib)} · peak ${gib(s.plan.predicted_peak_gib)}` : "";
    return `${head}${plan}${s.label ? `<br><span class="hint">${esc(s.label)}</span>` : ""}`;
  }
  function floorCell(variant) {
    const f = variant.floors || {};
    const hosts = Object.entries(f.hosts || {}).map(([h, row]) => `${esc(h)} free ${gib(row?.free_ram_gib, "not in the facts")}${row?.free_commit_gib != null ? ` · commit ${gib(row.free_commit_gib)}` : ""}`);
    return `floor ${gib(f.floor_gib, "not reported")} · commit floor ${gib(f.commit_floor_gib, "not reported")}${hosts.length ? `<br>${hosts.join("<br>")}<br><span class="hint">${esc(f.hosts_label || "saved facts")}</span>` : ""}`;
  }
  function upCell(variant) {
    const up = variant.up || {};
    if (up.ok === true) return `${H().statusPill("ok")} <span class="hint">conditional: checked again at start</span>`;
    if (up.ok === false) return `${H().statusPill("refused")} <span class="t2">${esc(up.refused || "")}</span>`;
    return H().unavailable(variant.plan?.error || "plan did not answer");
  }
  function simulated(variant) {
    const s = variant.simulated_request;
    if (!s) return H().unavailable("no prompt length given");
    if (s.refused) return `${H().statusPill("refused")} <span class="t2">${esc(s.refused)}</span>`;
    const trace = (s.trace || []).map(x => `${esc(x.route)}: ${esc(x.verdict)}${x.why ? ` (${esc(x.why)})` : ""}`).join("<br>");
    return `${H().statusPill("ok")} ${esc(s.route || "?")}${s.handoff_route ? ` → ${esc(s.handoff_route)}` : ""}${trace ? `<br><span class="hint">${trace}</span>` : ""}`;
  }
  function whatifResults() {
    const answer = V.answer;
    if (!answer) return "";
    if (!answer.result) return H().card("What-if result", `<p class="field-error" role="alert">${esc(answer.error || "refused")}</p>${cli(answer)}`);
    const r = answer.result, variants = r.variants || [];
    const previous = V.stale ? `<p class="banner warn" role="note">Previous run: the last request failed (see above).</p>` : "";
    const routes = [...new Set(variants.flatMap(v => (v.routes || []).map(x => x.id)))];
    const paths = [...new Set(variants.flatMap(v => Object.keys(v.set || {})))];
    const head = ["", ...variants.map(v => v.name)];
    const line = (label, fn) => H().cells([`<b>${esc(label)}</b>`, ...variants.map(v => v.evaluated === false ? H().unavailable(v.why) : fn(v))]);
    const body = [
      line("Overrides", v => Object.keys(v.set || {}).length ? Object.entries(v.set).map(([p, x]) => `<code class="mono">${esc(p)}=${esc(JSON.stringify(x))}</code>`).join("<br>") : "none (base)"),
      ...paths.map(p => line(p, v => settled(v, p))),
      line("Hardware match", v => v.match == null ? H().unavailable(v.explain?.error || "not reported") : v.match.length ? `${H().statusPill("refused")} ` + v.match.map(m => `<span class="t2">${esc(m)}</span>`).join("<br>") : H().statusPill("ok")),
      line("Decision at up", v => v.decision ? `${H().statusPill("ok")} decode ${esc(v.decision.decode)}` : `${H().statusPill("refused")} <span class="t2">${esc(v.refused || v.explain?.error || "no decision")}</span>`),
      ...routes.map(id => line(`Route ${id}`, v => routeCell(v, id))),
      ...(r.prompt_tokens ? [line(`Request of ${r.prompt_tokens} tokens`, simulated)] : []),
      line("Budgets (recipe fields)", budgetCell),
      line("Host charges", chargeCell),
      line("RTX prefill service", serviceCell),
      line("Floors and saved free RAM", floorCell),
      line("up (plan)", upCell),
      line("Predictions", v => `<span class="tag">${esc(v.predictions?.label || "predicted")}</span>`),
      line("Command lines", v => v.cli ? `<details><summary>CLI</summary><pre class="raw">${esc(v.cli.explain)}\n${esc(v.cli.plan)}${v.layer ? `\n\n${esc(v.id)}.json:\n${esc(v.layer.text)}` : ""}</pre></details>` : H().unavailable("not run")),
      line("Save", v => Object.keys(v.set || {}).length ? saveControl(v) : "—"),
    ].join("");
    const facts = r.facts || {};
    return `${H().card("Variants side by side", `${previous}<p class="hint">Facts ${esc(facts.name)} of ${esc(facts.profile)}, observed ${esc(when(facts.observed_at))} (saved, not live). Recipe ${esc(r.recipe)} · ${esc(r.elapsed_s)} s.</p>${H().table(head, body, "What-if variants")}${cli(answer)}`, `${variants.length} variants`)}${predictions(r)}`;
  }
  function predictions(r) {
    const rows = r.predictions?.rows || [];
    if (!rows.length) return H().card("Predictions", `<p>${H().unavailable(r.predictions?.error || "the recipe has no predictions")}</p>`);
    const body = rows.map(p => H().cells([esc(p.route || "—"), esc(p.label || p.metric), `${esc(num(p.value, 3) ?? p.value)} ${esc(p.unit || "")}`,
      esc([p.at_prompt_tokens != null ? `prompt ${p.at_prompt_tokens}` : "", p.batch != null ? `batch ${p.batch}` : "",
        p.concurrency != null ? `concurrency ${p.concurrency}` : "", p.scope ? `scope: ${p.scope}` : ""].filter(Boolean).join(" · ") || "—"),
      `<span class="tag basis-${esc(p.basis || "")}">${esc(p.basis || "?")}</span>`,
      `<span class="hide-n">${esc(Array.isArray(p.evidence) ? p.evidence.join(", ") : p.evidence || "—")}</span>`])).join("");
    return H().card("Predictions", `<p class="hint">The recipe's own predicted rows${r.predictions.inherited ? " (a copy: its base's numbers)" : ""}. They apply to the base as written; a variant's column says "base recipe prediction, not re-measured". Nothing here is re-measured.</p>${H().table(["Route", "Metric", "Value", "Conditions and scope", "Basis", ["Evidence", "hide-n"]], body, "Predictions")}`, "predicted");
  }
  function saveControl(v) {
    const s = V.save[v.id] || {};
    if (!s.open) return pageButton("Save as layer…", "planner-save-open", {extra: `data-variant="${esc(v.id)}"`, style: "sm"});
    const id = `planner-layer-${esc(v.id)}`;
    const valid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(s.name || "");
    return `<div class="field"><label for="${id}">Layer name</label><input class="inp" id="${id}" data-page-action="planner-save-name" data-variant="${esc(v.id)}" value="${esc(s.name || "")}" aria-describedby="${id}-hint" autocomplete="off"><span class="hint" id="${id}-hint">Written to the user layers directory by recipes/save-layer (audited).</span></div>
      <div class="row">${pageButton(s.busy ? "Saving…" : "Save layer", "planner-save", {style: "pri sm", extra: `data-variant="${esc(v.id)}"`, disabled: s.busy ? "saving" : valid ? "" : "a name [A-Za-z0-9._-], up to 64 characters"})}${pageButton("Cancel", "planner-save-close", {style: "sm", extra: `data-variant="${esc(v.id)}"`})}</div>
      ${s.answer ? (s.answer.ok ? `<p>${H().statusPill("ok")} saved ${esc(s.answer.result?.name || "")}</p>` : `<p class="field-error" role="alert">${esc(s.answer.error || "refused")}</p>`) + cli(s.answer) : ""}${s.error ? `<p class="field-error" role="alert">${esc(s.error)}</p>` : ""}`;
  }
  function cli(answer) {
    return answer?.cli ? `<p class="hint cli">CLI: <code class="mono">${esc(answer.cli)}</code></p>` : "";
  }

  // --- placement -----------------------------------------------------------------------------------------------------
  function placementBlocker() {
    const pl = doc().placement || {};
    const state = (pl.models || {})[V.p.model];
    if (!pl.available) return pl.why || "the planner is unavailable";
    if (!V.p.model) return "no model";
    if (!state?.available) return state?.why || "no model parts for this model";
    if (!factsOf(profilesOf(V.p.model)).length && !V.p.facts) return `no saved hardware facts for a profile serving ${V.p.model}: run a probe with an output name on the Recipes page`;
    for (const [key, low, high] of [["prompt", 1, 1048576], ["output", 1, 65536], ["slots", 1, 64], ["top_k", 1, 10]]) {
      const value = Number(V.p[key]);
      if (!Number.isInteger(value) || value < low || value > high) return `${key.replace("_", "-")} must be a whole number in ${low}..${high}`;
    }
    if (V.p.busy) return "running";
    return "";
  }
  function placementForm() {
    const pl = doc().placement || {}, models = Object.entries(pl.models || {});
    const facts = factsOf(profilesOf(V.p.model));
    const modelSelect = `<select class="inp" id="planner-model" data-page-action="planner-p" data-key="model">${models.map(([m, s]) =>
      `<option value="${esc(m)}"${m === V.p.model ? " selected" : ""}>${esc(m)}${s.available ? "" : ` (unavailable · ${esc(s.why)})`}</option>`).join("")}</select>`;
    const factsSelect = `<select class="inp" id="planner-pfacts" data-page-action="planner-p" data-key="facts"${facts.length ? "" : " disabled"}>${facts.length
      ? `<option value="">newest (${esc(facts[0].name)} of ${esc(facts[0].profile)})</option>` + facts.map(f => `<option value="${esc(f.name)}"${f.name === V.p.facts ? " selected" : ""}>${esc(f.name)} · ${esc(f.profile)} · ${esc(when(f.observed_at))}</option>`).join("")
      : "<option>no saved facts for this model</option>"}</select>`;
    const number = (key, label, min, max) => `<div class="field"><label for="planner-p-${key}">${esc(label)}</label><input class="inp" id="planner-p-${key}" type="number" min="${min}" max="${max}" step="1" data-page-action="planner-p" data-key="${key}" value="${esc(V.p[key])}"></div>`;
    const objective = `<div class="field"><label for="planner-p-objective">Objective</label><select class="inp" id="planner-p-objective" data-page-action="planner-p" data-key="objective">${(pl.objectives || ["latency"]).map(o => `<option value="${esc(o)}"${o === V.p.objective ? " selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
    return H().card("Model and request", `<div class="grid g3"><div class="field"><label for="planner-model">Model</label>${modelSelect}</div><div class="field"><label for="planner-pfacts">Hardware facts</label>${factsSelect}<span class="hint">Saved observations, not live.</span></div>${objective}</div>
      <div class="grid g4">${number("prompt", "Prompt tokens", 1, 1048576)}${number("output", "Generated tokens", 1, 65536)}${number("slots", "Concurrent sequences", 1, 64)}${number("top_k", "Candidates per scenario", 1, 10)}</div>
      <div class="row between"><span class="hint">Three planner runs: nodes only, nodes + RTX prefill, all devices.</span>${pageButton(V.p.busy ? "Planning…" : "Preview placements", "planner-place", {style: "pri", disabled: placementBlocker()})}</div>
      ${V.p.error ? `<p class="field-error" role="alert">${esc(V.p.error)}</p>` : ""}`, pl.advisory || "advisory, not qualified");
  }
  function memoryCell(memory) {
    if (!object(memory) || !Object.keys(memory).length) return H().unavailable("not reported");
    return Object.entries(memory).map(([d, g]) => `${esc(d)} ${gib(g)}`).join("<br>");
  }
  const rate = (value, unit) => num(value, 1) == null ? H().unavailable("not reported") : `${num(value, 1)} ${unit}`;
  function candidateRows(list) {
    const memory = c => {
      const unreported = (c.devices || []).filter(d => !Object.hasOwn(c.memory_gib || {}, d));
      return memoryCell(c.memory_gib) + (c.replicas_label ? `<br><span class="hint">${esc(c.replicas_label)}</span>` : "") +
        (unreported.length ? `<br>${unreported.map(d => `${esc(d)} ${H().unavailable("not reported by the planner")}`).join("<br>")}` : "");
    };
    return list.filter(Boolean).map(c => H().cells([`<code class="mono">${esc(c.name)}</code>`, esc(c.mode || ""), memory(c),
      rate(c.decode_tok_s, "tok/s") + (c.aggregate_tok_s != null ? `<br><span class="hint">aggregate ${rate(c.aggregate_tok_s, "tok/s")}</span>` : ""),
      rate(c.prefill_tok_s, "tok/s"), num(c.ttft_s, 2) == null ? H().unavailable("not reported") : `${num(c.ttft_s, 2)} s`,
      `<span class="hide-n">${(c.uses_preemptible || []).map(d => H().tag(d)).join(" ") || "—"}</span>`])).join("");
  }
  function scenarioCard(s) {
    if (s.status === "unavailable" || s.status === "failed") {
      return H().card(s.label, `<p>${s.status === "failed" ? `<span class="field-error">${esc(s.error)}</span>` : H().unavailable(s.why)}</p>${s.cli ? `<p class="hint cli">CLI: <code class="mono">${esc(s.cli)}</code></p>` : ""}`, s.status);
    }
    const best = s.best;
    const headers = ["Candidate", "Mode", "Memory per device", "Decode", "Prefill", "TTFT", ["Preemptible", "hide-n"]];
    const head = best ? `<div class="chips"><span class="chip"><span class="lbl">best</span> ${esc(best.mode)}</span><span class="chip"><span class="lbl">decode</span> ${rate(best.decode_tok_s, "tok/s")}</span><span class="chip"><span class="lbl">TTFT</span> ${num(best.ttft_s, 2) ?? "?"} s</span><span class="chip"><span class="lbl">prefill</span> ${rate(best.prefill_tok_s, "tok/s")}</span></div>`
      : `<p>${H().unavailable(`no feasible placement (${s.status || "status not reported"})`)}</p>`;
    const fallback = s.fallback?.best ? `<p class="hint">Without ${esc((s.fallback.without || []).join(", "))}: ${esc(s.fallback.best.name)} · ${rate(s.fallback.best.decode_tok_s, "tok/s")}.</p>` : "";
    const infeasible = (s.infeasible || []).length ? `<details><summary>${esc(s.infeasible.length)} infeasible</summary><ul>${s.infeasible.map(x => `<li><code class="mono">${esc(x.name)}</code>: ${esc(x.problem || "")}</li>`).join("")}</ul></details>` : "";
    return H().card(s.label, `${head}${H().table(headers, candidateRows(s.candidates || []), `${s.label} candidates`)}${fallback}${infeasible}
      <p class="hint">${esc(s.basis || "predicted (planner)")} · qualified: ${esc(String(s.qualified))} · executable: ${esc(String(s.executable))} · not measured: ${esc((s.unmeasured_inputs || []).join(", ") || "none")}</p>
      ${s.cli ? `<p class="hint cli">CLI: <code class="mono">${esc(s.cli)}</code></p>` : ""}`, s.status || "");
  }
  function placementResults() {
    const answer = V.p.answer;
    if (!answer) return "";
    if (!answer.result) return H().card("Placement preview", `<p class="field-error" role="alert">${esc(answer.error || "refused")}</p>${cli(answer)}`);
    const r = answer.result, facts = r.facts || {};
    const previous = V.p.stale ? `<p class="banner warn" role="note">Previous run: the last request failed (see above).</p>` : "";
    const sources = (r.hardware_sources || []).map(s => s.node ? H().cells([`machine ${esc(s.node)}`, "", `disk read: ${esc(s.disk_read)}`,
      `<span class="hide-n">model files: ${esc(s.model_files)}; disk free: ${esc(s.disk_free)}</span>`, ""]) :
      s.group ? H().cells([`group ${esc(s.group)}`, "", "", `<span class="hide-n">${esc(s.supports)}</span>`, ""]) :
      H().cells([esc(s.device || s.link || ""), esc(s.name || ""), esc(s.memory || s.rate || ""),
      `<span class="hide-n">${esc(s.rates || s.latency || "")}${s.reserve ? `; reserve ${esc(s.reserve)}` : ""}</span>`, esc(s.availability || "")])).join("");
    const devices = Object.fromEntries((r.hardware?.devices || []).map(d => [d.id, d]));
    const excluded = (r.excluded || []).map(x => `<li>${esc(x.device || x.link)}${x.name ? ` (${esc(x.name)})` : ""}: ${H().unavailable(x.why)}</li>`).join("");
    const inputs = H().card("Planner inputs", `<p class="hint">Facts ${esc(facts.name)} of ${esc(facts.profile)}, observed ${esc(when(facts.observed_at))} (saved, not live). Memory from the facts; rates from the example hardware shipped with TensorMeld.</p>
      ${H().table(["Device or link", "Name", "Memory or rate", ["Rates and latency", "hide-n"], "Availability"], sources, "Planner inputs")}
      ${Object.keys(devices).length ? `<p class="hint">Usable memory: ${Object.values(devices).map(d => `${esc(d.id)} ${bytesGib(d.memory_bytes - (d.reserved_bytes || 0))}`).join(" · ")}</p>` : ""}
      ${excluded ? `<p><b>Left out</b></p><ul>${excluded}</ul>` : ""}${(r.notes || []).map(n => `<p class="hint">${esc(n)}</p>`).join("")}${cli(answer)}`);
    return `${previous}<div class="banner info" role="note"><b>${esc(r.advisory)}</b><span>Every number is predicted by the planner from the inputs below; nothing is measured, deployed or qualified.</span></div>
      ${(r.scenarios || []).map(scenarioCard).join("")}${inputs}`;
  }

  // --- page ----------------------------------------------------------------------------------------------------------
  (window.HyperloomPages ||= {}).planner = {
    title: "Recipe planner",
    group: "Workspace",
    order: 40,
    icon: '<path d="M4 16V8M10 16V4M16 16v-5"/><path d="M2 16h16"/><circle cx="16" cy="6" r="2"/>',
    endpoints: () => ["planner"],
    ready: () => !!H().state.docs.planner,
    render() {
      loadCatalog();
      if (V.view === "whatif") loadShown(V.recipe);
      defaults();
      const d = doc();
      const tabs = `<fieldset class="seg planner-tabs"><legend class="sr">View</legend>${[["whatif", "What-if"], ["placement", "Placement"]].map(([id, label]) =>
        `<label><input type="radio" name="planner-view" value="${id}" data-page-action="planner-view"${V.view === id ? " checked" : ""}> ${esc(label)}</label>`).join("")}</fieldset>`;
      const why = Object.values(d.why || {}).filter(Boolean).map(w => `<p class="hint">${esc(w)}</p>`).join("");
      const body = V.view === "whatif" ? whatifForm() + whatifResults() : placementForm() + placementResults();
      return `<div class="grid planner">${tabs}${why}${body}</div>`;
    },
    refresh() { loadCatalog(true); },
  };

  async function runWhatif() {
    V.busy = true; V.error = null; rerender();
    try {
      const args = {recipe: V.recipe, profile: V.profile};
      if (V.facts) args.facts = V.facts;
      const v = vary();
      if (Object.keys(v).length) args.vary = v;
      if (V.tokens !== "") args.prompt_tokens = Number(V.tokens);
      V.answer = await post("planner/whatif", args); V.save = {}; V.stale = false;
    } catch (error) { V.error = error.message; V.stale = !!V.answer; H().announce(error.message); }
    finally { V.busy = false; rerender(); }
  }
  async function runPlacement() {
    V.p.busy = true; V.p.error = null; rerender();
    try {
      const args = {model: V.p.model, prompt: Number(V.p.prompt), output: Number(V.p.output), slots: Number(V.p.slots),
        objective: V.p.objective, top_k: Number(V.p.top_k)};
      if (V.p.facts) args.facts = V.p.facts;
      V.p.answer = await post("planner/placement", args); V.p.stale = false;
    } catch (error) { V.p.error = error.message; V.p.stale = !!V.p.answer; H().announce(error.message); }
    finally { V.p.busy = false; rerender(); }
  }
  async function saveLayer(id) {
    const variant = (V.answer?.result?.variants || []).find(v => v.id === id), s = V.save[id];
    if (!variant || !s) return;
    s.busy = true; s.error = null; rerender();
    try {
      s.answer = await post("recipes/save-layer", {name: s.name, recipe: V.answer.result.recipe,
        set: Object.entries(variant.set).map(([path, value]) => `${path}=${JSON.stringify(value)}`)});
    } catch (error) { s.error = error.message; }
    finally { s.busy = false; rerender(); }
  }
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled || !document.querySelector(".planner")) return;
    const action = control.dataset.pageAction, index = Number(control.dataset.index);
    if (action === "planner-add") { V.fields.push({path: "", values: ""}); rerender(); }
    else if (action === "planner-remove") { V.fields.splice(index, 1); if (!V.fields.length) V.fields.push({path: "", values: ""}); rerender(); }
    else if (action === "planner-run") runWhatif();
    else if (action === "planner-place") runPlacement();
    else if (action === "planner-save-open") { V.save[control.dataset.variant] = {open: true, name: `${V.recipe}.${control.dataset.variant}`.slice(0, 64)}; rerender(); }
    else if (action === "planner-save-close") { delete V.save[control.dataset.variant]; rerender(); }
    else if (action === "planner-save") saveLayer(control.dataset.variant);
  });
  function onInput(event) {
    const el = event.target, action = el.dataset?.pageAction;
    if (!action || !document.querySelector(".planner")) return;
    if (action === "planner-view" && el.checked) { V.view = el.value; try { localStorage.setItem("hyperloom-planner-view", V.view); } catch (_) {} rerender(); }
    else if (action === "planner-input") {
      V[el.dataset.key] = el.value;
      if (el.dataset.key === "recipe") { V.fields = [{path: "", values: ""}]; V.answer = null; V.facts = ""; loadShown(V.recipe); }
      if (el.dataset.key === "profile") V.facts = "";
      if (event.type === "change") rerender();
    } else if (action === "planner-path") { V.fields[Number(el.dataset.index)].path = el.value; rerender(); }
    else if (action === "planner-values") { V.fields[Number(el.dataset.index)].values = el.value; if (event.type === "change") rerender(); }
    else if (action === "planner-p") { V.p[el.dataset.key] = el.value; if (el.dataset.key === "model") V.p.facts = ""; if (event.type === "change") rerender(); }
    else if (action === "planner-save-name") { V.save[el.dataset.variant].name = el.value; if (event.type === "change") rerender(); }
  }
  document.addEventListener("input", onInput);
  document.addEventListener("change", onInput);
})();
