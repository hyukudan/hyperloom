# Model downloads (user-confirmed)

hyperloom never downloads or converts a model on its own (SPEC §5.8). It can show the exact pinned commands
and run them on a node after you confirm the exact plan.

## Where sources come from
`hyperloom/presets/sources.json` lists every asset of each profile:
- `download`: a repository, an **immutable 40-hex revision**, and each file with its size and sha256.
  - The source comes only from a ledger at the pins.
  - Sizes and hashes are the upstream LFS metadata at that revision.
- `unavailable`: the reason, for example a local artifact with no public immutable source.

At engine `9211b07e` (the catalog is refused if `versions.lock` pins another engine commit):

| profile | asset | source |
|---|---|---|
| qwen-replicas | `model` | `unsloth/Qwen3.8-Flash-Next-GGUF@38bb39ee97821de2c9009abb7e93950eec396e66`, `UD-Q4_K_XL/…-0000{1..4}-of-00004.gguf` (engine `SOURCES_LEDGER.md`; listed there as a candidate, not approved) |
| qwen-replicas | `sidecar`, `golden`, `calibration_tokens` | unavailable: local artifacts of the qualification |
| qwen-replicas | `vocab` | unavailable: ships in the engine source (`data/draft-vocab`) |
| ds41-pair | all | unavailable: no immutable revision of the Q2 GGUF is pinned in any ledger; drafter, overlay and packs are local artifacts |

**Merging split files is documented only.** The Qwen model comes in four split parts, and the engine reads one
GGUF (engine request R6). The merge needs a tool outside the pinned components, so hyperloom never runs it. Merge
the parts yourself into the path configured for the `model` asset. Its manifest bytes and sha256 then check the result.

## Flow
Once per node, create the directory of the asset's manifest path and mark it as dedicated to hyperloom downloads
(hyperloom never writes into a directory without this marker, so a lab model directory is never touched):
```
mkdir -p /path/of/the/asset/directory && touch /path/of/the/asset/directory/.hyperloom-assets
```
Then:
```
hyperloom assets sources qwen-replicas                         # read-only: every asset and its source or reason
hyperloom assets download qwen-replicas model --machine halo-1 # the plan; nothing runs
hyperloom assets download qwen-replicas model --machine halo-1 --confirm <code>
hyperloom assets job halo-1 <job>                              # state and log tail
hyperloom assets cancel halo-1 <job>
```

### What the plan shows
- The exact argv each file runs on the node: `nice -n 19 [ionice -c3] /usr/bin/curl --fail --location --proto =https …
  --continue-at - --output <dir>/.<name>.hyperloom-part https://huggingface.co/<repo>/resolve/<revision>/<file>`.
- The destination, the expected bytes and sha256, and the free disk space per directory (it needs 1 GiB of headroom).
- A 12-character confirmation code: the digest of the files, commands and destinations.

### What `--confirm` checks
The node recomputes the plan. The job starts only when the code is the same, so a changed curl path, revision,
destination or hash needs a new review.

## Safety
- The asset must be registered in `cluster.json` with its real bytes and sha256 first. Files are written only into
  that asset's directory. The directory must exist, hold the `.hyperloom-assets` marker (a regular file you created),
  have no symlinks, and not overlap the install root or the lab directory.
- An existing destination is never overwritten (`os.link` from the verified partial file, which fails if the name exists).
- Every file is checked (size and sha256) before it gets its final name. A mismatch deletes the partial file.
  The sha256 is read with the same hasher as `assets verify` (O_DIRECT where the filesystem supports it, so the file
  is not read through the page cache again), and the job runs at idle CPU and I/O priority.
- Each partial file is held under an exclusive lock while its job runs: a second job for the same file stops.
- A failed or interrupted command keeps the partial file, and rerunning the same plan resumes it.
- A job whose node rebooted, or whose process is gone, is reported `failed` (not `running`) by `assets job` and
  `assets cancel`; rerun the same plan to resume.
- The job is refused while a hyperloom lease of the install root is active, and in lab mode while any GPU turn is held
  (single or pair). It stops if either starts during the download or the hash (the partial file is kept). A model-sized download fills the page cache, and reclaiming a serving engine's
  registered model pages stalls its decode. Run downloads when the node is not serving or measuring.
- Only `https://huggingface.co/` URLs are built, with https-only redirects. There is no free-form shell.
