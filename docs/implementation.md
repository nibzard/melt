# Implementation reference

[← Back to Melt](../README.md)

This reference describes the implemented components, command-line workflows,
and acceptance evidence. Run commands from the repository root. Paths in
code spans are relative to that root. [SPEC.md](../SPEC.md) defines the
`melt/v0.1` contract; the [semantic extension](rfcs/001-semantic-interactions.md)
defines the experimental additions.

## Layout

```text
python/melt/    schema, sdk, runner     (Python package `melt`)
runtime/        compiler, workspace, execution, storage, harness
protocol/       generated JSON Schema and language models
fixtures/       valid, invalid, sales
tests/          contract, recovery, native, integration
scripts/        developer commands
```

The Linux runtime stays independent of `native/`.
The [first native host source](../native/README.md) now lives there, with macOS compilation and interaction checks still pending.
The Linux gate does not require Apple tools (SPEC.md sections 2.2 and 20).

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
  `runtime/storage/pi_adapter/package-lock.json` (SPEC.md section 14),
  and for the pinned Pele renderer `pele@1.6.0` in
  `runtime/rendering/pele_adapter/package-lock.json` (RFC 001
  section 7.3).

Set up the environment:

```bash
uv sync --locked
make probe-setup
make pele-setup
```

## Run the full test suite

One command runs everything:

```bash
make check
```

`make check` calls `scripts/check.sh`. The script checks the pinned
interpreter, syncs `.venv` from `uv.lock`, installs the pinned Pi Durable
adapter and the pinned Pele renderer when missing, verifies that
regenerating the protocol artifacts leaves the committed files
unchanged, verifies the canonical document hashes with the independent
JavaScript canonicalizer and the generated TypeScript models, and runs
pytest over
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

Run the Pele rendering probe alone (`make pele-setup` first); it proves
the rendered-element identity mapping, the host-owned hit regions, and
the sanitized, external-resource-free output of the pinned renderer, and
records its limitations in
`runtime/rendering/pele_adapter/README.md`:

```bash
make probe-pele
```

After you change `pyproject.toml`, refresh the lockfile:

```bash
make lock
```

## Regenerate the protocol artifacts

One command exports the JSON Schema and generates the TypeScript and
Swift models from the single restricted vocabulary of
`python/melt/schema` (SPEC.md section 17, backlog task T007):

```bash
make protocol
```

The command writes every file under `protocol/schema` and
`protocol/generated`; each README there documents its files. Never edit
a generated file by hand and never author an independent schema: change
the vocabulary and regenerate. `scripts/check.sh` reruns the generator
with `--check` and fails when regeneration changes committed output, so
`make check` enforces the rule.

The generated models also verify canonical hashes: they validate a
document, normalize it into the wire form (every field present, absent
optional fields explicit nulls or their defaults), and hash it with
RFC 8785 canonicalization. `scripts/verify_canonical_hashes.mjs` runs
the TypeScript models over `fixtures/valid/document-hashes.json` and
`fixtures/semantic/document-hashes.json`, so the semantic extension
fixtures join the cross-language agreement in their own index and the
v0.1 suite stays unchanged (SPEC.md sections 4.2 and 18.1). Where a
Swift toolchain exists, `scripts/check.sh` also runs the generated
Swift harness, which replays the RFC 8785 reference vectors and both
hash indexes; the Linux gate itself never requires Swift (SPEC.md
section 2.2).

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
idempotently; a cancellation also signals the owned process group of a
run this service still executes, escalating from `SIGTERM` to `SIGKILL`
after the configured grace, and answers the signal report (backlog task
T035, SPEC.md section 13.5). Artifact and script tokens are minted by the host capture
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
- `runtime.execution.spawn_run(manifest, log_directory=...)`, which
  spawns the runner as the leader of its own session and returns the
  `OwnedRunProcess` handle: `wait()` reports the launch result and
  `terminate(grace_seconds=...)` signals the owned process group,
  escalating from `SIGTERM` to `SIGKILL` (backlog task T035)
