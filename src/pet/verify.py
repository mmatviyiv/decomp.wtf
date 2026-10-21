"""Run the project's build/test commands and record the outcome."""

from __future__ import annotations

import subprocess
from pathlib import Path

DEFAULT_TIMEOUT = 1800


def run_check(root: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    if not command.strip():
        return False, "no command configured for this check"
    try:
        proc = subprocess.run(command, cwd=root, shell=True, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return False, f"timeout after {timeout}s\n{error.stdout or ''}{error.stderr or ''}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output
