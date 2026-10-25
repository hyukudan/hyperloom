/* Request timeline (F8) and observed conversation affinity (F16): router-observed phases of every recent and
   in-flight request, joined by request id with the engine's and the prefill service's own records. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const FILTERS = ["profile", "model", "route", "outcome", "caller", "state"];
  const view = {tab: "requests", filters: {}, selected: "", message: ""};
  try { Object.assign(view, JSON.parse(localStorage.getItem("hyperloom-timeline") || "{}"), {message: ""}); } catch (_) { /* storage may be disabled */ }
  const save = () => { try { localStorage.setItem("hyperloom-timeline", JSON.stringify({tab: view.tab, filters: view.filters})); } catch (_) {} };
  const listEndpoint = () => {
    const query = new URLSearchParams({limit: "50"});
    for (const [k, v] of Object.entries(view.filters)) if (v) query.set(k, v);
    return `observe/requests?${query}`;
  };
  const detailEndpoint = () => /^[0-9a-f]{16}$/.test(view.selected) ? `observe/requests/${view.selected}` : null;
  const ms = v => v == null || !Number.isFinite(v) ? null : v >= 10000 ? `${(v / 1000).toFixed(1)} s` : `${v.toFixed(v >= 100 ? 0 : 1)} ms`;
  const val = (v, why, fmt = x => String(x)) => v == null ? H().unavailable(why || "not reported") : H().esc(fmt(v));
  const time = t => t == null ? "—" : new Date(t * 1000).toLocaleTimeString();
  const COLOR = {router: "--s1", engine: "--s2", "prefill service": "--s3"};

  function pill(row) {
    const state = row.state === "in_flight" ? "in flight" : row.state === "unknown" ? "final state not observed" : row.outcome || row.state;
    const style = row.state === "in_flight" ? "p-info" : row.state === "unknown" ? "p-warn" : row.outcome === "served" ? "p-ok" : "p-err";
    return `<span class="pill ${style}">${H().esc(state || "unknown")}</span>`;
  }

  /* One observer's phases as horizontal bars on its own ms axis (never mixed with another clock). */
  function bars(title, observer, phases, {elapsed} = {}) {
    if (!phases?.length && elapsed == null) return "";
    const end = Math.max(1, elapsed || 0, ...phases.map(p => p.end_ms));
    const W = Math.round(Math.max(300, Math.min(760, (document.querySelector("main")?.clientWidth || 700) - 72))), rowH = 18, L = W < 480 ? 104 : 130, R = 64;
    const x = v => L + (v / end) * (W - L - R);
    const rows = phases.map((p, i) => {
      const y = 4 + i * rowH, w = Math.max(1.5, x(p.end_ms) - x(p.start_ms));
      return `<text x="${L - 6}" y="${y + 12}" text-anchor="end" class="tl-ax">${H().esc(p.phase)}</text>`
        + `<rect x="${x(p.start_ms).toFixed(1)}" y="${y + 2}" width="${w.toFixed(1)}" height="${rowH - 6}" rx="2" fill="var(${COLOR[observer]})"><title>${H().esc(`${p.phase}: ${ms(p.start_ms)} → ${ms(p.end_ms)}`)}</title></rect>`
        + `<text x="${(x(p.end_ms) + 4).toFixed(1)}" y="${y + 12}" class="tl-ax">${H().esc(ms(p.end_ms - p.start_ms))}</text>`;
    });
    if (elapsed != null) {
      const y = 4 + phases.length * rowH;
      rows.push(`<text x="${L - 6}" y="${y + 12}" text-anchor="end" class="tl-ax">in flight</text><rect x="${L}" y="${y + 2}" width="${(x(elapsed) - L).toFixed(1)}" height="${rowH - 6}" rx="2" fill="none" stroke="var(${COLOR[observer]})" stroke-dasharray="4 3"/><text x="${(x(elapsed) + 4).toFixed(1)}" y="${y + 12}" class="tl-ax">${H().esc(ms(elapsed))} so far</text>`);
    }
    const H_ = 8 + (phases.length + (elapsed != null ? 1 : 0)) * rowH;
    const table = `<details><summary>View as table</summary>${H().table(["Phase", "Start", "End", "Duration"], phases.map(p => H().cells([H().esc(p.phase), H().esc(ms(p.start_ms)), H().esc(ms(p.end_ms)), H().esc(ms(p.end_ms - p.start_ms))])).join(""), title)}</details>`;
    return `<figure class="tl-fig"><figcaption>${H().esc(title)}</figcaption><svg viewBox="0 0 ${W} ${H_}" width="${W}" height="${H_}" role="img" aria-label="${H().esc(title)}" preserveAspectRatio="xMinYMin meet">${rows.join("")}</svg>${table}</figure>`;
  }

  function detail(row) {
    const {esc, kv, table, cells} = H();
    const router = row.router || {};
    const parts = [bars("Router-observed · ms from arrival", "router", router.phases || [], {elapsed: row.state === "in_flight" ? row.elapsed_ms : undefined})];
    if (row.engine) parts.push(bars(`Engine-observed · ${row.engine.replica || "engine"} · attempt ${row.engine.attempt ?? "?"} · its own clock`, "engine", row.engine.phases || []));
    else parts.push(`<p>Engine record: ${H().unavailable(row.why?.engine)}</p>`);
    if (row.service) parts.push(`<p class="t2">Prefill service record (its own clock): prefill ${esc(ms(row.service.prefill_ms) || "—")}, hand-off ${esc(ms(row.service.handoff_ms) || "—")}, computed ${esc(row.service.computed_tokens ?? "—")} tokens · ${esc(row.service.basis)}</p>`);
    else if (row.why?.service) parts.push(`<p>Prefill service record: ${H().unavailable(row.why.service)}</p>`);
    const e = row.engine || {};
    const accounting = `<dl class="kv"><dt>Prompt</dt><dd>${val(e.prompt_tokens, row.why?.engine)}</dd><dt>Computed here</dt><dd>${val(e.computed_tokens, e.why?.computed_tokens || row.why?.engine)}</dd><dt>Restored from cache</dt><dd>${val(e.cached_tokens, row.why?.engine)}</dd><dt>Imported (RTX)</dt><dd>${val(e.imported_tokens, row.why?.engine)}</dd><dt>Draft accepted / proposed</dt><dd>${e.draft_proposed ? esc(`${e.draft_accepted} / ${e.draft_proposed}`) : H().unavailable("not reported")}</dd><dt>Engine TTFT</dt><dd>${e.ttft_ms != null ? esc(ms(e.ttft_ms)) : e.ttft_approx_ms != null ? esc(`${ms(e.ttft_approx_ms)} (queue + prefill, approximation)`) : H().unavailable(e.why?.ttft_ms || row.why?.engine)}</dd></dl>`;
    const attempts = table(["#", "Kind", "Target", "At", "Outcome", "Why"], (row.attempts || []).map(a => cells([esc(a.n ?? "?"), esc(a.kind || ""), esc(a.target || ""), esc(ms(a.at_ms) || "—"), esc(a.outcome || ""), esc(a.why || "")])).join(""), "Attempts");
    const decisions = row.decisions?.length ? table(["At", "Decision", "Target", "Why"], row.decisions.map(d => cells([esc(ms(d.at_ms) || "—"), esc(d.what || ""), esc(d.target || ""), esc(d.why || "")])).join(""), "Decisions") : "";
    const why = Object.entries(row.why || {}).filter(([k]) => !["engine", "service"].includes(k)).map(([k, v]) => `<li>${esc(k.replaceAll("_", " "))}: ${H().unavailable(v)}</li>`).join("");
    const head = `<p>${pill(row)} <span class="tag">${esc(row.model || "model unknown")}</span> <span class="tag">route ${esc(row.route || "direct")}</span> <span class="tag">caller ${esc(row.caller || "anonymous")}</span> <span class="tag">${esc(row.path || "")}</span>${row.reason ? ` <span class="t2">${esc(row.reason)}</span>` : ""}</p>`;
    return H().card(`Request ${row.id}`, head + parts.join("") + `<div class="grid g2">${H().card("Prompt accounting (engine-observed, C8)", accounting)}${H().card("Attempts", attempts + decisions)}</div>` + (why ? `<ul class="hint">${why}</ul>` : ""),
      `arrived ${time(row.started_at)}${row.client_id ? ` · client id ${row.client_id}` : ""}`);
  }

  function requestsTab() {
    const {esc, table, cells, card, empty} = H();
    const doc = H().state.docs[listEndpoint()] || {};
    if (doc.requests == null) return empty("Request timeline unavailable", doc.why?.requests || "metrics store not open");
    const filters = doc.filters || {};
    const selects = FILTERS.map(name => {
      const options = filters[name] || [];
      return `<label class="field" for="tl-${name}">${esc(name)} <select class="inp" id="tl-${name}" data-page-action="tl-filter" data-filter="${name}"><option value="">all</option>${options.map(o => `<option value="${esc(o)}" ${view.filters[name] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`;
    }).join("");
    const notes = [];
    for (const [profile, source] of Object.entries(doc.sources || {})) {
      for (const [part, why] of Object.entries(source.why || {})) if (why) notes.push(`${profile} · ${part}: ${why}`);
      for (const [part, stale] of Object.entries(source.stale || {})) if (stale && !(source.why || {})[part]) notes.push(`${profile} · ${part}: stale (no answer within 3 collector intervals)`);
    }
    for (const [profile, gap] of Object.entries(doc.gaps || {})) notes.push(`${profile}: ${gap.why} (${gap.count} times, last ${time(gap.last)})`);
    const rows = (doc.requests || []).map(row => {
      const e = row.engine || {};
      const total = row.router?.total_ms ?? row.elapsed_ms;
      const mini = (row.router?.phases || []).length ? `<span class="tl-mini" aria-hidden="true">${(row.router.phases).map(p => `<i style="left:${(p.start_ms / Math.max(1, total || p.end_ms) * 100).toFixed(1)}%;width:${Math.max(1, (p.end_ms - p.start_ms) / Math.max(1, total || p.end_ms) * 100).toFixed(1)}%"></i>`).join("")}</span>` : "";
      return cells([`<span class="tl-nw">${esc(time(row.started_at))}</span>`, `<button type="button" class="btn tl-id" data-page-action="tl-select" data-id="${esc(row.id)}" aria-pressed="${row.id === view.selected}">${esc(row.id.slice(0, 8))}</button>`, pill(row),
        esc(row.model || "—"), `<span class="tl-nw">${esc(row.route || "direct")}</span>`, `<span class="hide-n tl-nw">${esc(row.caller || "anonymous")}</span>`,
        row.router?.ttft_ms != null ? esc(ms(row.router.ttft_ms)) : H().unavailable(row.why?.ttft_ms),
        e.ttft_ms != null ? esc(ms(e.ttft_ms)) : H().unavailable(row.engine ? e.why?.ttft_ms : row.why?.engine),
        `<span class="hide-n">${total != null ? esc(ms(total)) + (row.state === "in_flight" ? " so far" : "") : "—"} ${mini}</span>`]);
    }).join("");
    const unjoined = doc.unjoined?.length ? card("Engine-only records", `<p class="hint">Engine-observed records without a router request id (older engine builds or requests not sent through the router).</p>` + table(["Time", "Profile", "Replica", "Route", "Prompt", "Output", "Queue + prefill"], doc.unjoined.map(u => cells([esc(time(u.time)), esc(u.profile), esc(u.replica), esc(u.route || ""), esc(u.prompt_tokens ?? "—"), esc(u.output_tokens ?? "—"), esc(ms(u.ttft_ms ?? u.ttft_approx_ms) || "—")])).join(""), "Engine-only records")) : "";
    const list = card("Recent and in-flight requests", `<div class="row tl-filters">${selects}</div>` + (notes.length ? `<ul class="hint">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "")
      + (rows ? table(["Arrived", "Request", "Outcome", "Model", "Route", ["Caller", "hide-n"], "TTFT · router", "TTFT · engine", ["Total · router", "hide-n"]], rows, "Requests") : `<p>${H().unavailable(doc.why?.requests)}</p>`),
      "router-observed and engine-observed times are labelled separately");
    const selected = H().state.docs[detailEndpoint()]?.request;
    return list + (selected ? detail(selected) : view.selected ? `<p class="hint">Loading request ${esc(view.selected)}…</p>` : "") + unjoined;
  }

  function conversationsTab() {
    const {esc, table, cells, card, empty} = H();
    const doc = H().state.docs["observe/sessions"] || {};
    const settings = H().state.docs["observe/settings"] || {};
    if (doc.sessions == null) return empty("Conversation view unavailable", doc.why?.sessions || "metrics store not open");
    const coverage = Object.entries(doc.coverage || {}).map(([p, c]) => `<li>${esc(p)} · ${esc(c.source)}${c.router_idle_s != null ? ` · router expiry ${esc(Math.round(c.router_idle_s))} s` : ""}${c.returned != null ? ` · ${esc(c.returned)} of ${esc(c.router_count ?? c.returned)} read` : ""}${c.why ? ` · ${esc(c.why)}` : ""}</li>`).join("");
    const rows = doc.sessions.map(s => cells([`<span class="mono">${esc(s.key)}</span>`, esc(s.profile), esc(s.endpoint || "—"), esc(s.turns ?? "—"), esc(s.replica || "—"),
      `<span class="hide-n">${esc((s.reused_per_turn || []).map(v => v ?? "?").join(", ") || "—")}</span>`, esc(time(s.last_seen)), `<span class="hide-n">${esc(time(s.expires_at))}</span>`])).join("");
    const reason = !H().csrf() || H().csrf() === "__HYPERLOOM_CSRF__" ? "Launch the portal through hyperloom ui to change settings" : settings.why?.settings ? settings.why.settings : "";
    const idle = settings.settings?.affinity_idle_s ?? doc.idle_expiry_s;
    const form = `<div class="row"><label class="field" for="tl-idle">Forget a conversation after (s) <input class="inp" id="tl-idle" type="number" min="60" max="86400" step="60" value="${esc(idle ?? 1800)}" ${reason ? "disabled" : ""}></label>${H().button("Save expiry", "", {extra: 'data-page-action="tl-save-idle"', disabled: reason})}</div>${view.message ? `<p class="hint" role="status">${esc(view.message)}</p>` : ""}`;
    return card("Observed affinity", `<p class="t2">${esc(doc.label)}</p>${coverage ? `<ul class="hint">${coverage}</ul>` : ""}` + (rows ? table(["Session", "Profile", "Endpoint", "Turns", "Replica", "Prefix tokens reused per turn", "Last seen", "Expires"], rows, "Conversations") : `<p>${H().unavailable(doc.why?.sessions)}</p>`) + form,
      "pseudonymous keys from the router; not confirmed cache residency");
  }

  (window.HyperloomPages ||= {}).timeline = {
    title: "Request timeline",
    group: "Inference",
    order: 20,
    icon: '<path d="M3 5h8M6 10h9M4 15h6"/><path d="M3 3v14"/>',
    endpoints: () => view.tab === "requests" ? [listEndpoint(), ...(detailEndpoint() ? [detailEndpoint()] : [])] : ["observe/sessions", "observe/settings"],
    ready: () => view.tab === "requests" ? !!H().state.docs[listEndpoint()] : !!H().state.docs["observe/sessions"],
    render() {
      const tabs = `<div class="row" role="tablist" aria-label="Timeline views">${[["requests", "Requests"], ["conversations", "Conversations"]].map(([id, label]) => `<button type="button" role="tab" class="btn ${view.tab === id ? "pri" : ""}" aria-selected="${view.tab === id}" data-page-action="tl-tab" data-tab="${id}">${label}</button>`).join("")}</div>`;
      return `<div class="grid">${tabs}${view.tab === "requests" ? requestsTab() : conversationsTab()}</div>`;
    },
  };

  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action]");
    if (!control) return;
    const action = control.dataset.pageAction;
    if (action === "tl-tab") { view.tab = control.dataset.tab; save(); H().refresh(); }
    else if (action === "tl-select") { view.selected = view.selected === control.dataset.id ? "" : control.dataset.id; H().refresh(); }
    else if (action === "tl-save-idle") {
      const value = Number(document.getElementById("tl-idle")?.value);
      try {
        await H().request("observe/settings/save", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify({affinity_idle_s: value})});
        view.message = `Expiry saved: ${value} s`;
      } catch (error) { view.message = `Not saved: ${error.message}`; }
      H().announce(view.message); H().refresh();
    }
  });
  document.addEventListener("change", event => {
    if (event.target.dataset?.pageAction !== "tl-filter") return;
    view.filters[event.target.dataset.filter] = event.target.value; save(); H().refresh();
  });
})();