- `runtime.execution.launch_run(manifest, log_directory=...)`, the
  blocking spawn-and-wait shape of the same launch
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
`runtime.execution.spawn_run` with the pre-launch rechecks —
permission, registration availability, and the resolved environment
fingerprint against the admission snapshot — and drives one project
execution at a time per workspace
(`service.launch_admitted_run`, `service.launch_next_run`). The launch
registers the owned process handle before the running transition
commits, so a run the record shows as running always has a signalable
handle. The default launch waits for its process; the non-blocking
execution slot of backlog task T035 parks the wait on the service's own
execution thread instead (`launch_admitted_run(run_id, wait=False)`),
so the framed channel keeps answering every request — `run.cancel`,
`workspace.inspect`, `workspace.apply`, `table.page` — while a project
process executes: `service.execution_run_id` names the executing run
and `service.wait_for_execution(timeout=...)` delivers its terminal
outcome. A cancellation stops scheduling durably and signals the owned
process group with escalation (SPEC.md section 13.5), and closing the
service terminates a still-executing owned process and concludes its
run interrupted (SPEC.md section 14). A restart
marks pending timers and active jobs interrupted, keeps the reserved
generations, and never executes anything on its own. Entry points:

- `service.dispatch_due_intents(now_epoch=...)` to fire due timers
- `service.launch_admitted_run(run_id)` and `service.launch_next_run()`
- `service.launch_admitted_run(run_id, wait=False)`,
  `service.execution_run_id`, and
  `service.wait_for_execution(timeout=...)` for the non-blocking slot
- `service.output_groups`, `service.pending_intents`, and
  `service.run_is_newest_generation(run_id)`
- `service.dispatch_as(envelope, origin="programmatic")` for the agent
  event origin, which commits changes without firing bindings

`runtime/workspace/scheduling.py` also carries the parameter identity
of every admission (backlog task T044, SPEC.md sections 7.3 and 12.3).
Each captured parameter records the identity incarnation of the
workspace parameter it addressed beside the value and state revision,
in the pending intent and the run record, and the run also names the
workspace parameter of every captured entry. Dispatch and the
pre-launch recheck compare the captured identity against the stored
one through `captured_parameter_identity_error`, so an invocation
whose parameter a later transaction deleted and recreated expires at
dispatch and refuses its launch even when the recreated name, value,
and state revision match: a recreated parameter is a new identity, and
no event of the old one may target it. An older run record saved
before identity capture reads its missing incarnation as the first,
which no recreation can satisfy again, so an unrecreated parameter
keeps its older runs publishable and nothing uncertain reruns. The
regression suite is `tests/integration/test_parameter_identity.py`.

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
that no current reference points at, while the owning action's previous
current run flips to superseded when a newer publication lands. Output
source identifiers are service-owned mappings from `(action id, output
name)` rebuilt from committed run provenance, never derived from
titles. Every output group reports `empty`, `pending`, `current`,
`stale`, or `error` status with `resultIsStale` and the captured
parameters of the retained result, through
`service.output_group_status()` and the `outputGroups` view of
`workspace.inspect`. The status, the retained result, and the pending
request of an entry all belong to the action that owns the group, so a
freed group name never lends another action's run to the entry (backlog
task T045). The eligibility recheck compares captured
parameter identity, not revisions alone: a parameter a later
transaction deleted and recreated holds a new incarnation while its
state restarts at revision one, so the older run archives as superseded
and the group's retained result reports stale with its identity
reason, while the previous visible sources survive untouched (backlog
task T044, SPEC.md sections 12.3, 13.3, and 13.5). Entry points:

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

The table reader caches bounded batches, decoded tables, and query row positions
within each service session. See [the optimization measurements](performance/optimization-2026-10-19.md)
for cache limits, first-read costs, and before/after results.

The [workspace scale and concurrency measurements](performance/scale-2026-10-19.md)
cover larger histories and simultaneous reads and edits. Run them with
`uv run --locked python3 scripts/benchmark_scale.py`.
The [fixes and repeated measurements](performance/scale-fixes-2026-10-19.md)
cover bounded history scans, partial table reads, and edits during sorting.

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

