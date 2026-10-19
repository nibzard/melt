# Round 1 Linux gate record

This document closes the round 1 Linux gate of `SPEC.md` section 20
step 5 (backlog task T024). The machine-readable evidence sits beside it
as `round-1-linux.json`; the contract test
`tests/contract/test_gate_record.py` keeps the two honest against the
tree they describe.

Gate closed: 2026-10-18T12:57:20Z. Verified tree: `5ea5f01`
(`Integrate T023`); the commit that carries this record sits on top of
that tree and adds only the record, its test, and the README note. A
confirmation run with the record in place collected the nine
record-honesty tests as well: 798 passed, 1 skipped in 126.04 s.

## What the gate requires

Section 20 step 5 passes the hero workflow, the contract suite, the
race suite, and the publication fault tests, records the compiler and
reader measurements, and needs no native application build. Section 20
also fixes the bar: round 1 is complete when the Linux harness proves
the full language and execution loop, including patches and restart
recovery. **A valid JSON document alone does not pass.** Everything
recorded below exercised the loop through the command-line harness and
the same service handlers the later native host will drive, on real
durable state, with real project processes and real kills.

## Recorded run

| Property | Value |
|---|---|
| Command | `./scripts/check.sh` (or `make check`) |
| Result | 789 passed, 1 skipped in 128.85 s |
| Canonical hashes | 6 RFC 8785 reference vectors and 7 valid-document hashes agree between the JavaScript and Python implementations |
| Skipped | `tests/native/test_native_round2.py` — the round-2 placeholder that claims no behavior (backlog task T030) |
| Platform | Ubuntu 24.04.5 LTS, Linux 6.8.0-147-generic, x86_64, 8 logical cores, 12,246,824 KiB memory |

The gate ran on the machine that also produced the performance record,
including its declared deviation from the 16 GiB class. The suite needs
`python3` 3.12 through `uv sync --locked`, `uv`, and Node with npm for
the pinned Pi Durable adapter — nothing else.

## The four suites

### Hero workflow — `SPEC.md` section 18.2

`uv run --locked python3 -m pytest tests/integration/test_hero_workflow.py`
— 1 passed in 16.26 s, through real harness processes that each reopen
the durable store.

The input holds rows (2026-01, 100), (2026-01, 300), and (2026-02, 200).
At minimum zero the output table has three rows. At minimum 200 it has
two rows and the monthly totals are 300 and 200. The six steps:

1. `create`, `import`, `register`, and `apply` build the workspace
   without executing: no run exists and the output group stays empty.
2. `approve`, `invoke --launch --publish` run the action and publish
   both outputs together, with run provenance on both output sources
   and both targets on that one publication; the plot exports as a
   real PNG artifact.
3. `set --value 200 --dispatch --debounce-wait 400 --launch --publish`
   reruns through the bound action with zero model calls: fake model
   credentials ride every command's environment, every run still
   succeeds, and no persisted byte under the data root carries them.
4. The pane moves through `apply --operations
   fixtures/sales/layout-move.json` in the staged window of an
   execution — its process completed, its publication pending — and the
   moved layout survives that publication and the compiled plan.
5. A fresh command reopens the workspace offline: values, bindings,
   sources, and layout restore, and nothing executes; the restart
   interrupts the pending debounce request and keeps the retained
   result readable, with its captured parameters, marked stale.
6. `invoke` again admits a run whose captured inputs name `sales-input`
   version 1 and whose captured parameters name the committed minimum;
   reopening marks it interrupted instead of executing it.

### Contract suite — `SPEC.md` section 18.1

`node scripts/verify_canonical_hashes.mjs && uv run --locked python3
-m pytest tests/contract` — 525 passed in 3.54 s, and the verifier
printed agreement across the implementations.

Every fixture in `fixtures/invalid/manifest.json` fails with its
recorded diagnostic code and JSON Pointer at its recorded layer —
schema, compiler, or service. Every document in `fixtures/valid/`
compiles through the full stages against `fixtures/catalog.json`,
including the full sales example. `melt validate` and `melt compile`
answer exit codes 0, 1, and 2 over the same fixtures. Canonical RFC
8785 hashes agree between the Python implementation and the independent
JavaScript canonicalizer over the reference vectors and every valid
document. The toolchain tests inside the same suite prove the pinned
interpreter, the exact dependency pins, the hashed lockfile, and that
no round-1 source imports an Apple or Ghostty framework.

### Race suite — `SPEC.md` section 18.3

`uv run --locked python3 -m pytest tests/integration/test_race_suite.py`
— 11 passed in 13.35 s, one test per scenario of the section 18.3
table:

