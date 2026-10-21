"""One-command binary analysis: inventory, report and explicit unknowns.

`pet analyze <binary> --out <dir>` writes a self-contained report without
needing an agent or a project database. Everything is derived from the system
Mach-O tools; nothing is executed.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from .inventory import inventory, thin_slice

CLASS_RE = re.compile(r"_Tt[CP]11Bartender_6\d+([A-Za-z0-9_]+)")
OBJC_RE = re.compile(r"_OBJC_CLASS_\$_([A-Za-z0-9_]+)")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
PLACEHOLDER_RE = re.compile(r"%[@difs]|%@|\$\{")
DEFAULTS_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(\.[A-Za-z0-9]+){1,4}\b")


def _run(*cmd: str) -> str:
    result = subprocess.run(list(cmd), capture_output=True, text=True)
    return result.stdout


def build_info(binary: Path) -> dict:
    text = _run("vtool", "-show-build", str(binary))
    info = {"platform": "", "minos": "", "sdk": "", "uuid": ""}
    for line in _run("otool", "-l", str(binary)).splitlines():
        if " uuid " in line:
            info["uuid"] = line.split("uuid", 1)[1].strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("platform"):
            info["platform"] = stripped.split(maxsplit=1)[-1]
        elif stripped.startswith("minos"):
            info["minos"] = stripped.split(maxsplit=1)[-1]
        elif stripped.startswith("sdk"):
            info["sdk"] = stripped.split(maxsplit=1)[-1]
        elif stripped.startswith("uuid"):
            info["uuid"] = stripped.split(maxsplit=1)[-1]
    return info


def linked_libraries(binary: Path) -> list[str]:
    lines = _run("otool", "-L", str(binary)).splitlines()[1:]
    return [line.strip().split(" (")[0] for line in lines if line.strip()]


def imported_symbols(binary: Path) -> list[str]:
    lines = _run("nm", "-u", str(binary)).splitlines()
    return [line.split()[-1] for line in lines if line.strip()]


def objc_classes(binary: Path) -> list[str]:
    text = _run("otool", "-ov", str(binary))
    names = set(CLASS_RE.findall(text))
    names.update(OBJC_RE.findall(text))
    return sorted(names)


def strings_inventory(binary: Path, minimum: int = 4) -> dict:
    result = subprocess.run(["strings", "-a", "-n", str(minimum), str(binary)],
                            capture_output=True, text=True)
    items = result.stdout.splitlines()
    urls = sorted({match.group(0) for line in items for match in URL_RE.finditer(line)})
    keys = sorted({match.group(0) for line in items
                   for match in DEFAULTS_RE.finditer(line) if "." in match.group(0)})
    formats = sorted({line for line in items if PLACEHOLDER_RE.search(line)})
    return {"total": len(items), "urls": urls, "keys": keys, "formats": formats}


def source_classes(sources: Path) -> set[str]:
    names: set[str] = set()
    for path in sources.rglob("*.swift"):
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"\b(?:class|struct|enum|protocol|actor)\s+([A-Za-z_][A-Za-z0-9_]*)", text))
    return names


def analyze(binary: Path, out: Path, names_file: Path | None = None,
            sources: Path | None = None) -> dict:
    slice_path = thin_slice(binary)
    from .inventory import load_names
    names = load_names(names_file) if names_file else {}
    units = inventory(slice_path, names)
    classes = objc_classes(slice_path)
    imports = imported_symbols(slice_path)
    libraries = linked_libraries(slice_path)
    strings = strings_inventory(slice_path)
    build = build_info(slice_path)

    unnamed = [u for u in units if u["name"].startswith("sub_")]
    missing_classes: list[str] = []
    if sources and sources.exists():
        implemented = source_classes(sources)
        missing_classes = [name for name in classes if name not in implemented]

    unknowns = {
        "functions_without_names": len(unnamed),
        "classes_without_source": missing_classes,
        "unresolved_imports": [s for s in imports if s.startswith("_$s")][:50],
        "format_strings": strings["formats"][:50],
    }
    report = {
        "binary": str(binary),
        "slice": str(slice_path),
        "build": build,
        "arch": "arm64" if "arm64" in _run("lipo", "-info", str(binary)) else "unknown",
        "functions": len(units),
        "named_functions": len(units) - len(unnamed),
        "classes": classes,
        "libraries": libraries,
        "imports": len(imports),
        "strings": {k: v if not isinstance(v, list) else len(v) for k, v in strings.items()},
        "sources": str(sources) if sources else "",
        "unknowns": unknowns,
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "inventory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (out / "units.tsv").open("w", encoding="utf-8") as handle:
        handle.write("address\tsize\tname\tdemangled\n")
        for unit in units:
            handle.write(f"{unit['address']}\t{unit['size']}\t{unit['name']}\t{unit['demangled']}\n")
    (out / "strings.txt").write_text("\n".join(strings["urls"] + strings["keys"]), encoding="utf-8")
    (out / "unknowns.md").write_text(render_unknowns(report, strings, unnamed, missing_classes),
                                     encoding="utf-8")
    (out / "report.md").write_text(render_report(report), encoding="utf-8")
    return report


def render_report(report: dict) -> str:
    build = report["build"]
    lines = [
        f"# {Path(report['binary']).name}", "",
        f"- slice: `{report['slice']}`",
        f"- arch: {report['arch']}",
        f"- platform: {build.get('platform','?')}, minos {build.get('minos','?')}, sdk {build.get('sdk','?')}",
        f"- uuid: {build.get('uuid','?')}",
        f"- functions: {report['functions']} ({report['named_functions']} named)",
        f"- ObjC/Swift classes: {len(report['classes'])}",
        f"- imported symbols: {report['imports']}",
        f"- linked libraries: {len(report['libraries'])}",
        f"- strings: {report['strings'].get('total',0)} "
        f"({report['strings'].get('urls',0)} URLs, {report['strings'].get('keys',0)} dotted keys, "
        f"{report['strings'].get('formats',0)} format strings)",
        "",
        "## Unknowns", "",
        f"- functions without a name: {report['unknowns']['functions_without_names']}",
        f"- classes without reconstructed source: {len(report['unknowns']['classes_without_source'])}",
        f"- Swift imports not typed here: {len(report['unknowns']['unresolved_imports'])}",
        "",
        "See `unknowns.md` for the lists, `units.tsv` for every function start.",
    ]
    return "\n".join(lines) + "\n"


def render_unknowns(report: dict, strings: dict, unnamed: list[dict],
                    missing_classes: list[str]) -> str:
    lines = ["# Unknowns", "", "## Functions without names", ""]
    if unnamed:
        lines.append("| address | size |")
        lines.append("|---|---|")
        for unit in unnamed[:200]:
            lines.append(f"| {unit['address']} | {unit['size']} |")
        if len(unnamed) > 200:
            lines.append(f"| … {len(unnamed) - 200} more | |")
    else:
        lines.append("none")
    lines += ["", "## Classes without reconstructed source", ""]
    lines += [f"- `{name}`" for name in missing_classes] or ["none"]
    lines += ["", "## Format strings", ""]
    lines += [f"- `{value}`" for value in strings["formats"][:100]] or ["none"]
    lines += ["", "## Dotted keys", ""]
    lines += [f"- `{value}`" for value in strings["keys"][:200]] or ["none"]
    return "\n".join(lines) + "\n"
