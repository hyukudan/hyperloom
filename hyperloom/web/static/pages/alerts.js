/* Alert channels (F21): webhooks and push topics; URLs are write-only and shown masked. */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const blank = () => ({id: null, name: "", kind: "webhook", url: "", min_severity: "warning", reminders: "none"});
  const view = {form: null, removing: null, busy: false, note: "", result: null};
  const doc = () => H().state.docs.alerts;
  const SEVERITY = {critical: "err", warning: "warn", info: "info"};
  async function post(action, body) {
    const token = H().csrf();
    if (!token || token === "__HYPERLOOM_CSRF__") throw new Error("Launch the portal through hyperloom ui to enable actions.");
    return H().request(action, {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": token},
      body: JSON.stringify(body), signal: AbortSignal.timeout(30000)});
  }

  function form(d) {
    const {esc, button} = H();
    const f = view.form;
    const option = (list, value) => list.map(o => `<option value="${esc(o)}" ${o === value ? "selected" : ""}>${esc(o)}</option>`).join("");
    const urlRule = "https, or http only to a numeric loopback address; no user credentials; no fragment";
    return `<section class="card hl-edit" aria-label="${f.id ? "Edit channel" : "Add a channel"}"><h2>${f.id ? `Edit · ${esc(f.name)}` : "Add a channel"}</h2><div class="grid g2 hl-form">
      <div class="field"><label for="alert-name">Name</label><input class="inp" id="alert-name" maxlength="64" data-page-action="alerts-field" data-key="name" value="${esc(f.name)}" autocomplete="off"></div>
      <div class="field"><label for="alert-kind">Kind</label><select class="inp" id="alert-kind" data-page-action="alerts-field" data-key="kind" ${f.id ? "disabled" : ""}>${option(d.kinds || ["webhook", "push"], f.kind)}</select><span class="hint">${f.id ? "The kind of a saved channel cannot change; add a new channel for another kind." : "Webhook: a JSON document per message. Push: the message text posted to a topic URL."}</span></div>
      <div class="field"><label for="alert-url">${f.id ? "New URL (leave empty to keep the stored one)" : "URL"}</label><input class="inp" type="password" id="alert-url" data-page-action="alerts-field" data-key="url" value="${esc(f.url)}" autocomplete="off" spellcheck="false" aria-describedby="alert-url-hint"><span class="hint" id="alert-url-hint">${esc(urlRule)}. Kept in the secret store; never shown again.</span></div>
      <div class="field"><label for="alert-severity">Minimum severity</label><select class="inp" id="alert-severity" data-page-action="alerts-field" data-key="min_severity">${option(d.severities || [], f.min_severity)}</select></div>
      <div class="field"><label for="alert-reminders">Reminders while a finding stays active</label><select class="inp" id="alert-reminders" data-page-action="alerts-field" data-key="reminders">${option(d.reminders || [], f.reminders)}</select></div>
    </div>${view.note ? `<p class="field-error" role="alert">${esc(view.note)}</p>` : ""}
    <div class="row">${button(f.id ? "Save channel" : "Add channel", "", {style: "pri", extra: `data-page-action="alerts-save"`, disabled: view.busy ? "Saving…" : !f.name.trim() ? "Enter a name" : !f.id && !f.url.trim() ? "Enter the channel URL" : ""})}${button("Cancel", "", {extra: `data-page-action="alerts-cancel"`})}</div>
    <p class="hint">CLI: <span class="mono">hyperloom alerts ${f.id ? `edit ${esc(f.id)}` : `add --name … --kind ${esc(f.kind)}`} --url-stdin</span> (the URL is read from standard input)</p></section>`;
  }

  (window.HyperloomPages ||= {}).alerts = {
    title: "Alert channels",
    group: "Operations",
    order: 11,
    icon: '<path d="M5 14V9a5 5 0 0 1 10 0v5l1.5 2h-13z"/><path d="M8.5 18a1.8 1.8 0 0 0 3 0"/>',
    endpoints: () => ["alerts"],
    actions: () => H().button("Add channel", "", {style: "pri", extra: `data-page-action="alerts-add"`, disabled: view.form ? "The form is open" : (doc()?.channels?.length ?? 0) >= 20 ? "At most 20 channels" : doc()?.channels == null ? "Alert channels unavailable" : ""}),
    render() {
      const {esc, card, table, cells, display, empty, button, tag} = H();
      const d = doc();
      if (d.channels == null) return empty("Alert channels unavailable", display(null, d.why?.channels || "store not open"));
      const pending = d.outbox?.per_channel || {};
      const rows = d.channels.map(c => cells([`<b>${esc(c.name)}</b> ${tag(c.kind)}<div class="hint mono hl-url">${esc(c.url_masked || "")}</div><div class="hint mono">${esc(c.id)}</div>`,
        `<span class="pill p-${SEVERITY[c.min_severity] || "off"}">${esc(c.min_severity)}</span><div class="hint">reminders ${esc(c.reminders)}</div>`,
        `<span class="hide-n">${esc(pending[c.id] || 0)}</span>`,
        `<div class="row">${button("Test", "", {extra: `data-page-action="alerts-test" data-id="${esc(c.id)}" aria-label="Send a test to ${esc(c.name)}"`, disabled: view.busy ? "Busy…" : ""})}${button("Edit", "", {extra: `data-page-action="alerts-edit" data-id="${esc(c.id)}" aria-label="Edit ${esc(c.name)}"`})}${button("Remove", "", {style: "dan", extra: `data-page-action="alerts-remove" data-id="${esc(c.id)}" aria-label="Remove ${esc(c.name)}"`})}</div>`])).join("");
      const removing = d.channels.find(c => c.id === view.removing);
      const confirm = removing ? `<div class="banner warn" role="alertdialog" aria-labelledby="remove-question"><p id="remove-question"><b>Remove ${esc(removing.name)}?</b> Its stored URL and its queued messages are deleted.</p><div class="row">${button("Confirm remove", "", {style: "dan", extra: `data-page-action="alerts-remove-confirm" id="alerts-remove-confirm"`, disabled: view.busy ? "Removing…" : ""})}${button("Cancel", "", {extra: `data-page-action="alerts-remove-cancel"`})}</div></div>` : "";
      const result = view.result ? `<div class="banner ${view.result.ok ? "info" : "warn"}" role="status">Test to ${esc(view.result.name)}: ${view.result.ok ? `delivered (HTTP ${esc(view.result.status)})` : esc(view.result.error || "failed")}</div>` : "";
      const list = d.channels.length ? table(["Channel", "From severity", ["Queued", "hide-n"], ""], rows, "Alert channels")
        : `<p>No channels yet. Findings still appear in the exceptions strip and the activity log.</p>`;
      const recent = (d.recent || []).length ? table(["When", "Result", "Message"], d.recent.map(e => cells([`<span class="mono hl-nowrap">${esc(new Date(e.ts * 1000).toLocaleTimeString())}</span><div class="hint">${esc(new Date(e.ts * 1000).toLocaleDateString())}</div>`, `<span class="pill p-${e.severity === "info" ? "ok" : "warn"}">${e.severity === "info" ? "sent" : "failed"}</span>`, esc(e.message)])).join(""), "Recent deliveries") : `<p class="hint">No deliveries yet.</p>`;
      const outbox = d.outbox ? `${esc(d.outbox.length)} queued message(s)${d.outbox.oldest_age_s != null ? ` · oldest ${esc(Math.round(d.outbox.oldest_age_s))} s` : ""}` : display(null, d.why?.outbox);
      return `${view.form ? form(d) : ""}${result}${card("Channels", list + confirm + `<p class="hint">One message per channel per evaluation with every change (firing, escalated, resolved, reminders); sent oldest first, retried after 5 s, 20 s, 60 s and then every 5 min; redirects are refused.</p>`, outbox)}${card("Recent deliveries", recent, "activity log · category alerts")}`;
    },
  };

  const rerender = () => { H().state.lastExternal = null; H().render(); };
  const inPage = control => control && (document.querySelector("main")?.contains(control) || document.getElementById("page-actions")?.contains(control));
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled || !inPage(control) || !control.dataset.pageAction.startsWith("alerts-")) return;
    const action = control.dataset.pageAction;
    const channel = (doc()?.channels || []).find(c => c.id === control.dataset.id);
    if (action === "alerts-add") { view.form = blank(); view.note = ""; rerender(); document.getElementById("alert-name")?.focus(); return; }
    if (action === "alerts-edit" && channel) { view.form = {...blank(), ...channel, url: ""}; view.note = ""; rerender(); document.getElementById("alert-name")?.focus(); return; }
    if (action === "alerts-cancel") { view.form = null; view.note = ""; rerender(); return; }
    if (action === "alerts-remove" && channel) { view.removing = channel.id; rerender(); document.getElementById("alerts-remove-confirm")?.focus(); return; }
    if (action === "alerts-remove-cancel") { view.removing = null; rerender(); return; }
    view.busy = true; view.note = ""; rerender();
    try {
      if (action === "alerts-save") {
        const f = view.form, body = {name: f.name.trim(), min_severity: f.min_severity, reminders: f.reminders};
        if (f.id) body.id = f.id; else body.kind = f.kind;
        if (f.url.trim()) body.url = f.url.trim();
        await post("alerts/channels", body);
        H().announce(`Channel ${body.name} saved`);
        view.form = null;
      } else if (action === "alerts-remove-confirm") {
        await post("alerts/channels/delete", {id: view.removing});
        H().announce("Channel removed");
        view.removing = null;
      } else if (action === "alerts-test" && channel) {
        const result = await post("alerts/test", {id: channel.id});
        view.result = {...result, name: channel.name};
        H().announce(result.ok ? `Test delivered to ${channel.name}` : `Test to ${channel.name} failed: ${result.error}`);
      }
    } catch (error) { view.note = error.message; H().announce(error.message); }
    finally { view.busy = false; if (view.form) view.form.url = view.note ? view.form.url : ""; await H().refresh(); rerender(); }
  });
  const field = event => {
    const control = event.target.closest?.('[data-page-action="alerts-field"]');
    if (control && view.form) view.form[control.dataset.key] = control.value;
  };
  document.addEventListener("input", event => { field(event); });
  document.addEventListener("change", event => {
    field(event);
    if (event.target.closest?.('[data-page-action="alerts-field"]')?.dataset.key === "name") rerender();
  });
})();
