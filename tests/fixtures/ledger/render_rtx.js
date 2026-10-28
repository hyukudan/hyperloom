// Renders the RTX prefill page against a `ledger/rtx` document given as JSON on stdin, with the smallest page shell the
// page needs, and prints the HTML. Used by tests/test_ledger_census_rtx.py.
"use strict";
const fs = require("fs");
const path = require("path");
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
global.document = {addEventListener() {}};
global.window = {
  Hyperloom: {
    state: {docs: {"ledger/rtx": input.doc}}, esc,
    card: (title, body, sub) => `<section><h3>${esc(title)}</h3>${body}${sub ? `<small>${esc(sub)}</small>` : ""}</section>`,
    unavailable: why => `<span class="unavailable">unavailable · ${esc(why)}</span>`,
    kpi: (label, value, hint) => `<div class="kpi">${esc(label)}: ${value} (${hint})</div>`,
    kv: () => "<dl></dl>",
    button: label => `<button>${esc(label)}</button>`,
    mutationReason: () => "",
  },
  HyperloomTS: {fmt: (value, unit) => `${value} ${unit}`},
};
require(path.resolve(__dirname, "../../../hyperloom/web/static/pages/rtx.js"));
process.stdout.write(window.HyperloomPages.rtx.render());
