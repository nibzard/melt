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

`runtime/workspace` implements the service core of backlog tasks T009
and T010 (SPEC.md sections 12.1 through 12.6). `workspace.apply` plans
each mutation against a private candidate, checks the `expected`
revision map, validates state migrations, and validates the final
candidate once through the compiler stages. A successful transaction
commits the new snapshot, its request receipt, and its change
notification in one atomic append, so the commit sequence advances once
and a retry with the same request identifier replays the stored
response while reuse with different content fails with
`E_REQUEST_REUSE`. Requests arrive framed: a four-byte big-endian
length prefix followed by UTF-8 JSON, rejected above the frame limit
before any payload is read. Reopening a workspace restores the last
committed document, state, receipts, notifications, and run records
without executing project code. Entry points:

- `runtime.workspace.WorkspaceService.open(platform, workspace_id)`
- `service.dispatch(envelope)` for any section 12.6 method
- `service.apply(envelope)`, `service.preview(envelope)`, and
  `service.serve(read, write)`

The service methods of section 12.6 ride the same envelope.
`session.hello` negotiates protocol and language versions and reports
the epoch and the limit table. `workspace.create` answers the initial
snapshot of an absent workspace whose `sources` and `actions`
collections are empty. `workspace.inspect` returns the bounded
document, entity revisions, parameter state, active runs, and the
diagnostics of recompiling the committed definitions, so drifted
registrations are named while saved projections stay readable.
`workspace.preview` plans the same transaction an apply would and
reports diagnostics, affected records, controller replacements, state
resets, invalidated runs, and the candidate document hash without
writing. `workspace.subscribe` answers ordered changes or the
successful snapshot-required status. `control.activate` routes a button
press through its binding under projection and binding revisions, and
`action.invoke` invokes an action manually under its expected action
revision; both admit through the scheduling core of backlog task T016.
`run.cancel` and `run.inspect` answer and update run records
idempotently. Artifact and script tokens are minted by the host capture
operation: opaque, workspace-scoped handles that expire after
15 minutes (`service.capture_artifact_token` and
`service.redeem_artifact_token`). Table paging arrives with its owning
backlog task (T012).

`runtime/workspace` also implements the artifact store registration and
source import of backlog task T011 (SPEC.md sections 5 and 12.6).
`source.import` redeems one artifact token, rereads the stored bytes,
verifies their content hash and size, measures the schema the bytes
carry (Parquet columns with retained original labels, PNG dimensions,
UTF-8 text), and commits one immutable, append-only source version
with import provenance. The `expected` map carries the source record
revision, or zero for a new source. No document command can forge an
artifact record: the mutation operations address no source collection,
and references name only versions the service issued. Run provenance
is attached by the publication path alone. The verified artifact
records rebuild the trusted source catalog on restart, so saved
sources compile again without rereading any bytes.

`runtime/workspace` also implements the action registration registry of
backlog task T013 (SPEC.md sections 7.2 and 12.6). `action.register`
redeems one script token, rereads the stored bytes, verifies their
content hash and size, and commits one trusted registration record:
the registration identifier and immutable revision, the captured
script artifact and hash, the project root and working-directory
policy, the resolved environment fingerprint, the parameter and input
contract with declared outputs, the required execution permission, and
the fixed `nonReplaySafe` classification. Registration never executes
the script. The identifier covers the captured bytes and the resolved
environment, so editing a script produces a new registration and an
unchanged capture resolves to the registration it already has; a
changed contract over the same capture advances the registration's
revision. Action mappings must exactly satisfy the registration
contract: the compiler rejects a mapped parameter that widens or
leaves open any constraint dimension the registration bounds. The
committed registration records rebuild the registry on restart, so
saved actions compile again; an action whose registration is
unavailable after restoration stays disabled while its projections
remain readable. Environments resolve through the service
(`service.known_environments()`), and the local interpreter is always
known.

