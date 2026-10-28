"""Keep the portal's public route inventory and docs/api.md in sync.

Entries are Markdown rows whose first cell is exactly a backticked METHOD path.
GET prefixes use <id>, regardless of the handler's internal parameter name.
"""
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from hyperloom.web import features, recipes
from hyperloom.web.api import MUTATIONS, SCREENS


ENTRY = re.compile(r"^\s*\|\s*`([A-Z]+ [^`]+)`\s*\|", re.MULTILINE)


class APIDocsTests(unittest.TestCase):
    def test_documented_routes_match_portal(self):
        # Use the portal's real loader and register functions, never API()/serve().
        # This optional capability probes third_party files during registration;
        # it does not affect routes. Suppress that probe to keep this test passive.
        with patch("hyperloom.cliext.planner.planner_available", return_value=(False, "docs test")):
            registry = features.load()
        self.assertFalse(registry.failed, f"Feature registration failed: {registry.failed}")
        self.assertEqual(set(features.MODULES), set(registry.loaded), "A feature module was not loaded")

        expected = {f"GET {name}" for name in SCREENS}
        expected.update(f"POST {name}" for name in MUTATIONS)
        expected.update(f"GET {name}" for name in recipes.READS)
        expected.update(f"POST {name}" for name in recipes.POSTS)
        expected.update({"GET recipes/<id>", "GET jobs", "GET jobs/<id>",
                         "POST jobs/renew", "GET stream"})
        expected.update(f"GET {name}" for name in registry.gets)
        expected.update(f"GET {prefix}<id>" for prefix in registry.prefixes)
        expected.update(f"POST {name}" for name in registry.posts)
        expected.update(f"POST {name}" for name in registry.jobs)

        document = (Path(__file__).resolve().parents[1] / "docs" / "api.md").read_text(encoding="utf-8")
        entries = ENTRY.findall(document)
        documented = set(entries)
        missing = sorted(expected - documented)
        stale = sorted(documented - expected)
        duplicates = sorted(route for route in documented if entries.count(route) > 1)
        self.assertFalse(missing or stale or duplicates,
                         "API documentation mismatch\n"
                         f"Missing ({len(missing)}): {missing}\n"
                         f"Stale ({len(stale)}): {stale}\n"
                         f"Duplicate ({len(duplicates)}): {duplicates}")


if __name__ == "__main__":
    unittest.main()
