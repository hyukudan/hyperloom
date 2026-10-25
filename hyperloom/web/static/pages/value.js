/* Token value report (F47): the ledger's tokens priced from a local price table (per million tokens); cache savings
   from cache-restored tokens only (C8). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const ranges = [["24 h", "86400"], ["7 d", "604800"], ["30 d", "2592000"], ["All", "all"]];
  const view = {range: "2592000", draft: null, message: "", error: "", errors: {}};
  try { view.range = localStorage.getItem("hyperloom-value-range") || view.range; } catch (_) { /* storage may be disabled */ }
  const endpoint = () => `ledger/value?${new URLSearchParams({range: view.range})}`;
  const money = (value, currency) => value == null || !Number.isFinite(value) ? null : `${value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})} ${currency || ""}`.trim();
  const tokens = value => value == null ? null : value.toLocaleString(undefined, {maximumFractionDigits: 0});
  function draftFrom(prices) {
    const blank = {input: "", cached: "", output: ""};
    return {currency: prices?.currency || "EUR", useDefault: !!prices?.default, default: prices?.default ? {...prices.default} : {...blank},
            models: Object.entries(prices?.models || {}).map(([name, p]) => ({name, ...p}))};
  }
  function editor(doc) {
    const {esc, button, card} = H();
    const d = view.draft ||= draftFrom(doc.prices);
    const field = (id, label, value) => {
      const error = view.errors?.[id];
      return `<label class="field" for="${id}">${esc(label)}<input class="inp" id="${id}" inputmode="decimal" value="${esc(value)}" autocomplete="off"${error ? ` aria-invalid="true" aria-describedby="${id}-error"` : ""}>${error ? `<span class="field-error lg-error" id="${id}-error">${esc(error)}</span>` : ""}</label>`;
    };
    const models = d.models.map((m, i) => `<div class="lg-form" style="margin-top:8px">${field(`value-m${i}-name`, "Model", m.name)}${field(`value-m${i}-input`, "Input (computed)", m.input)}${field(`value-m${i}-cached`, "Input (cache-restored)", m.cached)}${field(`value-m${i}-output`, "Output", m.output)}<div>${button("Remove", "", {extra: `data-page-action="value-remove" data-index="${i}"`})}</div></div>`).join("");
    return card("Price table", `<form data-page-form="value-prices"><div class="lg-form">${field("value-currency", "Currency", d.currency)}<label class="field" for="value-use-default"><span><input type="checkbox" id="value-use-default" ${d.useDefault ? "checked" : ""}> Default price for every other model</span></label></div>
<div class="lg-form" style="margin-top:8px">${field("value-d-input", "Default input (computed)", d.default.input)}${field("value-d-cached", "Default input (cache-restored)", d.default.cached)}${field("value-d-output", "Default output", d.default.output)}</div>
${models}<div class="row" style="margin-top:10px">${button("Add model price", "", {extra: 'data-page-action="value-add"'})}${submit("Save prices")}</div></form>
${view.error ? `<p class="lg-error" role="alert">${esc(view.error)}</p>` : ""}${view.message ? `<p role="status" class="hint">${esc(view.message)}</p>` : ""}<p class="hint">Prices per million tokens. Saving is validated by the portal and recorded in the audit log.</p>`, "local, edited here or with hyperloom token-value set");
  }
  function report(doc) {
    const {esc, unavailable, card} = H();
    if (doc.models == null) return card("Value", `<p>${unavailable(doc.why?.models)}</p>`);
    const c = doc.currency;
    const rows = doc.models.map(r => `<tr><td>${esc(r.model)}${r.price_source ? `<div class="muted">${esc(r.price_source)} price</div>` : ""}</td><td class="lg-num">${esc(tokens(r.input_tokens))}${r.input_tag !== "measured" ? `<span class="lg-tag e">partly estimated</span>` : ""}</td><td class="lg-num">${r.cached_tokens == null ? unavailable("not reported") : esc(tokens(r.cached_tokens)) + (r.cached_tag === "partial" ? `<span class="lg-tag e">partial</span>` : "")}</td><td class="lg-num">${esc(tokens(r.output_tokens))}</td><td class="lg-num">${r.input_cost == null ? unavailable(r.why.total || r.why.input_cost) : esc(money(r.input_cost, c))}</td><td class="lg-num">${r.cached_cost == null ? unavailable(r.why.cached_cost || r.why.total) : esc(money(r.cached_cost, c))}</td><td class="lg-num">${r.output_cost == null ? unavailable(r.why.total) : esc(money(r.output_cost, c))}</td><td class="lg-num"><b>${r.total == null ? unavailable(r.why.total) : esc(money(r.total, c))}</b>${r.total != null ? (r.exact ? `<span class="lg-tag m">exact</span>` : `<span class="lg-tag e">qualified</span>${(r.qualifiers || []).map(q => `<div class="hint">${esc(q)}</div>`).join("")}`) : ""}</td><td class="lg-num">${r.savings == null ? unavailable(r.why.savings || r.why.total) : esc(money(r.savings, c))}${r.why.savings && r.savings != null ? `<div class="hint">${esc(r.why.savings)}</div>` : ""}</td></tr>`).join("");
    const t = doc.totals;
    const totals = t ? `<tr><th scope="row">All priced models</th><td></td><td></td><td></td><td class="lg-num">${esc(money(t.input_cost, c))}</td><td class="lg-num">${esc(money(t.cached_cost, c))}</td><td class="lg-num">${esc(money(t.output_cost, c))}</td><td class="lg-num"><b>${esc(money(t.total, c))}</b>${t.exact ? `<span class="lg-tag m">exact</span>` : `<span class="lg-tag e">qualified</span>${(t.qualifiers || []).map(q => `<div class="hint">${esc(q)}</div>`).join("")}`}</td><td class="lg-num">${esc(money(t.savings, c))}</td></tr>` : "";
    const flags = doc.flags || {};
    const notes = [doc.why?.totals, doc.why?.prices, flags.estimated_input ? `${tokens(flags.estimated_input)} input tokens priced as computed are an upper bound (cache-restored or imported not reported).` : "",
      flags.unavailable_input ? `${tokens(flags.unavailable_input)} input tokens are not priced: their computed share is unavailable.` : "",
      flags.uncertain ? `${tokens(flags.uncertain)} tokens have an approximate hour.` : "",
      flags.unverified_identity_tokens ? `${tokens(flags.unverified_identity_tokens)} tokens come from engines without an instance identity.` : ""].filter(Boolean);
    return card("Value", `<div class="table-wrap" tabindex="0" role="region" aria-label="Token value by model"><table class="lg-value"><thead><tr><th scope="col">Model</th><th scope="col" class="lg-num">Input tokens</th><th scope="col" class="lg-num">Cache-restored</th><th scope="col" class="lg-num">Output tokens</th><th scope="col" class="lg-num">Input</th><th scope="col" class="lg-num">Cache-restored</th><th scope="col" class="lg-num">Output</th><th scope="col" class="lg-num">Total</th><th scope="col" class="lg-num">Cache savings</th></tr></thead><tbody>${rows || `<tr><td colspan="9">No tokens in this range.</td></tr>`}${totals}</tbody></table></div>${notes.map(n => `<p class="hint">${esc(n)}</p>`).join("")}<p class="hint">${esc(doc.basis || "")}</p>`, `range ${view.range === "all" ? "all time" : ranges.find(r => r[1] === view.range)?.[0] || view.range}`);
  }

  (window.HyperloomPages ||= {}).value = {
    title: "Token value",
    group: "Operations",
    order: 60,
    icon: '<circle cx="10" cy="10" r="7"/><path d="M12.5 7.5c-.6-.8-1.5-1.2-2.5-1.2-1.4 0-2.5.8-2.5 2s1.1 1.6 2.5 1.8 2.5.6 2.5 1.8-1.1 2-2.5 2c-1 0-1.9-.4-2.5-1.2M10 5v10"/>',
    endpoints: () => [endpoint()],
    ready: () => !!H().state.docs[endpoint()],
    render() {
      const {esc} = H();
      const doc = H().state.docs[endpoint()];
      const seg = `<div class="row" role="group" aria-label="Range">${ranges.map(([label, value]) => `<button type="button" class="btn ${value === view.range ? "pri" : ""}" data-page-action="value-range" data-value="${value}" aria-pressed="${value === view.range}">${esc(label)}</button>`).join("")}</div>`;
      return `<div class="grid">${H().card("Range", seg + `<p class="hint">Times in ${esc(doc.timezone || "")}; token counts from the token ledger.</p>`)}${report(doc)}${editor(doc)}</div>`;
    },
  };

  // Writes wait for authoritative state and never overlap another action (the shell's gate), then refresh.
  async function guarded(action, body) {
    const blocker = H().mutationReason();
    if (blocker) throw new Error(blocker);
    H().state.busy = true; H().render();
    try {
      return await H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify(body)});
    } finally { H().state.busy = false; H().state.fresh = false; }
  }
  const submit = label => { const blocker = H().mutationReason(); return blocker ? `<span class="disabled-control"><button type="submit" class="btn pri" disabled title="${H().esc(blocker)}">${H().esc(label)}</button><span class="hint">${H().esc(blocker)}</span></span>` : `<button type="submit" class="btn pri">${H().esc(label)}</button>`; };
  function focusError() {
    const first = Object.keys(view.errors || {})[0];
    if (first) document.getElementById(first)?.focus();
  }
  function readDraft() {
    const d = view.draft;
    const get = id => document.getElementById(id)?.value ?? "";
    d.currency = get("value-currency");
    d.useDefault = !!document.getElementById("value-use-default")?.checked;
    for (const k of ["input", "cached", "output"]) d.default[k] = get(`value-d-${k}`);
    d.models.forEach((m, i) => { m.name = get(`value-m${i}-name`); for (const k of ["input", "cached", "output"]) m[k] = get(`value-m${i}-${k}`); });
  }
  const number = text => text === "" ? NaN : Number(String(text).replace(",", "."));
  document.addEventListener("input", event => { if (event.target.id?.startsWith("value-") && view.draft) readDraft(); });
  document.addEventListener("change", event => { if (event.target.id === "value-use-default" && view.draft) readDraft(); });
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action^='value-']");
    if (!control || control.disabled) return;
    const action = control.dataset.pageAction;
    if (action === "value-range") { view.range = control.dataset.value; try { localStorage.setItem("hyperloom-value-range", view.range); } catch (_) {} H().refresh(); return; }
    if (!view.draft) return;
    readDraft();
    if (action === "value-add") view.draft.models.push({name: "", input: "", cached: "", output: ""});
    if (action === "value-remove") view.draft.models.splice(Number(control.dataset.index), 1);
    H().render();
  });
  document.addEventListener("submit", async event => {
    if (event.target.dataset?.pageForm !== "value-prices") return;
    event.preventDefault();
    readDraft();
    const d = view.draft, set = p => ({input: number(p.input), cached: number(p.cached), output: number(p.output)});
    const body = {currency: d.currency.trim(), default: d.useDefault ? set(d.default) : null,
                  models: Object.fromEntries(d.models.filter(m => m.name.trim()).map(m => [m.name.trim(), set(m)]))};
    view.errors = {};
    if (!body.currency) view.errors["value-currency"] = "Give a currency (letters or a currency sign).";
    if (d.useDefault) for (const k of ["input", "cached", "output"]) if (!Number.isFinite(body.default[k]) || body.default[k] < 0) view.errors[`value-d-${k}`] = "A price per million tokens (0 or more).";
    d.models.forEach((m, i) => {
      if (!m.name.trim() && (m.input || m.cached || m.output)) view.errors[`value-m${i}-name`] = "Name the model or remove the row.";
      if (m.name.trim()) for (const k of ["input", "cached", "output"]) { const v = number(m[k]); if (!Number.isFinite(v) || v < 0) view.errors[`value-m${i}-${k}`] = "A price per million tokens (0 or more)."; }
    });
    if (Object.keys(view.errors).length) { view.error = "Some prices are not valid: see the marked fields."; view.message = ""; H().render(); focusError(); return; }
    try {
      await guarded("ledger/prices", body);
      view.error = ""; view.errors = {}; view.message = "Prices saved."; view.draft = null;
    } catch (error) {
      // The portal names the rejected field (e.g. "models.x.cached" or "currency"): point at it.
      view.error = `Not saved: ${error.message}`; view.message = ""; view.errors = {};
      const where = /^(currency|default\.(input|cached|output)|models\.(.+)\.(input|cached|output))\b/.exec(error.message);
      if (where) {
        const index = where[3] ? view.draft.models.findIndex(m => m.name.trim() === where[3]) : -1;
        const id = where[1] === "currency" ? "value-currency" : where[2] ? `value-d-${where[2]}` : index >= 0 ? `value-m${index}-${where[4]}` : "";
        if (id) view.errors[id] = error.message;
      }
    }
    H().announce(view.error || view.message); await H().refresh(); focusError();
  });
})();
