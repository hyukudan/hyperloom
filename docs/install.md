# Installing hyperloom from scratch

This guide takes a control machine with nothing installed to a validated configuration and a passing
`doctor` on every node. Every step was run on a clean path (see [How this guide was checked](#how-this-guide-was-checked)).
Running a profile on the GPUs (`install`, `up`) needs the node toolchain and, in lab mode, the lab's
turn scripts; those steps are listed at the end.

`hloom` is a short alias for `hyperloom` everywhere.

## 1. What you need

**Control machine** (where you run `hyperloom`; Windows is the main target, Linux works too):

| Need | Windows | Linux |
|---|---|---|
| Python | 3.11, 3.12 or 3.13 (`py -3.13` or `python` on PATH) | `python3` ≥ 3.11 with `venv` |
| git | Git for Windows | distribution package |
| ssh client | the Windows OpenSSH client (Settings → Optional features), plus Git for Windows' copy, see [Troubleshooting](#troubleshooting) | `openssh-client` |
| ssh key | a key in `%USERPROFILE%\.ssh` (for example `id_ed25519`) whose public half is in each node's `~/.ssh/authorized_keys` | the same in `~/.ssh` |
| network | the nodes' private LAN addresses; PyPI once for `setuptools`, unless the release has `wheels/` | same |

**Each node** (Linux; `hyperloom doctor <machine>` checks all of this and lists every command it ran):

| Need | Checked by doctor as |
|---|---|
| `python3` ≥ 3.10, `git` | `python`, `git` |
| `cmake` ≥ 3.24, `ninja` | `cmake`, `ninja` |
| ROCm core 10.1 at `/opt/rocm/core-10.1` with `amdclang++` | `rocm_root`, `rocm_version`, `rocm_package_version`, `compiler` |
| a gfx1151 GPU visible in sysfs | `gfx_targets` |
| disk space for sources, builds and releases | `disk` |
| lab mode only: the lab directory with `gpu-turn.sh` / `gpu-turn-pair.sh` | `lab` |
| DeepSeek pair only: the USB4 stream driver and layout 1 | `usb4` |

hyperloom never installs or changes any of these, needs no root, and never touches the kernel, drivers or system units.

## 2. Get hyperloom

### From a release archive (recommended)

A release is `hyperloom-<version>-g<commit>.zip` (Windows) or `.tar.gz` (Linux) with a `SHA256SUMS` file.
It contains the product and the pinned TensorMeld source; every file is listed with its sha256 in `RELEASE.json`.

Check the archive before unpacking it:

```powershell
# Windows (PowerShell)
Get-Content SHA256SUMS
certutil -hashfile hyperloom-<version>-g<commit>.zip SHA256
Expand-Archive hyperloom-<version>-g<commit>.zip -DestinationPath .
```

```sh
# Linux
grep tar.gz SHA256SUMS | sha256sum -c -
tar xzf hyperloom-<version>-g<commit>.tar.gz
```

### From a git checkout

The repository is private; you need read access. Only the TensorMeld submodule is needed on the control machine
(the engine and strix-trellis are fetched and built on the nodes by their pinned SHAs):

```sh
git clone https://github.com/hyukudan/hyperloom.git
cd hyperloom
git submodule update --init --depth 1 third_party/tensormeld
```

## 3. Install on the control machine

### Windows

From the unpacked release (or the checkout), in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -WhatIf   # shows the interpreter, venv, pip arguments and shortcut; changes nothing
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
& "$env:LOCALAPPDATA\hyperloom\hyperloom.cmd" --version
```

What it does:

- picks one interpreter (`py -3.13`, `-3.12`, `-3.11`, then `python`) and checks `git` and `ssh.exe`;
- **release:** verifies every file against `RELEASE.json` (a changed, missing or extra file stops the install before anything is written),
  copies the release to `%LOCALAPPDATA%\hyperloom\releases\<name>`, creates `%LOCALAPPDATA%\hyperloom\venvs\<name>`,
  and makes it the active release;
- **checkout:** one venv at `%LOCALAPPDATA%\hyperloom\venv` with an editable install of the checkout;
- installs TensorMeld from the bundled pinned source, then hyperloom, from the verified copy; with a `wheels\`
  folder (verified like every other file), pip runs offline;
- writes the launchers `%LOCALAPPDATA%\hyperloom\hyperloom.cmd` and `hloom.cmd`, and a Start-menu shortcut
  **Hyperloom** that opens the portal.

Add `%LOCALAPPDATA%\hyperloom` to your user PATH if you want to type `hloom` anywhere; the rest of this guide assumes you did.

### Linux

```sh
cd hyperloom-<version>-g<commit>            # or the checkout
python3 -m venv ~/.local/share/hyperloom/venv
~/.local/share/hyperloom/venv/bin/pip install ./third_party/tensormeld -e .
ln -s ~/.local/share/hyperloom/venv/bin/hyperloom ~/.local/bin/hloom   # optional
hloom --version
```

Keep the unpacked directory: the install is editable, and the node tool is copied from it.

There is no Linux installer: `sha256sum -c` checks the archive as a whole, but nothing checks the unpacked
files against `RELEASE.json` (the Windows installer does). Unpack into an empty directory and do not edit it.

## 4. Describe the cluster

The configuration lives in `%APPDATA%\hyperloom\cluster.json` (Linux: `~/.config/hyperloom/cluster.json`).
It never holds secrets, only paths. Start from [`examples/cluster.example.json`](../examples/cluster.example.json):

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\hyperloom" | Out-Null
Copy-Item examples\cluster.example.json "$env:APPDATA\hyperloom\cluster.json"
notepad "$env:APPDATA\hyperloom\cluster.json"
hloom config validate "$env:APPDATA\hyperloom\cluster.json"
```

Per node, set:

- `http_address`: the node's **numeric** private IP. Hostnames are not accepted, and the ssh connection also goes to this address.
- `ssh_target`: **`<user>@<name>`**. hyperloom runs ssh with its own minimal config, so aliases in `~/.ssh/config`
  are not used: only the user part matters, and the host part is a label. A bare name means your local user name.
- `ssh_port` if it is not 22.
- `install_root`: an absolute path the node user owns or can create, for example `/home/<user>/hyperloom`. It must
  not be or contain your home directory, not be inside a git checkout, and not overlap the lab directory or any asset path.
  hyperloom marks it and writes only inside it (plus `~/.hyperloom-boot` for the node tool).
- `lab_mode` and `lab_dir`: in the lab, `lab_mode: true` (the default) and the lab directory that holds the turn scripts;
  every GPU process then runs inside a GPU turn. This guide covers lab mode only.
- `assets`: per node, each file's path, size in bytes and sha256. Files are never downloaded or converted automatically.

`config validate` rejects unknown keys, non-private addresses and malformed values, and names the field.

## 5. Pair each node (host key)

hyperloom keeps its own `known_hosts` and connects only to a key you confirmed:

```powershell
hloom hosts scan halo-1                       # candidate keys with their SHA256 fingerprints; nothing is trusted yet
```

On the node itself (console or an ssh session you already trust), print the real fingerprint and compare:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Then pin exactly that key:

```powershell
hloom hosts pin halo-1 --fingerprint SHA256:<the fingerprint you compared>
```

A later change of that host key is a hard failure until you pin again.

## 6. Check every node

```powershell
hloom doctor halo-1
hloom doctor halo-2
```

`doctor` changes nothing on a node. On a node without the node tool it says so (`node_tool.status: absent`) and
stops: the tool reaches a node only through `hloom nodes update <profile> --machine <machine>`, which copies it to
`~/.hyperloom-boot` (fixed commands: `mkdir`, `scp`, `chmod`); a machine without the tool has no lease, so no
`--force` is needed there. With the tool in place, `doctor` runs the read-only checks: each is `ok` or `unavailable`
with the reason, and `commands` lists everything that ran. `node_tool` compares the deployed node tool with this
release's: `stale` means it came from another release; `doctor` does not replace it (a lease may be running on it), so
run `hloom nodes update <profile> --machine <machine>`, which refuses while a lease is live.
Fix whatever your profile needs (section 1) before going on.

## 7. Build the engine and run a profile

These steps use the nodes' CPUs (build) and GPUs (run). In lab mode they follow the lab's rules: builds only
outside measurement turns, and every GPU process inside a GPU turn of at most 20 minutes.

```powershell
hloom install qwen-replicas --build-on halo-1   # fetch the pinned sources, build once, copy to halo-2, verify shas, activate
hloom up qwen-replicas                          # takes the GPU turns, starts the engines and the local router
hloom status qwen-replicas
hloom endpoint qwen-replicas                    # the router address for clients
hloom down qwen-replicas
hloom ui                                        # the portal on http://127.0.0.1:18700
```

## 8. Upgrade, roll back, uninstall

Windows, release installs:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1            # from the newer release: installs and activates it
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -List      # installed releases; * marks the active one
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -Activate hyperloom-<version>-g<commit>   # roll back
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -Remove hyperloom-<version>-g<commit>     # not the active one
```

Stop every profile (`hloom down`) before switching releases. Your configuration, keys and host keys in
`%APPDATA%\hyperloom` are shared by all releases and are never removed by the installer.

To remove hyperloom from the control machine: `hloom down` for each profile, then delete `%LOCALAPPDATA%\hyperloom`,
the shortcut `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Hyperloom.lnk`, and, if you no longer need them,
`%APPDATA%\hyperloom` (configuration, secrets, pinned host keys). On the nodes, the install root and `~/.hyperloom-boot`
are the only things hyperloom created; assets are never touched.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `hosts scan` fails with `OpenSSH command failed (exit 1)` | The `ssh-keyscan` bundled with Windows 11 (OpenSSH_for_Windows 9.5p2) aborts with `choose_kex: unsupported KEX method sntrup761x25519-sha512@openssh.com` against current servers. hyperloom then retries with Git for Windows' `ssh-keyscan` (`Git\usr\bin`); install Git for Windows if it is missing. |
| `doctor` reports `node_tool` `stale`, or `up` says `node tool differs from this checkout` | The node tool on the node is from another release. Stop the profile, then `hloom nodes update <profile>` (add `--machine <machine>` for one node). |
| `unknown host key; confirm a fingerprint before connecting` | Section 5 was skipped for this machine, or `http_address`/`ssh_port` changed since pinning. |
| `Permission denied (publickey)` | Your public key is not in the node user's `~/.ssh/authorized_keys`, or `ssh_target` names the wrong user (it must be `<user>@...`, see section 4). |
| `install root is or contains HOME`, `overlaps a git repository` | Choose another `install_root` (section 4). |
| `Python 3.11 or newer was not found` | Install Python 3.11+ or the `py` launcher, then rerun the installer. |
| The installer stops with `sha256 mismatch` or `File not in RELEASE.json` | The unpacked release was changed or mixed with other files; unpack the archive again into an empty directory. |
| `hyperloom` runs a different version than installed | Use the launchers: they run Python with `-P`, so a hyperloom checkout in the current directory cannot shadow the install. |

## How this guide was checked

On 2026-10-23, with release `hyperloom-0.1.0-ga937c4c7c82b`:

- **Windows 11**, fresh `LOCALAPPDATA`/`APPDATA` in a path with spaces: archive verified and installed, launcher and
  `--version`, a second release installed, `-List`, rollback with `-Activate`, `-Remove` refused for the active release,
  a tampered file refused, the shortcut target checked.
- **Linux**, a clean `python:3.11-slim` container as an unprivileged user: `sha256sum -c`, unpack, venv install,
  `--version`, `config validate`.
- **Node first contact** against a fresh Debian 12 container with only `openssh-server`, `git` and Python 3.11:
  `hosts scan` (this found the Windows `ssh-keyscan` failure handled under Troubleshooting), fingerprint compared
  with `ssh-keygen -lf` on the machine, `hosts pin`, `nodes update` (the node tool reaches a node only that way), `doctor`;
  doctor then reported the missing node toolchain as `unavailable` with reasons.
- Section 7 needs the real nodes and GPU turns; it follows the Phase 1a acceptance runs in `docs/acceptance-1a.md`.
