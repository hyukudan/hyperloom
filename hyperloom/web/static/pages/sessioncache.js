"use strict";
(() => {
  const H = () => window.Hyperloom;
  const number = (n, why) => n == null ? H().unavailable(why || "not reported by this engine") : H().esc(n);
  const bytes = (n, why) => n == null ? number(n, why) : H().esc((n / 1048576).toFixed(1) + " MiB");
  function profile(p, staleAfter) {
    const {esc, card} = H(), s = p.stats || {}, why = s.why || {};
    const stale = p.as_of == null || Date.now() / 1000 - p.as_of > staleAfter;
    let body = '<p class="hint">' + esc(p.engine) + ' · As of ' + (p.as_of == null ? 'unknown' : esc(new Date(p.as_of * 1000).toLocaleString())) + (stale ? ' · stale' : '') + '</p>';
    if (p.supported !== true) return card(p.profile, body + '<p>' + esc(p.supported === false ? 'Unsupported: ' : 'Unavailable: ') + esc(Object.values(p.why || {}).join('; ') || 'not collected yet') + '</p>');
    body += '<div class="sessioncache-strip"><div>RAM used / budget<strong>' + bytes(s.ram_bytes, why.ram_bytes) + ' / ' + bytes(s.ram_budget_bytes, why.ram_budget_bytes) + '</strong>' + (s.ram_bytes != null && s.ram_budget_bytes > 0 ? '<meter aria-label="RAM used" min="0" max="' + s.ram_budget_bytes + '" value="' + Math.min(s.ram_bytes, s.ram_budget_bytes) + '"></meter>' : '') + '</div><div>Entries / limit<strong>' + number(s.entries, why.entries) + ' / ' + number(s.entry_limit, why.entry_limit) + '</strong></div><div>Evictions<strong>' + number(s.evictions, why.evictions) + '</strong></div><div>Hits / misses<strong>' + number(s.hits, why.hits) + ' / ' + number(s.misses, why.misses) + '</strong></div></div>';
    const rows = (p.sessions || []).map(r => '<tr><th scope="row" class="mono">' + esc(r.id) + '<div class="hint">' + esc(r.replica || '') + '</div></th><td>' + esc(r.location) + '</td><td>' + number(r.tokens ?? r.position, r.why?.tokens) + '</td><td>' + bytes(r.bytes, r.why?.bytes) + '</td><td>' + number(r.last_used, r.why?.last_used) + '</td></tr>').join('');
    body += '<div class="sessioncache-scroll" role="region" tabindex="0" aria-label="Saved sessions for ' + esc(p.profile) + '"><table><caption>Saved sessions</caption><thead><tr><th scope="col">ID</th><th scope="col">Where</th><th scope="col">Tokens / position</th><th scope="col">Size</th><th scope="col">Last used</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    if (!rows) body += '<p>No session details available.</p>';
    const c = p.coverage || {};
    body += '<p class="hint">' + esc(c.scope || '') + '</p>';
    body += '<p class="hint">Showing ' + esc(c.returned ?? 0) + ' of ' + esc(c.parsed ?? 0) + ' parsed sessions · limit 256 · ' + esc(c.order || '') + (c.complete ? ' · complete' : ' · partial coverage') + '</p>';
    for (const source of p.sources || []) body += '<p class="hint">' + esc(source.replica) + ': ' + esc(Object.values(source.why || {}).join('; ')) + '</p>';
    return card(p.profile, body);
  }
  (window.HyperloomPages ||= {}).sessioncache = {
    title: "Session cache", group: "Inference", order: 55,
    icon: '<ellipse cx="10" cy="4" rx="7" ry="2"/><path d="M3 4v12c0 3 14 3 14 0V4M3 10c0 3 14 3 14 0"/>',
    endpoints: () => ["sessioncache"],
    render() {
      const {card, unavailable} = H(), d = H().state.docs.sessioncache;
      return '<div class="sessioncache">' + card('Session cache', '<p>Restoring a saved session skips the prompt prefill.</p><p class="hint">Actions: later step · needs a qualified engine build</p>') + (!d ? card('Saved sessions', unavailable(H().state.errors.sessioncache || 'loading')) : d.why?.profiles || d.profiles == null ? card('Saved sessions', unavailable(d.why?.profiles)) : d.profiles.length ? d.profiles.map(p => profile(p, d.stale_after_s)).join('') : card('Saved sessions', '<p>No running profiles collected yet.</p>')) + '</div>';
    }
  };
})();
