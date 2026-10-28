Synthetic values using fields emitted by the supplied source:

- DeepSeek: ds-deepseek41_serve_main.cpp, list loop and shared stats fields in lead(): sessions (string array), ram_bytes, ram_entries, evictions, refused, seconds.
- MiniMax: mm-minimax_serve_main.cpp, dedicated session serve dispatcher: list returns sessions (string array), stats returns bytes, entries, evictions; both include ok.
- Qwen: qwen-hip_session_store.hpp, command(): list returns sessions objects (id, parked, disk); stats returns ram_bytes, ram_budget, tombstone_bytes, tombstones, disk_fallback_disabled, parked, max_parked, evicted, hits, misses. parked is RAM presence/count; RAM bytes/budget include tombstone reservation. Hits/misses count session matching, despite prose calling them prefix counts. No per-session detail or recency is emitted.

Neither DeepSeek nor MiniMax emits per-session location, tokens/position, size, generation or recency. No budget, entry limit or hit/miss count is fabricated. Bytes are local to the queried endpoint; TP2 peer bytes are not inferred.
