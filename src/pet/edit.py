"""Write reconstructed code into a source file and keep the unit binding."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .db import Store

BEGIN = "// pet:begin {anchor}"
END = "// pet:end {anchor}"


def _block(anchor: str, code: str) -> str:
    body = code.rstrip("\n")
    return f"{BEGIN.format(anchor=anchor)}\n{body}\n{END.format(anchor=anchor)}"


def apply_code(store: Store, project_id: int, unit, path: Path, code: str,
               old: str | None = None, new: str | None = None,
               commit: bool = False, root: Path | None = None) -> dict:
    """Apply code either by literal replacement (old/new) or by marker block."""
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    text = path.read_text(encoding="utf-8")
    anchor = unit["address"]

    if old is not None and new is not None:
        if old not in text:
            raise SystemExit("the `old` text is not present in the file")
        text = text.replace(old, new, 1)
    else:
        block = _block(anchor, code)
        begin = BEGIN.format(anchor=anchor)
        end = END.format(anchor=anchor)
        if begin in text and end in text:
            start = text.index(begin)
            stop = text.index(end, start) + len(end)
            text = text[:start] + block + text[stop:]
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")

    with store.db:
        store.db.execute(
            """INSERT INTO bindings(unit_id,path,anchor,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(unit_id) DO UPDATE SET path=excluded.path,
                 anchor=excluded.anchor, updated_at=excluded.updated_at""",
            (unit["id"], str(path), anchor, time.time()))
    store.event(project_id, unit["id"], "pet", "apply", str(path))

    committed = False
    if commit and root is not None:
        relative = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        result = subprocess.run(["git", "add", relative], cwd=root, capture_output=True, text=True)
        if result.returncode == 0:
            message = f"pet: {unit['demangled'] or unit['name']} ({unit['address']})"
            result = subprocess.run(["git", "commit", "-m", message], cwd=root,
                                    capture_output=True, text=True)
            committed = result.returncode == 0
    return {"path": str(path), "anchor": anchor, "committed": committed}
