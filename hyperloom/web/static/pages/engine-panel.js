/* Engine panel (F10): one card per engine instance with what the engine reports about itself. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const docName = "observe/engines";
  const view = {message: ""};
  const engines = () => H().state.docs[docName]?.engines || [];
  const charts = row => [row.series.engine_generated_tokens, row.series.engine_queue_waiting];
  const n = v => v == null ? null : Number(v).toLocaleString();
  const ago = s => s == null ? null : s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  const bytes = v => v == null ? null : v >= 2 ** 30 ? `${(v / 2 ** 30).toFixed(1)} GiB` : `${(v / 2 ** 20).toFixed(1)} MiB`;

  function pool(slots) {
    if (slots.pool_positions == null && slots.pool_bytes == null && slots.pool_largest_free == null) return null;
    return [slots.pool_positions != null ? `${n(slots.pool_positions)} positions` : "positions not reported",
      slots.pool_bytes != null ? bytes(slots.pool_bytes) : "size not reported",
      slots.pool_largest_free != null ? `largest free ${n(slots.pool_largest_free)}` : "largest free not reported"].join(" · ");
  }

  function item(label, value, why, note) {
    return `<dt>${H().esc(label)}</dt><dd>${value == null ? H().unavailable(why || "not reported") : H().esc(value) + (note ? ` <span class="pill p-warn">${H().esc(note)}</span>` : "")}</dd>`;
  }

  function card(row) {
    const {esc, card: box} = H();
    const why = row.why || {};
    const slots = row.slots || {}, queue = row.queue || {}, batch = row.batch || {}, group = row.group || {};
    const cache = row.prefix_cache || {}, spec = row.speculative || {};
    const since = row.prompt_accounting.since_armed, life = row.prompt_accounting.lifetime;
    const state = row.state === "stale" ? `<span class="pill p-warn">stale · as of ${esc(new Date((row.as_of || 0) * 1000).toLocaleTimeString())}</span>` : H().statusPill(row.state, why.status);
    const idle = row.idle_s != null ? `idle · last served ${ago(row.idle_s)} ago` : null;
    const identity = `<dl class="kv">${item("Instance", row.instance ? `${row.instance} (${row.instance_basis})` : null, why.instance || why.counters)}${item("Started", row.started_at ? new Date(row.started_at * 1000).toLocaleString() : null, "not reported")}${item("Read", row.via)}${item("Instrumentation", row.armed == null ? null : row.armed ? "armed" : "not armed", "engine build without the armed flag")}${item("Activity", idle || (row.last_served ? `last served ${new Date(row.last_served * 1000).toLocaleTimeString()}` : null), why.last_served)}</dl>`;
    const capacity = `<dl class="kv">${item("Slots busy / total", slots.total != null ? `${slots.busy ?? "?"} / ${slots.total}` : null, why.slots)}${item("Context", n(row.context_tokens ?? slots.context_tokens), why.context_tokens)}${item("Queue waiting / running", queue.queued != null || queue.running != null ? `${queue.queued ?? "?"} / ${queue.running ?? "?"}` : null, why.status)}${item("KV / state pool", pool(slots), "engine reports no pool")}${item("Rows per step", batch.mean_rows_per_step != null ? batch.mean_rows_per_step.toFixed(2) : null, why.batch)}${item("Step time", batch.mean_step_ms != null ? `${batch.mean_step_ms.toFixed(1)} ms` : null, why.batch)}${item("USB4 exchange per step", row.usb4_exchange_seconds_per_step != null ? `${(row.usb4_exchange_seconds_per_step * 1000).toFixed(2)} ms` : null, why.usb4_exchange_seconds_per_step)}${item("Group", group.world ? `rank ${group.rank} of ${group.world} · ${group.transport || ""}` : null, why.group)}</dl>`;
    const accounting = `<dl class="kv">${item("Prompt tokens", n(since.prompt_tokens), why.computed_tokens)}${item("Computed here", n(since.computed_tokens), why.computed_tokens, since.computed_tokens != null && why.computed_tokens)}${item("Restored from cache", n(since.cached_tokens), why.computed_tokens)}${item("Imported from the RTX service", n(since.imported_tokens), why.computed_tokens)}${item("Requests", n(since.requests), why.computed_tokens)}</dl><p class="hint">${esc(since.basis)} · ${esc(row.prompt_accounting.scope)}</p><dl class="kv">${item("Lifetime prompt tokens", n(life.prompt_tokens), why.status)}${item("Lifetime cached", n(life.cached_tokens), why.status)}${item("Lifetime imported", n(life.imported_tokens), life.why?.computed_tokens)}${item("Lifetime computed here", n(life.computed_tokens), life.why?.computed_tokens, life.computed_tokens != null && life.why?.computed_tokens)}${item("Lifetime generated", n(life.generated_tokens), why.status)}</dl>`;
    const cacheBlock = row.prefix_cache ? `<dl class="kv">${item("Lookups", n(cache.lookups))}${item("Hits", n(cache.hits))}${item("Hit tokens", n(cache.hit_tokens))}${item("Disk hits", n(cache.disk_hits))}${item("Bytes", bytes(cache.bytes))}</dl><details><summary>All counters</summary>${H().table(["Counter", "Value"], Object.entries(cache.raw || {}).map(([k, v]) => H().cells([esc(k), esc(n(v))])).join(""), "Prefix cache counters")}</details>` : `<p>${H().unavailable(why.prefix_cache)}</p>`;
    const specBlock = row.speculative ? `<dl class="kv">${item("Method", spec.method)}${item("Accepted / proposed", spec.proposed && spec.accepted != null ? `${n(spec.accepted)} / ${n(spec.proposed)} (${(spec.accepted / spec.proposed * 100).toFixed(1)} %)` : null, spec.proposed && spec.accepted == null ? "engine reports proposed drafts without an accepted count" : "no drafts yet")}${item("Accepted per cycle", spec.cycles && spec.accepted != null ? (spec.accepted / spec.cycles).toFixed(2) : null, spec.cycles ? "engine reports no accepted count" : "engine reports no cycles")}${item("Counted", spec.since)}</dl>` : `<p>${H().unavailable(why.speculative)}</p>`;
    const memory = row.memory || {};
    const memoryBlock = row.memory ? `<dl class="kv">${Object.entries(memory).filter(([k, v]) => k.endsWith("_bytes") && v != null).map(([k, v]) => item(k.replace(/_bytes$/, "").replaceAll("_", " "), bytes(v))).join("")}</dl><p class="hint">as the engine reports it; what no report covers is unattributed</p>` : `<p>${H().unavailable(why.memory)}</p>`;
    const sources = Object.entries(row.last_ok || {}).map(([part, t]) => `${part} ${new Date(t * 1000).toLocaleTimeString()}`).join(" · ");
    const answers = Object.entries(row.last_answer || {}).filter(([part, t]) => (row.last_ok || {})[part] !== t).map(([part, t]) => `${part} ${new Date(t * 1000).toLocaleTimeString()}`).join(" · ");
    const resets = row.counter_resets?.length ? `<p class="hint">Counter resets (new instance or counters back): ${row.counter_resets.map(r => esc(`${new Date(r.ts * 1000).toLocaleTimeString()} · ${Math.round(r.unobserved_s)} s unobserved`)).join("; ")}</p>` : "";
    const plots = charts(row).map((endpoint, i) => {
      const h = H().state.docs[endpoint];
      if (!h) return "";
      if (h.lines == null) return `<p>${H().display(null, h.why?.lines)}</p>`;
      return window.HyperloomTS.chart({title: i ? "Queue waiting (engine-observed)" : "Generated tokens per bucket (engine counters)", unit: i ? "" : "tokens", step: h.step || 2, cadence: h.tier === "raw" ? h.interval_s || 0 : 0, start: h.start, end: h.end, lines: window.HyperloomTS.lines(h, {label: l => l.instance || "engine"}), group: "engine-panel", height: 140});
    }).join("");
    return box(`${row.profile} / ${row.replica}`, `<p>${state} <span class="tag">${esc(row.model || "model unknown")}</span> <span class="tag">${esc(row.machine || "")}</span></p>`
      + `<div class="ob-grid">${box("Identity", identity)}${box("Capacity and steps", capacity)}${box("Prompt accounting (C8)", accounting)}${box("Prefix cache", cacheBlock)}${box("Speculative decoding", specBlock)}${box("Memory (engine report)", memoryBlock)}</div>` + `<p class="hint">Last good answers: ${esc(sources || "none yet")}${answers ? ` · answered without a usable payload: ${esc(answers)}` : ""}</p>` + resets + plots,
      row.engine || "");
  }

  function retention() {
    const settings = H().state.docs["observe/settings"] || {};
    const csrf = H().csrf();
    const reason = !csrf || csrf === "__HYPERLOOM_CSRF__" ? "Launch the portal through hyperloom ui to change settings" : settings.why?.settings || "";
    const days = settings.settings?.retention_days ?? 7;
    return H().card("Record retention", `<div class="row"><label class="field" for="ep-days">Keep request and engine records (days) <input class="inp" id="ep-days" type="number" min="1" max="30" value="${H().esc(days)}" ${reason ? "disabled" : ""}></label>${H().button("Save retention", "", {extra: 'data-page-action="ep-save-days"', disabled: reason})}</div>${view.message ? `<p class="hint" role="status">${H().esc(view.message)}</p>` : ""}`,
      "at most 20,000 records per profile are kept in any case");
  }

  document.addEventListener("click", async event => {
    if (!event.target.closest('[data-page-action="ep-save-days"]')) return;
    const value = Number(document.getElementById("ep-days")?.value);
    try {
      await H().request("observe/settings/save", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify({retention_days: value})});
      view.message = `Retention saved: ${value} days`;
    } catch (error) { view.message = `Not saved: ${error.message}`; }
    H().announce(view.message); H().refresh();
  });

  (window.HyperloomPages ||= {})["engine-panel"] = {
    title: "Engine panel",
    group: "Inference",
    order: 22,
    icon: '<rect x="3" y="4" width="14" height="12" rx="2"/><path d="M6 8h3M6 11h6M13 8h1"/>',
    endpoints: () => [docName, "observe/settings", ...engines().flatMap(charts)],
    ready: () => !!H().state.docs[docName],
    render() {
      const d = H().state.docs[docName] || {};
      const omitted = d.inventory?.omitted || {};
      const note = omitted.profiles || omitted.replicas ? `<p class="hint">Not observed: ${H().esc(omitted.profiles || 0)} profiles and ${H().esc(omitted.replicas || 0)} replicas beyond the limits (8 profiles x 8 replicas).</p>` : "";
      if (d.engines == null) return H().empty("Engine panel unavailable", d.why?.engines || "metrics store not open");
      if (!d.engines.length) return H().empty("No engine observed", (d.why?.engines || "no running profile") + (note ? " · some profiles were not observed (limits)" : ""));
      return `<div class="grid">${note}${d.engines.map(card).join("")}${retention()}</div>`;
    },
    after(main) { window.HyperloomTS.bind(main); },
  };
})();
