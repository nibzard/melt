# RFC 001 acceptance audit

Date: 2026-10-19.

The Linux implementation covers the runtime behavior in [RFC 001](../rfcs/001-semantic-interactions.md).
It uses the experimental identifier `melt/semantic-draft-1`.
This audit does not assign a stable language version or close the native release gate.

## Baseline evidence

Ran `make check` before the host changes against source revision `a38ac5f`.
Result: **1,115 passed, 2 skipped in 239.13 seconds**.
The skips cover generated Swift verification and the existing native integration placeholder.
The machine runs Linux and has no Swift toolchain or Apple frameworks.

Protocol regeneration and the independent JavaScript canonicalization checks also passed.
The gate verifies v0.1 and the semantic draft in the same run.
The existing [round 1 record](round-1-linux.md) remains a historical record of its original revision.

## Acceptance map

| RFC section | Implementation and evidence | Result |
|---|---|---|
| 3: Collection identity | `runtime/workspace/semantic.py`; `tests/integration/test_semantic_collections.py` | Unique string keys, pinned versions, issued incarnations, and unresolved references are covered. |
| 4: Shared state | `python/melt/schema/semantic.py`; `tests/integration/test_extension_state_durability.py` | Reference values and revisions survive commits and restart. |
| 5: Pure events | `tests/contract/test_semantic_events.py`; `tests/integration/test_semantic_interactions.py` | Selection and clearing use typed events without model calls or project execution. |
| 6: Observers | `runtime/workspace/observers.py`; `tests/integration/test_observer_availability.py` | Linked inspectors follow one selection; missing artifacts degrade individually. |
| 7: Graph boundary | `runtime/rendering/pele_adapter`; `tests/integration/test_pele_rendering.py` | Linux rendering, identity mapping, host hit regions, and output sanitization are covered. Native gestures and accessibility remain open. |
| 8: Independent outputs | `tests/integration/test_output_independence.py`; `tests/integration/test_output_owner_identity.py` | Publication without panes, later subscriptions, owner identity, and obsolete results are covered. |
| 8.3: Migration | `tests/integration/test_migration.py` | Explicit preview, backup, refusal during active work, preservation, and failed migration recovery are covered. |
| 9: Inspection and preview | `tests/integration/test_semantic_interactions.py`; `tests/contract/test_semantic_compiler.py` | Plans expose identities, dependencies, observers, subscriptions, and revisions. |
| 10.1: Baseline | `tests/integration/test_hero_workflow.py`; `tests/integration/test_migration.py` | The v0.1 workflow and explicit migration pass. |
| 10.2–10.3: Supplier workflow | `tests/integration/test_semantic_harness.py`; `examples/03-linked-views/run.py` | A new inspector observes the current selection without resetting it. |
| 10.4–10.5: Publication, conflicts, recovery | `tests/integration/test_output_independence.py`; `tests/integration/test_semantic_conflicts.py` | Atomic publication, stale guards, duplicate receipts, failed commits, and recovery are covered. |

## Findings and changes

No missing Linux acceptance behavior surfaced in the baseline run.
Two documentation gaps needed correction:

- Section 6 used illustrative renderer fields that do not match the implemented document vocabulary. It now uses canonical projection shapes.
- Section 8 did not describe the implemented pinned subscription form. It now documents that form alongside direct source pinning.

These corrections describe the existing experimental behavior.
They do not change v0.1, its language identifier, or its migration requirements.

The [café example](../../examples/04-cafe/README.md) adds a real application workflow over these contracts.
It publishes a break-even result before any view subscribes, then adds the view without another run.

## Follow-up validation

After adding the café example and private host adapter, `make check` passed with **1,124 passed, 3 skipped in 258.45 seconds**.
Protocol regeneration and JavaScript canonicalization checks also passed.

The nine tests in `tests/integration/test_desktop_bridge.py` exercise the actual durable service and child-process channel:

- Reopen the same compiled layout, parameter values, table, plot, and text without execution.
- Reject a stale edit while retaining the newer committed value and previous results.
- Launch and publish a calculation asynchronously while inspection remains available.
- Dispatch a debounced parameter binding through host polling.
- Keep execution grants outside programmatic proposals.
- Reject oversized frames before reading their payloads.
- Degrade one missing artifact without losing other saved views.
- Keep child stdout limited to framed responses.
- Route graph events with displayed revision guards and update both linked inspectors without execution.

The three skips cover generated Swift verification, the new AppKit build and controller checks, and the existing native integration placeholder.
Shell syntax and bundle metadata checks passed on Linux.
`native/build.sh` refuses Linux explicitly; no native compilation result is claimed.

A subsequent focused run passed all ten desktop bridge tests in 19.42 seconds.
The added test closes the actual child channel while a worker runs, verifies that worker termination completes,
and reopens the same workspace without replay while retaining the previous views and parameter values.
This focused result does not replace the full-suite record above.

The historical gate test now checks absence of native sources at its recorded revision.
It continues to enforce the current Linux framework-import boundary.

## Remaining acceptance work

- Review the experimental semantics before assigning a stable language identifier.
- Compile and exercise the [native host](../../native/README.md) on macOS.
- Verify focus, input methods, accessibility, pane continuity, overflow, and native response times.
- Integrate and test Ghostty alongside Melt panes.
- Complete packaging, signing, and clean-machine checks before claiming an application release.

Editable domain records, portable app packages, live sharing, and automatic migrations remain separate design work.
The local `.meltapp` launcher is a host reference to an existing workspace, not a portable export format.
