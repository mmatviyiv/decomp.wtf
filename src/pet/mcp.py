"""Minimal MCP (Model Context Protocol) stdio server.

Only the pieces an agent needs: initialize, tools/list, tools/call and ping.
No third-party packages, so it also runs on a bare macOS Python.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import progress
from .brief import brief, brief_text
from .config import ProjectConfig
from .edit import apply_code
from .db import STATES, Store
from .inventory import inventory, load_names
from .verify import run_check

TOOLS = [
    ("project_info", "Project name, binary and progress summary.", {}),
    ("list_units", "List reconstruction units.",
     {"state": {"type": "string", "enum": list(STATES)},
      "limit": {"type": "integer", "default": 40}}),
    ("claim_unit", "Claim a unit (next smallest when omitted).",
     {"unit": {"type": "string"}, "agent": {"type": "string", "default": "agent"}}),
    ("release_unit", "Release a claim with notes.",
     {"unit": {"type": "string"}, "notes": {"type": "string"},
      "agent": {"type": "string", "default": "agent"}}),
    ("add_note", "Append a note to a unit.",
     {"unit": {"type": "string"}, "text": {"type": "string"}}),
    ("attach_evidence", "Attach an Evidence ID (REA/Ghidra) to a unit.",
     {"unit": {"type": "string"}, "evidence_id": {"type": "string"},
      "note": {"type": "string"}}),
    ("set_state", "Set a unit state (tested/verified need a passing test).",
     {"unit": {"type": "string"}, "state": {"type": "string", "enum": list(STATES)}}),
    ("run_check", "Run the configured build or test command.",
     {"check": {"type": "string", "enum": ["build", "test"]},
      "unit": {"type": "string"}}),
    ("brief_unit", "Disassembly, neighbours, evidence, notes and binding for a unit.",
     {"unit": {"type": "string"}}),
    ("bind_unit", "Bind a unit to a source file.",
     {"unit": {"type": "string"}, "file": {"type": "string"}, "anchor": {"type": "string"}}),
    ("apply_code", "Write code into the bound (or given) source file.",
     {"unit": {"type": "string"}, "file": {"type": "string"},
      "code": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
      "commit": {"type": "boolean", "default": False}}),
    ("verify_unit", "Run build, test and the optional per-unit verify command.",
     {"unit": {"type": "string"}}),
    ("import_binary", "Inventory a Mach-O into the project.",
     {"binary": {"type": "string"}, "names": {"type": "string"}}),
    ("export_progress", "Markdown progress table.", {}),
]


class Server:
    def __init__(self, root: Path):
        self.root = root
        self.config = ProjectConfig.load(root)
        self.store = Store(root / ".pet/state.db")

    def project(self):
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in self.config.name).strip("-")
        return self.store.resolve(slug)

    def call(self, name: str, args: dict) -> str:
        project = self.project()
        if name == "project_info":
            units = self.store.units(project["id"])
            summary = progress.summarize(units)
            return (f"{project['name']} ({project['binary']})\n"
                    f"{summary['verified']}/{summary['total']} verified, "
                    f"{summary['percent_units']:.1f}% units, {summary['percent_bytes']:.1f}% bytes, "
                    f"{summary['assumptions']} assumptions, {summary['blocked']} blocked")
        if name == "list_units":
            rows = self.store.units(project["id"], args.get("state"))
            limit = int(args.get("limit") or 40)
            return "\n".join(f"{u['address']}\t{u['state']:<10}\t{u['demangled'] or u['name']}"
                             for u in rows[:limit])
        if name == "claim_unit":
            unit = (self.store.unit(project["id"], args["unit"]) if args.get("unit")
                    else self._next(project["id"]))
            self.store.claim(unit["id"], args.get("agent") or "agent")
            self.store.event(project["id"], unit["id"], args.get("agent") or "agent", "claim")
            return f"{unit['address']} {unit['demangled'] or unit['name']} (state {unit['state']})"
        if name == "release_unit":
            unit = self.store.unit(project["id"], args["unit"])
            self.store.release(unit["id"], args["notes"], args.get("agent") or "agent")
            self.store.event(project["id"], unit["id"], args.get("agent") or "agent",
                             "release", args["notes"])
            return "released"
        if name == "add_note":
            unit = self.store.unit(project["id"], args["unit"])
            with self.store.db:
                self.store.db.execute(
                    "UPDATE units SET notes=notes || ? || char(10) WHERE id=?",
                    (args["text"], unit["id"]))
            return "note added"
        if name == "attach_evidence":
            unit = self.store.unit(project["id"], args["unit"])
            self.store.attach_evidence(unit["id"], args["evidence_id"], args.get("note") or "")
            self.store.event(project["id"], unit["id"], "agent", "evidence", args["evidence_id"])
            return "evidence attached"
        if name == "set_state":
            unit = self.store.unit(project["id"], args["unit"])
            if args["state"] in ("tested", "verified") and not self.store.last_passing(unit["id"], "test"):
                return "refused: run_check test must pass first"
            self.store.set_state(unit["id"], args["state"])
            self.store.event(project["id"], unit["id"], "agent", "state", args["state"])
            return f"{unit['name']} -> {args['state']}"
        if name == "brief_unit":
            unit = self.store.unit(project["id"], args["unit"])
            return brief_text(brief(self.store, project, unit))
        if name == "bind_unit":
            unit = self.store.unit(project["id"], args["unit"])
            path = Path(args["file"]).resolve()
            if not path.exists():
                return f"no such file: {path}"
            with self.store.db:
                self.store.db.execute(
                    """INSERT INTO bindings(unit_id,path,anchor,updated_at)
                       VALUES(?,?,?,strftime('%s','now'))
                       ON CONFLICT(unit_id) DO UPDATE SET path=excluded.path,
                         anchor=excluded.anchor""",
                    (unit["id"], str(path), args.get("anchor") or unit["address"]))
            self.store.event(project["id"], unit["id"], "agent", "bind", str(path))
            return f"bound {unit['address']} -> {path}"
        if name == "apply_code":
            unit = self.store.unit(project["id"], args["unit"])
            path = Path(args["file"]) if args.get("file") else None
            if path is None:
                row = self.store.db.execute(
                    "SELECT path FROM bindings WHERE unit_id=?", (unit["id"],)).fetchone()
                if row is None:
                    return "pass file or run bind_unit first"
                path = Path(row["path"])
            result = apply_code(self.store, project["id"], unit, path,
                                args.get("code") or "",
                                old=args.get("old"), new=args.get("new"),
                                commit=bool(args.get("commit")), root=self.root)
            return f"wrote {result['path']} (anchor {result['anchor']})" + (
                " and committed" if result["committed"] else "")
        if name == "verify_unit":
            unit = self.store.unit(project["id"], args["unit"])
            env = {"PET_UNIT_ADDRESS": unit["address"],
                   "PET_UNIT_NAME": unit["demangled"] or unit["name"]}
            results = {}
            for check in ("build", "test", "verify"):
                command = self.config.checks.command(check)
                if not command:
                    continue
                ok, output = run_check(self.root, command, env=env)
                self.store.record_check(unit["id"], check, command, ok, output)
                self.store.event(project["id"], unit["id"], "agent", "check",
                                 f"{check}:{'ok' if ok else 'fail'}")
                results[check] = (ok, output)
                if not ok:
                    break
            if results.get("build", (False, ""))[0] and unit["state"] in ("open", "draft"):
                self.store.set_state(unit["id"], "compiles")
            if results.get("test", (False, ""))[0]:
                self.store.set_state(unit["id"], "tested")
            if results.get("verify", (False, ""))[0]:
                self.store.set_state(unit["id"], "verified")
            lines = [f"{check}: {'ok' if ok else 'failed'}" for check, (ok, _) in results.items()]
            return "\n".join(lines) + f"\nstate: {self.store.unit(project['id'], unit['address'])['state']}"
        if name == "run_check":
            check = args["check"]
            command = self.config.checks.command(check)
            ok, output = run_check(self.root, command)
            unit = self.store.unit(project["id"], args["unit"]) if args.get("unit") else None
            self.store.record_check(unit["id"] if unit else None, check, command, ok, output)
            self.store.event(project["id"], unit["id"] if unit else None, "agent", "check",
                             f"{check}:{'ok' if ok else 'fail'}")
            return f"{check}: {'ok' if ok else 'failed'}\n{output[-4000:]}"
        if name == "import_binary":
            names = load_names(Path(args["names"])) if args.get("names") else {}
            units = inventory(Path(args["binary"]), names)
            added = self.store.add_units(project["id"], units)
            return f"imported {added} units"
        if name == "export_progress":
            units = self.store.units(project["id"])
            summary = progress.summarize(units)
            return json.dumps(summary, indent=2)
        return f"unknown tool {name}"

    def _next(self, project_id: int):
        row = self.store.db.execute(
            """SELECT u.* FROM units u LEFT JOIN claims c ON c.unit_id=u.id
               WHERE u.project_id=? AND u.state IN ('open','draft','compiles') AND c.unit_id IS NULL
               ORDER BY u.size ASC, u.address LIMIT 1""", (project_id,)).fetchone()
        if row is None:
            raise SystemExit("nothing left to claim")
        return row


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def serve(root: Path) -> None:
    server = Server(root)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            continue  # notification
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "pet", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "description": description,
                 "inputSchema": _schema(properties)}
                for name, description, properties in TOOLS]}
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            known = {tool[0] for tool in TOOLS}
            if name not in known:
                response = {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32601, "message": f"unknown tool {name}"}}
                print(json.dumps(response), flush=True)
                continue
            try:
                text = server.call(name, arguments)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except SystemExit as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            except Exception as error:  # keep the session alive for the agent
                result = {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                          "isError": True}
        else:
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": f"unknown method {method}"}}
            print(json.dumps(response), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
