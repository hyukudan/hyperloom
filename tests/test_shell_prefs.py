"""Portal preferences (F52): validation, storage, documents, audit, HTTP rules and the CLI family."""

import argparse
import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from hyperloom import telemetry
from hyperloom.cliext import prefs as prefs_cli
from hyperloom.telemetry.store import Store
from hyperloom.web import features, server
from hyperloom.web.api import API, APIError
from hyperloom.web.features import prefs


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def registry(*extra):
    reg = features.Registry()
    prefs.register(reg)
    for name in extra:
        reg.post(name, lambda api, a: {})
    return reg


class PrefsValidationTests(unittest.TestCase):
    def test_accepts_every_field(self):
        values = prefs.validate({"refresh_s": 5, "temperature_unit": "F", "timezone": "UTC", "language": "es",
                                 "theme": "black", "density": "compact", "hidden_panels": ["logs", "topology", "logs"]})
        self.assertEqual(values["hidden_panels"], ["logs", "topology"])
        self.assertEqual(prefs.validate({"timezone": "local"}), {"timezone": "local"})
        if prefs.zone_database():
            self.assertEqual(prefs.validate({"timezone": "Europe/Madrid"}), {"timezone": "Europe/Madrid"})
            self.assertEqual(prefs.validate({"timezone": "America/Argentina/Buenos_Aires"})["timezone"],
                             "America/Argentina/Buenos_Aires")

    def test_refusals(self):
        bad = [{}, [], {"unknown": 1}, {"refresh_s": 3}, {"refresh_s": True}, {"refresh_s": 2.0}, {"refresh_s": "2"},
               {"temperature_unit": "K"}, {"language": "fr"}, {"theme": "Dark"}, {"density": 1},
               {"timezone": ""}, {"timezone": "../etc/passwd"}, {"timezone": "Europe/../x"}, {"timezone": "x" * 65},
               {"timezone": 5}, {"timezone": "Mars/Olympus"}, {"timezone": "C:\\zones\\x"},
               {"hidden_panels": "logs"}, {"hidden_panels": ["settings"]}, {"hidden_panels": ["Bad"]},
               {"hidden_panels": [1]}, {"hidden_panels": [f"p{i}" for i in range(65)]}]
        for arguments in bad:
            with self.assertRaises(ValueError, msg=arguments):
                prefs.validate(arguments)

    def test_without_zone_database_only_local_and_utc(self):
        with mock.patch.object(prefs, "zone_database", return_value=False), \
                mock.patch("zoneinfo.ZoneInfo", side_effect=Exception("no data")):
            self.assertEqual(prefs.validate({"timezone": "UTC"}), {"timezone": "UTC"})
            with self.assertRaisesRegex(ValueError, "no time zone database"):
                prefs.validate({"timezone": "Europe/Madrid"})


