"""pet command line: import a Mach-O, track reconstruction units, run checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import mcp, progress
from .brief import brief, brief_text
from .config import ProjectConfig
from .edit import apply_code
from .db import STATES, Store
from .inventory import inventory, load_names
from .verify import run_check

import json as _json
import subprocess

DEFAULT_DB = Path(".pet/state.db")


def store_for(root: Path) -> Store:
    return Store(root / DEFAULT_DB)


def slugify(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-") or "project"


def cmd_init(args) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    binary = Path(args.binary).expanduser().resolve() if args.binary else None
    lines = ["[project]", f'name = "{args.name or root.name}"']
    if binary:
        lines.append(f'binary = "{binary}"')
    lines += ['source_roots = ["Sources"]', "", "[checks]",
              'build = "swift build"', 'test = "swift test"', ""]
    (root / "pet.toml").write_text("\n".join(lines), encoding="utf-8")
    print(f"pet.toml written to {root}")
    return 0


def cmd_import(args) -> int:
    root = Path(args.project or ".").resolve()
    config = ProjectConfig.load(root)
    binary = Path(args.binary or config.binary).expanduser()
    if not binary or not binary.exists():
        print("no binary: pass --binary or set project.binary in pet.toml", file=sys.stderr)
        return 2
    names = load_names(Path(args.names)) if args.names else {}
    units = inventory(binary, names)
    store = store_for(root)
    project_id = store.upsert_project(
        slugify(config.name), config.name, root, str(binary),
        {"source_roots": config.source_roots,
         "checks": {"build": config.checks.build, "test": config.checks.test}})
    added = store.add_units(project_id, units)
    total = len(store.units(project_id))
    print(f"imported {added} new units ({total} total) from {binary.name}")
    store.close()
    return 0


def _project_and_store(args):
    root = Path(args.project or ".").resolve()
    store = store_for(root)
    slug = getattr(args, "slug", None) or slugify(ProjectConfig.load(root).name)
    return store, store.resolve(slug)


def cmd_status(args) -> int:
    store, project = _project_and_store(args)
    units = store.units(project["id"])
    summary = progress.summarize(units)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{project['name']}: {summary['verified']}/{summary['total']} verified "
              f"({summary['percent_units']:.1f}% units, {summary['percent_bytes']:.1f}% bytes), "
              f"{summary['assumptions']} assumptions, {summary['blocked']} blocked, "
              f"{summary['evidence']} evidence items")
        for state in STATES:
            count = summary["states"].get(state, 0)
            if count:
                print(f"  {state:<10} {count}")
        claims = store.claims()
        if claims:
            print("claims:")
            for claim in claims:
                print(f"  {claim['name']} → {claim['agent']}")
    store.close()
    return 0


def cmd_list(args) -> int:
    store, project = _project_and_store(args)
    for unit in store.units(project["id"], args.state):
        if args.limit and unit["id"] > 0 and args.limit <= 0:
            break
        label = unit["demangled"] or unit["name"]
        print(f"{unit['address']}\t{unit['size']:>6}\t{unit['state']:<10}\t{label}")
        args.limit -= 1
        if args.limit == 0:
            break
    store.close()
    return 0


def cmd_claim(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit) if args.unit else _next_open(store, project["id"])
    store.claim(unit["id"], args.agent)
    store.event(project["id"], unit["id"], args.agent, "claim")
    print(f"{unit['address']}\t{unit['demangled'] or unit['name']}")
    print(f"state: {unit['state']}  evidence: {len(json.loads(unit['evidence']))}")
    store.close()
    return 0


def _next_open(store: Store, project_id: int):
    row = store.db.execute(
        """SELECT u.* FROM units u LEFT JOIN claims c ON c.unit_id=u.id
           WHERE u.project_id=? AND u.state IN ('open','draft','compiles')
             AND c.unit_id IS NULL
           ORDER BY u.size ASC, u.address LIMIT 1""", (project_id,)).fetchone()
    if row is None:
        raise SystemExit("nothing left to claim")
    return row


def cmd_release(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    store.release(unit["id"], args.notes, args.agent)
    store.event(project["id"], unit["id"], args.agent, "release", args.notes)
    print("released")
    store.close()
    return 0


def cmd_note(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    with store.db:
        store.db.execute("UPDATE units SET notes=notes || ? || char(10) WHERE id=?",
                         (args.text, unit["id"]))
    print("note added")
    store.close()
    return 0


def cmd_evidence(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    store.attach_evidence(unit["id"], args.id, args.note or "")
    store.event(project["id"], unit["id"], args.agent, "evidence", args.id)
    print("evidence attached")
    store.close()
    return 0


def cmd_state(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    if args.state in ("tested", "verified") and not store.last_passing(unit["id"], "test"):
        print("refusing: the last `pet run test` must pass first", file=sys.stderr)
        store.close()
        return 2
    store.set_state(unit["id"], args.state)
    store.event(project["id"], unit["id"], args.agent, "state", args.state)
    print(f"{unit['name']} → {args.state}")
    store.close()
    return 0


def cmd_run(args) -> int:
    root = Path(args.project or ".").resolve()
    config = ProjectConfig.load(root)
    command = config.checks.command(args.check)
    ok, output = run_check(root, command)
    store = store_for(root)
    project = store.resolve(slugify(config.name))
    unit = store.unit(project["id"], args.unit) if args.unit else None
    store.record_check(unit["id"] if unit else None, args.check, command, ok, output)
    store.event(project["id"], unit["id"] if unit else None, args.agent, "check",
                f"{args.check}:{'ok' if ok else 'fail'}")
    if unit and ok and args.check == "build":
        if unit["state"] in ("open", "draft"):
            store.set_state(unit["id"], "compiles")
    print(output.strip()[-4000:])
    print(f"\n{args.check}: {'ok' if ok else 'failed'}")
    store.close()
    return 0 if ok else 1


def cmd_export(args) -> int:
    store, project = _project_and_store(args)
    units = store.units(project["id"])
    summary = progress.summarize(units)
    out = [f"# {project['name']} — reconstruction progress", "",
           f"- units: {summary['verified']}/{summary['total']} verified",
           f"- weighted: {summary['percent_units']:.1f}% of units, "
           f"{summary['percent_bytes']:.1f}% of bytes",
           f"- assumptions: {summary['assumptions']}, blocked: {summary['blocked']}, "
           f"evidence: {summary['evidence']}", "",
           "| address | size | state | unit | evidence |",
           "|---|---|---|---|---|"]
    for unit in units:
        out.append(f"| {unit['address']} | {unit['size']} | {unit['state']} | "
                   f"`{unit['demangled'] or unit['name']}` | {len(json.loads(unit['evidence']))} |")
    text = "\n".join(out) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    store.close()
    return 0


def _unit_env(unit) -> dict:
    return {"PET_UNIT_ADDRESS": unit["address"], "PET_UNIT_NAME": unit["demangled"] or unit["name"]}


def cmd_brief(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    data = brief(store, project, unit)
    if args.json:
        print(_json.dumps(data, indent=2))
    else:
        print(brief_text(data, include_asm=not args.no_asm))
    store.close()
    return 0


def cmd_bind(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    path = Path(args.file).resolve()
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    from .db import Store as _Store
    with store.db:
        store.db.execute(
            """INSERT INTO bindings(unit_id,path,anchor,updated_at) VALUES(?,?,?,strftime('%s','now'))
               ON CONFLICT(unit_id) DO UPDATE SET path=excluded.path, anchor=excluded.anchor""",
            (unit["id"], str(path), args.anchor or unit["address"]))
    store.event(project["id"], unit["id"], args.agent, "bind", str(path))
    print(f"{unit['address']} → {path}")
    store.close()
    return 0


def cmd_apply(args) -> int:
    store, project = _project_and_store(args)
    unit = store.unit(project["id"], args.unit)
    path = Path(args.file) if args.file else None
    if path is None:
        row = store.db.execute("SELECT path FROM bindings WHERE unit_id=?", (unit["id"],)).fetchone()
        if row is None:
            print("pass --file or run `pet bind` first", file=sys.stderr)
            store.close()
            return 2
        path = Path(row["path"])
    if args.code_file:
        code = Path(args.code_file).read_text(encoding="utf-8")
    elif args.code is not None:
        code = args.code
    elif not sys.stdin.isatty():
        code = sys.stdin.read()
    else:
        code = ""
    if not code and not (args.old is not None and args.new is not None):
        print("no code: pass --code, --code-file or pipe it on stdin", file=sys.stderr)
        store.close()
        return 2
    result = apply_code(store, project["id"], unit, path, code,
                        old=args.old, new=args.new, commit=args.commit,
                        root=Path(args.project or ".").resolve())
    print(f"wrote {result['path']} (anchor {result['anchor']})"
          + (" and committed" if result["committed"] else ""))
    store.close()
    return 0


def cmd_verify(args) -> int:
    root = Path(args.project or ".").resolve()
    config = ProjectConfig.load(root)
    store = store_for(root)
    project = store.resolve(slugify(config.name))
    unit = store.unit(project["id"], args.unit)
    env = _unit_env(unit)
    results = {}
    for check in ("build", "test", "verify"):
        command = config.checks.command(check)
        if not command:
            continue
        ok, output = run_check(root, command, env=env)
        store.record_check(unit["id"], check, command, ok, output)
        store.event(project["id"], unit["id"], args.agent, "check", f"{check}:{'ok' if ok else 'fail'}")
        results[check] = (ok, output)
        if not ok:
            break
    if results.get("build", (False, ""))[0] and unit["state"] in ("open", "draft"):
        store.set_state(unit["id"], "compiles")
    if results.get("test", (False, ""))[0] and store.last_passing(unit["id"], "test"):
        if unit["state"] in ("open", "draft", "compiles"):
            store.set_state(unit["id"], "tested")
    if results.get("verify", (False, ""))[0]:
        store.set_state(unit["id"], "verified")
    for check, (ok, output) in results.items():
        print(f"{check}: {'ok' if ok else 'failed'}")
        if not ok:
            print(output.strip()[-4000:])
    print(f"state: {store.unit(project['id'], unit['address'])['state']}")
    store.close()
    return 0 if all(ok for ok, _ in results.values()) else 1


def cmd_work(args) -> int:
    root = Path(args.project or ".").resolve()
    config = ProjectConfig.load(root)
    store = store_for(root)
    project = store.resolve(slugify(config.name))
    done = 0
    while args.limit <= 0 or done < args.limit:
        unit = _next_open(store, project["id"])
        store.claim(unit["id"], args.agent)
        store.event(project["id"], unit["id"], args.agent, "claim")
        payload = _json.dumps({"brief": brief(store, project, unit), "checks": {
            "build": config.checks.build, "test": config.checks.test,
            "verify": config.checks.verify}}, indent=2)
        proc = subprocess.run(args.agent_cmd, cwd=root, shell=True, input=payload,
                              capture_output=True, text=True, timeout=args.timeout)
        agent_out = proc.stdout.strip()
        if proc.returncode != 0:
            store.release(unit["id"], f"agent failed: {proc.stderr.strip()[-1000:]}", args.agent)
            store.event(project["id"], unit["id"], args.agent, "release", "agent failed")
            print(f"{unit['address']} agent failed")
            store.close()
            return 1
        try:
            response = _json.loads(agent_out[agent_out.index("{"):agent_out.rindex("}") + 1])
        except (ValueError, IndexError):
            store.release(unit["id"], "agent produced no JSON", args.agent)
            print(f"{unit['address']} no JSON from agent")
            store.close()
            return 1
        if response.get("release"):
            store.release(unit["id"], str(response["release"]), args.agent)
            store.event(project["id"], unit["id"], args.agent, "release", str(response["release"]))
            print(f"{unit['address']} released: {response['release'][:80]}")
            store.release(unit["id"], str(response["release"]), args.agent)
            done += 1
            continue
        path = response.get("file") or ""
        code = response.get("code") or ""
        try:
            if response.get("old") is not None and response.get("new") is not None:
                apply_code(store, project["id"], unit, Path(path), "", old=response["old"],
                           new=response["new"], root=root)
            else:
                apply_code(store, project["id"], unit, Path(path), code, root=root)
        except SystemExit as error:
            store.release(unit["id"], f"apply failed: {error}", args.agent)
            print(f"{unit['address']} apply failed: {error}")
            done += 1
            continue
        code_ok = True
        env = _unit_env(unit)
        for check in ("build", "test", "verify"):
            command = config.checks.command(check)
            if not command:
                continue
            ok, output = run_check(root, command, env=env)
            store.record_check(unit["id"], check, command, ok, output)
            if not ok:
                code_ok = False
                break
        if code_ok and store.last_passing(unit["id"], "test"):
            state = "verified" if store.last_passing(unit["id"], "verify") else "tested"
            store.set_state(unit["id"], state)
        store.release(unit["id"], response.get("notes") or "agent turn", args.agent)
        done += 1
        print(f"{unit['address']} {'ok' if code_ok else 'needs work'} → "
              f"{store.unit(project['id'], unit['address'])['state']}")
    store.close()
    return 0


def cmd_ui(args) -> int:
    from .server import serve
    serve(Path(args.project or ".").resolve(), args.host, args.port,
          open_browser=not args.no_open)
    return 0


def cmd_mcp(args) -> int:
    root = Path(args.project or ".").resolve()
    mcp.serve(root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pet", description="macOS reconstruction tracker")
    parser.add_argument("--project", default=".", help="project root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a pet.toml")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--binary")
    p.add_argument("--name")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="inventory a Mach-O binary")
    p.add_argument("--binary")
    p.add_argument("--names", help="address<TAB>name file (REA procedures/objc methods)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("status", help="progress summary")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="list units")
    p.add_argument("--state", choices=STATES)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("claim", help="claim a unit (or the smallest free one)")
    p.add_argument("unit", nargs="?")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="release a unit with notes")
    p.add_argument("unit")
    p.add_argument("--notes", required=True)
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("note", help="append a note to a unit")
    p.add_argument("unit")
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("evidence", help="attach an Evidence ID")
    p.add_argument("unit")
    p.add_argument("--id", required=True)
    p.add_argument("--note")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("state", help="set a unit state")
    p.add_argument("unit")
    p.add_argument("state", choices=STATES)
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("run", help="run the configured build/test command")
    p.add_argument("check", choices=["build", "test"])
    p.add_argument("--unit")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("export", help="write a markdown progress report")
    p.add_argument("--output", "-o")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("brief", help="disassembly, evidence and neighbours for a unit")
    p.add_argument("unit")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-asm", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("bind", help="bind a unit to a source file")
    p.add_argument("unit")
    p.add_argument("--file", required=True)
    p.add_argument("--anchor")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("apply", help="write code into the bound source file")
    p.add_argument("unit")
    p.add_argument("--file")
    p.add_argument("--code")
    p.add_argument("--code-file")
    p.add_argument("--old")
    p.add_argument("--new")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify", help="build, test and optional per-unit verify")
    p.add_argument("unit")
    p.add_argument("--agent", default="me")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("work", help="run an external agent command over claimed units")
    p.add_argument("--agent-cmd", required=True,
                   help="command reading the brief JSON on stdin and printing a JSON edit")
    p.add_argument("--limit", type=int, default=0, help="0 = until nothing is left")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--agent", default="work")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("ui", help="local web interface")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("mcp", help="serve MCP tools over stdio")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