The handshake offers both language identifiers, `melt/v0.1` and
`melt/semantic-draft-1`, so the same harness drives a semantic
workspace: `create --language melt/semantic-draft-1` starts one, and
`select`, `clear`, and `render` drive the pure interaction loop of
RFC 001 section 10.2 through the test renderer (`runtime/harness
/test_renderer.py`). `--guard path=revision` overrides one observed
revision of an event, which replays a stale gesture on purpose.
`migrate [--dry-run]` moves a quiesced `melt/v0.1` workspace to the
extension language (RFC 001 section 8.3): the dry run explains what
changes, and the committed migration saves a backup first.

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
3. `set --value 200 --dispatch --debounce-wait 400 --launch --publish`
   reruns through the bound action with zero model calls: every command
   runs with fake model credentials in its environment, the runs still
   succeed, and no persisted byte under the data root carries them.
   The `--debounce-wait 400` holds the command open past the binding's
   250 ms debounce so the just-admitted intent dispatches in the same
   session; without it a reopen interrupts the pending timer before it
   fires.
4. The pane moves through `apply --operations
   fixtures/sales/layout-move.json` in the staged window of an
   execution — its process completed, its publication pending — and
   the moved layout survives that publication and the compiled plan.
   The single-process CLI still serializes this way; a mutation
   concurrent with a live project process runs through the
   non-blocking execution slot of backlog task T035 (execution-slot
   suite and race suite T021).
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
- `fixtures/semantic/` the isolated fixtures of the
  `melt/semantic-draft-1` extension draft; see the section below.

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
replays the RFC 8785 reference vectors. The generated TypeScript and
Swift models of `protocol/generated` verify the same indexes through
their own table-driven validators, and the semantic extension fixtures
join the agreement through the separate
`fixtures/semantic/document-hashes.json` index, so the v0.1 suite runs
unchanged (backlog task T007). `make check` runs everything, so the
contract suite blocks merges on failure.

## Round 1 gate

The round 1 Linux gate of `SPEC.md` section 20 step 5 is closed
(backlog task T024). The recorded evidence lives in
`docs/gates/round-1-linux.md` with the machine-readable
`round-1-linux.json` beside it, and `tests/contract/test_gate_record.py`
keeps that record honest against the tree: every suite and module it
names exists, the race suite still holds one test per section 18.3
scenario, the fault suite still holds one test per section 18.4 fault,
and the compiler and reader measurements still agree with
`docs/performance/round-1-linux.json`.

The gate record names the closing run — `make check`, 789 passed and
one round-2 placeholder skipped — the per-suite commands and results,
the measured numbers against every section 19 target, and the proof
that no macOS or Apple tool took part. A valid JSON document alone
does not pass that gate: the recorded run exercised the full language
and execution loop, patches and restart recovery included, through the
command-line harness. Native macOS integration and the RFC 001
extension stay outside it under their own backlog tasks.

## Semantic extension draft

The RFC 001 extension starts under the provisional identifier
`melt/semantic-draft-1` (backlog task T025). It is not a stable
interchange version: it marks isolated experimental fixtures, and a
`melt/v0.1` document still rejects both extension fields, so the v0.1
contract suite runs unchanged (RFC 001 section 1).

What landed:

- `python/melt/schema/semantic.py` adds the `collections` and `state`
  maps: typed identity indexes over table source versions, and
  nullable reference state with workspace scope (RFC 001 sections 3
  and 4). `parse_document_for_language` dispatches on the language
  identifier.
- The compiler extends the existing plans (RFC 001 section 9): the
  state plan carries each reference entry with its collection, key
  column, pinned source, and the incarnation the service issued, and
  the dependency plan indexes the source version each collection
  reads. Stage 5 applies the same continuity rule to stored
  selections that parameters follow.
- The service issues an opaque incarnation when a collection entity is
  created (`runtime/workspace/semantic.py`); replacing a binding keeps
  it, and deleting and recreating the entity issues another one, so an
  old reference cannot select an object in the new collection.
- Admission verifies the index: the service reads the pinned table
  bytes and rejects a binding whose key column repeats or nulls a key
  (`E_REFERENCE` at `/collections/<id>/key`), keeping the previous
  definitions.
