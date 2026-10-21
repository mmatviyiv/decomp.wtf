"""Mach-O metadata that survives stripping: LC_FUNCTION_STARTS and __TEXT."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FunctionStarts:
    text_vmaddr: int
    addresses: list[int]


def _load_commands(binary: Path) -> str:
    return subprocess.run(["otool", "-l", str(binary)], capture_output=True, text=True).stdout


def text_vmaddr(binary: Path) -> int:
    lines = _load_commands(binary).splitlines()
    in_text = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("segname __TEXT"):
            in_text = True
            continue
        if in_text and stripped.startswith("vmaddr"):
            return int(stripped.split()[1], 16)
        if in_text and stripped.startswith("segname"):
            in_text = False
    return 0x100000000


def _uleb(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7


def function_starts(binary: Path) -> list[int]:
    text = _load_commands(binary)
    lines = text.splitlines()
    offset = size = None
    for index, line in enumerate(lines):
        if line.strip() == "cmd LC_FUNCTION_STARTS":
            for follow in lines[index:index + 5]:
                if "dataoff" in follow:
                    offset = int(follow.split()[1])
                elif "datasize" in follow:
                    size = int(follow.split()[1])
            break
    if offset is None or not size:
        return []
    with binary.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(size)
    base = text_vmaddr(binary)
    addresses: list[int] = []
    current = base
    index = 0
    while index < len(data):
        delta, index = _uleb(data, index)
        if delta == 0:
            break
        current += delta
        addresses.append(current)
    return addresses
