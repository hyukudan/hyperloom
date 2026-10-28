# Frozen function sources from main 8539c28; executed with the current module globals.

def nvidia_gpus(run=subprocess.run):
    """[{uuid, name, memory_gib, used_gib}] for every NVIDIA GPU of this machine (nvidia-smi, passive)."""
    try:
        out = run(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=SMI_TIMEOUT_S, **no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"nvidia-smi unavailable: {exc}") from exc
    if out.returncode != 0:
        raise ProbeError("nvidia-smi failed: " + (out.stderr or out.stdout).strip()[:300])
    gpus = []
    for line in out.stdout.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) == 4 and cells[2].isdigit() and cells[3].isdigit():
            gpus.append({"uuid": cells[0], "name": cells[1], "memory_gib": int(cells[2]) / 1024,
                         "used_gib": int(cells[3]) / 1024})
    return gpus
