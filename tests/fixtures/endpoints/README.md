Fixture provenance
==================

- `strix-status.json`: copy of `tests/fixtures/adapters/engine-status.json` (sanitized captured engine status).
- `strix-metrics.txt`: `tests/fixtures/adapters/engine-metrics.txt` with the generated and prompt token totals set
  to the status document's totals, so both sources of one engine agree.
- `host-listeners.json`: a fleet host answer as the fleet collector stores it (namespace `fleet/host`), reduced to
  the listener section, in the shape of the fleet package's node `host` verb.
