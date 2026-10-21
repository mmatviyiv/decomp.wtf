"""SQLite state store for units, claims, evidence and checks."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

STATES = ("open", "draft", "compiles", "tested", "verified", "assumption", "blocked")
# States that count as real progress for the completion percentage.
DONE_STATES = ("verified",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    root TEXT NOT NULL,
    binary TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    demangled TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open',
    evidence TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS claims (
    unit_id INTEGER PRIMARY KEY REFERENCES units(id),
    agent TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    unit_id INTEGER,
    agent TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER REFERENCES units(id),
    kind TEXT NOT NULL,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

CLAIM_TTL = 30 * 60


@dataclass
class Unit:
    id: int
    name: str
    demangled: str
    address: str
    size: int
    state: str
    evidence: list[dict]
    notes: str


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The web UI serves requests from several threads; Python's sqlite3 is
        # built with serialized threading (threadsafety 3) on macOS.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- projects

    def upsert_project(self, slug: str, name: str, root: Path,
                       binary: str = "", config: dict | None = None) -> int:
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT INTO projects(slug,name,root,binary,config,created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name, root=excluded.root,
                     binary=excluded.binary, config=excluded.config""",
                (slug, name, str(root), binary, json.dumps(config or {}), now),
            )
        row = self.db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        return int(row["id"])

    def resolve(self, slug: str):
        """Project by slug, falling back to the only project (pet.toml renames happen)."""
        row = self.db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        if row is not None:
            return row
        rows = self.db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        if len(rows) == 1:
            return rows[0]
        raise SystemExit(f"no project {slug!r}; run `pet import`")

    def project(self, slug: str | None = None):
        if slug:
            return self.db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        rows = self.db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        if len(rows) == 1:
            return rows[0]
        raise SystemExit("several projects; pass --project <slug>")

    # ------------------------------------------------------------------- units

    def add_units(self, project_id: int, units: list[dict]) -> int:
        now = time.time()
        added = 0
        with self.db:
            for unit in units:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO units(project_id,name,demangled,address,size,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (project_id, unit["name"], unit.get("demangled", ""),
                     unit.get("address", ""), unit.get("size", 0), now),
                )
                added += cur.rowcount
        return added

    def units(self, project_id: int, state: str | None = None):
        if state:
            return self.db.execute(
                "SELECT * FROM units WHERE project_id=? AND state=? ORDER BY address, name",
                (project_id, state)).fetchall()
        return self.db.execute(
            "SELECT * FROM units WHERE project_id=? ORDER BY address, name", (project_id,)).fetchall()

    def unit(self, project_id: int, name: str):
        normalized = name.lower() if name.lower().startswith("0x") else name
        row = self.db.execute(
            "SELECT * FROM units WHERE project_id=? AND (name=? OR demangled=? OR address=?)",
            (project_id, name, name, normalized)).fetchone()
        if row is None:
            raise SystemExit(f"no unit {name!r}")
        return row

    # ------------------------------------------------------------------ claims

    def claim(self, unit_id: int, agent: str, ttl: int = CLAIM_TTL) -> None:
        now = time.time()
        with self.db:
            self._expire(now)
            held = self.db.execute("SELECT * FROM claims WHERE agent=? AND unit_id!=?",
                                   (agent, unit_id)).fetchone()
            if held is not None:
                raise SystemExit(f"{agent} already holds unit {held['unit_id']}; release it first")
            other = self.db.execute("SELECT * FROM claims WHERE unit_id=?", (unit_id,)).fetchone()
            if other is not None and other["agent"] != agent and other["expires_at"] > now:
                raise SystemExit(f"unit is claimed by {other['agent']}")
            self.db.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?)",
                            (unit_id, agent, now + ttl))

    def release(self, unit_id: int, notes: str, agent: str = "") -> None:
        with self.db:
            self.db.execute("DELETE FROM claims WHERE unit_id=?", (unit_id,))
            self.db.execute("UPDATE units SET notes=?, updated_at=? WHERE id=?",
                            (notes, time.time(), unit_id))

    def claims(self):
        return self.db.execute(
            "SELECT c.*, u.name FROM claims c JOIN units u ON u.id=c.unit_id").fetchall()

    def _expire(self, now: float) -> None:
        self.db.execute("DELETE FROM claims WHERE expires_at < ?", (now,))

    # ---------------------------------------------------------------- evidence

    def attach_evidence(self, unit_id: int, evidence_id: str, note: str = "") -> None:
        row = self.db.execute("SELECT evidence FROM units WHERE id=?", (unit_id,)).fetchone()
        items = json.loads(row["evidence"]) if row else []
        items.append({"id": evidence_id, "note": note, "added_at": time.time()})
        with self.db:
            self.db.execute("UPDATE units SET evidence=?, updated_at=? WHERE id=?",
                            (json.dumps(items), time.time(), unit_id))

    # ------------------------------------------------------------------ checks

    def record_check(self, unit_id: int | None, kind: str, command: str,
                     ok: bool, output: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO checks(unit_id,kind,command,ok,output,created_at) VALUES(?,?,?,?,?,?)",
                (unit_id, kind, command, int(ok), output[-8000:], time.time()))

    def last_passing(self, unit_id: int, kind: str) -> bool:
        row = self.db.execute(
            "SELECT ok FROM checks WHERE unit_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (unit_id, kind)).fetchone()
        return bool(row and row["ok"])

    def set_state(self, unit_id: int, state: str) -> None:
        if state not in STATES:
            raise SystemExit(f"state must be one of {', '.join(STATES)}")
        with self.db:
            self.db.execute("UPDATE units SET state=?, updated_at=? WHERE id=?",
                            (state, time.time(), unit_id))

    # ------------------------------------------------------------------ events

    def event(self, project_id: int, unit_id: int | None, agent: str,
              kind: str, detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events(project_id,unit_id,agent,kind,detail,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, unit_id, agent, kind, detail, time.time()))

    def close(self) -> None:
        self.db.close()
