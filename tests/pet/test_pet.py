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


@unittest.skipUnless(SLICE.exists(), "Bartender slice not available")
class LoopTests(unittest.TestCase):
    """brief → bind → apply → verify, plus the external-agent runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "pet.toml").write_text(
            f'[project]\nname = "Loop"\nbinary = "{SLICE}"\n\n'
            '[checks]\nbuild = "true"\ntest = "true"\nverify = "true"\n')
        (self.root / "Source.swift").write_text("func placeholder() {}\n")
        self.store = Store(self.root / ".pet/state.db")
        self.project_id = self.store.upsert_project(
            "loop", "Loop", self.root, str(SLICE))
        self.store.add_units(self.project_id, [
            {"name": "a", "address": "0x100002a98", "size": 92},
            {"name": "b", "address": "0x100002af4", "size": 184}])
        self.unit = self.store.unit(self.project_id, "a")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_brief_has_assembly_and_neighbours(self):
        from pet.brief import brief
        project = self.store.resolve("loop")
        data = brief(self.store, project, self.unit)
        self.assertIn("sub", data["asm"])
        self.assertTrue(any(n["address"] == "0x100002af4" for n in data["neighbours"]))
        self.assertEqual(data["state"], "open")

    def test_apply_writes_marker_block_and_replaces_it(self):
        from pet.edit import apply_code
        path = self.root / "Source.swift"
        apply_code(self.store, self.project_id, self.unit, path, "func first() {}")
        apply_code(self.store, self.project_id, self.unit, path, "func second() {}")
        text = path.read_text()
        self.assertEqual(text.count("pet:begin 0x100002a98"), 1)
        self.assertIn("func second() {}", text)
        self.assertNotIn("func first() {}", text)
        binding = self.store.db.execute(
            "SELECT path FROM bindings WHERE unit_id=?", (self.unit["id"],)).fetchone()
        self.assertEqual(binding["path"], str(path.resolve()))

    def test_apply_literal_replacement(self):
        from pet.edit import apply_code
        path = self.root / "Source.swift"
        apply_code(self.store, self.project_id, self.unit, path, "",
                   old="func placeholder() {}", new="func real() {}")
        self.assertEqual(path.read_text().strip(), "func real() {}")

    def test_verify_promotes_through_the_chain(self):
        from pet.cli import cmd_verify

        class Args:
            project = str(self.root)
            unit = "0x100002a98"
            agent = "test"
        self.assertEqual(cmd_verify(Args()), 0)
        self.assertEqual(self.store.unit(self.project_id, "0x100002a98")["state"], "verified")
        self.assertTrue(self.store.last_passing(self.unit["id"], "verify"))

    def test_work_applies_agent_json(self):
        from pet.cli import cmd_work
        agent = self.root / "agent.py"
        agent.write_text(
            "import json,sys\n"
            "data=json.load(sys.stdin)\n"
            "print(json.dumps({'file': '" + str(self.root / 'Source.swift') + "',"
            " 'code': 'func fromAgent() {}', 'notes': 'done'}))\n")

        Args = type("Args", (), {
            "project": str(self.root),
            "agent_cmd": f"{sys.executable} {agent}",
            "limit": 1,
            "timeout": 60,
            "agent": "tester",
        })
        self.assertEqual(cmd_work(Args()), 0)
        self.assertIn("fromAgent", (self.root / "Source.swift").read_text())
        states = [u["state"] for u in self.store.units(self.project_id)]
        self.assertIn("verified", states)


class McpToolTests(unittest.TestCase):
    def test_tools_include_the_loop(self):
        sys.path.insert(0, str(ROOT / "src"))
        from pet.mcp import TOOLS
        names = {name for name, _description, _schema in TOOLS}
        for expected in ("brief_unit", "bind_unit", "apply_code", "verify_unit"):
            self.assertIn(expected, names)


class ServerTests(unittest.TestCase):
    def _api(self, tmp: Path):
        from pet.server import Api
        from pet.db import Store

        (tmp / "pet.toml").write_text(
            '[project]\nname = "Demo"\n\n[checks]\nbuild = "true"\ntest = "true"\n')
        store = Store(tmp / ".pet/state.db")
        project = store.upsert_project("demo", "Demo", tmp, "")
        store.add_units(project, [{"name": "a", "address": "0x100", "size": 4},
                                  {"name": "b", "address": "0x104", "size": 8}])
        store.close()
        return Api(tmp)

    def test_html_and_api(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from pet.server import Handler

        with tempfile.TemporaryDirectory() as tmp:
            api = self._api(Path(tmp))
            handler = type("Bound", (Handler,), {"api": api})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base + "/") as response:
                    self.assertIn(b"pet", response.read())
                with urllib.request.urlopen(base + "/api/status") as response:
                    status = json.loads(response.read())
                    self.assertEqual(status["summary"]["total"], 2)
                request = urllib.request.Request(
                    base + "/api/state",
                    data=json.dumps({"unit": "0x100", "state": "tested"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 400)
                request = urllib.request.Request(
                    base + "/api/check",
                    data=json.dumps({"check": "test", "unit": "0x100"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                request = urllib.request.Request(
                    base + "/api/state",
                    data=json.dumps({"unit": "0x100", "state": "tested"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.loads(response.read())["state"], "tested")
            finally:
                server.shutdown()
                server.server_close()
                api.store.close()


if __name__ == "__main__":
    unittest.main()
