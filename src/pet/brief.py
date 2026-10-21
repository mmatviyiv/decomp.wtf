"""Per-unit brief: disassembly, evidence, notes, neighbours and bindings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .db import Store
from .inventory import thin_slice


def disassemble(binary: Path, address: str, size: int, limit: int = 400) -> str:
    start = int(address, 16)
    end = start + max(4, min(size, 4096))
    result = subprocess.run(
        ["objdump", "-d", f"--start-address=0x{start:x}", f"--stop-address=0x{end:x}",
         str(thin_slice(binary))],
        capture_output=True, text=True)
    lines = [line for line in result.stdout.splitlines()
             if line and not line.startswith(("Disassembly", "0000", str(binary)))]
    return "\n".join(lines[:limit])


def neighbours(store: Store, project_id: int, address: str, span: int = 3) -> list[dict]:
    rows = store.db.execute(
        "SELECT name, demangled, address, size FROM units WHERE project_id=? ORDER BY address",
        (project_id,)).fetchall()
    index = next((i for i, row in enumerate(rows) if row["address"] == address), None)
    if index is None:
        return []
    window = rows[max(0, index - span): index] + rows[index + 1: index + 1 + span]
    return [dict(row) for row in window]


def binding(store: Store, unit_id: int):
    return store.db.execute("SELECT * FROM bindings WHERE unit_id=?", (unit_id,)).fetchone()


def brief(store: Store, project, unit) -> dict:
    evidence = json.loads(unit["evidence"] or "[]")
    checks = [dict(row) for row in store.db.execute(
        "SELECT kind, ok, created_at FROM checks WHERE unit_id=? ORDER BY id DESC LIMIT 5",
        (unit["id"],))]
    bind = binding(store, unit["id"])
    asm = ""
    if project["binary"]:
        asm = disassemble(Path(project["binary"]), unit["address"], unit["size"])
    return {
        "name": unit["demangled"] or unit["name"],
        "raw_name": unit["name"],
        "address": unit["address"],
        "size": unit["size"],
        "state": unit["state"],
        "notes": unit["notes"],
        "evidence": evidence,
        "checks": checks,
        "binding": dict(bind) if bind else None,
        "neighbours": neighbours(store, unit["project_id"], unit["address"]),
        "asm": asm,
    }


def brief_text(data: dict, include_asm: bool = True) -> str:
    lines = [f"{data['address']}  {data['name']}  ({data['size']} B, {data['state']})"]
    if data.get("binding"):
        lines.append(f"source: {data['binding']['path']} (anchor {data['binding']['anchor']})")
    if data.get("neighbours"):
        lines.append("neighbours: " + ", ".join(
            f"{n['address']} {n['demangled'] or n['name']}" for n in data["neighbours"]))
    if data.get("evidence"):
        lines.append("evidence: " + ", ".join(e["id"] for e in data["evidence"]))
    if data.get("checks"):
        lines.append("checks: " + ", ".join(
            f"{c['kind']}:{'ok' if c['ok'] else 'fail'}" for c in data["checks"]))
    if data.get("notes"):
        lines.append("notes:\n" + data["notes"].rstrip())
    if include_asm and data.get("asm"):
        lines += ["", "assembly:", data["asm"]]
    return "\n".join(lines)
