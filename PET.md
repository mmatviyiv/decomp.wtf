# pet — macOS reconstruction tracker (this fork)

This fork keeps decomp.wtf's **coordination model** (claims, attempts, notes,
evidence, agents) and drops its **ROM matching pipeline**. It targets shipped
macOS software reconstructed from disassembly, where "score 0 = byte-identical"
is usually unreachable: Swift/ObjC metadata, SDK and linker versions, dead
stripping, LTO and code signing make byte equality a poor oracle.

`pet` is dependency-free (stdlib only) so it runs on any macOS Python 3.12+.

## Loop

```
pet init . --binary /path/to/Binary --name "My App"
pet import --names evidence/procedures.tsv      # optional address→name map
pet status
pet claim --agent me                             # smallest free unit
pet run build --unit 0x100012340                 # -> compiles on success
pet run test  --unit 0x100012340                 # -> gates tested/verified
pet evidence 0x100012340 --id ev_abc --note "selector literal"
pet state 0x100012340 verified
pet export -o docs/progress.md
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
| `verify.py` | runs the project's `build`/`test` commands from `pet.toml` |
| `mcp.py` | minimal JSON-RPC stdio server: `project_info`, `claim_unit`, `add_note`, `attach_evidence`, `set_state`, `run_check`, `import_binary`, `export_progress`, … |

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

States that claim real progress: `verified` (unit behaves, checks pass,
Evidence attached). `assumption` counts as partial and is reported separately —
it must never be silently promoted.

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
