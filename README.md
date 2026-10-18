# Melt

Melt describes durable interfaces that agents can change and people can
operate directly. `SPEC.md` defines the `melt/v0.1` contract. Round 1
develops and tests on Linux only.

## Layout

```text
python/melt/    schema, sdk, runner     (Python package `melt`)
runtime/        compiler, workspace, execution, storage
protocol/       generated JSON Schema and language models
fixtures/       valid, invalid, sales
tests/          contract, recovery, native, integration
scripts/        developer commands
```

Round 1 omits `native/`. That code arrives with the macOS round
(SPEC.md sections 2.2 and 20).

## Toolchain

Round 1 pins one interpreter generation and one locked dependency set:

- Python 3.12 (`>=3.12,<3.13`).
- `uv` 0.10.x manages `.venv` from the committed `uv.lock`. The
  `required-version` field in `pyproject.toml` enforces this pin.
- Dependencies: pydantic, pyarrow, pandas, matplotlib, and pytest.
  Every dependency uses an exact `==` pin in `pyproject.toml`, and
  `uv.lock` records the full transitive closure with hashes.
- Node 22.19 or newer with npm, for the pinned Pi Durable storage
  adapter `@earendil-works/pi-durable@1.3.0` in
  `runtime/storage/pi_adapter/package-lock.json` (SPEC.md section 14).

Set up the environment:

```bash
uv sync --locked
make probe-setup
```

## Run the full test suite

One command runs everything:

```bash
make check
```

`make check` calls `scripts/check.sh`. The script checks the pinned
interpreter, syncs `.venv` from `uv.lock`, installs the pinned Pi Durable
adapter when missing, and runs pytest over `tests/contract`,
`tests/recovery`, `tests/native`, and `tests/integration`. It needs no
macOS or Apple tools.

Run pytest directly inside the environment:

```bash
uv run python3 -m pytest
```

Run the Pi Durable storage probe alone (`make probe-setup` first); it
proves atomic updates, committed observation, and the verified
`synchronous=FULL` connection of the pinned adapter, and records the
tested filesystem and crash model in
`runtime/storage/pi_adapter/README.md`:

```bash
make probe
```

After you change `pyproject.toml`, refresh the lockfile:

```bash
make lock
```

## Status

`python/melt/schema` holds the `melt/v0.1` document models, the
command envelope, the versioned persisted records, and RFC 8785
canonicalization (backlog task T003). Parsing entry points:

- `melt.schema.document.parse_document`
- `melt.schema.commands.parse_command`
- `melt.schema.records.parse_record`

Each returns the typed model or raises with sorted diagnostics that
carry a code and a JSON Pointer path. The RFC 8785 conformance vectors
live in `tests/contract/jcs/`.

`runtime/compiler` implements the seven compiler stages of backlog task
T005 (SPEC.md section 11). It validates a document, resolves it
against a trusted source catalog and action registry, checks
registration contracts and artifact schemas, validates parameter state
continuity, and emits the render, state, interaction, action,
dependency, identity, and layout plans. Parsing entry point:

- `runtime.compiler.compile_document` with a `CompileRequest`
- `runtime.compiler.load_catalog` for `fixtures/catalog.json`

Compilation performs no project execution, network access, or package
installation. Equivalent canonical inputs with equal registry versions
produce equivalent plans. `melt validate` and `melt compile` arrive with
backlog task T006. Until then `uv run python3 -m melt --version` reports
the pinned versions.

`runtime/storage` implements workspace storage and ownership (backlog
task T008, SPEC.md section 14). Each workspace resolves its directory
through a platform adapter (`$XDG_DATA_HOME` or `$HOME/.local/share` on
Linux), creates the `state.sqlite`, `owner.lock`, `artifacts/`,
`staging/`, `logs/`, and `backups/` tree, takes the exclusive
operating-system lock, and drives exactly one owning connection inside
a Node helper process over the framed channel of section 12.1.
Artifact bytes stay outside the database. Entry point:

- `runtime.storage.WorkspaceStore.open(platform, workspace_id)`

`runtime/workspace` implements the service core of backlog task T009
(SPEC.md sections 12.1 through 12.5). `workspace.apply` plans each
mutation against a private candidate, checks the `expected` revision
map, validates state migrations, and validates the final candidate once
through the compiler stages. A successful transaction commits the new
snapshot, its request receipt, and its change notification in one
atomic append, so the commit sequence advances once and a retry with
the same request identifier replays the stored response while reuse
with different content fails with `E_REQUEST_REUSE`. Requests arrive
framed: a four-byte big-endian length prefix followed by UTF-8 JSON,
rejected above the frame limit before any payload is read. Reopening a
workspace restores the last committed document, state, receipts, and
notifications without executing project code. Entry points:

- `runtime.workspace.WorkspaceService.open(platform, workspace_id)`
- `service.apply(envelope)` and `service.serve(read, write)`

The remaining service methods of section 12.6 arrive with backlog task
T010. The SDK and runner stay scaffolded boundaries until backlog tasks
T011 and T015.

## Contract fixtures

`fixtures/` holds the shared contract suite of SPEC.md section 18.1
(backlog task T004):

- `fixtures/valid/` documents and command envelopes that must pass,
  including the full sales example with a real artifact hash.
- `fixtures/invalid/` fixtures that must fail, each with an expected
  diagnostic code and JSON Pointer in `fixtures/invalid/manifest.json`.
- `fixtures/sales/` the deterministic artifact bytes; regenerate them
  with `make fixtures`.
- `fixtures/catalog.json` the trusted catalog (measured artifact
  schemas and the `sales-script-v1` registration) for full compilation.

`tests/contract/test_fixtures.py` runs the set. Entries enforced by
the compiler (T005) or the service (T009) name their owning task in
the manifest; the suite drives them through full compilation and a
seeded workspace service respectively.
