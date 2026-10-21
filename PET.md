# pet — macOS reconstruction tracker (this fork)

This fork keeps decomp.wtf's **coordination model** (claims, attempts, notes,
evidence, agents) and drops its **ROM matching pipeline**. It targets shipped
macOS software reconstructed from disassembly, where "score 0 = byte-identical"
is usually unreachable: Swift/ObjC metadata, SDK and linker versions, dead
stripping, LTO and code signing make byte equality a poor oracle.

`pet` is dependency-free (stdlib only) so it runs on any macOS Python 3.12+.

## Loop

```
# one-shot: binary in, report + unknowns out
pet analyze /path/to/Binary --out .pet/analysis \
    --names evidence/procedures.tsv --sources main/Sources/MyApp

pet init . --binary /path/to/Binary --name "My App"
pet import --names evidence/procedures.tsv      # optional address→name map
pet status
pet claim --agent me                             # smallest free unit
pet brief 0x100012340                            # disassembly, evidence, neighbours
pet bind  0x100012340 --file Sources/Foo.swift
pet apply 0x100012340 --code-file /tmp/foo.swift  # marker block in the file
pet verify 0x100012340                           # build → test → verify, promotes state
pet evidence 0x100012340 --id ev_abc --note "selector literal"
pet work --agent-cmd "./my-agent" --limit 10     # external agent loop
pet export -o docs/progress.md
pet ui                                           # local web UI at 127.0.0.1:8765
pet mcp                                          # stdio MCP for agents
```

`pet` lives in this checkout: `./pet ...` (sets `PYTHONPATH=src`).

## What each piece does

| Module | Role |
|---|---|
| `inventory.py` | Mach-O units: symbol table when present, otherwise **LC_FUNCTION_STARTS** (works on stripped release binaries), optional address→name TSV from REA |
| `macho.py` | `otool -l` parsing: `__TEXT` vmaddr and the ULEB128 function-start stream |
| `db.py` | SQLite: projects, units, claims (30 min TTL), evidence, checks, events |
| `progress.py` | weighted completion: open 0, draft .2, compiles .4, tested .8, verified 1, assumption .6 |
| `analyze.py` | one-shot report: Mach-O build metadata, function starts, ObjC/Swift classes, imports, libraries, strings; writes `report.md`, `inventory.json`, `units.tsv`, `unknowns.md`, `strings.txt` |
| `brief.py` | disassembly (`objdump`), neighbours, evidence, notes, bindings, checks |
| `edit.py` | marker-block (`// pet:begin <addr>`) or literal replacement, optional git commit |
| `verify.py` | runs `build`/`test`/`verify` with `PET_UNIT_ADDRESS`/`PET_UNIT_NAME` in the environment |
| `mcp.py` | minimal JSON-RPC stdio server: `project_info`, `claim_unit`, `brief_unit`, `bind_unit`, `apply_code`, `verify_unit`, `add_note`, `attach_evidence`, `run_check`, `import_binary`, `export_progress`, … |
| `server.py` + `static/index.html` | local web UI: progress cards, filter/search, unit table, detail panel, claim/release, Evidence, build/test output, events; JSON API under `/api/*` |

## pet.toml

```toml
[project]
name = "My App"
binary = "/path/to/MyApp"
source_roots = ["Sources"]

[checks]
build = "swift build"
test = "swift test"
```

States that claim real progress: `verified` (unit behaves, checks pass).
`assumption` counts as partial and is reported separately — it must never be
silently promoted. `score` in the table/UI is the state base (0–100) plus up to
10 points for attached Evidence.

### Verification chain

`pet verify <unit>` runs, in order:

1. `build` → state `compiles` on success;
2. `test` → state `tested`;
3. optional `verify` (per-unit command; env `PET_UNIT_ADDRESS`,
   `PET_UNIT_NAME`) → state `verified`.

Only the configured commands can promote a unit; a missing `verify` command
leaves the unit at `tested`.

### Agent loop

`pet work --agent-cmd "<command>"` claims the next unit, sends a JSON brief on
stdin, and expects one JSON object on stdout:

```json
{"file": "Sources/Foo.swift", "code": "func foo() {}", "notes": "first draft"}
```

or `{"old": "...", "new": "..."}` for a literal edit, or
`{"release": "what is left"}` to give up. The code is written into the source
file, `build`/`test`/`verify` run, state is promoted, and the claim is released.
Any MCP agent (opencode, Claude Code, Codex) can drive the same tools directly.

## Why not the byte judge

- Rebuilding Apple frameworks' binaries byte-for-byte requires the exact Xcode,
  SDK, linker and flags, and still differs on signing/metadata.
- Swift/ObjC semantics (KVO, dynamic dispatch, runtime metadata) are proven by
  tests and literal/selector diffs, not instruction equality.
- Therefore the verifier is pluggable: build, tests, attached Evidence, and
  optionally normalized disassembly similarity.

## Agents

Point any MCP client at `./pet mcp` from the project root. Each agent claims a
unit, works in the reconstructed sources, runs `run_check`, attaches Evidence
IDs, and releases with notes when stuck. Claims and notes are shared through
`.pet/state.db` (git-ignored or committed deliberately).

## Upstream

Forked from `i12bp8/decomp.wtf` (MIT, archived). The ROM pipeline, pinned
period compilers, WSL/Linux installers and web UI are still in the tree for
reference but are not used by `pet`.

## Web UI

```
./pet ui                  # http://127.0.0.1:8765/ (opens the browser)
./pet ui --port 9000 --no-open
```

Loopback only. The page is dependency-free HTML/CSS/JS and uses the same
`Store` methods as the CLI. API:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | project, weighted progress, claims, configured checks |
| GET | `/api/units?state=&q=&limit=&offset=` | filtered unit page |
| GET | `/api/unit/<address or name>` | unit, evidence, checks, events |
| GET | `/api/events` | recent project events |
| GET | `/api/export` | markdown progress report |
| POST | `/api/claim` `{unit?, agent}` | claim a unit (next smallest when omitted) |
| POST | `/api/release` `{unit, notes, agent}` | release with notes (required) |
| POST | `/api/evidence` `{unit, id, note?}` | attach Evidence |
| POST | `/api/note` `{unit, text}` | append a note |
| POST | `/api/state` `{unit, state}` | tested/verified require a passing `test` check |
| POST | `/api/check` `{check: build|test, unit?}` | run the configured command |

The UI auto-refreshes the summary every 5 s; the table reloads after every
mutation.

## UI additions

The detail panel now has a **Brief + asm** button (disassembly and neighbours),
**Source binding**, a **Reconstructed code** box with **Apply to file**, and
**Verify** (build / test / per-unit verify). New endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/brief/<unit>` | brief JSON plus rendered text |
| POST | `/api/bind` `{unit, file, anchor?}` | bind a unit to a source file |
| POST | `/api/apply` `{unit, file?, code, old?, new?, commit?}` | write code |
| POST | `/api/verify` `{unit}` | run the verification chain |

## One-shot analysis

```
./pet analyze <binary> --out <dir> [--names addr<TAB>name.tsv] [--sources <dir>]
```

Writes:

| File | Content |
|---|---|
| `report.md` | build metadata, counts, unknown summary |
| `inventory.json` | full machine-readable inventory |
| `units.tsv` | address, size, symbol, demangled name |
| `unknowns.md` | unnamed functions, classes without source, format strings, dotted keys |
| `strings.txt` | URLs and dotted keys |

This is the "just point it at a binary" mode: no project database, no agent.
