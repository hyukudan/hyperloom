/* RTX prefill service panel (F13): each service's own provider counters, strix-stream tier statistics where a service
   is built on it, and the last stats line of a batch run log chosen from the configured logs directory. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const view = {message: "", draft: null, busy: ""};
  const bytes = value => value == null ? null : window.HyperloomTS.fmt(value, "bytes");
  const pct = value => value == null ? null : `${(value * 100).toFixed(1)} %`;
  const shown = (value, why) => value == null ? H().unavailable(why) : H().esc(value);
  function stats(s, title) {
    const {kpi, esc, card} = H();
    const t = s.total;
    const tiles = `<div class="lg-kpis">${[
      kpi("Hits / routed", shown(pct(s.hit_rate), s.why.hit_rate), `${esc(t.hits ?? "unavailable")} of ${esc(t.routed ?? "unavailable")} units already resident`),
      kpi("Acquire wait", shown(s.wait_s == null ? null : `${s.wait_s.toFixed(3)} s`, s.why.wait_s), "total wait for units, as the run reported"),
      kpi("Pinned tier", shown(bytes(t.bytes_pinned), "not reported"), "bytes served from pinned host memory"),
      kpi("Disk tier", shown(bytes(t.bytes_disk), "not reported"), "bytes read from disk"),
      kpi("Speculative used", shown(pct(s.spec_used_share), s.why.spec_used_share), `issued ${esc(t.spec_issued ?? "unavailable")} · dropped ${esc(pct(s.spec_dropped_share) ?? "unavailable")}`),
      kpi("Admitted · transient · evicted", `${esc(t.admitted ?? "–")} · ${esc(t.transient ?? "–")} · ${esc(t.evicted ?? "–")}`, `prefetched ${esc(t.prefetched ?? "unavailable")}`),
    ].join("")}</div>`;
    const layers = s.layers || [];
    const strip = layers.length ? `<div class="lg-strip" role="img" aria-label="Hits per routed unit for ${esc(layers.length)} layers (darker = more hits)">${layers.map(l => {
      const v = l.hit_rate;
      const style = v == null ? "background:var(--surface-2);outline:1px dashed var(--border)" : `background:color-mix(in srgb, var(--s1) ${Math.round(15 + v * 85)}%, var(--surface))`;
      return `<span style="${style}" title="layer ${esc(l.layer)}: ${v == null ? "unavailable" : esc(pct(v))} hits, wait ${l.wait_s == null ? "unavailable" : esc(l.wait_s.toFixed(3) + " s")}"></span>`;
    }).join("")}</div>` : `<p class="muted">No per-layer statistics in this run.</p>`;
    const rows = layers.map(l => `<tr><td>${esc(l.layer)}</td><td class="lg-num">${esc(l.routed ?? "–")}</td><td class="lg-num">${esc(l.hits ?? "–")}</td><td class="lg-num">${l.hit_rate == null ? "unavailable" : esc(pct(l.hit_rate))}</td><td class="lg-num">${esc(l.prefetched ?? "–")}</td><td class="lg-num">${esc(bytes(l.bytes_pinned) ?? "–")}</td><td class="lg-num">${esc(bytes(l.bytes_disk) ?? "–")}</td><td class="lg-num">${l.wait_s == null ? "unavailable" : esc(l.wait_s.toFixed(3) + " s")}</td></tr>`).join("");
    const table = layers.length ? `<details><summary>View layers as table</summary><div class="table-wrap" tabindex="0" role="region" aria-label="Layer statistics"><table><thead><tr><th scope="col">Layer</th><th scope="col" class="lg-num">Routed</th><th scope="col" class="lg-num">Hits</th><th scope="col" class="lg-num">Hits / routed</th><th scope="col" class="lg-num">Prefetched</th><th scope="col" class="lg-num">Pinned</th><th scope="col" class="lg-num">Disk</th><th scope="col" class="lg-num">Wait</th></tr></thead><tbody>${rows}</tbody></table></div></details>` : "";
    const extra = ["readahead", "exclusive"].map(key => s[key] ? `<p class="hint">${esc(key)}: ${esc(Object.entries(s[key]).map(([k, v]) => `${k} ${v}`).join(" · "))}</p>` : `<p class="hint">${esc(key)}: ${H().unavailable(s.why[key])}</p>`).join("");
    return card(title, `${tiles}<h3 class="muted" style="margin:12px 0 0;font-size:13px">Per-layer heat (hits / routed)</h3>${strip}${table}${extra}<p class="hint">strix-stream/stats-v1 · backend ${esc(s.backend || "unavailable")} · ${esc(s.forwards ?? "unavailable")} forwards · ${esc(s.sources_direct ?? "unavailable")} sources read directly</p>`);
  }
  function service(svc) {
    const {card, esc, kv} = H();
    const health = svc.health ? kv(svc.health, Object.keys(svc.health).map(k => [k.replaceAll("_", " "), k])) : H().unavailable(svc.why.service || "health unavailable");
    const provider = svc.provider ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Provider counters"><table><thead><tr><th scope="col">Counter</th><th scope="col" class="lg-num">Value</th></tr></thead><tbody>${svc.provider.counters.map(c => `<tr><td>${esc(c.name)}</td><td class="lg-num">${esc(c.unit === "bytes" ? bytes(c.value) : c.value.toLocaleString())}${c.unit && c.unit !== "bytes" ? ` ${esc(c.unit)}` : ""}</td></tr>`).join("") || `<tr><td colspan="2">No counters reported.</td></tr>`}</tbody></table></div>` : `<p>${H().unavailable(svc.why.provider)}</p>`;
    const stream = svc.strix_stream ? stats(svc.strix_stream, "Tier statistics") : `<p class="hint">strix-stream: ${H().unavailable(svc.why.strix_stream)}</p>`;
    return card(`Service of ${svc.profile}`, `<h3 class="muted" style="font-size:13px">Health</h3>${health}<h3 class="muted" style="font-size:13px">Provider counters (as the service reports them)</h3>${provider}${stream}`, `loopback port ${svc.port}`);
  }
  function runs(doc) {
    const {card, esc, button} = H();
    const draft = view.draft ?? doc.logs_dir ?? "";
    const form = `<form class="lg-form" data-page-form="rtx-dir"><label class="field" for="rtx-dir">Run logs directory<input class="inp" id="rtx-dir" name="dir" value="${esc(draft)}" autocomplete="off" spellcheck="false" placeholder="absolute path"></label><div class="row"><button type="submit" class="btn pri">Save directory</button>${doc.logs_dir_set ? button("Clear", "", {extra: 'data-page-action="rtx-clear"'}) : ""}</div></form>`;
    const list = doc.runs == null ? `<p>${H().unavailable(doc.why.runs)}</p>` : `<div class="table-wrap" tabindex="0" role="region" aria-label="Batch run logs"><table><thead><tr><th scope="col">Run log</th><th scope="col" class="lg-num">Size</th><th scope="col">Modified</th><th scope="col">Stats</th></tr></thead><tbody>${doc.runs.map(r => `<tr><td class="lg-wrap">${esc(r.name)}</td><td class="lg-num">${esc(bytes(r.bytes))}</td><td>${esc(new Date(r.mtime * 1000).toLocaleString())}</td><td>${button(view.busy === r.id ? "Reading…" : "Read last stats", "", {disabled: view.busy ? "another log is being read" : "", extra: `data-page-action="rtx-parse" data-id="${esc(r.id)}"`})}</td></tr>`).join("") || `<tr><td colspan="4">No .log, .txt or .out files in the directory.</td></tr>`}</tbody></table></div>`;
    return card("Batch RTX runs", form + list + (doc.notes || []).map(n => `<p class="hint">${esc(n)}</p>`).join("") + (view.message ? `<p role="status" class="hint lg-wrap">${esc(view.message)}</p>` : "") + `<p class="hint">Only files listed here can be read; the portal finds the last <code>ss stats</code> line in the last 8 MiB of the chosen log.</p>`);
  }

  (window.HyperloomPages ||= {}).rtx = {
    title: "RTX prefill",
    group: "Inference",
    order: 45,
    icon: '<rect x="3" y="5" width="14" height="10" rx="1.5"/><path d="M6 8h3v4H6zM12 8h2M12 10h2M12 12h2"/>',
    endpoints: () => ["ledger/rtx"],
    render() {
      const {esc} = H();
      const doc = H().state.docs["ledger/rtx"];
      const parts = [];
      parts.push(...(doc.services || []).map(service));
      if (!(doc.services || []).length) parts.push(H().card("RTX prefill services", `<p>${H().unavailable(doc.why.services)}</p>`));
      parts.push(runs(doc));
      parts.push(doc.last ? stats(doc.last.stats, `Last batch run · ${doc.last.name}`) : H().card("Last batch run", `<p>${H().unavailable(doc.why.last)}</p>`));
      parts.push(`<p class="hint">Observer: ${esc(doc.observer)}.</p>`);
      return `<div class="grid">${parts.join("")}</div>`;
    },
  };

  const post = (action, body) => H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify(body)});
  async function saveDirectory(value) {
    try { await post("ledger/settings", {logs_dir: value}); view.message = value ? "Run logs directory saved." : "Run logs directory cleared."; view.draft = null; }
    catch (error) { view.message = `Not saved: ${error.message}`; }
    H().announce(view.message); H().refresh();
  }
  document.addEventListener("input", event => { if (event.target.id === "rtx-dir") view.draft = event.target.value; });
  document.addEventListener("submit", event => {
    if (event.target.dataset?.pageForm !== "rtx-dir") return;
    event.preventDefault();
    saveDirectory(event.target.elements.dir.value.trim() || null);
  });
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action^='rtx-']");
    if (!control || control.disabled) return;
    if (control.dataset.pageAction === "rtx-clear") { saveDirectory(null); return; }
    if (control.dataset.pageAction === "rtx-parse") {
      view.busy = control.dataset.id; view.message = ""; H().render();
      try { const result = await post("ledger/rtx-parse", {id: control.dataset.id}); view.message = `Read the last stats line of ${result.name}.`; }
      catch (error) { view.message = `Not read: ${error.message}`; }
      view.busy = ""; H().announce(view.message); H().refresh();
    }
  });
})();
