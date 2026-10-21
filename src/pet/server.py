"""Local web UI for pet: progress, units, claims, evidence and checks.

Stdlib only (http.server). Binds to loopback by default; every mutating call
goes through the same Store methods as the CLI.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import progress
from .config import ProjectConfig
from .db import STATES, Store
from .verify import run_check

STATIC = Path(__file__).parent / "static" / "index.html"


class Api:
    def __init__(self, root: Path):
        self.root = root
        self.config = ProjectConfig.load(root)
        self.store = Store(root / ".pet/state.db")
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in self.config.name).strip("-")
        self.project = self.store.resolve(slug)

    # ------------------------------------------------------------------ reads

    def status(self) -> dict:
        units = self.store.units(self.project["id"])
        summary = progress.summarize(units)
        claims = [dict(row) for row in self.store.claims()]
        return {
            "project": {"name": self.project["name"], "binary": self.project["binary"],
                        "slug": self.project["slug"]},
            "summary": summary,
            "claims": claims,
            "config": {"build": self.config.checks.build, "test": self.config.checks.test},
        }

    def unit_row(self, row) -> dict:
        evidence = json.loads(row["evidence"] or "[]")
        claim = self.store.db.execute(
            "SELECT agent, expires_at FROM claims WHERE unit_id=?", (row["id"],)).fetchone()
        return {
            "id": row["id"], "name": row["name"], "demangled": row["demangled"],
            "address": row["address"], "size": row["size"], "state": row["state"],
            "evidence": evidence, "notes": row["notes"],
            "claim": dict(claim) if claim else None,
        }

    def units(self, state: str | None, query: str, limit: int, offset: int) -> dict:
        units = self.store.units(self.project["id"], state or None)
        if query:
            needle = query.lower()
            units = [u for u in units
                     if needle in (u["name"] or "").lower()
                     or needle in (u["demangled"] or "").lower()
                     or needle in (u["address"] or "").lower()]
        total = len(units)
        page = units[offset:offset + limit]
        return {"total": total, "items": [self.unit_row(u) for u in page]}

    def unit(self, key: str) -> dict:
        row = self.store.unit(self.project["id"], key)
        checks = [dict(c) for c in self.store.db.execute(
            "SELECT kind,command,ok,created_at FROM checks WHERE unit_id=? ORDER BY id DESC LIMIT 10",
            (row["id"],))]
        events = [dict(e) for e in self.store.db.execute(
            "SELECT agent,kind,detail,created_at FROM events WHERE unit_id=? ORDER BY id DESC LIMIT 20",
            (row["id"],))]
        data = self.unit_row(row)
        data["checks"] = checks
        data["events"] = events
        return data

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT agent,kind,detail,created_at FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (self.project["id"], limit))
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------- writes

    def claim(self, body: dict) -> dict:
        agent = (body.get("agent") or "ui").strip() or "ui"
        key = body.get("unit")
        if key:
            unit = self.store.unit(self.project["id"], key)
        else:
            unit = self.store.db.execute(
                """SELECT u.* FROM units u LEFT JOIN claims c ON c.unit_id=u.id
                   WHERE u.project_id=? AND u.state IN ('open','draft','compiles') AND c.unit_id IS NULL
                   ORDER BY u.size ASC, u.address LIMIT 1""", (self.project["id"],)).fetchone()
            if unit is None:
                return {"error": "nothing left to claim"}
        self.store.claim(unit["id"], agent)
        self.store.event(self.project["id"], unit["id"], agent, "claim")
        return self.unit(unit["address"])

    def release(self, body: dict) -> dict:
        unit = self.store.unit(self.project["id"], body["unit"])
        notes = (body.get("notes") or "").strip()
        if not notes:
            return {"error": "notes are required"}
        agent = body.get("agent") or "ui"
        self.store.release(unit["id"], notes, agent)
        self.store.event(self.project["id"], unit["id"], agent, "release", notes)
        return self.unit(unit["address"])

    def evidence(self, body: dict) -> dict:
        unit = self.store.unit(self.project["id"], body["unit"])
        evidence_id = (body.get("id") or "").strip()
        if not evidence_id:
            return {"error": "evidence id is required"}
        self.store.attach_evidence(unit["id"], evidence_id, body.get("note") or "")
        self.store.event(self.project["id"], unit["id"], body.get("agent") or "ui",
                         "evidence", evidence_id)
        return self.unit(unit["address"])

    def note(self, body: dict) -> dict:
        unit = self.store.unit(self.project["id"], body["unit"])
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "note text is required"}
        with self.store.db:
            self.store.db.execute("UPDATE units SET notes=notes || ? || char(10) WHERE id=?",
                                  (text, unit["id"]))
        self.store.event(self.project["id"], unit["id"], body.get("agent") or "ui", "note", text)
        return self.unit(unit["address"])

    def state(self, body: dict) -> dict:
        target = body.get("state")
        if target not in STATES:
            return {"error": "unknown state"}
        unit = self.store.unit(self.project["id"], body["unit"])
        if target in ("tested", "verified") and not self.store.last_passing(unit["id"], "test"):
            return {"error": "run the test check successfully first"}
        self.store.set_state(unit["id"], target)
        self.store.event(self.project["id"], unit["id"], body.get("agent") or "ui", "state", target)
        return self.unit(unit["address"])

    def check(self, body: dict) -> dict:
        kind = body.get("check")
        if kind not in ("build", "test"):
            return {"error": "check must be build or test"}
        command = self.config.checks.command(kind)
        ok, output = run_check(self.root, command)
        unit = self.store.unit(self.project["id"], body["unit"]) if body.get("unit") else None
        self.store.record_check(unit["id"] if unit else None, kind, command, ok, output)
        self.store.event(self.project["id"], unit["id"] if unit else None,
                         body.get("agent") or "ui", "check", f"{kind}:{'ok' if ok else 'fail'}")
        if unit and ok and kind == "build" and unit["state"] in ("open", "draft"):
            self.store.set_state(unit["id"], "compiles")
        return {"ok": ok, "output": output[-8000:], "unit": self.unit(unit["address"]) if unit else None}

    def export(self) -> str:
        units = self.store.units(self.project["id"])
        summary = progress.summarize(units)
        lines = [f"# {self.project['name']} — reconstruction progress", "",
                 f"- units: {summary['verified']}/{summary['total']} verified",
                 f"- weighted: {summary['percent_units']:.1f}% units, "
                 f"{summary['percent_bytes']:.1f}% bytes",
                 f"- assumptions: {summary['assumptions']}, blocked: {summary['blocked']}, "
                 f"evidence: {summary['evidence']}", "",
                 "| address | size | state | unit | evidence |", "|---|---|---|---|---|"]
        for u in units:
            lines.append(f"| {u['address']} | {u['size']} | {u['state']} | "
                         f"`{u['demangled'] or u['name']}` | {len(json.loads(u['evidence']))} |")
        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    api: Api

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status: int = 200) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            self._json(self.api.status())
            return
        if parsed.path == "/api/units":
            self._json(self.api.units(
                (query.get("state") or [""])[0] or None,
                (query.get("q") or [""])[0],
                int((query.get("limit") or ["100"])[0]),
                int((query.get("offset") or ["0"])[0])))
            return
        if parsed.path == "/api/events":
            self._json(self.api.events(int((query.get("limit") or ["100"])[0])))
            return
        if parsed.path == "/api/export":
            self._send(200, self.api.export().encode("utf-8"), "text/markdown; charset=utf-8")
            return
        if parsed.path.startswith("/api/unit/"):
            key = parsed.path.split("/api/unit/", 1)[1]
            self._json(self.api.unit(key))
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "invalid json"}, 400)
            return
        routes = {
            "/api/claim": self.api.claim,
            "/api/release": self.api.release,
            "/api/evidence": self.api.evidence,
            "/api/note": self.api.note,
            "/api/state": self.api.state,
            "/api/check": self.api.check,
        }
        handler = routes.get(urlparse(self.path).path)
        if handler is None:
            self._json({"error": "not found"}, 404)
            return
        try:
            result = handler(body)
        except SystemExit as error:
            self._json({"error": str(error)}, 400)
            return
        if isinstance(result, dict) and result.get("error"):
            self._json(result, 400)
            return
        self._json(result)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    api = Api(root)
    handler = type("BoundHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}/"
    print(f"pet ui → {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        api.store.close()
