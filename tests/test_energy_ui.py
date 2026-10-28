"""Browser checks of the energy and thermals pages against the fixture portal (skipped without a headless browser): drafts
survive polls, a reset needs Space or a click (never Enter) and focus lands on the safe choice, controls name their
blocker, sensor colours follow the sensor identity."""

import importlib.util
from urllib.parse import urlsplit
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


@unittest.skipUnless(smoke.find_chrome(), "no headless browser found (see tests/ui/README.md)")
class EnergyPagesInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for attempt in range(3):            # a loaded machine can miss the fixture's start-up window once
            try:
                cls.server = smoke.FixtureServer()
                break
            except (AssertionError, OSError):
                if attempt == 2:
                    raise
        cls.host = urlsplit(cls.server.origin).hostname        # the fixture's own loopback address
        cls.profile = tempfile.TemporaryDirectory(prefix="hyperloom-browser-", ignore_cleanup_errors=True)
        port = smoke.free_port()
        command = [smoke.find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   "--disable-extensions", "--proxy-server=direct://",
                   f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {cls.host}",
                   f"--remote-debugging-address={cls.host}", f"--remote-debugging-port={port}",
                   f"--user-data-dir={cls.profile.name}", "about:blank"]
        if sys.platform.startswith("linux") and hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
            command.append("--no-sandbox")
        cls.browser = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
            cls.browser.terminate()
            try:
                cls.browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.browser.kill()
                cls.browser.wait(timeout=5)
        finally:
            try:
                cls.server.close()
            except OSError:
                # A just-ended process may hold its files for a moment on some systems; retry the cleanup.
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        cls.server.temporary.cleanup()
                        break
                    except OSError:
                        continue
            cls.profile.cleanup()

    def open(self, route):
        self.client.call("Page.navigate", url=self.server.url + "#/" + route)
        smoke.eventually(lambda: self.client.evaluate(
            "document.readyState === 'complete' && document.querySelector('main')?.getAttribute('aria-busy') === 'false'"), timeout=30)

    def js(self, expression):
        return self.client.evaluate(expression)

    def click(self, selector):
        """Click a control as soon as it is enabled (the shell's freshness gate flickers between polls)."""
        self.js("""new Promise((resolve, reject) => { const started = Date.now(); const tick = () => {
          const c = document.querySelector(%s);
          if (c && !c.disabled) { c.click(); resolve(true); }
          else if (Date.now() - started > 20000) reject(new Error('control stayed disabled: ' + %s));
          else setTimeout(tick, 25); }; tick(); })""" % (json.dumps(selector), json.dumps(selector)))

    def wait_poll(self):
        self.js("new Promise(resolve => setTimeout(resolve, 2600))")

    def api(self, path):
        status, _, body = self.server.request("GET", "/hyperloom/api/" + path, headers={"Host": f"{self.host}:{self.server.port}"})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_energy_page_drafts_reset_and_blockers(self):
        self.open("energy")
        self.assertIn("Estimated from sensors, not metered", self.js("document.body.innerText"))
        smoke.eventually(lambda: self.js("document.querySelector('[data-page-action=\"energy-price-save\"]')?.disabled === false"), timeout=30)
        # A typed price survives the 2 s poll re-render.
        self.js("{ const i = document.getElementById('energy-price'); i.value = '0.31'; i.dispatchEvent(new Event('input', {bubbles: true})); }")
        self.wait_poll()
        self.assertEqual(self.js("document.getElementById('energy-price').value"), "0.31")
        self.click('[data-page-action="energy-price-save"]')
        smoke.eventually(lambda: self.api("energy?range=24h")["price"] == {"price_per_kwh": 0.31, "currency": "EUR"}, timeout=30)
        smoke.eventually(lambda: self.js("document.querySelector('[data-page-action=\"energy-reset\"]')?.disabled === false"), timeout=30)
        # Reset: the first click reveals the confirmation with focus on the safe choice; Enter never confirms.
        self.click('[data-page-action="energy-reset"]')
        self.assertEqual(self.js("document.activeElement?.dataset.pageAction"), "energy-reset-cancel")
        self.js("document.querySelector('[data-page-action=\"energy-reset-confirm\"]').focus()")
        smoke.key(self.client, "Enter")
        self.wait_poll()
        self.assertIsNone(self.api("energy?range=24h")["reset_at"])
        self.assertTrue(self.js("!!document.querySelector('[data-page-action=\"energy-reset-confirm\"]')"))
        self.js("document.querySelector('[data-page-action=\"energy-reset-cancel\"]').click()")
        self.assertFalse(self.js("!!document.querySelector('[data-page-action=\"energy-reset-confirm\"]')"))
        # Without authoritative state the controls are disabled and say why.
        self.assertEqual(self.js("window.Hyperloom.state.fresh = false; window.Hyperloom.render(); "
                                 "document.querySelector('[data-page-action=\"energy-price-save\"]').title"),
                         "Waiting for authoritative state")
        # Another portal action holds the shell's mutation state: the page's controls say so.
        self.assertEqual(self.js("window.Hyperloom.state.fresh = true; window.Hyperloom.state.busy = true; "
                                 "window.Hyperloom.render(); "
                                 "document.querySelector('[data-page-action=\"energy-price-save\"]').title"),
                         "Another action is running")
        self.js("window.Hyperloom.state.busy = false; window.Hyperloom.render();")
        self.assertEqual(self.client.errors, [])

    def test_a_slow_poll_started_before_a_save_never_shows_stale_data_after_it(self):
        self.open("energy")
        smoke.eventually(lambda: self.js("document.querySelector('[data-page-action=\"energy-price-save\"]')?.disabled === false"), timeout=30)
        state = self.js("""new Promise(resolve => {
          const real = window.fetch;
          // Polls of the energy document issued from now on answer late, the write itself is not delayed.
          window.fetch = (url, options) => String(url).includes('/api/energy?') && !(options && options.method === 'POST')
            ? new Promise(r => setTimeout(r, 1500)).then(() => real(url, options)) : real(url, options);
          window.Hyperloom.refresh();                       // a poll in flight before the save
          const input = document.getElementById('energy-price');
          input.value = '0.42'; input.dispatchEvent(new Event('input', {bubbles: true}));
          document.querySelector('[data-page-action="energy-price-save"]').click();
          const started = Date.now();
          const check = () => {
            const button = document.querySelector('[data-page-action="energy-price-save"]');
            if (button && !button.disabled && !window.Hyperloom.state.busy) {
              window.fetch = real;
              resolve(window.Hyperloom.state.docs['energy?range=24h'].price);
            } else if (Date.now() - started > 15000) { window.fetch = real; resolve('timeout'); }
            else setTimeout(check, 50);
          };
          setTimeout(check, 50);
        })""")
        self.assertEqual(state, {"price_per_kwh": 0.42, "currency": "EUR"})
        self.assertEqual(self.client.errors, [])

    def test_a_save_during_which_the_page_is_left_shows_the_saved_value_on_return(self):
        self.open("energy")
        smoke.eventually(lambda: self.js("document.querySelector('[data-page-action=\"energy-price-save\"]')?.disabled === false"), timeout=30)
        self.js("""(() => {
          const real = window.fetch;
          window.__energyRealFetch = real;
          // The write answers late; meanwhile the user goes to another page.
          window.fetch = (url, options) => options && options.method === 'POST'
            ? new Promise(r => setTimeout(r, 1500)).then(() => real(url, options)) : real(url, options);
          const input = document.getElementById('energy-price');
          input.value = '0.47'; input.dispatchEvent(new Event('input', {bubbles: true}));
          document.querySelector('[data-page-action="energy-price-save"]').click();
          location.hash = '#/thermals';
        })()""")
        self.js("new Promise(r => setTimeout(r, 4000))")
        # Back on the page while its next read is held: nothing stale may be shown or editable meanwhile.
        held = self.js("""(() => {
          const real = window.__energyRealFetch;
          window.fetch = (url, options) => String(url).includes('/api/energy?')
            ? new Promise(r => setTimeout(r, 2000)).then(() => real(url, options)) : real(url, options);
          location.hash = '#/energy';
          return new Promise(r => setTimeout(() => r({price: document.getElementById('energy-price')?.value ?? null,
            doc: window.Hyperloom.state.docs['energy?range=24h']?.price ?? null}), 300));
        })()""")
        self.assertIn(held["price"], (None, "0.47"))
        self.assertIn(held["doc"], (None, {"price_per_kwh": 0.47, "currency": "EUR"}))
        self.js("window.fetch = window.__energyRealFetch;")
        smoke.eventually(lambda: self.js("!!window.Hyperloom.state.docs['energy?range=24h'] && "
                                         "document.getElementById('energy-price')?.value === '0.47'"), timeout=20)
        self.assertEqual(self.js("window.Hyperloom.state.docs['energy?range=24h'].price"),
                         {"price_per_kwh": 0.47, "currency": "EUR"})
        self.assertEqual(self.client.errors, [])

    def test_thermal_threshold_drafts_survive_polls(self):
        self.open("thermals")
        # The threshold form renders with the page's first document; wait for it on a loaded machine.
        smoke.eventually(lambda: self.js("!!document.getElementById('th-cpu-warn')"), timeout=20)
        self.js("{ const i = document.getElementById('th-cpu-warn'); i.value = '82'; i.dispatchEvent(new Event('input', {bubbles: true})); }")
        self.wait_poll()
        self.assertEqual(self.js("document.getElementById('th-cpu-warn').value"), "82")
        self.assertEqual(self.client.errors, [])

    def test_thermals_colours_follow_the_sensor(self):
        self.open("thermals")
        document = self.api("thermals?range=day")
        expected = {line["label"]: f"var(--s{line['colour'] % 5 + 1})" for m in document["machines"] for line in m["lines"]}
        shown = self.js("Object.fromEntries([...document.querySelectorAll('.th-sensor')]"
                        ".map(k => [k.dataset.sensor, k.querySelector('line').getAttribute('stroke')]))")
        for label, colour in shown.items():
            self.assertEqual(colour, expected[label], label)
        self.assertGreaterEqual(len(shown), 3)
        text = self.js("document.body.innerText")
        self.assertIn("Throttled:", text)
        self.assertIn("buckets reaching warn", text)
        self.assertEqual(self.client.errors, [])


if __name__ == "__main__":
    unittest.main()
