Passive placement probe (set 8)
==============================

NVIDIA inventory reports normalized names, UUID/PCI identity, architecture, platform, driver and memory capacity.
Topology reads PCI capability limits, not the currently negotiated idle link: Linux follows the real sysfs path and
reads every hop's `max_link_speed`, `max_link_width` and endpoint `numa_node`; Windows uses CfgMgr32 LocationPaths,
bus/address, parent chain, PciDevice MaxLinkSpeed/MaxLinkWidth and Numa_Node properties without elevation.
`pci_path` is the root-to-endpoint BDF array. Generation and width are the independent minima over the complete
path. Missing path properties invalidate the path; missing link properties invalidate the affected minimum.

`via` uses recognized bridge identities only. Known chipset bridges yield `chipset`; a completely recognized CPU
path yields `direct`; unrecognized roots/switches remain `unknown`. This conservative table is intentionally not a
guess based on the number of bridges. An unknown topology cannot supply a matching transfer calibration.

Placement candidate reports preserve `pci_path`, `link: {gen, width}`, `via`, `numa_node` and the top-level
`xfer_host`, `file_store`, `engram_store` keys. Passive host identity contains CPU model, socket and NUMA counts,
without a hostname. The pinned policy and store provenance require explicit local inputs; missing values stay
`unknown`. No transfer, storage or full-manifest measurements are added here. The later explicitly gated probe
commands own those measurements and cache refreshes.

The live placement source resolves the published decode release executable and passes it to
`facts.gather(release_executable=...)` for the advisory NVAPI DRS read of that exact release.
It creates/loads a DRS session, finds the executable's profile, enumerates settings and destroys the session;
it exposes no settings/profile mutation or save function. A basename alone is insufficient: the returned application
must match the full release path or explicitly scope its filename to that directory. The driver read runs in a
hidden helper with a five-second timeout. Missing, unreadable, mismatched or timed-out policy is `unknown`.
The selected WDDM placement device requires `Prefer No Sysmem Fallback`; unknown/allowed policy refuses placement.
Legacy evaluation never requests this check. Without a published placement release executable, the policy stays
unknown. The engine independently checks the effective policy at every placement-mode start.

CPU-only tests: `python -m pytest -q tests/test_placement_probe.py tests/test_recipes_grants.py`.
All fixture identities and absolute executable paths are generated at runtime. Probe documents carrying live
UUIDs, BDFs or executable paths are local data and must not be committed. Native GPU/driver qualification remains
outside these fake-API tests.
