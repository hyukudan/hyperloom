/* Shared time-series charts for feature pages (loaded first; see pages/manifest.json).
   HyperloomTS.endpoint(series, {range, labels})      -> "history/<series>?range=..&l.<k>=<v>"
   HyperloomTS.lines(historyDoc, {value, label})        -> [{label, color, points: [[t, v], ...]}]
                                                           (value defaults to the document's kind: gauge avg,
                                                           delta sum, state last)
   HyperloomTS.chart({title, unit, lines, step, cadence, start, end, height, max, min, group}) -> HTML (SVG + legend + table)
   HyperloomTS.bind(root)                               -> shared crosshair for charts with the same group
   Gaps: consecutive points further apart than 2.5 x the expected spacing (the bucket step, or the sampling
   cadence when it is longer) are not joined; an isolated point is drawn as a dot (offline time is a gap, never a
   zero). The expected spacing comes from the data source, never from the data itself. */
"use strict";
(() => {
  const colors = ["--s1", "--s2", "--s3", "--s4", "--s5"];
  const esc = value => String(value).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
  const units = {bytes: [["TiB", 2 ** 40], ["GiB", 2 ** 30], ["MiB", 2 ** 20], ["KiB", 1024]], W: [["kW", 1000]], "B/s": [["GB/s", 1e9], ["MB/s", 1e6], ["kB/s", 1e3]]};
  function fmt(value, unit = "") {
    if (value == null || !Number.isFinite(value)) return "unavailable";
    for (const [name, size] of units[unit] || []) if (Math.abs(value) >= size) return `${(value / size).toFixed(value / size >= 100 ? 0 : 1)} ${name}`;
    const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
    return `${value.toFixed(digits)}${unit === "bytes" ? " B" : unit ? ` ${unit}` : ""}`;
  }
  function clock(t, span) {
    const d = new Date(t * 1000);
    return span > 2 * 86400 ? d.toLocaleDateString(undefined, {month: "short", day: "numeric"}) + " " + d.toLocaleTimeString(undefined, {hour: "2-digit"})
      : d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"});
  }
  function endpoint(series, {range = 3600, labels = {}, tier} = {}) {
    const query = new URLSearchParams({range: String(range)});
    if (tier) query.set("tier", tier);
    for (const [k, v] of Object.entries(labels)) query.set(`l.${k}`, v);
    return `history/${series}?${query}`;
  }
  function lines(doc, {value, label} = {}) {
    const index = {avg: 1, max: 2, min: 3, n: 4, sum: 5, last: 6}[value || doc?.value || "avg"] ?? 1;
    return (doc?.lines || []).map((line, i) => ({
      label: label ? label(line.labels || {}) : Object.values(line.labels || {}).join(" · ") || doc.series,
      color: colors[i % colors.length],
      points: (line.points || []).map(p => [p[0], p[index], p[7] ?? null]),
    }));
  }
  let counter = 0;
  function chart({title = "", unit = "", lines: series = [], step = 2, cadence = 0, start, end, height = 180, max, min = 0, group = ""} = {}) {
    const all = series.flatMap(s => s.points).filter(p => Number.isFinite(p[1]));
    const id = `ts-${++counter}`;
    if (!all.length) return `<figure class="ts ts-empty"><figcaption>${esc(title)}</figcaption><p class="muted">No samples in this range (unavailable, not zero).</p></figure>`;
    const t0 = start ?? Math.min(...all.map(p => p[0])), t1 = end ?? Math.max(...all.map(p => p[0]));
    const span = Math.max(1, t1 - t0);
    const top = max ?? (Math.max(...all.map(p => p[1])) * 1.1 || 1), bottom = Math.min(min, ...all.map(p => p[1]));
    // Drawn at the container's real width so axis text keeps its size on any screen.
    const W = Math.round(Math.max(300, Math.min(1600, (document.querySelector("main")?.clientWidth || 700) - 64))), H = height, L = 52, R = 8, T = 8, B = 22;
    const x = t => L + (t - t0) / span * (W - L - R), y = v => T + (1 - (v - bottom) / ((top - bottom) || 1)) * (H - T - B);
    const gap = Math.max(step || 2, cadence || 0) * 2.5;
    const paths = series.map(s => {
      const valid = s.points.filter(p => Number.isFinite(p[1]));
      let d = "", dots = "";
      valid.forEach(([t, v], i) => {
        const joinedBefore = i > 0 && t - valid[i - 1][0] <= gap, joinedAfter = i < valid.length - 1 && valid[i + 1][0] - t <= gap;
        d += `${joinedBefore ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
        if (!joinedBefore && !joinedAfter) dots += `<circle cx="${x(t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.2" fill="var(${s.color})"/>`;
      });
      return `<path d="${d}" fill="none" stroke="var(${s.color})" stroke-width="1.6" vector-effect="non-scaling-stroke"/>${dots}`;
    }).join("");
    const ticks = [0, .25, .5, .75, 1].map(f => bottom + f * (top - bottom));
    const grid = ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${L - 4}" y="${y(v) + 3}" text-anchor="end" class="ts-ax">${esc(fmt(v, unit))}</text>`).join("");
    const xt = [0, .5, 1].map(f => t0 + f * span).map((t, i) => `<text x="${x(t)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}" class="ts-ax">${esc(clock(t, span))}</text>`).join("");
    const legend = series.map(s => { const lastPoint = s.points.filter(p => Number.isFinite(p[1])).at(-1); return `<span class="ts-key"><i style="background:var(${s.color})"></i>${esc(s.label)} <b>${esc(fmt(lastPoint?.[1], unit))}</b></span>`; }).join("");
    const rowsHtml = series.flatMap(s => s.points.slice(-200).map(p => `<tr><td>${esc(s.label)}</td><td>${esc(new Date(p[0] * 1000).toLocaleString())}</td><td>${esc(fmt(p[1], unit))}</td><td>${p[2] == null ? "unavailable" : esc(Math.round(p[2] * 100) + " %")}</td></tr>`)).join("");
    const data = esc(JSON.stringify({t0, t1, L, R, W, unit, gap, series: series.map(s => ({label: s.label, color: s.color, points: s.points}))}));
    return `<figure class="ts" id="${id}"><figcaption>${esc(title)}</figcaption><div class="ts-legend">${legend}</div>
<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}: ${esc(series.length)} series" data-ts="${data}" data-ts-group="${esc(group)}" width="${W}" height="${H}">${grid}${paths}${xt}<line class="ts-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--text-3)" visibility="hidden"/></svg>
<div class="ts-readout" aria-live="off"></div><details><summary>View as table</summary><table class="tbl"><thead><tr><th>Series</th><th>Time</th><th>Value</th><th>Coverage</th></tr></thead><tbody>${rowsHtml}</tbody></table></details></figure>`;
  }
  function bind(root) {
    const svgs = [...(root || document).querySelectorAll("svg[data-ts]")];
    for (const svg of svgs) {
      if (svg.dataset.tsBound) continue;
      svg.dataset.tsBound = "1";
      svg.addEventListener("mousemove", event => {
        const meta = JSON.parse(svg.dataset.ts), box = svg.getBoundingClientRect();
        const px = (event.clientX - box.left) / box.width * meta.W;
        const t = meta.t0 + (px - meta.L) / (meta.W - meta.L - meta.R) * (meta.t1 - meta.t0);
        for (const other of svgs.filter(s => s === svg || (svg.dataset.tsGroup && s.dataset.tsGroup === svg.dataset.tsGroup))) show(other, t);
      });
      svg.addEventListener("mouseleave", () => { for (const other of svgs) { other.querySelector(".ts-cross")?.setAttribute("visibility", "hidden"); const r = other.parentElement.querySelector(".ts-readout"); if (r) r.textContent = ""; } });
    }
  }
  function show(svg, t) {
    const meta = JSON.parse(svg.dataset.ts);
    if (t < meta.t0 || t > meta.t1) return;
    const px = meta.L + (t - meta.t0) / ((meta.t1 - meta.t0) || 1) * (meta.W - meta.L - meta.R);
    const cross = svg.querySelector(".ts-cross");
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    const values = meta.series.map(s => {
      let best = null;
      for (const p of s.points) if (best == null || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      // Inside a gap there is no sample: say so instead of borrowing a distant value.
      if (!best || Math.abs(best[0] - t) > meta.gap / 2) return `${s.label}: no sample`;
      return `${s.label}: ${fmt(best[1], meta.unit)}`;
    });
    const readout = svg.parentElement.querySelector(".ts-readout");
    if (readout) readout.textContent = `${new Date(t * 1000).toLocaleTimeString()} · ${values.join(" · ")}`;
  }
  window.HyperloomTS = {endpoint, lines, chart, bind, fmt, colors};
})();
