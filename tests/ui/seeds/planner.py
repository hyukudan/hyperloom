"""Browser-smoke seed for the recipe planner: a saved hardware observation of the fixture's two-node profile (with a
control-machine GPU and links, synthetic), and in-process stand-ins for the two seams that start processes (the
fixture refuses subprocesses): the planner's command runner and its TensorMeld planner child."""
import contextlib
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
GPU = "GPU-00000000-0000-0000-0000-000000000001"
NODE = {"name": "AMD Radeon 8060S", "memory_gib": 120.0, "host_ram_gib": 125.0}


def facts_record(now):
    return {"schema": "hyperloom/recipe-facts-v1", "profile": "qwen-two", "observed_at": now - 1800,
            "inventory": {"cuda": [{"uuid": GPU, "name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6}]},
            "facts": {"model": "qwen3.8-flash-next",
                      "devices": {GPU: {"name": "NVIDIA RTX PRO 6000 Blackwell", "memory_gib": 95.6,
                                        "host_ram_gib": 127.7},
                                  "halo-1": dict(NODE), "halo-2": dict(NODE)},
                      "links": [{"kind": "ethernet", "hosts": ["workstation", "halo-1"], "gbps": 2.5},
                                {"kind": "ethernet", "hosts": ["workstation", "halo-2"], "gbps": 2.5},
                                {"kind": "usb4", "hosts": ["halo-1", "halo-2"], "gbps": 0.0}]},
            "raw": {"devices": {GPU: {"available": False, "why": "not the profile's prefill device"},
                                "halo-1": {"available": True}, "halo-2": {"available": True}},
                    "hosts": {"workstation": {"free_ram_gib": 100.0, "free_commit_gib": 150.0},
                              "halo-1": {"free_ram_gib": 500.0}, "halo-2": {"free_ram_gib": 500.0}},
                    "lan": {"halo-1": {"up": True}, "halo-2": {"up": True}},
                    "usb4": {"halo-1": {"up": True}, "halo-2": {"up": True}}},
            "notes": ["synthetic observation for the browser smoke"]}


def run_in_process(api, argv, stdin):
    """The planner command line in this process: the CLI's own parser and the command's ext_run."""
    from hyperloom import cli
    from hyperloom.web import recipes as recipe_web
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            _stdin((stdin or b"").decode("utf-8")):
        try:
            args = cli.build_parser(recipe_web.RaisingParser).parse_args(argv)
            code = args.ext_run(args)
        except Exception as exc:  # the CLI's own error line
            print(f"hyperloom: {exc}", file=sys.stderr)
            code = 1
    return recipe_web.Result(code, out.getvalue().encode("utf-8"), err.getvalue().encode("utf-8"))


@contextlib.contextmanager
def _stdin(text):
    previous = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = previous


def planner_in_process(parts, hardware_path, request, *, timeout_s, workdir):
    """TensorMeld's planner called in process (a pure computation) instead of its child process."""
    source = str(ROOT / "third_party" / "tensormeld" / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from tensormeld import cli as tensormeld_cli
    options = ["--prompt", str(request["prompt"]), "--context", str(request["prompt"] + request["output"]),
               "--output", str(request["output"]), "--slots", str(request["slots"]),
               "--objective", request["objective"], "--top-k", str(request["top_k"])]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        tensormeld_cli.main(["plan-hetero", str(parts), str(hardware_path), *options])
    cli = " ".join(["python", "-m", "tensormeld", "plan-hetero", f"examples/hetero/{Path(parts).name}",
                    "hardware.json", *options])
    try:
        return json.loads(out.getvalue()), None, cli
    except ValueError:
        return None, "the planner printed no plan", cli


def patches(directory, now):
    from hyperloom.cliext import planner as cli_planner
    from hyperloom.web.features import planner as feature
    facts = Path(directory) / "hyperloom" / "run" / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    (facts / "planner-lab.json").write_text(json.dumps(facts_record(now)), encoding="utf-8")
    return [(feature, "run", run_in_process), (cli_planner, "run_planner", planner_in_process)]
