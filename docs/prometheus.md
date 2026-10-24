# Prometheus re-export

`hyperloom metrics` re-exports the router and engine telemetry that the portal already reads, in the Prometheus text format (version 0.0.4). It adds no new measurements: every value comes from a component endpoint through the pinned adapters (SPEC §3.2).

```
hyperloom metrics dump [profile]                          # print the exposition once
hyperloom metrics serve [profile] [--listen 127.0.0.1:18701]   # serve GET /metrics
```

## Sources

For every profile with saved running state (`hyperloom up`), one scrape reads:

| Source label | Endpoint | Adapter |
|---|---|---|
| `router` | router `/api/status` (`tensormeld/replica-router-status-v1`) | `adapters.router` |
| `engine_metrics` | each replica's `/metrics` | `adapters.engine.parse_metrics` |
| `engine_status` | each replica's `/api/status` (`strix-serve-status-v1`) | `adapters.engine.parse_status` |

Replica endpoints come from the router status. When the router does not answer, no engine is scraped for that profile.
If the router reports the same replica id twice, only the first record is exported and scraped, and
`hyperloom_router_duplicate_ids{kind="replica"}` counts the others (the same for `kind="prefill_service"`).

Phase 3a prefill services are exported from the router's view only (`prefill.services` in its status:
state, in-flight and assigned/completed/failed counts). The service itself is not fetched: it has no `/metrics` or
`/api/status` yet (SPEC R13) and its `/v1/describe` needs the service key.

- Each source reports `hyperloom_source_up` (1 or 0) and `hyperloom_source_scrape_seconds`.
- A value that is missing, null, invalid or non-finite is **left out**; it is never written as 0. An unsupported schema marks the source down.
- Each fetch is bounded (2 s per socket operation, 1 MiB). All routers are fetched in parallel, then all replica
  sources in parallel (at most 8 at once), against one **8 s deadline** for the whole scrape, below Prometheus'
  default 10 s scrape timeout. A source still unfinished at the deadline is reported with `hyperloom_source_up 0`.
- Any upstream failure (refused or reset connection, timeout, malformed HTTP, bad JSON, unsupported schema, an
  endpoint without address or port) marks only that source down. If building the exposition itself fails, the
  server answers 500 rather than dropping the connection.
- At most 2 scrapes run at once; a further request gets `503` with `Retry-After: 5`. A scrape keeps no state.

## Security

- The listener accepts only a **loopback** address; any other listen address is refused. LAN exposure is a later phase.
- Only `GET /metrics` is served (a query string is ignored); the `Host` header must be the listen address or `localhost` with the same port; other methods get 405.
- No key or key path appears in the output. Engine `/metrics` and `/api/status` are the components' public endpoints.

Scrape configuration for a Prometheus running on the same machine:

```yaml
scrape_configs:
  - job_name: hyperloom
    scrape_interval: 15s
    static_configs:
      - targets: ["127.0.0.1:18701"]
```

## Metrics

Labels: `profile`; replicas add `replica` and `machine` (the router's replica id and node).

| Metric | Type | Extra labels | From |
|---|---|---|---|
| `hyperloom_build_info` | gauge (1) | `version`, `engine_commit`, `tensormeld_commit` | `versions.lock` |
| `hyperloom_source_up` | gauge | `source` | each fetch |
| `hyperloom_source_scrape_seconds` | gauge | `source` | each fetch |
| `hyperloom_router_waiting` | gauge | | router `waiting` |
| `hyperloom_router_rejected_total` | counter | | router `rejected` |
| `hyperloom_router_held` | gauge | | router `held` |
| `hyperloom_router_duplicate_ids` | gauge | `kind` (`replica`, `prefill_service`) | repeated ids in the router status |
| `hyperloom_router_replica_up` | gauge | | replica `state == "up"` |
| `hyperloom_router_replica_admitting` | gauge | | replica `admitting` |
| `hyperloom_router_replica_inflight`, `_max_inflight` | gauge | | replica |
| `hyperloom_router_replica_{assigned,completed,failed}_total` | counter | | replica |
| `hyperloom_router_replica_last_health_seconds` | gauge | | replica `last_health_ms` / 1000 |
| `hyperloom_router_prefill_placements_total` | counter | `placement` | router prefill placements |
| `hyperloom_router_prefill_offload_seconds_total` | counter | | router `offload_ms_total` / 1000 |
| `hyperloom_router_prefill_service_up` | gauge | `service`, `machine` | router `prefill.services[].state == "up"` |
| `hyperloom_router_prefill_service_inflight`, `_max_inflight` | gauge | `service`, `machine` | router `prefill.services[]` |
| `hyperloom_router_prefill_service_{assigned,completed,failed}_total` | counter | `service`, `machine` | router `prefill.services[]` |
| `hyperloom_engine_{completed,failed,cancelled,prompt_tokens,cached_tokens,generated_tokens,busy_seconds,batch_steps,batch_rows,group_exchanges}_total` | counter | | engine `/metrics` (`strix_*`) |
| `hyperloom_engine_{waiting,running,slots_busy}` | gauge | | engine `/metrics` |
| `hyperloom_engine_info` | gauge (1) | `model`, `trellis_identity` | engine `/api/status` |
| `hyperloom_engine_state` | gauge (1) | `state` | engine `/api/status` `state` (its own series, so a state change does not churn `_info`) |
| `hyperloom_engine_uptime_seconds`, `_context_tokens` | gauge | | engine `/api/status` |
| `hyperloom_engine_slots` | gauge | `kind` (`total`, `busy`, `free`) | DeepSeek `slots.*` (slot counts) |
| `hyperloom_engine_slot_positions` | gauge | `kind` (`context_tokens`, `pool_positions`, `pool_largest_free`) | DeepSeek `slots.*` (token positions) |
| `hyperloom_engine_slot_pool_bytes` | gauge | | DeepSeek `slots.pool_bytes` |
| `hyperloom_engine_memory_bytes` | gauge | `kind` | DeepSeek `memory.*` |
| `hyperloom_engine_group_bytes_sent_total`, `_group_exchange_seconds_total` | counter | | DeepSeek `group.*` |

Counter resets follow the engines: a restarted engine starts its counters again, which Prometheus `rate()` handles. Engine-side figures are not client measurements; client TTFT and throughput stay in bench receipts.
