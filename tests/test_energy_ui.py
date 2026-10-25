"""Browser checks of the energy and thermals pages against the fixture portal (skipped without Chrome): drafts
survive polls, a reset needs Space or a click (never Enter) and focus lands on the safe choice, controls name their
blocker, sensor colours follow the sensor identity."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _smoke():
    spec = importlib.util.spec_from_file_location("hyperloom_ui_smoke_for_energy", ROOT / "tests" / "ui" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _smoke()


@unittest.skipUnless(smoke.find_chrome(), "Chrome/Chromium is absent (set CHROME)")
class EnergyPagesInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = smoke.FixtureServer()
        cls.profile = tempfile.TemporaryDirectory(prefix="hyperloom-chrome-", ignore_cleanup_errors=True)
        port = smoke.free_port()
        command = [smoke.find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   "--disable-extensions", "--proxy-server=direct://",
                   "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                   "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
                   f"--user-data-dir={cls.profile.name}", "about:blank"]
        if sys.platform.startswith("linux") and hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
            command.append("--no-sandbox")
        cls.chrome = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def ready():
            try:
                return next((row for row in smoke.get_json(port, "/json/list") if row["type"] == "page"), None)
            except OSError:
                return None
        target = smoke.eventually(ready)
        cls.client = smoke.DevTools(target["webSocketDebuggerUrl"])
        cls.client.call("Runtime.enable")
        cls.client.call("Page.enable")
        cls.client.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.close()
            cls.chrome.terminate()
            try:
                cls.chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.chrome.kill()
                cls.chrome.wait(timeout=5)
        finally:
            cls.server.close()
            cls.profile.cleanup()

    def open(self, route):
        self.client.call("Page.navigate", url=self.server.url + "#/" + route)
        smoke.eventually(lambda: self.client.evaluate(
            "document.readyState === 'complete' && document.querySelector('main')?.getAttribute('aria-busy') === 'false'"), timeout=30)

    def js(self, expression):
        return self.client.evaluate(expression)

    def wait_poll(self):
        self.js("new Promise(resolve => setTimeout(resolve, 2600))")

    def api(self, path):
        status, _, body = self.server.request("GET", "/hyperloom/api/" + path, headers={"Host": f"127.0.0.1:{self.server.port}"})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_energy_page_drafts_reset_and_blockers(self):
        self.open("energy")
        self.assertIn("Estimated from sensors, not metered", self.js("document.body.innerText"))
        # A typed price survives the 2 s poll re-render.
        self.js("const i = document.getElementById('energy-price'); i.value = '0.31'; i.dispatchEvent(new Event('input', {bubbles: true}));")
        self.wait_poll()
        self.assertEqual(self.js("document.getElementById('energy-price').value"), "0.31")
        self.js("document.querySelector('[data-page-action=\"energy-price-save\"]').click()")
        smoke.eventually(lambda: self.api("energy?range=24h")["price"] == {"price_per_kwh": 0.31, "currency": "EUR"})
        smoke.eventually(lambda: self.js("document.querySelector('[data-page-action=\"energy-reset\"]')?.disabled === false"))
        # Reset: the first click reveals the confirmation with focus on the safe choice; Enter never confirms.
        self.js("document.querySelector('[data-page-action=\"energy-reset\"]').click()")
        self.assertEqual(self.js("document.activeElement?.dataset.pageAction"), "energy-reset-cancel")
        self.js("document.querySelector('[data-page-action=\"energy-reset-confirm\"]').focus()")
        smoke.key(self.client, "Enter")
        self.wait_poll()
        self.assertIsNone(self.api("energy?range=24h")["reset_at"])
        self.assertTrue(self.js("!!document.querySelector('[data-page-action=\"energy-reset-confirm\"]')"))
        self.js("document.querySelector('[data-page-action=\"energy-reset-cancel\"]').click()")
        self.assertFalse(self.js("!!document.querySelector('[data-page-action=\"energy-reset-confirm\"]')"))
        # Without authoritative state the controls are disabled and say why.
        self.js("window.Hyperloom.state.fresh = false; window.Hyperloom.render();")
        self.assertEqual(self.js("document.querySelector('[data-page-action=\"energy-price-save\"]').title"),
                         "Waiting for authoritative state")
        self.assertEqual(self.client.errors, [])

    def test_thermals_colours_follow_the_sensor(self):
        self.open("thermals")
        document = self.api("thermals?range=day")
        expected = {line["label"]: f"var(--s{line['colour'] % 5 + 1})" for m in document["machines"] for line in m["lines"]}
        shown = self.js("Object.fromEntries([...document.querySelectorAll('.ts-key')].filter(k => k.querySelector('i[style]'))"
                        ".map(k => [k.childNodes[1].textContent.trim(), k.querySelector('i').style.background]))")
        for label, colour in shown.items():
            self.assertEqual(colour, expected[label], label)
        self.assertGreaterEqual(len(shown), 3)
        for machine in document["machines"]:                                         # distinct within one chart
            slots = [line["colour"] for line in machine["lines"]]
            self.assertEqual(len(slots), len(set(slots)), machine["id"])
        text = self.js("document.body.innerText")
        self.assertIn("Throttled:", text)
        self.assertIn("buckets reaching warn", text)
        self.assertEqual(self.client.errors, [])


if __name__ == "__main__":
    unittest.main()
