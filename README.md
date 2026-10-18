# Melt

Melt describes durable interfaces that agents can change and people can
operate directly. `SPEC.md` defines the `melt/v0.1` contract. Round 1
develops and tests on Linux only.

## Layout

```text
python/melt/    schema, sdk, runner     (Python package `melt`)
runtime/        compiler, workspace, execution, storage, harness
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
adapter when missing, verifies the canonical document hashes with the
independent JavaScript canonicalizer, and runs pytest over
`tests/contract`, `tests/recovery`, `tests/native`, and
`tests/integration`. It needs no macOS or Apple tools. The command is
the merge gate: do not merge while it fails.

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
produce equivalent plans.

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
- `service.process_frame(frame, origin=...)` and
  `service.handle_request(payload, origin=...)` for one framed
  request; the origin argument is the event origin of the
  authenticated caller (SPEC.md sections 2.2 and 9.1)

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
`service.redeem_artifact_token`). Admission answers carry the event
origin of the authenticated caller: `dispatch` and the framed channel
speak for the local user, `dispatch_as(envelope,
origin="programmatic")` speaks for an agent, and the admitted intent
records which one spoke (SPEC.md sections 2.2 and 9.1).

`runtime/workspace/reader.py` implements the trusted table reader
(SPEC.md sections 8.2 and 12.7). `table.page` resolves the immutable
source reference to stored artifact bytes, validates sort keys and
filter operators against the declared column types before any byte is
read, combines filters with logical AND, and treats a non-null
comparison against a null cell as false. Strings compare by Unicode
code point, sorting places nulls last whatever the direction with the
original row position as the final tie-breaker, and filters apply
before sorting and paging. Responses identify the source reference and
a query hash that covers the source, sort, and filters while excluding
offset and limit; `int64` and `decimal` cells travel as strings. A
table wider than the column bound answers an explicit
column-selection requirement, the encoded byte bound may reduce rows
but never below one display row, and `hasMore` is true only while a
further row exists to name in `nextOffset`.

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
the allowed bound across the schema, command, frame, import, and table
page layers.

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

`runtime/workspace/publication.py` implements atomic publication and
result status (backlog task T017, SPEC.md sections 13.3 to 13.5).
`service.publish_run(run_id)` applies the six publication steps of
section 13.4 inside one owning transaction: it rereads and measures
every staged file independently of the runner's report, rejects
non-finite table floats, finalizes the immutable content-addressed
artifact paths, rechecks every eligibility condition of section 13.3
inside the transaction, allocates the output source versions through
the service-only `run_version` provenance hook, updates all target
references together, and stores the run disposition with a publication
receipt in one commit. A missing or invalid output rejects the entire
publication, so the previous table and plot remain together. A denial
of the publication permission gate rejects the publication, and
revocation additionally requests cancellation of the owning run. A
verified but obsolete run archives as `superseded` historical output
that no current reference points at, while the previous current run of
the group flips to superseded when a newer publication lands. Output
source identifiers are service-owned mappings from `(action id, output
name)` rebuilt from committed run provenance, never derived from
titles. Every output group reports `empty`, `pending`, `current`,
`stale`, or `error` status with `resultIsStale` and the captured
parameters of the retained result, through
`service.output_group_status()` and the `outputGroups` view of
`workspace.inspect`. Entry points:

- `service.publish_run(run_id)` to publish one completed run
- `service.output_group_status()` and
  `workspace.inspect`'s `outputGroups` for result status
- `service.publications` for the committed publication receipts
- `runtime.workspace.publication.verify_staged_outputs` for the
  independent staged-file verification

`runtime/workspace/recovery.py` implements the startup recovery pass of
backlog task T022 (SPEC.md sections 14 and 18.4). Opening a workspace
loads the last committed state, validates it without importing or
executing project code, and then: skips committed records that no
longer parse and reports each one as a corrupt descriptor while the
last valid snapshot keeps the previous readable interface; reports
every artifact a saved reference names but storage lacks, one
diagnostic per source version and per captured registration script,
live in `workspace.inspect` as well as at startup; marks previous
active jobs and pending timers interrupted; and reconciles every
`staging/<run-id>` tree against the committed publication receipts —
a concluded or unpublishable run leaves abandoned staging behind that
recovery removes, while a run that succeeded before its publication
committed keeps its staged outputs for an explicit retry. The pass
ends with the rerun and recovery actions a host may offer, never
performed by the service itself: rerunning an interrupted intent
through a fresh invocation and retrying the publication of a kept
run. An unsupported stored version — a newer workspace format, record
kind, record schema version, or snapshot language — refuses to open
with every file preserved instead. The report rides `service.recovery_report`
and the `recovery` view of `workspace.inspect`. The fault-injection
suite that proves all of this lives in `tests/recovery`: it kills a
real service process on both sides of artifact finalization, database
commit, and acknowledgement, drives disk-full, missing-artifact,
missing-environment, lost-notification, changed-epoch, reused-request,
corrupt-descriptor, cancelled-run, and incompatible-version faults,
and proves that acknowledged references always point to complete
files, that retries reconcile unacknowledged work, and that no
uncertain Python effect ever reruns after a restart. Entry points:

- `service.recovery_report` for what startup found and did
- `workspace.inspect`'s `recovery` view and missing-artifact
  diagnostics for the host surface
- `runtime.workspace.recovery.reconcile_staging` for the staging
  reconciliation and `missing_artifact_diagnostics` for the
  individual reports

The round 1 performance measurements of SPEC.md section 19 are
recorded (backlog task T023). `scripts/benchmark_round1.py` measures
compilation of a generated 32-projection document against the cached
fixture catalogs, the framed service commits of a bound parameter edit
and a small definition patch with its notification, `publish_run` of a
succeeded run, and `table.page` over a generated 1,000,000-row,
20-column table, warm and with a dropped kernel page cache, plus the
full-table sort section 19 reports separately. It also times reopening
a saved five-pane workspace through the framed channel. The recorded
run, its environment declaration, and the comparison against every
section 19 target live in `docs/performance/round-1-linux.md` with the
raw JSON beside it; rerun with `make benchmark-round1`. Entry point:

- `scripts/benchmark_round1.py` (or `make benchmark-round1`)

## Command-line compiler

`melt validate` and `melt compile` are the fixture and diagnostics
command line of SPEC.md section 11.3 (backlog task T006). Both accept
`--json` for one structured result object and share the exit code
convention: 0 on success, 1 for invalid input, and 2 for operational
failure.

```bash
uv run --locked python3 -m melt validate fixtures/valid/sales-full.json
# valid: fixtures/valid/sales-full.json (structural validation only)
# coverage: Structural validation only. No catalog or registry was supplied, ...

