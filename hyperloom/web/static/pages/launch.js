/* Shared launch view; the shell owns polling. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const seconds = value => Number.isFinite(value) ? Math.round(value) + " s" : "unknown";
  const detailText = detail => {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(p => [p.machine, p.role, p.state].filter(Boolean).join(" · ")).join("; ");
    if (detail?.stopped || detail?.stopping || detail?.requested) return [
      detail.stopped?.length ? "Stopped: " + detail.stopped.join(", ") : "",
      detail.requested?.length ? "Stop requested: " + detail.requested.join(", ") : "",
      detail.stopping ? "Stopping: " + detail.stopping : ""
    ].filter(Boolean).join("; ");
    return "";
  };
  window.HyperloomLaunch = {
    render(document) {
      if (!document) return "";
      const phases = (document.phases || []).map(p => {
        const detail = detailText(p.detail);
        return '<li' + (p.state === "running" ? ' aria-current="step"' : '') + '><strong>' + esc(p.label) + '</strong> <span>' + esc(p.state) + '</span>' + (detail || p.why ? '<p class="hint">' + esc([detail, p.why].filter(Boolean).join(" · ")) + '</p>' : '') + '</li>';
      }).join("");
      const eta = document.eta, elapsed = document.elapsed_s;
      const waiting = document.job_state === "running" && !document.failure && !document.phases?.some(p => p.id === "ready" && p.state === "done");
      const remaining = waiting && Number.isFinite(elapsed) ? (elapsed >= eta?.seconds ? "Estimate exceeded; waiting for READY." : "Estimated remaining " + seconds(eta?.seconds - elapsed)) : "";
      const bar = eta?.n > 0 && Number.isFinite(eta.seconds) ? '<label>Elapsed ' + seconds(elapsed) + ' / estimated total ' + seconds(eta.seconds) + '<progress max="' + Math.max(1, eta.seconds) + '" value="' + Math.max(0, Math.min(elapsed || 0, eta.seconds)) + '"></progress></label><p class="hint">ETA is an estimate from ' + esc(eta.n) + ' previous starts on this portal · range ' + seconds(eta.min) + '–' + seconds(eta.max) + '. ' + esc(remaining) + '</p>' : '<p>Elapsed ' + seconds(elapsed) + ' · ' + esc(eta?.why || "No start history available") + '</p>';
      return '<section class="launch-progress" aria-label="Launch progress"><ol aria-label="Launch phases">' + phases + '</ol>' + bar + (document.failure ? '<p class="field-error" role="alert">' + esc(document.failure.phase || "Unknown phase") + ': ' + esc(document.failure.reason) + '</p>' : '') + '</section>';
    }
  };
})();
