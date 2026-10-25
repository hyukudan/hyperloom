// Renders one observability page against documents given as JSON on stdin, with the smallest page shell the page
// needs, and prints the HTML. Used by tests/test_observe_pages.py.
"use strict";
const fs = require("fs");
const path = require("path");
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
global.document = {addEventListener() {}, getElementById() { return null; }, querySelector() { return null; }};
global.localStorage = {getItem: () => input.storage ? JSON.stringify(input.storage) : null, setItem() {}};
global.window = {
  Hyperloom: {
    state: {docs: input.docs}, esc,
    card: (title, body, sub) => `<section><h3>${esc(title)}</h3>${body}${sub ? `<small>${esc(sub)}</small>` : ""}</section>`,
    unavailable: why => `<span class="unavailable">unavailable · ${esc(why)}</span>`,
    display: (value, why) => value == null ? `<span class="unavailable">unavailable · ${esc(why)}</span>` : esc(value),
    table: (head, rows) => `<table><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr>${rows}</table>`,
    cells: values => `<tr>${values.map(v => `<td>${v}</td>`).join("")}</tr>`,
    empty: (title, why) => `<div class="empty">${esc(title)} · ${esc(why)}</div>`,
    statusPill: state => `<span>${esc(state)}</span>`,
    button: () => "<button></button>", csrf: () => "", request() {}, announce() {}, refresh() {},
  },
  HyperloomTS: {chart: () => "", lines: () => [], bind() {}},
};
require(path.resolve(__dirname, "../../../hyperloom/web/static/pages", input.page + ".js"));
process.stdout.write(window.HyperloomPages[input.name || input.page].render());
