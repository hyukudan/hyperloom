/* Hardware-ceiling meter (F12): census-v1 artifacts with measured / estimated / not determinable tags, checked
   dimension by dimension against the running profiles (a live value is set beside a cell only on a full match). */
"use strict";
(() => {
  const H = () => window.Hyperloom;
  const view = {message: "", draft: null};
  const tagClass = {M: "m", E: "e", ND: "nd"};
  const tagName = {M: "measured", E: "estimated", ND: "not determinable"};
  const num = (value, digits = 1) => value == null || !Number.isFinite(value) ? null : value.toLocaleString(undefined, {maximumFractionDigits: digits});
  function value(cell, key, unit, digits = 1) {
    const {esc, unavailable} = H();
    return cell[key] == null ? unavailable(cell.why?.[key] || "not in the artifact") : `${esc(num(cell[key], digits))}${unit ? ` ${esc(unit)}` : ""}`;
  }
  function tag(code, text) {
    const {esc} = H();
    if (!code) return `<span class="lg-tag" title="${esc(text || "")}">unavailable · tag not given</span>`;
    return `<span class="lg-tag ${tagClass[code] || ""}" title="${esc(text || tagName[code])}">${esc(code)} · ${esc(tagName[code])}</span>`;
  }
  function artifactCard(artifact) {
    const {card, esc} = H();
    const rows = artifact.cells.map(cell => {
      const match = cell.match || {};
      const dims = match.dims ? `<details><summary>dimension checks</summary><ul class="lg-insights">${Object.entries(match.dims).map(([d, v]) => `<li>${esc(d)}: ${esc(v)}</li>`).join("")}</ul></details>` : "";
      return `<tr><td class="lg-wrap">${esc(cell.cell)}</td><td class="lg-num">${value(cell, "wall_ms", "ms", 2)}${cell.wall_ms != null ? tag(cell.wall_tag, cell.wall_tag_text) : ""}</td><td class="lg-num">${value(cell, "tok_s", "tok/s")}</td><td class="lg-num">${value(cell, "bytes_gb", "GB", 3)}${cell.bytes_gb != null ? `<span class="lg-tag e" title="${esc(cell.bytes_tag)}">E</span>` : ""}</td><td class="lg-num">${value(cell, "floor_ms", "ms", 2)}</td><td class="lg-num">${value(cell, "pct_ceiling", "%")}</td><td class="lg-num">${value(cell, "ceiling_tok_s", "tok/s")}${tag(cell.ceiling_tag, cell.ceiling_tag_text)}</td><td class="lg-wrap">${H().unavailable(match.why || "not checked")}${dims}</td></tr>`;
    }).join("");
    const table = `<div class="table-wrap" tabindex="0" role="region" aria-label="Census cells of ${esc(artifact.file)}"><table class="lg-cells"><thead><tr><th scope="col">Cell</th><th scope="col" class="lg-num">Wall</th><th scope="col" class="lg-num">Measured tok/s</th><th scope="col" class="lg-num">Bytes / window</th><th scope="col" class="lg-num">Byte floor</th><th scope="col" class="lg-num">Share of the bandwidth ceiling</th><th scope="col" class="lg-num">Ceiling</th><th scope="col">Live value beside this cell</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    const notes = [...Object.values(artifact.why || {}), ...(artifact.notes || [])].map(n => `<p class="hint lg-wrap">${esc(n)}</p>`).join("");
    const dropped = artifact.cells_dropped || artifact.cells_truncated ? `<p class="hint">${esc(artifact.cells_dropped)} malformed and ${esc(artifact.cells_truncated)} excess cells not shown.</p>` : "";
    return card(`${artifact.engine} · ${artifact.sha}`, table + notes + dropped, `${artifact.file} · binary ${artifact.binsha || "unavailable"}${artifact.schema_declared ? " · census-v1" : ""}`);
  }
  function directoryCard(doc) {
    const {card, esc, button} = H();
    const draft = view.draft ?? doc.directory ?? "";
    return card("Census directory", `<form class="lg-form" data-page-form="ceiling-dir"><label class="field" for="ceiling-dir">Directory with census JSON files<input class="inp" id="ceiling-dir" name="dir" value="${esc(draft)}" autocomplete="off" spellcheck="false" placeholder="absolute path"></label><div class="row"><button type="submit" class="btn pri">Save directory</button>${doc.directory_set ? button("Clear", "", {extra: 'data-page-action="ceiling-clear"'}) : ""}</div></form>${view.message ? `<p role="status" class="hint lg-wrap">${esc(view.message)}</p>` : ""}<p class="hint">Read by the portal only from this saved directory (plain directory, no links); at most 64 files of 1 MiB.</p>`);
  }
  function live(doc) {
    const {esc, unavailable} = H();
    const entries = Object.entries(doc.live || {});
    if (!entries.length) return `<p class="muted">No running profile of a census engine.</p>`;
    return `<ul class="lg-insights">${entries.map(([name, rate]) => `<li>${esc(name)}: ${rate.tok_s == null ? unavailable(rate.why) : `<b>${esc(num(rate.tok_s))} tok/s</b> from ${esc(rate.sources)} engine(s) over ${esc(num(rate.window_s, 0))} s of counter intervals`} <span class="hint">${esc(rate.label)}</span></li>`).join("")}</ul>`;
  }

  (window.HyperloomPages ||= {}).ceiling = {
    title: "Hardware ceiling",
    group: "Benchmarks",
    order: 50,
    icon: '<path d="M3 16h14"/><path d="M4 16V9l4-3 4 2 4-4v12"/><path d="M3 4h14" stroke-dasharray="2 2"/>',
    endpoints: () => ["ledger/census"],
    render() {
      const {card, esc, empty} = H();
      const doc = H().state.docs["ledger/census"];
      const parts = [directoryCard(doc)];
      if (doc.artifacts == null) parts.push(empty("Census unavailable", doc.why?.artifacts || "no census directory"));
      else {
        parts.push(card("Live throughput", live(doc), "not compared with any cell"));
        parts.push(...doc.artifacts.map(artifactCard));
        if (!doc.artifacts.length) parts.push(empty("No census artifacts", doc.why?.artifacts || "the directory holds no census JSON files"));
      }
      if ((doc.rejected || []).length) parts.push(card("Files not read", `<ul class="lg-insights">${doc.rejected.map(r => `<li>${esc(r.file)}: ${esc(r.why)}</li>`).join("")}</ul>`));
      parts.push(...(doc.notes || []).map(n => `<p class="hint">${esc(n)}</p>`));
      parts.push(`<p class="hint">Tags: M measured, E estimated (inventory or path-model arithmetic), ND not determinable. The ceiling is bandwidth-only; its ratio is the share of the bandwidth ceiling, never spare capacity. A live value is set beside a cell only when the running build and every cell dimension (${esc((doc.required_dims || []).join(", "))}) match with evidence.</p>`);
      return `<div class="grid">${parts.join("")}</div>`;
    },
  };

  async function saveDirectory(value) {
    try {
      await H().request("ledger/settings", {method: "POST", headers: {"Content-Type": "application/json", "X-Hyperloom-CSRF": H().csrf() || ""}, body: JSON.stringify({census_dir: value})});
      view.message = value ? "Census directory saved." : "Census directory cleared.";
      view.draft = null;
    } catch (error) { view.message = `Not saved: ${error.message}`; }
    H().announce(view.message); H().refresh();
  }
  document.addEventListener("input", event => { if (event.target.id === "ceiling-dir") view.draft = event.target.value; });
  document.addEventListener("submit", event => {
    if (event.target.dataset?.pageForm !== "ceiling-dir") return;
    event.preventDefault();
    saveDirectory(event.target.elements.dir.value.trim() || null);
  });
  document.addEventListener("click", event => {
    const control = event.target.closest("[data-page-action='ceiling-clear']");
    if (control && !control.disabled) saveDirectory(null);
  });
})();
