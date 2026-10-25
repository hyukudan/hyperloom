Fixture provenance (observability package)
==========================================

All files are synthetic, written from the field contracts of the components that produce them; no machine output
is included.

- `router-requests.json`: one page of the router request log (`tensormeld/router-requests-v1`): an RTX hand-off
  served as a stream, a request refused with no replica, an in-flight stream, and a non-stream answer served on a
  second decode attempt after a connect failure.
- `router-sessions.json`: the router's session view (`tensormeld/router-sessions-v1`) for the conversation of the
  first and last requests above.
- `router-metrics.txt`: router `/metrics` with the armed histogram families (TTFT, delivery gap), the request
  counter and the instance gauge.
- `engine-status-c1.json`: `adapters/engine-status.json` extended with the observability fields of an instrumented
  engine (instance object, armed flag, C8 totals, request ids `<id>.<attempt>` with `first_token_ms` and C8 counts,
  the speculative block, the prefix cache block); one older-style record without a router id.
- `engine-metrics-c1.txt`: shared engine metric names plus the imported-token counter, the identity gauges and the
  armed TTFT histogram.
- `minimax-status.json`: the MiniMax `/api/status` of an instrumented build (shared schema, instance object,
  capabilities, one completed request with a router id).
- `minimax-metrics-cand7.txt`: the plain `mm_*` lines of the current MiniMax build (no HELP/TYPE).
- `minimax-metrics-sched.txt`: the `mm_*` lines of the scheduled serve loop, with step and draft counters.