uv run --locked python3 -m melt compile fixtures/valid/sales-full.json \
    --catalog fixtures/catalog.json --json
```

`melt validate <file>` performs structural validation alone and always
reports that limited coverage: the artifact, registration, and mapping
checks need the trusted catalog and registry. `--catalog` upgrades it
to the full compiler stages. `melt compile <file> --catalog <file>` is
full compilation: the catalog file carries the measured artifact
schemas and the action registry together (`fixtures/catalog.json`), and
`--json` includes the complete compiled plan with its `planKey`. A
compile without `--catalog` is an operational failure, because full
compilation requires the catalog and registry fixtures. An unreadable
document or a malformed catalog is an operational failure too, never a
diagnostic against the document.

## Command-line harness

`runtime/harness` is the round-1 stand-in for the native host (SPEC.md
sections 2.2 and 18.2). One command drives one workspace as the local
user: it opens the service, sends one request through the framed
channel the later native host drives, prints the JSON answer, and
closes the service again. Because every invocation reopens the durable
store, the loop also proves that nothing between two commands needs a
live service process.

```bash
H="uv run --locked python3 -m runtime.harness --root $DATA_ROOT --workspace sales-demo"
$H hello                                              # negotiate versions
$H create --document fixtures/sales/workspace.json    # workspace.create
$H import --source sales-input --file fixtures/sales/sales.parquet --label sales.csv
$H register --script fixtures/sales/sales-script.py --contract fixtures/sales/contract.json
$H apply --operations fixtures/sales/definitions.json --registration <registration-id>
$H plan                                               # inspect the compiled plan
$H set --parameter minimum --value 200                # user parameter edit
$H approve --registration <registration-id>           # host permission control
$H invoke --action analyze-sales --launch --publish   # run and publish
$H table --source analyze-sales-filtered              # read a table page
$H artifact --source analyze-sales-monthly --output monthly.png
$H inspect && $H status && $H runs && $H changes --after 0
```

The commands cover the full section 12.6 surface plus the host-side
operations no envelope reaches: `approve`/`revoke` operate the
authenticated permission endpoint, `pump` fires due debounce timers,
and `--launch`/`--publish` drive the one execution slot and the
publication transaction. Because a restart marks queued runs and
running timers interrupted instead of executing them (SPEC.md sections
9.3 and 14), admission and execution share one session: pass
`--dispatch`, `--launch`, and `--publish` on the event command itself.

Event origin is part of the contract. The harness speaks for the local
user by default; `--origin programmatic` on `set`, `invoke`,
`activate`, and `apply` routes the same envelope through the
agent-adapter path, so the admitted intent records a programmatic
origin instead of a user origin.

Exit codes follow the round-1 convention: 0 for a successful answer, 1
for a rejected request (`ok: false`), and 2 for an operational failure
of the harness itself.

### Hero workflow

`tests/integration/test_hero_workflow.py` proves the six hero steps of
SPEC.md section 18.2 through real harness processes (backlog task
T019). The input holds rows (2026-01, 100), (2026-01, 300), and
(2026-02, 200). At minimum zero the output table has three rows. At
minimum 200 it has two rows, and the monthly totals become 300 and
200. The steps:

1. `create`, `import`, `register`, and `apply` build the workspace
   without executing: no run exists and the output group stays empty.
2. `approve`, `invoke --launch --publish` run the action and publish
   both outputs together. Both output sources appear at version 1
   with run provenance, both target projections point at that one
   publication, and the plot exports as a PNG artifact.
3. `set --value 200 --dispatch --launch --publish` reruns through the
   bound action with zero model calls: every command runs with fake
   model credentials in its environment, the runs still succeed, and
   no persisted byte under the data root carries them.
4. The pane moves through `apply --operations
   fixtures/sales/layout-move.json` in the staged window of an
   execution — its process completed, its publication pending — and
   the moved layout survives that publication and the compiled plan.
   A mutation concurrent with a live process needs the non-blocking
   execution slot of backlog task T035 (race suite T021).
5. A fresh command reopens the workspace offline: values, bindings,
   sources, and layout restore, and nothing executes. The restart
   interrupts the pending debounce request, keeps the retained result
   readable with its captured parameters, and reports it stale.
6. `invoke` again admits a run whose captured inputs name `sales-input`
   version 1 and whose captured parameters name the committed
   minimum; reopening marks it interrupted instead of executing it.


## Contract fixtures

`fixtures/` holds the shared contract suite of SPEC.md section 18.1
(backlog tasks T004 and T020):

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
seeded workspace service respectively. Every valid command envelope
also commits: the state migration applies against a service seeded to
the revisions its `expected` map records. Every fixture decodes as its
recorded layer requires, so a schema entry that suddenly parsed, or a
compiler entry that stopped parsing, fails the suite.

`tests/contract/test_cli.py` drives `melt validate` and `melt compile`
over the same manifest and the whole valid set. Canonical hashes agree
across the supported languages: `fixtures/valid/document-hashes.json`
records the RFC 8785 hash of every valid document, the Python suite
fails when it goes stale, and `scripts/verify_canonical_hashes.mjs`
recomputes it with an independent JavaScript canonicalizer that also
replays the RFC 8785 reference vectors. `make check` runs both, so the
contract suite blocks merges on failure.
