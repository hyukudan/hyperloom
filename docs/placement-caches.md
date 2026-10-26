Placement component descriptors and content manifests are separate runtime caches, created through
`commands.components_store()` and `commands.manifest_store()`. They live under the configured runtime directory's
`components-v1/` and `content-manifests/` directories. Operational documents and local references are never committed.

`content_cache.reference(build_sha256, flags, receipts_sha256)` identifies one exact configuration. `flags` contains
every allocation-affecting option; callers must pass the engine configuration, including defaults that affect
allocation. Keys are hashed from canonical JSON, so dictionary order does not affect identity. Any change to build,
flags or receipts selects a different entry. There is no engine invocation or automatic refresh on a miss:
`PL_E_MISSING_DESCRIPTOR` requires explicit acquisition in a granted turn. Probe acquisition and its gate/watchdog
are separate work; `save` only persists already acquired documents.

An acquired manifest uses `tierstream/content-manifest-v1` with `files` entries containing absolute local `path`,
`size`, `mtime_ns`, `sha256`, and `header_sha256`. `ManifestStore.save` persists that document without reading model
data. `ComponentsStore.save` associates a descriptor with its manifest and a nonempty mapping of selected content
roles to paths. Include every selected model shard, draft, overlay, vocabulary and other input used by this
configuration. The local cache stores that exact selection separately from the engine document.

At admission, `load(reference, selected=paths)` checks the cache key, stored document integrity, exact selected
paths and the selected files' size and nanosecond mtime. Missing or changed content refuses `PL_E_STALE` until an
explicit approved `hyperloom probe --manifest` refresh. Planning never hashes content, reads full model files or
launches an engine. The engine's header checks and receipt validation remain authoritative at load. As specified
by the contract, same-size changes with deliberately preserved mtime are outside the stat-only admission check.

The placement bridge accepts the reference as `components` with `selected_content=paths`. Solver choices may each
carry their own reference and `selected_content`. Both resolve from cache before invoking the CPU Tierstream core;
literal cached documents/files retain the bridge's existing API. `launch.Options` accepts the same local sidecar
for preflight validation before evaluation. It does not enable placement launching, which remains gated on the
later adapter/supervision work. `placement.desc.load_inputs` returns desc-v1 and cached components-v1 separately:
no reference is inserted into desc-v1, so its hash and legacy plan identity remain unchanged. Legacy header caches
also use `mtime_ns`; older second-resolution entries must be explicitly refreshed.