class LocalZoneTests(unittest.TestCase):
    def test_iana_name_from_tz_or_the_localtime_link(self):
        if not prefs.zone_database():
            self.skipTest("no time zone database")
        with mock.patch.dict("os.environ", {"TZ": ":Europe/Madrid"}):
            self.assertEqual(prefs.local_iana(), "Europe/Madrid")
        with mock.patch.dict("os.environ", {"TZ": ""}), \
                mock.patch("os.path.realpath", return_value="/usr/share/zoneinfo/America/New_York"):
            self.assertEqual(prefs.local_iana(), "America/New_York")
        with mock.patch.dict("os.environ", {"TZ": "../../etc/passwd"}), \
                mock.patch("os.path.realpath", return_value="C:\\Windows\\localtime"):
            self.assertIsNone(prefs.local_iana())
        zone = prefs.local_zone(1_800_000_000)
        self.assertEqual(set(zone), {"name", "utc_offset_s", "iana"})
        self.assertIsInstance(zone["utc_offset_s"], int)

    def test_zone_list_is_what_validation_accepts(self):
        names = prefs.zone_names()
        if names is None:
            self.skipTest("no time zone database")
        self.assertIn("Europe/Madrid", names)
        for name in names[:: max(1, len(names) // 25)]:
            self.assertEqual(prefs.validate({"timezone": name}), {"timezone": name})


class PrefsStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def test_one_row_per_field_default_deletes(self):
        prefs.save(self.store, prefs.validate({"timezone": "UTC", "refresh_s": 5}))
        self.assertEqual(self.store.get("prefs/ui", "timezone"), "UTC")
        self.assertEqual(self.store.get("prefs/ui", "refresh_s"), 5)
        prefs.save(self.store, prefs.validate({"timezone": "local", "refresh_s": 2}))
        self.assertIsNone(self.store.get("prefs/ui", "timezone"))
        self.assertEqual(self.store.items("prefs/ui"), [])
        values, saved = prefs.read(self.store)
        self.assertEqual(values, prefs.DEFAULTS)
        self.assertFalse(any(saved.values()))

    def test_unusable_rows_read_as_defaults(self):
        self.store.put("prefs/ui", "theme", "neon")
        self.store.put("prefs/ui", "refresh_s", 10)
        values, saved = prefs.read(self.store)
        self.assertEqual((values["theme"], saved["theme"]), ("system", False))
        self.assertEqual((values["refresh_s"], saved["refresh_s"]), (10, True))

    def test_save_is_atomic(self):
        prefs.save(self.store, {"theme": "dark"})
        original = self.store.transaction

        def failing(statements):
            original(statements + [("INSERT INTO no_such_table VALUES (1)", ())])
        with mock.patch.object(self.store, "transaction", side_effect=failing):
            with self.assertRaises(Exception):
                prefs.save(self.store, {"theme": "light", "density": "compact"})
        self.assertEqual(prefs.read(self.store)[0]["theme"], "dark")
        self.assertEqual(prefs.read(self.store)[0]["density"], "comfortable")


class PrefsFeatureTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        features.reset(registry("energy/price"))
        self.addCleanup(features.reset)
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.api.clock = Clock()
        self.addCleanup(lambda: getattr(self.api, "telemetry", None) and self.api.telemetry.close())

    def audit(self):
        path = Path(self.dir.name) / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_get_without_store_shows_defaults_and_creates_nothing(self):
        document = self.api.read("prefs", {})
        self.assertEqual(document["schema"], "hyperloom/ui-prefs-v1")
        self.assertEqual(document["values"], prefs.DEFAULTS)
        self.assertIn("not open", document["why"]["values"])
        self.assertIn("utc_offset_s", document["local_zone"])
        self.assertFalse((Path(self.dir.name) / "telemetry.sqlite3").exists())
        self.assertIsNone(telemetry.existing_store(self.api))
        with self.assertRaises(APIError):
            self.api.read("prefs", {"x": "1"})

    def test_save_round_trip_is_audited(self):
        status, document = self.api.feature_post("prefs/save", {"theme": "black", "hidden_panels": ["logs"]})
        self.assertEqual(status, 200)
        self.assertEqual((document["values"]["theme"], document["saved"]["theme"]), ("black", True))
        self.assertTrue((Path(self.dir.name) / "telemetry.sqlite3").exists())
        again = self.api.read("prefs", {})
        self.assertEqual(again["values"]["hidden_panels"], ["logs"])
        self.assertNotIn("values", again["why"])
        entries = self.audit()
        self.assertEqual([(e["action"], e["result"]["state"]) for e in entries], [("prefs/save", "succeeded")])
        with self.assertRaises(APIError) as caught:
            self.api.feature_post("prefs/save", {"theme": "neon"})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.audit()[-1]["result"]["state"], "refused")
        self.assertEqual(self.api.read("prefs", {})["values"]["theme"], "black")

    def test_related_pointers_follow_the_registry(self):
        related = {row["id"]: row for row in self.api.read("prefs", {})["related"]}
        self.assertTrue(related["energy-price"]["available"])
        self.assertFalse(related["alert-channels"]["available"])
        self.assertEqual(related["alert-channels"]["why"], "needs the alerts feature")


class PrefsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        features.reset(registry())
        self.addCleanup(features.reset)
        self.api = API(Path(self.dir.name) / "cluster.json", orchestrator=mock.Mock(), remote_module=mock.Mock(),
                       audit_path=Path(self.dir.name) / "audit.jsonl")
        self.server = server.create_server(0, api=self.api)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address[:2]
        self.addCleanup(lambda: getattr(self.api, "telemetry", None) and self.api.telemetry.close())

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            base = {"Host": f"{self.host}:{self.port}"}
            connection.request(method, path, body=body, headers={**base, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post(self, data, **headers):
        base = {"Origin": f"http://{self.host}:{self.port}", "X-Hyperloom-CSRF": self.server.csrf_token,
                "Content-Type": "application/json", "Content-Length": str(len(data))}
        base.update({k.replace("_", "-"): v for k, v in headers.items()})
        return self.call("POST", "/hyperloom/api/prefs/save", data, {k: v for k, v in base.items() if v is not None})

    def test_portal_rules_hold_for_prefs(self):
        good = json.dumps({"density": "compact"}).encode()
        self.assertEqual(self.call("GET", "/hyperloom/api/prefs/save")[0], 405)
        self.assertEqual(self.call("GET", "/hyperloom/api/prefs", headers={"Host": f"{self.host}:{self.port + 1}"})[0], 403)
        self.assertEqual(self.post(good, Origin=f"http://{self.host}:{self.port + 1}")[0], 403)
        self.assertEqual(self.post(good, X_Hyperloom_CSRF="wrong")[0], 403)
        self.assertEqual(self.post(good, X_Hyperloom_CSRF=None)[0], 403)
        self.assertEqual(self.post(good, Content_Type="text/plain")[0], 415)
        self.assertEqual(self.post(b'{"density": "compact", "density": "compact"}')[0], 400)
        self.assertEqual(self.post(b'{"refresh_s": NaN}')[0], 400)
        self.assertEqual(self.post(b"[]")[0], 400)
        self.assertFalse((Path(self.dir.name) / "telemetry.sqlite3").exists(), "refused requests wrote nothing")
        status, body = self.call("GET", "/hyperloom/api/prefs")
        self.assertEqual(status, 200)
        self.assertFalse((Path(self.dir.name) / "telemetry.sqlite3").exists(), "a GET never creates the store")
        status, body = self.post(good)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["values"]["density"], "compact")
        self.assertEqual(json.loads(self.call("GET", "/hyperloom/api/prefs")[1])["values"]["density"], "compact")


class PrefsCLITests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch("hyperloom.cli.config_path", return_value=Path(self.dir.name) / "cluster.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        parser = argparse.ArgumentParser(prog="hyperloom")
        prefs_cli.add(parser.add_subparsers(dest="command"))
        self.parser = parser

    def run_cli(self, *argv):
        args = self.parser.parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = args.ext_run(args)
        return code, out.getvalue(), err.getvalue()

    def test_show_set_and_refusals_share_the_portal_function(self):
        code, out, _ = self.run_cli("prefs", "show")
        self.assertEqual((code, json.loads(out)["values"]), (0, prefs.DEFAULTS))
        self.assertFalse((Path(self.dir.name) / "telemetry.sqlite3").exists(), "show never creates the store")
        code, out, _ = self.run_cli("prefs", "set", "refresh_s=5", "hidden_panels=logs,topology", "timezone=UTC")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["values"]["hidden_panels"], ["logs", "topology"])
        store = Store(Path(self.dir.name) / "telemetry.sqlite3")
        try:
            self.assertEqual(store.get("prefs/ui", "refresh_s"), 5)
        finally:
            store.close()
        code, out, _ = self.run_cli("prefs", "set", "refresh_s=2")
        self.assertFalse(json.loads(out)["saved"]["refresh_s"])
        for bad in (["refresh_s=3"], ["refresh_s=x"], ["theme"], ["theme=neon"], ["hidden_panels=settings"],
                    ["theme=dark", "theme=light"]):
            code, _, err = self.run_cli("prefs", "set", *bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("hyperloom prefs:", err)
        self.assertEqual(json.loads(self.run_cli("prefs", "show")[1])["values"]["timezone"], "UTC")


if __name__ == "__main__":
    unittest.main()
