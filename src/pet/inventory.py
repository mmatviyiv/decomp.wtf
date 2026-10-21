"""Mach-O text-symbol inventory.

Uses the system toolchain (nm / otool / swift-demangle) so it works on any
macOS host without extra Python packages. Function starts come from the symbol
table; approximate sizes from address deltas inside the same symbol run.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

NM_LINE = re.compile(r"^([0-9a-fA-F]{8,16})\s+([tT])\s+(.+)$")
NAME_LINE = re.compile(r"^(?:0x)?([0-9a-fA-F]{6,16})[\t ,]+(.+)$")
ADDRESS = re.compile(r"^[0-9a-fA-F]+$")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def thin_slice(binary: Path) -> Path:
    """Return the arm64 slice for a universal binary, else the binary itself."""
    out = run("lipo", "-info", str(binary))
    if "Non-fat file" in out or not out:
        return binary
    if "arm64" not in out:
        return binary
    target = binary.with_name(binary.name + ".arm64")
    if not target.exists():
        result = subprocess.run(["lipo", "-thin", "arm64", str(binary), "-output", str(target)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return binary
    return target


def demangle(symbols: list[str]) -> list[str]:
    if not symbols:
        return []
    proc = subprocess.run(["xcrun", "swift-demangle"], input="\n".join(symbols),
                          capture_output=True, text=True)
    lines = proc.stdout.splitlines()
    if len(lines) != len(symbols):
        return symbols
    return [line.strip() or original for line, original in zip(lines, symbols)]


def text_symbols(binary: Path) -> list[tuple[int, str]]:
    """(address, raw symbol) for every text symbol, ordered by address."""
    out = run("nm", "-n", "-U", str(binary))
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        match = NM_LINE.match(line.strip())
        if not match:
            continue
        address, _kind, symbol = match.groups()
        if symbol.startswith(("___", "_objc_msgSend", "_$sSo", "_$sSa", "_$sSD")):
            continue
        found.append((int(address, 16), symbol))
    return found


def load_names(path: Path) -> dict[int, str]:
    """Address → name TSV/CSV, e.g. evidence/procedures.tsv or objc-methods.tsv."""
    names: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = NAME_LINE.match(line.strip())
        if not match:
            continue
        address, name = match.groups()
        names.setdefault(int(address, 16), name.strip())
    return names


def inventory(binary: Path, names: dict[int, str] | None = None) -> list[dict]:
    """Units for every function start.

    Prefers the symbol table when the binary keeps one; otherwise unfolds
    LC_FUNCTION_STARTS so stripped release binaries still get a unit per
    function. Names from `names` (address → name) win over synthesized ones.
    """
    from .macho import function_starts

    binary = thin_slice(binary)
    symbols = text_symbols(binary)
    names = names or {}
    real_starts = {address for address, raw in symbols if "mh_execute_header" not in raw}
    real_starts |= set(function_starts(binary))
    low = min(real_starts) if real_starts else 0
    high = max(real_starts) if real_starts else 0
    # Name files (REA exports) also carry external placeholders and unaligned
    # pseudo-addresses; only aligned addresses inside the real function range
    # may introduce a unit.
    extra = {address for address in names
             if address % 4 == 0 and low <= address <= high}
    starts = sorted(real_starts | extra)
    demangled = demangle([symbol for _address, symbol in symbols])
    by_address = {address: (raw, nice) for (address, raw), nice in zip(symbols, demangled)}
    units: list[dict] = []
    for index, address in enumerate(starts):
        next_address = starts[index + 1] if index + 1 < len(starts) else address + 16
        size = max(1, next_address - address)
        raw, nice = by_address.get(address, (f"sub_{address:x}", ""))
        if address in names:
            nice = names[address]
            raw = names[address]
        name = raw.lstrip("_") or raw
        units.append({
            "name": name,
            "demangled": nice,
            "address": f"0x{address:x}",
            "size": size,
        })
    return units