- `state.set` assigns one reference or null through the same
  revision-checked mutation machinery (`refstate/<id>` expectations).
  An assignment must name the declared collection and the current
  incarnation; a stale or forged incarnation conflicts.
- Resolution reports `resolved`, `missing`, or `unavailable` through
  `workspace.inspect`. A newer source version that omits the key
  leaves the stored reference untouched and reports `missing`; an
  unreadable artifact reports `unavailable`; a returning key resolves
  again (RFC 001 section 3.3). A reopen restores the incarnations,
  the selections, and their resolution without executing anything.
- Snapshots carry the extension state (`collectionIncarnations` and
  `referenceStates`), and a `melt/v0.1` workspace refuses the
  extension operations and fields with `E_VERSION` and `E_SCHEMA`.

`fixtures/semantic/` holds the supplier fixture of RFC 001 section
10.2 (`S-42` and `S-73`), its graph and table artifacts, its catalog,
the definitions and the stored agent patch of section 10.3, and an
invalid manifest; `tests/contract/test_semantic_*.py` and
`tests/integration/test_semantic_*.py` prove the behaviors above.
Extension conflicts and recovery are the T029 suite below.

### Pure events, observers, and the test renderer (T026)

RFC 001 sections 5 through 7 add the interaction half of the
extension:

- `object.select` and `selection.clear` are renderer events with
  typed `state.set` bindings and no expressions. A binding observes
  the projection whose renderer declares the event, and its value
  selector names the declared `objectRef` field or the typed null
  literal — nothing else parses. Each event address carries at most
  one writer, a pure event never invokes a capability, and its
  debounce is fixed at zero (`python/melt/schema/semantic.py`).
- The `melt.graph` and `melt.record` renderers join the trusted
  registry with their event schemas and observer slots. A graph
  source is an immutable JSON artifact whose measured schema is its
  validated identity map: stable node IDs bound to semantic
  references, edges between existing nodes, one collection per
  selectable graph (RFC 001 sections 7.1 and 7.2).
- The `highlight` and `record` observers read state through fixed
  selector slots. The compiled plans carry them: the render plan
  names each slot, the dependency plan indexes state to observers,
  and the identity plan maps graph nodes to semantic references
  (RFC 001 sections 6 and 9).
- The service commits one pure event in a single transaction: the
  state change, its notification, and the receipt land together. The
  revision guards ride the envelope's `expected` map — the target
  state, the projection, the binding, and the displayed source and
  collection records — so a stale graph or collection revision
  rejects the event even when its key still exists. A duplicate event
  ID replays the stored receipt; reuse with different content fails
  with `E_REQUEST_REUSE` (RFC 001 section 5.3).
- Nothing in the path launches Python or calls a model: no run is
  admitted, no generation reserved, no staging directory created.
  A `melt/v0.1` workspace refuses both event methods with `E_VERSION`.
- The Linux test renderer (`runtime/harness/test_renderer.py`) builds
  each event the way a host would and resolves every observer slot
  through the service's own inspection view. `select`, `clear`, and
  `render` drive it from the command line.

The supplier fixture proves the whole loop
(`tests/integration/test_semantic_interactions.py` and
`test_semantic_harness.py`): selecting `S-42` commits one state
revision, the inspector resolves the `S-42` record, the graph
highlights its node through the compiled observer plan, a restart
restores the selection and its resolution, a collection version that
omits `S-42` reports `missing` while the reference stays stored, and
an event from a previous graph or collection revision is rejected.
The stored agent patch of RFC 001 section 10.3
(`fixtures/semantic/patches/second-inspector.json`) then adds a second
inspector: the selected reference, the state revision, and every
existing projection and binding revision survive unchanged, and the
new inspector resolves the current selection immediately.

An artifact the store can no longer read never blocks that loop
(backlog task T046). The `record` observer reads its pinned table
inside a guarded read: a missing, unreadable, or no longer decodable
artifact answers one `unavailable` view that keeps the stored
reference and a recoverable reason, while the normal `E_ARTIFACT`
diagnostic and the startup recovery report still ride the same
inspection (RFC 001 sections 6 and 10.5, SPEC.md section 14). Unrelated
observers, sources, and state stay inspectable on the dispatch path and
on the framed channel, a restart with the file still missing keeps the
workspace readable, and restoring the same content-addressed bytes
resolves the observer again without a new selection or any Python run.
The regression suite is
`tests/integration/test_observer_availability.py`.