`runtime/workspace` also implements permissions and the permission
rechecks of backlog task T014 (SPEC.md section 15.1). Document
validity is not execution authority: specific-run and project
execution grants live in the service record stream, outside every
exported document, and only the authenticated host permission endpoint
(`service.grant_execution_permission` and
`service.revoke_execution_permission`) mints or revokes them. No
command envelope method, document field, or registration contract can
grant anything. Admission rechecks the grant set before anything runs:
a missing grant answers the successful approval-required result with
the captured intent a host approval binds to, and a covering grant
admits a real run through the scheduling core of backlog task T016.
The launch and publication paths recheck again through
`authority_at_launch` and `authority_at_publication`
(`service.execution_authority_for_run`), where revocation blocks
publication immediately and requests cancellation. Model credentials
never reach a project process: launch sites build child environments
through `runtime.execution.project_process_environment` and guard them
with `assert_no_model_credentials`. The default limit table of section
15.2 is enforced with `E_LIMIT` diagnostics that name the resource and
the allowed bound across the schema, command, frame, and import layers;
the table page bound lands with its owning subsystem (T012).

`python/melt/sdk` and `python/melt/runner` implement the Python SDK and
runner of backlog task T015 (SPEC.md section 13.2). A run receives one
versioned manifest: the run identifier, the captured parameter values,
the absolute path of every captured input artifact, the declared output
names with their kinds, and the private staging directory. The runner
binds `ui` from that manifest, executes the materialized script once in
a fresh process, and stages each declared output: `ui.output` writes
Parquet for a pandas DataFrame, PNG for a matplotlib Figure, and UTF-8
bytes for a `str`, and refuses duplicate and undeclared names. The
staged bytes of one run stay inside the 1 GiB service limit, and the
process exits with one of four codes: complete, script failure,
operational failure, or incomplete outputs. The service-side launch
harness lives in `runtime.execution.launcher`: it writes the manifest
and a read-only script copy into the staging directory, builds the
credential-free child environment through
`runtime.execution.project_process_environment`, pins the
non-interactive render backend, sets the staging directory as the
working directory, and bounds each run log stream with an explicit
truncation marker. The launcher installs no packages, and the service
validates the staged files independently before anything publishes.
Entry points:

- `from melt import ui` inside a project script
- `python -m melt.runner <manifest-file>` inside the spawned process
- `runtime.execution.launch_run(manifest, log_directory=...)`
- `runtime.execution.write_run_manifest` and `materialize_script`

`runtime/workspace` also implements scheduling, snapshots, and
generations (backlog task T016, SPEC.md sections 7.3, 9.3, and 13.1).
Every admission captures the full invocation snapshot — registration
revision, script hash, environment fingerprint, input source
references, parameter values with their state revisions, action
revision, output group revision, generation, and permission decision —
and reserves its generation in the admitting transaction, so older
runs stop being eligible for current publication the moment the
transaction commits, not when a timer fires. Bindings that a committed
user change fires coalesce into one pending intent per action with the
final parameter values; repeated events replace the intent and restart
its timer, and the debounce timers live in the service
(`service.dispatch_due_intents`). A dispatch rechecks definitions and
authority: a stale action, registration, or output group expires the
intent, and an approval dispatches only the exact captured intent it
covers, pinned by generation and output group revision. Launch wraps
`runtime.execution.launch_run` with the pre-launch rechecks —
permission, registration availability, and the resolved environment
fingerprint against the admission snapshot — and drives one project
execution at a time per workspace
(`service.launch_admitted_run`, `service.launch_next_run`). A restart
marks pending timers and active jobs interrupted, keeps the reserved
generations, and never executes anything on its own. Entry points:

- `service.dispatch_due_intents(now_epoch=...)` to fire due timers
- `service.launch_admitted_run(run_id)` and `service.launch_next_run()`
- `service.output_groups`, `service.pending_intents`, and
  `service.run_is_newest_generation(run_id)`
- `service.dispatch_as(envelope, origin="programmatic")` for the agent
  event origin, which commits changes without firing bindings

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
