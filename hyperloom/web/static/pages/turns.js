/* GPU turns (F35): who holds each node's GPU, verified waiting tickets with estimates, recent turns, the pair queue
   and the big-job ledger. Read-only; also defines the small dialog/request helpers the operations pages share. */
"use strict";
(() => {
  const H = () => window.Hyperloom;

  // Shared helpers of the operations pages (turns, log search, operations).
  const UI = window.HyperloomOpsUI ||= (() => {
    let opener = null, current = null;
    const esc = value => H().esc(value);
    function dur(seconds) {
      if (typeof seconds !== "number" || !Number.isFinite(seconds)) return null;
      const sign = seconds < 0 ? "-" : "", s = Math.round(Math.abs(seconds));
      const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
      return sign + (h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`);
    }
    const show = (value, why, suffix = "") => value == null ? H().unavailable(why) : esc(value) + suffix;
    const time = epoch => typeof epoch === "number" ? new Date(epoch * 1000).toLocaleTimeString() : null;
    const date = epoch => typeof epoch === "number" ? new Date(epoch * 1000).toLocaleString() : null;
    async function post(action, args) {
      const token = H().csrf();
      if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
      return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token}, body: JSON.stringify(args)});
    }
    function element() {
      let dialog = document.getElementById("ops-dialog");
      if (dialog) return dialog;
      dialog = document.createElement("dialog");
      dialog.id = "ops-dialog";
      dialog.className = "ops-dialog";
      dialog.setAttribute("aria-labelledby", "ops-dialog-title");
      document.body.append(dialog);
      dialog.addEventListener("close", () => { current = null; if (opener?.isConnected) opener.focus(); });
      dialog.addEventListener("click", event => { if (event.target.closest("[data-ops-close]")) dialog.close(); });
      dialog.addEventListener("input", gate);
      dialog.addEventListener("keydown", event => {
        if (event.key === "Enter" && current?.danger && event.target.tagName !== "TEXTAREA") event.preventDefault();
      });
      dialog.addEventListener("submit", async event => {
        event.preventDefault();
        if (!current || document.getElementById("ops-dialog-confirm").disabled) return;
        const error = document.getElementById("ops-dialog-error"), button = document.getElementById("ops-dialog-confirm");
        error.textContent = ""; button.disabled = true;
        try {
          const keep = await current.submit(dialog.querySelector("form"));
          if (keep !== true) { dialog.close(); H().refresh(); }
        } catch (failure) { error.textContent = failure.message; }
        finally { if (dialog.open) { button.disabled = false; gate(); } }
      });
      return dialog;
    }
    function gate() {
      const field = document.getElementById("ops-confirm-id"), button = document.getElementById("ops-dialog-confirm");
      if (!button || !current) return;
      const blocked = current.expect != null && field?.value !== current.expect;
      button.disabled = blocked;
      button.title = blocked ? `Type ${current.expect} to confirm` : "";
    }
    function open({title, body, confirm = "Confirm", danger = false, expect = null, submit}) {
      const dialog = element();
      opener = document.activeElement;
      current = {submit, danger, expect};
      const typed = expect == null ? "" : `<label class="field" for="ops-confirm-id">Type <b class="ops-confirm-id">${esc(expect)}</b> to confirm<input class="inp" id="ops-confirm-id" autocomplete="off" spellcheck="false"></label>`;
      dialog.innerHTML = `<form class="ops-form" novalidate><div class="row between"><h2 id="ops-dialog-title">${esc(title)}</h2><button type="button" class="btn" data-ops-close aria-label="Close dialog">Close</button></div>${body}${typed}<p class="field-error" id="ops-dialog-error" aria-live="polite"></p><div class="row dialog-actions"><button type="button" class="btn" data-ops-close>Cancel</button><button type="submit" class="btn ${danger ? "dan" : "pri"}" id="ops-dialog-confirm">${esc(confirm)}</button></div></form>`;
      dialog.showModal();
      gate();
      (dialog.querySelector("input:not([type=checkbox]),textarea,select") || document.getElementById("ops-dialog-confirm"))?.focus();
    }
    addEventListener("hashchange", () => { const dialog = document.getElementById("ops-dialog"); if (dialog?.open) dialog.close(); });
    const kv = entries => `<dl class="ops-kv">${entries.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
    return {dur, show, time, date, post, open, kv, esc};
  })();

  const state = () => H().state.docs.turns || {};

  function holderBlock(node) {
    const {esc, statusPill, unavailable} = H(), h = node.holder;
    if (!h) return `<div class="ops-holder"><div class="big">GPU free</div><p class="ops-note">${esc(node.why?.holder || "no holder file")}</p></div>`;
    if (!h.parsed) return `<div class="ops-holder">${unavailable(node.why?.holder || "holder line not recognised")}${h.raw ? `<p class="ops-mono">${esc(h.raw)}</p>` : ""}</div>`;
    const left = h.past_cap ? statusPill("past its cap") : UI.show(UI.dur(h.remaining_s), "cap unknown", " left");
    const live = h.alive === false ? `<p class="field-error">${esc(h.why_alive)}</p>` : h.verified === false && h.alive ? `<p class="ops-note">${esc(h.why_alive)}</p>` : "";
    return `<div class="ops-holder"><div class="row between"><span class="big">${esc(h.label)}</span><span class="row">${H().tag(h.kind)}${H().tag(h.side)}${h.pair_state ? H().tag(h.pair_state) : ""}</span></div>`
      + UI.kv([["Since", esc(h.since_clock) + " (node clock)"], ["Elapsed", UI.show(UI.dur(h.elapsed_s), "unknown")],
        ["Cap", `${UI.show(h.cap_s, "unknown", " s")} <span class="ops-note">· ${esc(h.cap_source || "")}</span>`], ["Time left", left]]) + live + `</div>`;
  }

  function flags(node) {
    const f = node.flags || {}, out = [];
    if (f.maintenance) out.push(`maintenance: ${f.maintenance}`);
    if (f.pair_pending) out.push(`pair turn pending since ${UI.time(f.pair_pending)}`);
    if (f.peer_down) out.push("peer marked down");
    return out.length ? `<p class="row">${out.map(text => H().tag(text)).join("")}</p>` : "";
  }

  function waitingTable(rows, label) {
    const {table, cells, esc} = H();
    if (!rows?.length) return `<p class="ops-note">No verified waiting ticket.</p>`;
    return table(["#", "Label", ["Kind", "hide-n"], "Waited", "Estimated start"], rows.map(t => cells([
      esc(t.position), t.label ? esc(t.label) : H().unavailable(t.label_why), `<span class="hide-n">${esc(t.kind)}</span>`, UI.show(UI.dur(t.waited_s), "unknown"),
      t.estimated_start_s == null ? H().unavailable("not enough recent turns") : `in ${esc(UI.dur(t.estimated_start_s))} <span class="ops-note">estimate</span>`])).join(""), label);
  }

  function recentTable(rows, machine) {
    const {table, cells, esc} = H();
    if (!rows?.length) return `<p class="ops-note">No turn in the log tail.</p>`;
    const reap = r => !r ? H().unavailable("no reap line") : r.local ? `${esc(r.local.reaped)} reaped · vram ${esc(r.local.vram_mib.join("→"))} MiB${r.warnings ? " · " + esc(r.warnings) + " warning(s)" : ""}` : esc(r.warnings ? "warning" : "—");
    return table(["Start", "Label", "Kind", "Duration", "Outcome", ["Reap", "hide-n"]], rows.slice(0, 12).map(t => cells([
      esc(t.start_clock || ""), esc(t.label), esc(t.side === "both nodes" ? "pair (both nodes)" : t.kind),
      UI.show(UI.dur(t.duration_s), t.outcome === "running" ? "running" : "no end line"), esc(t.outcome), `<span class="hide-n">${reap(t.reap)}</span>`])).join(""), `Recent turns on ${machine}`);
  }

  function nodeCard(node) {
    const {card, esc, statusPill, unavailable} = H();
    const age = node.age_s == null ? "" : ` · read ${Math.round(node.age_s)} s ago`;
    if (node.state === "unavailable") return card(node.machine, unavailable(node.why?.state), "turn queue" + age);
    const extra = [];
    if (node.unverified?.length) extra.push(`${node.unverified.length} waiting ticket(s) unverified (start time within the clock resolution of the ticket, or command line unreadable)`);
    if (node.stale_tickets) { const n = (node.stale_tickets["single-q"] || 0) + (node.stale_tickets["pair-q"] || 0); if (n) extra.push(`${n} stale ticket file(s) of processes that are gone`); }
    if (node.unconfirmed_attempts) extra.push(`${node.unconfirmed_attempts} pair lock attempt(s) not confirmed by the first node`);
    if (node.anomalies?.length) extra.push(`${node.anomalies.length} log anomaly(ies) (impossible durations)`);
    return card(node.machine, `<div class="row">${statusPill(node.state)}${node.coordinator ? H().tag("keeps the pair queue") : ""}</div>${holderBlock(node)}${flags(node)}
      <h3 class="ops-note">Waiting (single turns)</h3>${waitingTable(node.waiting, `Waiting tickets on ${node.machine}`)}
      <h3 class="ops-note">Recent turns</h3>${recentTable(node.recent, node.machine)}${extra.map(t => `<p class="ops-note">${esc(t)}</p>`).join("")}`, "turn queue" + age);
  }

  function ledgerCard(ledger) {
    const {card, esc, statusPill, unavailable, table, cells, button} = H();
    const settings = button("Ledger settings…", "", {extra: 'data-page-action="turns-settings"'});
    if (!ledger || ledger.state == null) return card("RTX PRO big-job ledger", `<p>${unavailable(ledger?.why)}</p>${settings}`, "control machine");
    const open = [...(ledger.holders || []), ...(ledger.stale || []).map(r => ({...r, stale: true}))];
    const openRows = open.length ? table(["Owner", "Since", "Elapsed", "Cap", "State"], open.map(r => cells([esc(r.owner), esc(r.clock), UI.show(UI.dur(r.elapsed_s), "unknown"), `${esc(r.cap_s)} s <span class="ops-note">${esc(r.cap_source)}</span>`, r.stale ? statusPill("stale: past its cap") : statusPill("running")])).join(""), "Open big jobs") : `<p class="ops-note">No open big job.</p>`;
    const recent = (ledger.recent || []).length ? table(["Owner", "Start", "End", "Duration"], ledger.recent.slice(0, 8).map(r => cells([esc(r.owner), esc(r.start_clock), esc(r.end_clock), UI.show(UI.dur(r.duration_s), "unknown")])).join(""), "Recent big jobs") : "";
    const anomalies = (ledger.anomalies || []).length ? `<p class="ops-note">${esc(ledger.anomalies.length)} unmatched line(s): ${ledger.anomalies.slice(-3).map(a => esc(`${a.owner} ${a.clock} (${a.why})`)).join(" · ")}</p>` : "";
    return card("RTX PRO big-job ledger", `<div class="row between">${statusPill(ledger.state === "free" ? "free" : ledger.state === "conflict" ? "conflict: more than one open" : "held")}${settings}</div>${openRows}${recent}${anomalies}<p class="ops-note">${esc(ledger.note || "")}</p>`, "control machine");
  }

  (window.HyperloomPages ||= {}).turns = {
    title: "GPU turns",
    group: "Operations",
    order: 40,
    icon: '<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/><path d="M3 3l2 2M17 3l-2 2"/>',
    endpoints: () => ["turns"],
    render() {
      const {esc, empty, card, table, cells, unavailable} = H();
      const d = state();
      const nodes = d.nodes || [];
      if (!nodes.length) return empty("No turn queue to show", d.why?.nodes || "No lab-mode node with a lab directory is configured.");
      const pair = d.pair_queue == null ? card("Pair queue", unavailable(d.why?.pair_queue), "both nodes")
        : card("Pair queue", d.pair_queue.length ? table(["#", "Kind", "Label", "Waited", "Estimated start"], d.pair_queue.map(t => cells([esc(t.position), esc(t.kind), t.label ? esc(t.label) : unavailable(t.label_why), UI.show(UI.dur(t.waited_s), "unknown"), t.estimated_start_s == null ? unavailable(t.kind === "gate" ? "a gate holds pair turns back" : "not enough recent pair turns") : `in ${esc(UI.dur(t.estimated_start_s))} <span class="ops-note">estimate</span>`])).join(""), "Pair queue") : `<p class="ops-note">No pair turn waiting.</p>`, "both nodes, kept by the first node");
      const skipped = (d.skipped || []).length ? `<p class="ops-note">Not shown: ${d.skipped.map(s => esc(`${s.machine} (${s.why})`)).join(" · ")}</p>` : "";
      return `<div class="grid">${nodes.map(nodeCard).join("")}</div><div class="grid g2 mtop">${pair}${ledgerCard(d.ledger)}</div>${skipped}${(d.notes || []).map(n => `<p class="ops-note">${esc(n)}</p>`).join("")}`;
    },
  };

  document.addEventListener("click", event => {
    if (!event.target.closest('[data-page-action="turns-settings"]')) return;
    const s = state().settings || {}, esc = H().esc;
    UI.open({
      title: "Big-job ledger settings", confirm: "Save",
      body: `<p class="ops-note">The coordination log is read on this machine only (its last 2 MiB); a big job whose line names no cap uses the default cap.</p>
        <p class="ops-note">Coordination log: <span class="mono">${esc(s.ledger_path || "not configured")}</span> (set in the configuration file as pc.turn_ledger)</p>
        <div class="two"><label class="field" for="turns-ledger-cap">Default big-job cap (s)<input class="inp" id="turns-ledger-cap" type="number" min="60" max="7200" value="${esc(s.ledger_cap_s ?? 1800)}"></label>
        <label class="field" for="turns-pair-cap">Pair-turn cap of the lab rule (s)<input class="inp" id="turns-pair-cap" type="number" min="60" max="7200" value="${esc(s.pair_cap_s ?? 480)}"></label></div>`,
      submit: () => UI.post("turns/settings", {
        ledger_cap_s: Number(document.getElementById("turns-ledger-cap").value), pair_cap_s: Number(document.getElementById("turns-pair-cap").value)}),
    });
  });
})();