### Pane-independent outputs and the v0.1 migration (T027)

RFC 001 section 8 separates publication from panes:

- An extension action declares output names, kinds, and a publication
  group, and carries no `targets` (`python/melt/schema/semantic.py`).
  A `melt/v0.1` action keeps requiring them, and a v0.1 projection
  source stays a source reference or null.
- A projection source may subscribe to an output by its stable
  incarnation: `{"actionOutput": {"action", "output",
  "incarnation"}, "follow": "current"}` follows the output's current
  pointer, and `"follow": "pinned"` with a `version` freezes one
  version of the output's stable logical source. The alternative form
  of section 8.1 — pinning through the existing source reference —
  keeps working.
- The service issues one opaque output incarnation when an action
  entity is created (`runtime/workspace/outputs.py`); replacing a
  definition keeps it, and deleting or recreating the action issues
  another one, so old subscribers cannot attach to a new calculation.
  The compiler verifies the incarnation of every subscription
  (`E_CONFLICT` at the subscription path); the mutation path also
  rejects a pinned subscription to a version the output never
  published.
- Publication stays atomic and eligibility no longer depends on pane
  existence: hiding, closing, or removing a projection neither cancels
  a run nor invalidates its outputs, and the publication transaction
  stops rewriting projections at all — a subscriber resolves the
  output pointers of the commit sequence it reads. Deleting the action
  itself invalidates the current publication in the same transaction
  that removes it. A `melt/v0.1` workspace keeps every v0.1 rule,
  including the rejected publication of a deleted target
  (`runtime/workspace/publication.py` and `service.py`).
- `workspace.inspect` exposes the outputs view: each action's
  incarnation, each output's current pointer with its provenance, and
  every subscriber's resolution — `current`, `empty`, `pinned` with a
  `stale` flag, or `unavailable` (RFC 001 section 9). The dependency
  plan carries the output-to-subscriber index, and the action plan
  carries the stable identity without pane ownership.
- A current pointer belongs to the output identity that published it,
  never to the reusable group name (backlog task T045). Resolution and
  inspection pick the owning action's own current run — the run also
  carries the incarnation its admission captured, and a run of a
  retired incarnation no longer counts — so a transaction that moves
  an action to another group and hands the freed name to an action
  that never ran leaves the new owner with no pointer and the previous
  owner with its own result, reported `stale` under the group it holds
  now. Retirement of a deleted or recreated action keys on the owner
  as well, so an action that moved before its deletion still loses its
  current publication. Nothing executes on either path, and the
  previous owner's source versions stay readable as history.

The explicit migration of RFC 001 section 8.3 is the
`workspace.migrate` method plus the harness `migrate` command:

- The migration refuses while a run is active or a request is pending:
  quiesce them first. `--dry-run` previews the same plan and writes
  nothing — no receipt, no backup, no revision — and its report
  explains that pane removal no longer invalidates output publication.
- The committed migration validates the new document, saves a backup
  of the database into `backups/` through the SQLite backup API, and
  changes the language in one transaction. Each v0.1 target becomes a
  following subscription; source versions, projection IDs, parameter
  values, and layout are unchanged. A failure at any step leaves
  `melt/v0.1` current, and the old histories keep their records.

`tests/integration/test_output_independence.py` proves the acceptance
of RFC 001 section 10.4 — publishing with no projections, two
subscribers resolving one saved output without a second execution,
removing a subscriber during another run, pinned subscriptions,
obsolete generations, a missing output rejecting the entire
publication, and recreated actions refusing old incarnations.
`tests/integration/test_output_owner_identity.py` proves the owner
binding of backlog task T045: a freed group name lends no run to the
action that reuses it, the moved owner keeps its subscribers and its
`stale` result without a new execution, group swaps between two
published actions keep their pointers apart, a renamed output contract
holds no pointer, recreation retires the previous incarnation's run
even after a group move, and inspection agrees with subscriber
resolution across a restart. `tests/integration/test_migration.py`
proves section 10.1 through the
harness and the service: equal saved sources, parameters, and layout
after the migration, a second table subscriber without a rerun, and
the v0.1 boundary that still rejects a deleted target.