| Scenario | Test |
|---|---|
| Run finishes before debounce fires | `test_a_run_finishing_before_debounce_fires_cannot_become_current` |
| Three changes during a run | `test_three_changes_during_a_run_leave_one_newest_pending_request` |
| A newer run fails | `test_a_newer_failed_run_keeps_the_last_good_result` |
| Action changes during execution | `test_an_action_change_during_execution_archives_the_old_output` |
| Target pane hidden | `test_a_hidden_target_pane_still_receives_its_publication` |
| Target pane deleted | `test_a_deleted_target_pane_rejects_the_entire_publication` |
| Layout or title changes | `test_layout_and_title_changes_keep_the_publication_valid` |
| Parameter round trip | `test_a_parameter_round_trip_still_prevents_old_publication` |
| Programmatic update | `test_a_programmatic_update_saves_and_marks_the_result_stale` |
| Duplicate activation | `test_a_duplicate_activation_request_admits_at_most_one_run` |
| Permission revoked during execution | `test_revoked_permission_during_execution_blocks_publication` |

The during-execution scenarios drive the real blocking
`launch_admitted_run` from a helper thread gated on the staging
directory, so the interleavings happen against a live project process.
The non-blocking execution slot and the process-group cancellation
signal stayed open as backlog task T035 when this gate closed. Both
landed afterwards, and `tests/integration/test_execution_slot.py`
proves them.

### Publication fault tests — `SPEC.md` sections 13.4, 14, and 18.4

`uv run --locked python3 -m pytest tests/recovery
tests/integration/test_publication.py` — 31 passed in 51.73 s.

The recovery module kills a real service child process on both sides of
every acknowledgement boundary — before and after artifact
finalization, before and after the database commit, and after the
commit but before the acknowledgement — and proves that acknowledged
references always point to complete files while retries reconcile the
unacknowledged work, including reused request identifiers answering
with the stored response or `E_REQUEST_REUSE`. It drives disk-full
during finalization, missing artifacts reported individually, missing
environments that disable new work but not saved views, lost
notifications and changed epochs answering snapshot-required, corrupt
descriptors skipped with the previous readable interface kept,
incompatible saved versions refused with every file preserved,
cancelled runs staying cancelled, a kill during execution that never
reruns uncertain Python effects, and startup following the seven
recovery steps of section 14.

The publication module proves atomicity: both outputs become current
together in one commit with all records, a missing, invalid, or
non-finite output rejects the entire publication so the previous table
and plot remain together, obsolete runs archive as superseded without
touching current sources, revoked permission blocks publication and
requests cancellation, and receipts rebuild after a restart.

## Compiler and reader measurements

Section 19 requires recorded measurements for compilation, service
commits, artifact publication, and table paging. The record lives in
`docs/performance/round-1-linux.md` with the raw JSON beside it, and
`tests/contract/test_performance_record.py` ran inside the gate run
above. Headline numbers against the section 19 targets:

| Measurement | Measured p95 | Target | Verdict |
|---|---|---|---|
| Compile a 32-projection document with cached catalogs | 10.546 ms | p95 below 50 ms | met |
| Warm first table page for 1 million rows and 20 columns | 79.699 ms | p95 below 300 ms | met |
| Local control feedback | 7.762 ms | p95 below 50 ms | met |
| Small committed patch to visible update | 6.998 ms | p95 below 100 ms | met |
| Restore five saved panes | 72.096 ms | below 2 seconds | met |

Every round 1 target is met, with warm and cold conditions, fixture
sizes, dependency versions, and the declared machine recorded in the
performance document. Full-table sorting is reported separately there,
as section 19 requires.

## No macOS tools

The gate ran on Linux only. `xcodebuild`, `swiftc`, `osascript`,
`plutil`, and `clang` are not installed on the machine that produced
this record, so nothing in the gate could have used them. Round 1 has
no `native/` directory; the compiler, service, storage, and harness
never import AppKit, SwiftUI, macOS paths, or Ghostty view objects,
and the toolchain test inside the recorded contract suite enforces
that. The single skipped test is the round-2 native placeholder, which
documents its own deferral and claims no behavior.

## Round boundary

Closing this gate completes `SPEC.md` section 20 step 5, not the
milestone: steps 6 and 7 (native macOS integration and the application
release gate) and the RFC 001 semantic-interaction extension remain
open work under their own backlog tasks and language version. Two
tracked round-1 items stayed open without blocking this gate when it
closed: the non-blocking execution slot with process-group cancellation
(T035) and the README hero step 3 debounce flag (T036). Both completed
and integrated afterwards, so backlog task T039 moved them out of
`roundBoundary.openTrackedWork`, which now records no open tracked
round-1 work.

## Reproduce

1. Run `uv sync --locked`.
2. Run `make check`.
3. Read the per-suite commands from `round-1-linux.json` to repeat any
   single suite.
4. Refresh the measurements with `make benchmark-round1` and update
   `docs/performance/` from that run.
