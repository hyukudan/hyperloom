# Asset integrity

`hyperloom assets verify PROFILE [--include-large] [--machine MACHINE] [--config PATH]`
hashes the preset's assets on its configured machines through scheduled node jobs.
Assets of 64 GiB or more require `--include-large`. Expected all-zero hashes mean
unknown: verification still computes and records a digest. Known config hashes
must match before an entry is saved. Existing lab hashing restrictions apply.

Hash jobs run at nice 19 and idle I/O priority (`ionice -c3`, required on nodes).
Linux uses `O_DIRECT` with an aligned anonymous mmap and 8 MiB requests. Only
unsupported direct I/O falls back to buffered reads with `POSIX_FADV_NOREUSE`.
Other I/O errors fail the job. Verification detects metadata changes during hashing.

A configured `bytes` of 0 means the size is not pinned (directory assets such as vocab and trellis packs);
any other value must match. A directory's bytes are the sum of its regular files. Its content digest is SHA-256
over the UTF-8 JSON sorted list `[relative_path, bytes, file_sha256]`, using POSIX
relative paths, ASCII escaping, and compact separators without a trailing newline.
Internal symlinks are followed under their relative names; escaping links, cycles,
and special files are refused. Empty directories do not affect the content digest.

The trusted cache is `%APPDATA%\hyperloom\assets-manifest.json` on Windows and
`~/.config/hyperloom/assets-manifest.json` on POSIX. Entries are keyed first by
machine, then by configured path, and contain bytes, mtime_ns, inode, dev, sha256,
and verified_at (Unix timestamp). Writes are atomic and restricted to the owner.
Directories also store a fingerprint of descendant metadata to detect changes
without reading contents. Verification does not move or modify assets.

`hyperloom assets status PROFILE [--config PATH]` compares the manifest to fresh
node stats and flags stale entries. Before any lease, `up` checks all asset sizes,
cached identities, known config digests, and known digests across pair ranks.
An unverified asset is allowed and labelled `size only` in asset status, the `up`
result, and running profile status.

This metadata cache is a convenience, not an integrity guarantee: content changed
while preserving all cached metadata needs a full `assets verify` to detect it.
