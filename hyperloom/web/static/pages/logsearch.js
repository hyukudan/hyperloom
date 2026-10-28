/* Log search (F37): literal text or a restricted regex over hyperloom logs and configured lab directories on every
   machine; per-machine deadlines and budgets; secrets redacted; a POST query (paths never leave the server). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const UI = () => window.HyperloomOpsUI;
  const windows = [["15 min", 900], ["1 h", 3600], ["6 h", 21600], ["24 h", 86400], ["7 days", 604800], ["any time", 0]];
  const view = {text: "", regex: false, ignore_case: false, machines: null, window_s: 86400, process: "", level: "any",
    context: 2, busy: false, result: null, error: ""};

  function machineList() { return H().state.docs.logsearch?.machines || []; }

  function form() {
    const {esc, button} = H();
    const machines = machineList();
    const chosen = view.machines || machines.filter(m => m.roots?.length).map(m => m.machine);
    const boxes = machines.map(m => `<label class="check" for="ls-m-${esc(m.machine)}"><input type="checkbox" id="ls-m-${esc(m.machine)}" data-page-action="ls-machine" value="${esc(m.machine)}" ${chosen.includes(m.machine) ? "checked" : ""} ${m.roots?.length ? "" : "disabled"}>${esc(m.machine)} <span class="ops-note">${m.roots?.length ? esc(m.roots.map(r => r.label).join(", ")) : esc(m.why?.roots || "nothing to search")}</span></label>`).join("");
    const blocker = !view.text.trim() ? "Type the text to search for" : !chosen.length ? "Choose at least one machine" : view.busy ? "A search is running" : "";
    return `<form class="ls-form" id="ls-form" novalidate>
      <div class="row"><label class="field ls-text" for="ls-text">Search for<input class="inp" id="ls-text" data-page-action="ls-field" data-key="text" value="${esc(view.text)}" maxlength="128" autocomplete="off" spellcheck="false"></label>
      <div class="row ls-toggles"><label class="check" for="ls-regex"><input type="checkbox" id="ls-regex" data-page-action="ls-field" data-key="regex" ${view.regex ? "checked" : ""}>Regex</label>
      <label class="check" for="ls-case"><input type="checkbox" id="ls-case" data-page-action="ls-field" data-key="ignore_case" ${view.ignore_case ? "checked" : ""}>Ignore case</label></div></div>
      ${view.regex ? `<p class="ops-note">Regex rules: ${esc((H().state.docs.logsearch?.regex_rules || []).join(" · "))}</p>` : ""}
      <div class="opts">
        <label class="field" for="ls-window">Files modified in<select class="inp" id="ls-window" data-page-action="ls-field" data-key="window_s">${windows.map(([l, s]) => `<option value="${s}" ${view.window_s === s ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
        <label class="field" for="ls-process">File name contains<input class="inp" id="ls-process" data-page-action="ls-field" data-key="process" value="${esc(view.process)}" maxlength="64" placeholder="e.g. router" spellcheck="false"></label>
        <label class="field" for="ls-level">Level<select class="inp" id="ls-level" data-page-action="ls-field" data-key="level">${["any", "warning", "error"].map(l => `<option ${view.level === l ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="field" for="ls-context">Context lines<select class="inp" id="ls-context" data-page-action="ls-field" data-key="context">${[0, 1, 2].map(c => `<option ${view.context === c ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      </div>
      <fieldset class="ls-machines"><legend class="ops-note">Machines</legend>${boxes || H().unavailable("no machine configured")}</fieldset>
      <div class="row">${button(view.busy ? "Searching…" : "Search", "", {style: "pri", disabled: blocker, extra: 'data-page-action="ls-search" id="ls-search"'})}<span class="ops-note">Each machine stops at 5 s, 64 MiB scanned or 2,000 files; at most 500 results; secrets are redacted.</span></div>
      ${view.error ? `<p class="field-error" role="alert">${esc(view.error)}</p>` : ""}
    </form>`;
  }

  function results() {
    const {esc, card, unavailable, statusPill} = H();
    const r = view.result;
    if (!r) return "";
    const status = (r.machines || []).map(m => {
      const rep = m.report || {};
      const text = m.error ? `error: ${m.error}` : `${rep.files ?? "?"} files · ${((rep.bytes ?? 0) / 1048576).toFixed(1)} MiB · ${rep.results ?? 0} results · ${rep.elapsed_s ?? "?"} s${rep.truncated?.length ? " · stopped at " + rep.truncated.join(", ") : ""}`;
      const refused = (rep.roots || []).filter(x => x.error).map(x => `${x.root}: ${x.error}`);
      return `<li><b>${esc(m.machine)}</b> ${m.error ? statusPill("error") : ""} <span class="ops-note">${esc(text)}</span>${refused.length ? `<div class="ops-note">${esc(refused.join(" · "))}</div>` : ""}</li>`;
    }).join("");
    const hits = (r.results || []).map(h => {
      const lines = [...(h.before || []).map(l => `<span class="c">${esc(l)}</span>`), `<span class="m">${esc(h.text)}${h.cut ? " …" : ""}</span>`, ...(h.after || []).map(l => `<span class="c">${esc(l)}</span>`)].join("\n");
      return `<div class="ls-hit"><div class="where">${esc(h.machine)} · ${esc(h.root)} / ${esc(h.file)}:${esc(h.line)} · file modified ${esc(UI().date(h.mtime) || "?")}</div><pre class="ls-lines">${lines}</pre></div>`;
    }).join("");
    return card(`Results (${(r.results || []).length}${r.truncated ? ", more not shown" : ""})`, `<p class="ops-note" role="status">Search finished: ${esc((r.results || []).length)} result(s).</p><ul class="ops-steps">${status}</ul>${hits || `<p>${unavailable(r.why?.results || "no match")}</p>`}`, `searched in ${r.elapsed_s} s`);
  }

  function rootsCard() {
    const {esc, card, table, cells, button} = H();
    const rows = machineList().filter(m => ["ssh", "local"].includes(m.access));
    return card("Searched directories", table(["Machine", "Hyperloom logs", "Configured lab roots", ""], rows.map(m => cells([
      esc(m.machine), esc((m.roots || []).filter(r => !r.id.startsWith("root-")).map(r => r.label).join(", ") || "—"),
      `<span class="ops-mono">${esc((m.configured || []).join("\n") || "none")}</span>`,
      button("Edit…", "", {extra: `data-page-action="ls-roots" data-machine="${esc(m.machine)}"`})])).join(""), "Searched directories"),
      "links, system and credential directories are always refused");
  }

  (window.HyperloomPages ||= {}).logsearch = {
    title: "Log search",
    group: "Operations",
    order: 41,
    icon: '<circle cx="8.5" cy="8.5" r="5"/><path d="M12.5 12.5L17 17"/><path d="M6.5 8.5h4"/>',
    endpoints: () => ["logsearch"],
    render() {
      return `${H().card("Search logs", form(), "hyperloom logs and configured lab directories")}${results()}${rootsCard()}`;
    },
  };

  async function search() {
    const machines = view.machines || machineList().filter(m => m.roots?.length).map(m => m.machine);
    const args = {[view.regex ? "regex" : "text"]: view.text.trim(), machines, level: view.level, context: view.context};
    if (view.ignore_case) args.ignore_case = true;
    if (view.window_s) args.window_s = view.window_s;
    if (view.process.trim()) args.process = view.process.trim();
    view.busy = true; view.error = ""; H().render();
    try { view.result = await UI().post("logsearch/query", args); }
    catch (error) { view.error = error.message; view.result = null; }
    finally { view.busy = false; H().render(); }
  }

  document.addEventListener("input", event => {
    const el = event.target;
    if (el.dataset?.pageAction !== "ls-field" || !["text", "process"].includes(el.dataset.key)) return;
    view[el.dataset.key] = el.value; H().render();
  });
  document.addEventListener("change", event => {
    const el = event.target;
    if (el.dataset?.pageAction === "ls-field") {
      const key = el.dataset.key;
      view[key] = el.type === "checkbox" ? el.checked : ["window_s", "context"].includes(key) ? Number(el.value) : el.value;
      H().render();
    } else if (el.dataset?.pageAction === "ls-machine") {
      const chosen = new Set(view.machines || machineList().filter(m => m.roots?.length).map(m => m.machine));
      if (el.checked) chosen.add(el.value); else chosen.delete(el.value);
      view.machines = [...chosen]; H().render();
    }
  });
  document.addEventListener("submit", event => {
    if (event.target.id !== "ls-form") return;
    event.preventDefault();
    if (!document.getElementById("ls-search")?.disabled) search();
  });
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action]");
    if (!control || control.disabled) return;
    if (control.dataset.pageAction === "ls-search") { search(); return; }
    if (control.dataset.pageAction !== "ls-roots") return;
    const machine = machineList().find(m => m.machine === control.dataset.machine);
    if (!machine) return;
    const esc = H().esc;
    UI().open({
      title: `Lab roots on ${machine.machine}`, confirm: "Save",
      body: `<p class="ops-note">One absolute directory per line (at most 16). The node checks each again: links, system and credential directories are refused.</p><label class="field" for="ls-roots-text">Directories<textarea class="inp" id="ls-roots-text" spellcheck="false">${esc((machine.configured || []).join("\n"))}</textarea></label>`,
      submit: () => UI().post("logsearch/roots", {machine: machine.machine, roots: document.getElementById("ls-roots-text").value.split("\n").map(s => s.trim()).filter(Boolean)}),
    });
  });
})();
