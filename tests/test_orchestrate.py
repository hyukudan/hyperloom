from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import importlib.util
import io
import json
import os
import signal
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hyperloom import cli, orchestrate as orch, secrets
from hyperloom.config import Cluster, Machine
from hyperloom.render import ProcessSpec


RELEASE = "a" * 12


def cluster_fixture(pair=False):
    machines = {mid: Machine(mid, "linux", "ssh", "192.0.2." + str(index + 10), mid, 22,
                              "/opt/hyperloom/" + mid, True, [{"id": "gpu"}])
                for index, mid in enumerate(("halo-1", "halo-2"))}
    profile = "ds41-pair" if pair else "qwen-replicas"
    settings = {"rank0": "halo-1", "rank1": "halo-2"} if pair else {"nodes": list(machines)}
    raw = {"machines": [{"id": mid, "lab_dir": "/opt/lab", "peer_ssh": {"halo-2": "halo-2"}}
                         for mid in machines]}
    return Cluster(machines, {}, {profile: settings}, {}, {"router_listen": "127.0.0.1:18600"}, raw)


class FakeRemote:
    def __init__(self, machine, calls, leases, fail):
        self.machine, self.calls, self.leases, self.fail = machine, calls, leases, fail

    def bootstrap(self):
        self.calls.append((self.machine.id, "bootstrap", {}))

    def call(self, verb, payload, timeout=60):
        self.calls.append((self.machine.id, verb, deepcopy(payload)))
        if (self.machine.id, verb) in self.fail:
            return {"error": "fixture failure"}
        if verb == "releases":
            return {"active": {"qwen-replicas": RELEASE, "ds41-pair": RELEASE},
                    "releases": [{"release_id": RELEASE, "manifest": {
                        "runtime_env": {"LD_LIBRARY_PATH": "/opt/rocm/core-10.1/lib"}}}]}
        if verb == "lease":
            lease = "lease-" + str(len(self.leases))
            self.leases[lease] = {"lease": lease, "state": "ready"}
            return {"lease": lease}
        if verb == "status":
            return {"leases": list(self.leases.values())}
        if verb == "job":
            return {"job": payload["job"], "state": "succeeded", "release_id": RELEASE, "tail": ""}
        if verb in ("stop", "cancel"):
            self.leases[payload["lease"]]["state"] = "stopped"
        if verb == "install":
            return {"job": "build-job"}
        if verb == "version":
            return {"checks": {"tool": {"status": "ok", "value": getattr(self, "tool", None) or orch.remote.tool_digest()}}}
        return {}


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hyperloom orchestration ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls, self.leases, self.fail = [], {}, set()
        self.cluster = cluster_fixture()
        patches = [
            patch.object(secrets, "runtime_dir", return_value=self.directory / "run"),
            patch.object(secrets, "_directory", return_value=self.directory / "stored"),
            patch.object(orch.remote, "Remote", side_effect=lambda machine, known: FakeRemote(machine, self.calls, self.leases, self.fail)),
            patch.object(orch.render, "render_processes", side_effect=self.processes),
            patch.object(orch, "_start_up_watch"),
            patch.object(orch, "_start_local_router", side_effect=self.local),
            patch.object(orch, "_stop_local_router", side_effect=lambda router: self.calls.append(("local", "router-stop", {}))),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def processes(self, cluster, profile):
        _, kind, mids = orch._profile(cluster, profile)
        return [ProcessSpec(mid, "replica" if kind == "qwen-replicas" else f"rank{index}",
                            ["/opt/hyperloom/" + mid + "/releases/" + RELEASE + "/bin/engine", "--http-port", "18501"],
                            {}, "qwen-stderr" if kind == "qwen-replicas" else "ds41-json")
                for index, mid in enumerate(mids)]

    def local(self, profile, rendered, keys, **kwargs):
        self.calls.append(("local", "router-start", {"rendered": rendered, "keys": keys}))
        return {"machine": "local", "directory": str(self.directory), "endpoint": rendered["listen"]}

    def test_doctor_precedes_all_leases_and_down_stops_router_first(self):
        before = deepcopy(self.cluster)
        state = orch.up(self.cluster, "qwen-replicas")
        self.assertEqual(self.cluster, before)
        verbs = [verb for _, verb, _ in self.calls]
        self.assertEqual(verbs[:2], ["doctor", "doctor"])
        self.assertEqual(verbs.count("lease"), 2)
        self.assertLess(max(i for i, verb in enumerate(verbs) if verb == "doctor"), verbs.index("lease"))
        payloads = [payload for _, verb, payload in self.calls if verb == "lease"]
        for payload in payloads:
            self.assertEqual(payload["kind"], "single")
            self.assertRegex(payload["label"], r"^hl-qwen-halo-[12]-[0-9a-f]{4}$")
            self.assertEqual(payload["deadline_s"], 1140)
        self.assertTrue(orch._state_path("qwen-replicas").exists())
        self.calls.clear()
        orch.down("qwen-replicas")
        self.assertEqual(self.calls[0][1], "router-stop")
        self.assertEqual([verb for _, verb, _ in self.calls].count("stop"), 2)
        self.assertFalse(orch._state_path("qwen-replicas").exists())

    def test_unreachable_declared_machine_and_unknown_node_fail_before_lease(self):
        self.fail.add(("halo-2", "doctor"))
        with self.assertRaisesRegex(orch.OrchestrationError, "halo-2.*doctor"):
            orch.up(self.cluster, "qwen-replicas")
        self.assertNotIn("lease", [verb for _, verb, _ in self.calls])
        self.calls.clear()
        for kwargs in ({"nodes": ["missing"]}, {"router": "missing"}, {"devices": ["halo-1:missing"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                orch.up(self.cluster, "qwen-replicas", **kwargs)
        self.assertEqual(self.calls, [])

    def test_launch_records_preserve_release_placement_and_rendered_inputs(self):
        state = orch.up(self.cluster, "qwen-replicas", nodes=["halo-2"], devices=["halo-2:gpu"])
        saved = orch._read(orch._state_path("qwen-replicas"))
        self.assertEqual(saved["release_id"], RELEASE)
        self.assertEqual(saved["nodes"], ["halo-2"])
        self.assertEqual(saved["devices"], ["halo-2:gpu"])
        self.assertIsInstance(saved["started_at"], float)
        lease_payload = next(payload for _, verb, payload in self.calls if verb == "lease")
        self.assertEqual(saved["process_specs"][0]["argv"], lease_payload["processes"][0]["argv"])
        self.assertEqual(saved["process_specs"][0]["env"], lease_payload["processes"][0]["env"])
        self.assertEqual(saved["rendered_router"], state["rendered_router"])
        orch.down("qwen-replicas")

    def test_partial_start_rolls_back_and_cleans_plaintext(self):
        self.fail.add(("halo-2", "lease"))
        with self.assertRaisesRegex(orch.OrchestrationError, "fixture failure"):
            orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([verb for _, verb, _ in self.calls].count("stop"), 1)
        self.assertFalse(list((self.directory / "run").glob("*.key")))
        self.assertFalse(orch._state_path("qwen-replicas").exists())

    def test_router_failure_rolls_back_both_engines(self):
        with patch.object(orch, "_start_local_router", side_effect=RuntimeError("router failed")):
            with self.assertRaisesRegex(RuntimeError, "router failed"):
                orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([verb for _, verb, _ in self.calls].count("stop"), 2)

    def test_readiness_failure_cleans_every_started_lease(self):
        with patch.object(orch, "_wait_ready", side_effect=orch.OrchestrationError("readiness failed")):
            with self.assertRaisesRegex(orch.OrchestrationError, "readiness failed"):
                orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([verb for _, verb, _ in self.calls].count("stop"), 2)
        self.assertNotIn("router-start", [verb for _, verb, _ in self.calls])

    def test_separate_router_machine_is_checked_before_engine_leases(self):
        self.cluster.machines["workstation"] = replace(self.cluster.machines["halo-1"], id="workstation", lab_mode=False)
        orch.up(self.cluster, "qwen-replicas", router="workstation")
        self.assertEqual([mid for mid, verb, _ in self.calls if verb == "doctor"],
                         ["halo-1", "halo-2", "workstation"])

    def test_duplicate_up_preserves_existing_ownership(self):
        state = orch.up(self.cluster, "qwen-replicas")
        self.calls.clear()
        with self.assertRaisesRegex(orch.OrchestrationError, "saved running state"):
            orch.up(self.cluster, "qwen-replicas")
        self.assertEqual(self.calls, [])
        self.assertEqual(orch._read(orch._state_path("qwen-replicas")), state)

    def test_failed_rollback_keeps_state_for_down_retry(self):
        self.fail.update({("halo-2", "lease"), ("halo-1", "stop")})
        with self.assertRaises(orch.OrchestrationError):
            orch.up(self.cluster, "qwen-replicas")
        self.assertTrue(orch._state_path("qwen-replicas").exists())
        self.fail.clear()
        orch.down("qwen-replicas")
        self.assertFalse(orch._state_path("qwen-replicas").exists())

    def test_queue_timeout_is_bounded_and_passed_to_the_wait(self):
        for bad in (59, orch.MAX_QUEUE_S + 1, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.up(self.cluster, "qwen-replicas", queue_timeout_s=bad)
        seen = []
        real = orch._wait_ready
        def spy(*args, **kwargs):
            seen.append(kwargs.get("queue_timeout"))
            return real(*args, **kwargs)
        with patch.object(orch, "_wait_ready", side_effect=spy):
            orch.up(self.cluster, "qwen-replicas", queue_timeout_s=7200)
        self.assertTrue(seen and all(value == 7200 for value in seen))

    def test_stale_node_tool_refused_before_any_lease(self):
        real = FakeRemote.call
        def stale(remote, verb, payload, timeout=60):
            if verb == "version" and remote.machine.id == "halo-2":
                remote.calls.append((remote.machine.id, verb, {}))
                return {"checks": {"version": {"status": "ok", "value": "0.1.0"}}}
            return real(remote, verb, payload, timeout)
        with patch.object(FakeRemote, "call", stale):
            with self.assertRaisesRegex(orch.OrchestrationError, "halo-2: node tool differs.*nodes update"):
                orch.up(self.cluster, "qwen-replicas")
        self.assertNotIn("lease", [verb for _, verb, _ in self.calls])

    def test_nodes_update_refuses_while_a_lease_is_live(self):
        self.leases["lease-x"] = {"lease": "lease-x", "state": "ready"}
        with self.assertRaisesRegex(orch.OrchestrationError, "lease is live"):
            orch.nodes_update(self.cluster, "qwen-replicas")
        self.assertNotIn("bootstrap", [verb for _, verb, _ in self.calls])
        result = orch.nodes_update(self.cluster, "qwen-replicas", machine="halo-1", force=True)
        self.assertEqual(list(result), ["halo-1"])
        self.leases["lease-x"]["state"] = "stopped"
        result = orch.nodes_update(self.cluster, "qwen-replicas")
        self.assertTrue(all(row["tool"] == row["expected"] for row in result.values()))
        self.assertEqual([mid for mid, verb, _ in self.calls if verb == "bootstrap"], ["halo-1", "halo-1", "halo-2"])

    def test_control_and_node_tool_digests_agree(self):
        import importlib.machinery, importlib.util
        path = Path(orch.repo_root()) / "node" / "hyperloom-node"
        loader = importlib.machinery.SourceFileLoader("hyperloom_node_digest", str(path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        self.assertEqual(module.tool_digest(path.parent), orch.remote.tool_digest())

    def test_down_cancels_queued_leases(self):
        orch.up(self.cluster, "qwen-replicas")
        next(iter(self.leases.values()))["state"] = "queued"
        self.calls.clear()
        orch.down("qwen-replicas")
        self.assertEqual([verb for _, verb, _ in self.calls].count("cancel"), 1)

    def test_down_while_up_waits_persists_and_cancels_all_queued_leases(self):
        wait = orch._wait_ready

        def queued(client, root, lease, **kwargs):
            saved = orch._read(orch._state_path("qwen-replicas"))
            self.assertEqual({r["lease"] for r in saved["leases"]}, set(self.leases))
            for row in self.leases.values():
                row["state"] = "queued"
            self.assertEqual(orch.down("qwen-replicas")["state"], "stop-requested")
            return wait(client, root, lease, **kwargs)

        with patch.object(orch, "_wait_ready", side_effect=queued):
            with self.assertRaisesRegex(orch.OrchestrationError, "stop requested"):
                orch.up(self.cluster, "qwen-replicas")
        self.assertEqual([verb for _, verb, _ in self.calls].count("cancel"), 2)
        self.assertTrue(all(r["state"] == "stopped" for r in self.leases.values()))
        self.assertFalse(orch._state_path("qwen-replicas").exists())
        self.assertNotIn("router-start", [verb for _, verb, _ in self.calls])

    def test_down_during_lease_acceptance_cleans_late_reply(self):
        original = FakeRemote.call

        def accept(client, verb, payload, timeout=60):
            result = original(client, verb, payload, timeout)
            if verb == "lease" and len(self.leases) == 2:
                self.assertEqual(len(orch._read(orch._state_path("qwen-replicas"))["leases"]), 1)
                orch.down("qwen-replicas")
            return result

        with patch.object(FakeRemote, "call", accept):
            with self.assertRaisesRegex(orch.OrchestrationError, "stop requested"):
                orch.up(self.cluster, "qwen-replicas")
        self.assertTrue(all(r["state"] == "stopped" for r in self.leases.values()))

    def test_polling_wait_observes_down_from_another_thread(self):
        accepted, finished = threading.Event(), threading.Event()
        errors = []
        original = FakeRemote.call

        def queued(client, verb, payload, timeout=60):
            result = original(client, verb, payload, timeout)
            if verb == "lease":
                self.leases[result["lease"]]["state"] = "queued"
            if verb == "status":
                accepted.set()
            return result

        def start():
            try:
                orch.up(self.cluster, "qwen-replicas")
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        with patch.object(FakeRemote, "call", queued):
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(accepted.wait(5))
                orch.down("qwen-replicas")
                self.assertTrue(finished.wait(5))
            finally:
                orch._state_path("qwen-replicas").with_suffix(".stop").touch()
                worker.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIn("stop requested", str(errors[0]))
        self.assertTrue(all(r["state"] == "stopped" for r in self.leases.values()))

    def test_signals_request_same_cleanup_and_restore_handlers(self):
        names = [name for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name)]
        for name in names:
            sig = getattr(signal, name)
            previous = signal.getsignal(sig)

            def interrupt(*args, **kwargs):
                signal.getsignal(sig)(sig, None)
                kwargs["check"]()

            with self.subTest(signal=name), patch.object(orch, "_wait_ready", side_effect=interrupt):
                with self.assertRaisesRegex(orch.OrchestrationError, "stop requested"):
                    orch.up(self.cluster, "qwen-replicas")
            self.assertEqual(signal.getsignal(sig), previous)
            self.assertFalse(orch._state_path("qwen-replicas").exists())
        self.assertTrue(all(r["state"] == "stopped" for r in self.leases.values()))

    def test_force_down_recovers_state_with_leftover_lock_file_and_is_idempotent(self):
        orch.up(self.cluster, "qwen-replicas")
        self.assertTrue(orch._state_path("qwen-replicas").with_suffix(".lock").exists())
        self.assertEqual(orch.down("qwen-replicas", force=True)["state"], "stopped")
        self.assertEqual(orch.down("qwen-replicas", force=True)["state"], "stopped")

    def test_local_router_receives_earliest_lease_deadline(self):
        deadline = time.time() + 30
        original = FakeRemote.call

        def ready(client, verb, payload, timeout=60):
            result = original(client, verb, payload, timeout)
            if verb == "lease":
                self.leases[result["lease"]]["deadline"] = deadline + len(self.leases)
            return result

        with patch.object(FakeRemote, "call", ready), patch.object(orch, "_start_local_router", side_effect=self.local) as start:
            orch.up(self.cluster, "qwen-replicas")
        self.assertEqual(start.call_args.kwargs["ownership"]["deadline"], deadline + 1)

    def test_endpoint_and_export_cli_use_router_client_key_without_echo(self):
        for router in ("local", "halo-1"):
            orch.up(self.cluster, "qwen-replicas", router=router)
            key_name = orch._secret_name("qwen-replicas", "router")
            key = secrets.get(key_name)
            out = self.directory / (router + ".key")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["endpoint", "qwen-replicas"]), 0)
                self.assertEqual(cli.main(["keys", "export", "qwen-replicas", "--out", str(out)]), 0)
            self.assertEqual(out.read_text(), key)
            self.assertIn(key_name, output.getvalue())
            self.assertIn("127.0.0.1" if router == "local" else "192.0.2.10", output.getvalue())
            self.assertNotIn(key, output.getvalue())
            with self.assertRaises(FileExistsError):
                orch.export_key("qwen-replicas", out)
            self.assertEqual(out.read_text(), key)
            orch.down("qwen-replicas")

    def test_force_down_cli(self):
        with patch.object(orch, "down", return_value={}) as down, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["down", "--force", "qwen-replicas"]), 0)
        down.assert_called_once_with("qwen-replicas", force=True)

    def test_remote_router_has_node_local_keys_and_no_gpu_turn(self):
        orch.up(self.cluster, "qwen-replicas", router="halo-1")
        payload = [payload for _, verb, payload in self.calls if verb == "lease"][-1]
        self.assertEqual(payload["kind"], "none")
        process = payload["processes"][0]
        config = json.loads(process["argv"][4])
        self.assertEqual(config["listen"]["address"], "192.0.2.10")
        self.assertEqual(config["api_key_file"].split("/")[:4], ["", "opt", "hyperloom", "halo-1"])
        self.assertTrue(all(row["api_key_file"].startswith("/opt/hyperloom/halo-1/") for row in config["replicas"]))
        self.assertIn("tensormeld-", process["argv"][-1])
        self.assertIn("PYTHONPATH", process["argv"][2])
        self.assertEqual(process["ready"], "http-health")
        module_path = Path(__file__).resolve().parents[1] / "node/hyperloom_lease.py"
        if module_path.exists():
            spec = importlib.util.spec_from_file_location("lease_contract", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Validate Linux payloads on Windows without starting node processes.
            with patch.object(module, "path", side_effect=PurePosixPath):
                module.validate_spec(payload)
        self.assertFalse(list((self.directory / "run").glob("*.key")))
        self.calls.clear()
        orch.down("qwen-replicas")
        stopped = [payload["lease"] for _, verb, payload in self.calls if verb == "stop"]
        self.assertEqual(stopped[0], "lease-2")

    def test_pair_is_one_lease_with_peer_and_layout_check(self):
        cluster = cluster_fixture(pair=True)
        cluster.raw["machines"][1]["lab_dir"] = "/opt/peer lab"
        orch.up(cluster, "ds41-pair")
        payloads = [payload for _, verb, payload in self.calls if verb == "lease"]
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["kind"], "pair")
        self.assertEqual(payload["layout_required"], "1")
        self.assertEqual([p["machine"] for p in payload["processes"]], ["local", "peer"])
        self.assertEqual(payload["peer_ssh"], "halo-2")
        self.assertEqual(payload["peer_lab_dir"], "/opt/peer lab")
        self.assertEqual([mid for mid, verb, _ in self.calls if verb == "lease"], ["halo-1"])
        self.assertEqual(payload["processes"][1]["health_url"], payload["processes"][0]["health_url"])
        orch.logs("ds41-pair", "rank1", 7)
        self.assertEqual(self.calls[-1][0:2], ("halo-2", "logs"))
        self.assertEqual(self.calls[-1][2]["root"], cluster.machines["halo-2"].install_root)
        self.assertEqual(self.calls[-1][2]["name"], "rank1")

    def test_selected_nodes_and_devices_filter_router_and_engines(self):
        orch.up(self.cluster, "qwen-replicas", nodes=["halo-2"], devices=["halo-2:gpu"])
        self.assertEqual([mid for mid, verb, _ in self.calls if verb == "doctor"], ["halo-2"])
        payload = [payload for _, verb, payload in self.calls if verb == "router-start"][0]
        self.assertEqual([r["id"] for r in payload["rendered"]["replicas"]], ["halo-2"])

    def test_approval_validation_happens_before_remote_work(self):
        for deadline, approval in ((1200, None), (0, None), (float("nan"), None),
                                   (1200, {"lease_s": 1260, "ref": "ACK", "end_epoch": 1})):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                orch.up(self.cluster, "qwen-replicas", deadline_s=deadline, approval=approval)
        self.assertEqual(self.calls, [])
        approval = {"lease_s": 1260, "ref": "coord ACK fixture", "end_epoch": time.time() + 6000}
        orch.up(self.cluster, "qwen-replicas", deadline_s=1200, approval=approval)
        for _, verb, payload in self.calls:
            if verb == "lease":
                self.assertEqual(payload["approval"], approval)

    def test_install_bootstraps_builds_polls_copies_then_activates(self):
        release = orch.install(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        self.assertEqual(release, RELEASE)
        verbs = [verb for _, verb, _ in self.calls]
        self.assertEqual(verbs[:2], ["bootstrap", "bootstrap"])
        self.assertLess(verbs.index("install"), verbs.index("job"))
        self.assertLess(verbs.index("push-release"), verbs.index("activate"))
        recipe = next(payload["recipe"] for _, verb, payload in self.calls if verb == "install")
        self.assertEqual(recipe["configure"], orch._versions()["toolchains"]["hip_configure"])
        self.assertFalse(recipe["trellis"])
        components = next(payload["components"] for _, verb, payload in self.calls if verb == "install")
        self.assertEqual(components["tensormeld"], {key: orch._versions()["components"]["tensormeld"][key]
                                                  for key in ("repo", "commit")})

    def test_build_failure_and_copy_failure_never_activate(self):
        self.fail.add(("halo-1", "push-release"))
        with self.assertRaises(orch.OrchestrationError):
            orch.install(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        self.assertNotIn("activate", [verb for _, verb, _ in self.calls])
        self.calls.clear()
        self.fail.clear()
        with patch.object(orch, "_wait_job", side_effect=orch.OrchestrationError("build failed")):
            with self.assertRaisesRegex(orch.OrchestrationError, "build failed"):
                orch.install(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        self.assertNotIn("push-release", [verb for _, verb, _ in self.calls])

    def test_failed_job_poll_reports_recorded_reason(self):
        client = Mock(machine=self.cluster.machines["halo-1"])
        client.call.return_value = {"job": "abc", "state": "failed", "reason": "synthetic compiler failure", "tail": "diagnostic"}
        with self.assertRaisesRegex(orch.OrchestrationError, "synthetic compiler failure"):
            orch._wait_job(client, "/opt/hyperloom/halo-1", "abc")
        client.call.assert_called_once_with("job", {"root": "/opt/hyperloom/halo-1", "job": "abc"}, timeout=60)

    def test_activation_failure_restores_previous_release(self):
        self.fail.add(("halo-2", "activate"))
        new_release = "b" * 12
        with patch.object(orch, "_wait_job", return_value={"release_id": new_release}):
            with self.assertRaises(orch.OrchestrationError):
                orch.install(self.cluster, "halo-1", ["strix-hip-decode-token"], profile="qwen-replicas")
        activations = [(mid, payload["release_id"]) for mid, verb, payload in self.calls if verb == "activate"]
        self.assertEqual(activations, [("halo-1", new_release), ("halo-2", new_release), ("halo-1", RELEASE)])

    def test_missing_asset_fails_before_any_lease(self):
        self.cluster.assets["halo-1"] = {"model": {"path": "/opt/models/model.gguf", "bytes": 1}}
        self.fail.add(("halo-1", "stat-assets"))
        with self.assertRaises(orch.OrchestrationError):
            orch.up(self.cluster, "qwen-replicas")
        self.assertNotIn("lease", [verb for _, verb, _ in self.calls])

    def test_router_stop_failure_retains_engines_and_retry_state(self):
        orch.up(self.cluster, "qwen-replicas", router="halo-1")
        self.fail.add(("halo-1", "stop"))
        self.calls.clear()
        with self.assertRaisesRegex(orch.OrchestrationError, "router did not stop"):
            orch.down("qwen-replicas")
        stops = [payload for _, verb, payload in self.calls if verb == "stop"]
        self.assertEqual([p["lease"] for p in stops], ["lease-2"])
        self.assertTrue(orch._state_path("qwen-replicas").exists())

    def test_status_merges_nodes_and_router_and_logs_routes(self):
        orch.up(self.cluster, "qwen-replicas")
        with patch.object(orch, "_http_json", return_value={"schema": "fixture"}):
            status = orch.status("qwen-replicas")
        self.assertEqual(set(status["nodes"]), {"halo-1", "halo-2"})
        self.assertEqual(status["router"], {"schema": "fixture"})
        orch.logs("qwen-replicas", "halo-2", 7)
        self.assertEqual(self.calls[-1][1], "logs")
        self.assertEqual(self.calls[-1][2]["tail"], 7)

    def test_cli_arguments_and_rotation_never_print_key(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(orch, "up", return_value={"state": "fixture"}) as up, patch(
                "hyperloom.config.load", return_value=self.cluster), redirect_stdout(output), redirect_stderr(error):
            result = cli.main(["up", "qwen-replicas", "--node", "halo-1", "halo-2", "--device", "halo-1:gpu",
                               "--deadline", "1200", "--approval-ref", "coord ACK fixture", "--approval-lease", "1260",
                               "--approval-end", "2099-01-01T00:00:00Z"])
        self.assertEqual(result, 0, error.getvalue())
        self.assertEqual(up.call_args.kwargs["nodes"], ["halo-1", "halo-2"])
        self.assertEqual(up.call_args.kwargs["approval"]["lease_s"], 1260)
        output = io.StringIO()
        with patch.object(secrets, "rotate", return_value="fixture-secret"), redirect_stdout(output):
            self.assertEqual(cli.main(["keys", "rotate", "replica"]), 0)
        self.assertNotIn("fixture-secret", output.getvalue())


class LocalRouterTests(unittest.TestCase):
    def test_watchdog_cli_starts_before_leases_and_exits_after_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "up-profile.json"
            state = {"token": "fixture-token", "phase": "starting"}
            orch._write(path, state)
            lock = orch.procs.InstanceLock(path.with_suffix(".lock")).acquire()
            helper = None
            try:
                helper = orch._start_up_watch(path, state["token"])
                self.assertIsNone(helper.poll())
                state["phase"] = "running"
                orch._write(path, state)
                helper.wait(timeout=5)
                self.assertEqual(helper.returncode, 0)
                self.assertEqual(orch._read(path), state)
            finally:
                lock.close()
                if helper is not None:
                    if helper.poll() is None:
                        helper.kill()
                        helper.wait(timeout=5)
                    orch._controllers.remove(helper)

    def test_watchdog_retries_failed_cleanup_and_preserves_ownership_until_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "up-profile.json"
            state = {"token": "fixture-token", "phase": "starting"}
            orch._write(path, state)

            def retry(snapshot):
                self.assertEqual(orch._read(path), state)
                if cleanup.call_count == 1:
                    raise OSError("fixture unreachable node")

            with patch.object(orch, "_down_state", side_effect=retry) as cleanup, patch.object(orch.time, "sleep"):
                orch.up_watch(path, state["token"])
            self.assertEqual(cleanup.call_count, 2)
            self.assertFalse(path.exists())

    def test_router_stops_at_deadline_and_when_all_replica_leases_stop(self):
        for stopped in (False, True):
            with self.subTest(replicas_stopped=stopped), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                key = directory / "client.key"
                key.write_text("synthetic-key")
                spec = directory / "spec.json"
                orch._write(spec, {"rendered": {"listen": {"address": "127.0.0.1", "port": 1}},
                                   "keys": [str(key)], "deadline": time.time() + (60 if stopped else -1),
                                   "ownership": {"leases": [{"role": "engine"}]}})
                child = Mock()
                child.alive.return_value = True
                with patch.object(orch.procs, "LocalChild", return_value=child), patch.object(
                        orch, "_replicas_stopped", return_value=stopped), patch.object(
                        orch, "_http_json", side_effect=OSError), patch.object(orch.signal, "signal"):
                    orch.local_router_run(spec)
                child.stop.assert_called_once_with(5)
                self.assertFalse(key.exists())
                self.assertFalse(spec.exists())
                self.assertEqual(orch._read(directory / "state.json"), {"state": "stopped"})

    def test_replica_monitor_requires_every_owned_engine_to_be_terminal(self):
        state = {"known_hosts": "synthetic-known-hosts", "machines": {"halo-1": {"install_root": "/opt/hyperloom"}},
                 "leases": [{"role": "engine", "machine": "halo-1", "lease": "one"},
                            {"role": "engine", "machine": "halo-1", "lease": "two"}]}
        for second in ("queued", "ready", "stopped", "failed", "aborted", "cancelled", None):
            client = Mock()
            client.call.return_value = {"leases": [{"lease": "one", "state": "stopped"},
                                                   {"lease": "two", "state": second}]}
            with self.subTest(second=second), patch.object(orch, "_client", return_value=client):
                self.assertEqual(orch._replicas_stopped(state), second in ("stopped", "failed", "aborted", "cancelled"))
        with patch.object(orch, "_client", side_effect=orch.remote.RemoteError("fixture unavailable")):
            self.assertFalse(orch._replicas_stopped(state))

    def test_hard_killed_waiting_up_is_cleaned_by_detached_watchdog(self):
        fixture = Path(__file__).parent / "fixtures/orchestrate_lifecycle.py"
        with tempfile.TemporaryDirectory(prefix="hyperloom killed controller ") as temporary:
            directory = Path(temporary)
            with (directory / "owner.log").open("wb") as log:
                owner = subprocess.Popen([sys.executable, str(fixture), str(directory), "owner"], stdout=log, stderr=log)
            try:
                end = time.monotonic() + 15
                while not (directory / "waiting").exists() and time.monotonic() < end:
                    if owner.poll() is not None:
                        self.fail((directory / "owner.log").read_text())
                    time.sleep(0.05)
                self.assertTrue((directory / "waiting").exists())
                owner.kill()  # TerminateProcess on Windows; SIGKILL on Linux.
                owner.wait(timeout=5)
                end = time.monotonic() + 15
                while (directory / "run/up-qwen-replicas.json").exists() and time.monotonic() < end:
                    time.sleep(0.05)
                self.assertFalse((directory / "run/up-qwen-replicas.json").exists(),
                                 (directory / "watch.log").read_text())
                self.assertTrue(all(row["state"] == "stopped" for row in orch._read(directory / "leases.json").values()))
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                # Allow the watchdog's log handle to close before temp cleanup on Windows.
                time.sleep(0.2)

    @unittest.skipUnless(os.name == "nt", "Windows console-close callback")
    def test_console_close_runs_cleanup_and_unregisters_handler(self):
        import ctypes
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "up-profile.json"
            orch._write(path, {"fixture": True})
            kernel = Mock()
            with patch.object(ctypes, "WinDLL", return_value=kernel), patch.object(orch, "_down_state") as cleanup:
                with orch._up_handlers(path):
                    callback = kernel.SetConsoleCtrlHandler.call_args.args[0]
                    self.assertTrue(callback(2))
                    self.assertTrue(path.with_suffix(".stop").exists())
                    cleanup.assert_called_once_with({"fixture": True})
                self.assertEqual(kernel.SetConsoleCtrlHandler.call_args.args[1], False)

    def test_persistent_local_router_stop_removes_plaintext_and_releases_port(self):
        with tempfile.TemporaryDirectory(prefix="hyperloom local router ") as temporary:
            directory = Path(temporary)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            key = directory / "replica.key"
            secrets._write(key, b"fixture-key")
            rendered = {"schema": "tensormeld/replica-router-v1",
                        "listen": {"address": "127.0.0.1", "port": port},
                        "replicas": [{"id": "halo-1", "node": "halo-1", "address": "127.0.0.1", "port": 1,
                                      "max_inflight": 1, "api_key_file": str(key)}]}
            with patch.object(secrets, "runtime_dir", return_value=directory):
                router = None
                try:
                    router = orch._start_local_router("qwen-replicas", rendered, [str(key)])
                    self.assertTrue(key.exists())
                    self.assertEqual(orch._http_json(router["endpoint"])["schema"], "tensormeld/replica-router-status-v1")
                finally:
                    if router:
                        orch._stop_local_router(router)
                    for helper in orch._controllers[:]:
                        helper.wait(timeout=10)
                        orch._controllers.remove(helper)
                self.assertFalse(key.exists())
                orch.procs.LocalChild([str(Path(os.sys.executable).resolve())], {}, directory,
                                      "probe", port=("127.0.0.1", port))._check_port()


class LaunchConfigTests(unittest.TestCase):
    def example(self):
        path = Path(__file__).resolve().parents[1] / "examples/cluster.example.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def load(self, raw):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cluster.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return orch.load_cluster(path)

    def test_loader_accepts_phase1a_fields_with_strict_phase0_validation(self):
        raw = self.example()
        worker = next(m for m in raw["machines"] if m["id"] == "halo-1")
        worker.update(lab_dir="/opt/lab", peer_ssh={"halo-2": "operator@halo-2"})
        raw["profiles"]["qwen-replicas"]["release_id"] = RELEASE
        cluster = self.load(raw)
        self.assertEqual(orch._lab(cluster, "halo-1"), "/opt/lab")
        self.assertEqual(orch._peer(cluster, "halo-1", "halo-2"), "operator@halo-2")
        self.assertEqual(cluster.profiles["qwen-replicas"]["release_id"], RELEASE)
        for key, value in (("lab_dir", "/opt/../lab"), ("peer_ssh", {"halo-2": "-bad"}),
                           ("peer_ssh", {"missing": "halo-2"}), ("unexpected", True)):
            bad = deepcopy(raw)
            target = next(m for m in bad["machines"] if m["id"] == "halo-1")
            target[key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, RuntimeError)):
                self.load(bad)


if __name__ == "__main__":
    unittest.main()