### Pele rendering and identity mapping (T028)

RFC 001 section 7.3 names the pinned Pele renderer an external
prerequisite. `runtime/rendering/pele_adapter` pins `pele@1.6.0` and
probes its actual Linux interface; `pele_render.mjs` is the
application-owned adapter every render goes through:

- One validated graph lowers to a Mermaid flowchart whose node IDs are
  the graph's own node IDs, so each rendered node group's `data-id`
  names the graph node. The adapter verifies the rendered identity set
  equals the graph's node ID set — no more, no less — and fails closed
  when it drifts. The graph and its identity map stay authoritative;
  the SVG is derived.
- Host-owned hit regions close the interaction gap: Pele 1.6.0 exposes
  no hit-testing, event, focus, or accessibility API on Linux, so the
  adapter extracts each node's shape bounds from the rendered SVG and
  the trusted host maps a click in user space to a node ID, then
  through the identity map to a semantic reference. SVG generation
  alone is not interaction, and nothing claims it is.
- Labels are reduced to plain text and Mermaid numeric character
  references before lowering; render options are frozen with `links`
  and `images` disabled; and the output is verified against an element
  allowlist plus scans for handlers, URL references, and CSS imports.
  A hostile label neither breaks the renderer nor smuggles markup.

`tests/integration/test_pele_rendering.py` runs the probe inside
`make check`; `runtime/rendering/pele_adapter/README.md` records the
pin, the proven properties, and the limitations (no interaction API,
ambiguous rendered edge IDs, no per-node accessibility output,
environment-dependent text measurement). The Linux semantic tests keep
using the test renderer; native rendering is the later macOS round.

### Extension conflicts and recovery (T029)

RFC 001 section 10.5 names six failure and conflict shapes and two
invariants. `tests/integration/test_semantic_conflicts.py` drives all
of them through the same service handlers the native host will use:

- Duplicate events replay their stored receipt; the commit sequence
  and the state revision move once. A reused request ID with a changed
  payload answers `E_REQUEST_REUSE`, before and after a restart,
  because receipts live for the workspace lifetime.
- A stale state revision conflicts without committing and stores no
  receipt, so the same request ID stays usable once the host
  refreshes its guards.
- A recreated collection ID issues another incarnation. The recreation
  transaction must clear or replace the stored selection that names
  the previous one (`E_STATE_MIGRATION` otherwise), and an event
  carrying the old incarnation conflicts even with fresh guards, so an
  old reference cannot select an object in the new collection.
- A failed commit is never acknowledged: a storage failure inside the
  append surfaces instead of a false receipt, nothing moves, and the
  retry of the same request commits exactly once.
- A lost acknowledgement reconciles through the receipt. The verbatim
  retry replays the stored response — across a restart — and the
  notification stream still carries the commit, so a host that lost
  the frame can resynchronize. A retry rebuilt from the new snapshot
  is different content and answers `E_REQUEST_REUSE`.
- Unavailable renderer versions degrade the host display only. The
  test renderer takes the registry the host ships
  (`render --renderer ID=VERSION` on the harness): a pinned renderer
  the set omits or disagrees with renders a fallback pane with source
  details and diagnostics, and refuses to originate a gesture.

The two invariants:

- An optimistic selection is never reported durable before commit.
  `OptimisticSelection` (`runtime/harness/test_renderer.py`) models the
  host display of RFC 001 section 5.3: the attempted key shows
  immediately with an unsaved marker, while a durable read taken
  inside the commit window — before the store append lands — still
  reports the previous committed value. A failed append restores the
  committed display as unanswered; only the acknowledgement turns the
  display committed, and a rejected gesture restores it with a
  conflict status.
- An unavailable graph renderer never blocks inspection of saved
  sources or semantic state. `workspace.inspect` keeps answering the
  full snapshot — sources, committed selections with their resolution,
  observer views, and the pinned renderer versions the host needs to
  detect the gap — while the render degrades to the fallback pane
  only.
