"""pet tests: store, progress, CLI, MCP. Run with `python3 -m unittest discover tests/pet`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pet import progress
from pet.db import Store
from pet.inventory import inventory, load_names

SLICE = Path("/tmp/bartender_slices/Bartender6_arm64")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / ".pet/state.db")
        self.project = self.store.upsert_project("demo", "Demo", Path("/tmp"), "bin")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_units_are_deduplicated(self):
        units = [{"name": "a", "address": "0x10", "size": 4},
                 {"name": "a", "address": "0x10", "size": 4},
                 {"name": "b", "address": "0x20", "size": 8}]
        self.assertEqual(self.store.add_units(self.project, units), 2)
        self.assertEqual(len(self.store.units(self.project)), 2)

    def test_claim_release_and_state(self):
        self.store.add_units(self.project, [{"name": "a", "address": "0x10", "size": 4}])
        unit = self.store.unit(self.project, "a")
        self.store.claim(unit["id"], "me")
        self.assertEqual(len(self.store.claims()), 1)
        self.store.release(unit["id"], "tried")
        self.assertEqual(self.store.claims(), [])
        self.store.set_state(unit["id"], "draft")
        self.assertEqual(self.store.units(self.project)[0]["state"], "draft")

    def test_second_agent_cannot_claim(self):
        self.store.add_units(self.project, [{"name": "a", "address": "0x10", "size": 4}])
        unit = self.store.unit(self.project, "a")
        self.store.claim(unit["id"], "me")
        with self.assertRaises(SystemExit):
            self.store.claim(unit["id"], "other")

    def test_evidence_is_kept_as_list(self):
        self.store.add_units(self.project, [{"name": "a", "address": "0x10", "size": 4}])
        unit = self.store.unit(self.project, "a")
        self.store.attach_evidence(unit["id"], "ev_1", "string literal")
        self.store.attach_evidence(unit["id"], "ev_2")
        stored = json.loads(self.store.unit(self.project, "a")["evidence"])
        self.assertEqual([item["id"] for item in stored], ["ev_1", "ev_2"])


class ProgressTests(unittest.TestCase):
    def test_weights_and_assumptions(self):
        units = [
            {"state": "verified", "size": 10, "evidence": "[]"},
            {"state": "tested", "size": 10, "evidence": '[{"id":"e"}]'},
            {"state": "assumption", "size": 10, "evidence": "[]"},
            {"state": "open", "size": 10, "evidence": "[]"},
        ]
        summary = progress.summarize(units)
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["assumptions"], 1)
        self.assertEqual(summary["evidence"], 1)
        self.assertAlmostEqual(summary["percent_units"], 60.0)  # 1 + 0.8 + 0.6


@unittest.skipUnless(SLICE.exists(), "Bartender slice not available")
class InventoryTests(unittest.TestCase):
    def test_stripped_binary_still_yields_functions(self):
        units = inventory(SLICE)
        self.assertGreater(len(units), 1000)
        self.assertTrue(all(unit["address"].startswith("0x") for unit in units))

    def test_names_file_overrides_symbols(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as handle:
            handle.write("0x100002a98\tFRFeedbackController::init\n")
            path = Path(handle.name)
        try:
            names = load_names(path)
            self.assertEqual(names[0x100002A98], "FRFeedbackController::init")
            units = inventory(SLICE, names)
            match = [unit for unit in units if unit["address"] == "0x100002a98"]
            self.assertEqual(match[0]["demangled"], "FRFeedbackController::init")
        finally:
            path.unlink()


class CliTests(unittest.TestCase):
    def test_init_import_status_roundtrip(self):
        if not SLICE.exists():
            self.skipTest("Bartender slice not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run([str(ROOT / "pet"), "--project", str(root), "init", str(root),
                            "--binary", str(SLICE), "--name", "Demo"], check=True,
                           capture_output=True, text=True)
            subprocess.run([str(ROOT / "pet"), "--project", str(root), "import"], check=True,
                           capture_output=True, text=True)
            result = subprocess.run([str(ROOT / "pet"), "--project", str(root), "status", "--json"],
                                    check=True, capture_output=True, text=True)
            summary = json.loads(result.stdout)
            self.assertGreater(summary["total"], 1000)
            self.assertEqual(summary["verified"], 0)

    def test_mcp_lists_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pet.toml").write_text('[project]\nname = "Demo"\n')
            subprocess.run([str(ROOT / "pet"), "--project", str(root), "init", str(root)],
                           check=True, capture_output=True, text=True)
            # init overwrites pet.toml with a default name; import is not needed for tools/list
            process = subprocess.Popen([str(ROOT / "pet"), "--project", str(root), "mcp"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                requests = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                ]
                for request in requests:
                    process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                first = json.loads(process.stdout.readline())
                second = json.loads(process.stdout.readline())
                self.assertEqual(first["result"]["serverInfo"]["name"], "pet")
                names = [tool["name"] for tool in second["result"]["tools"]]
                self.assertIn("claim_unit", names)
                self.assertIn("attach_evidence", names)
            finally:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
