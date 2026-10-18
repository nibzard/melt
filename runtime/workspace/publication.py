"""Atomic publication and result status (SPEC.md sections 13.3 to 13.5).

Publication is the transaction that turns one completed run into saved
results. The workspace service applies the six steps of section 13.4
inside one owning transaction:

1. Verify declared files, types, hashes, sizes, and output completeness.
2. Synchronize staged files and finalize immutable artifact paths.
3. Recheck eligibility within the owning service transaction.
4. Allocate source versions and update all target references together.
5. Store run provenance, disposition, and the publication receipt.
6. Commit and acknowledge before showing a saved result.

This module holds the pure pieces of that transaction: the independent
verification of staged files, the eligibility conditions of section
13.3, the service-owned mapping from ``(action ID, output name)`` to a
stable output source, and the result status of section 13.5. The
transaction itself belongs to :mod:`runtime.workspace.service`.

The eligibility split decides what a failed check leaves behind:

- An obsolete run (a newer generation exists, or its captured action,
  registration, output group, parameter, or input dependencies moved)
  verifies and archives as ``superseded``: its outputs become historical
  source versions that no current reference points at (section 13.3).
- A refused publication (invalid or missing output, a broken target, a
  denied permission recheck) is ``rejected``: nothing is allocated and
  the previous table and plot remain together.

The result status never reports a rejected publication as an error by
itself: section 13.5 reserves ``error`` for a latest intent whose
process failed or was cancelled or interrupted. The status entry
carries the newest run with its disposition, so a host can show why the
newest attempt produced nothing.
"""

from __future__ import annotations

import io
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from melt.schema.canonical import sha256_hash
from melt.schema.diagnostics import Diagnostic, DiagnosticCode, sorted_diagnostics
from melt.schema.records import (
    OutputGroupRecord,
    PendingIntentRecord,
    RunRecord,
)
from melt.sdk.context import OUTPUTS_DIRECTORY_NAME
from runtime.workspace.importer import (
    MeasuredArtifact,
    MeltImportError,
    append_source_version,
    measure_artifact,
    run_version,
)
from runtime.workspace.state import ParameterStateValue

PUBLICATION_ID_PREFIX = "meltpublication_"
"""Service-minted publication receipt identifiers carry this prefix."""

_ID_ENTROPY_BYTES = 18
"""Random bytes per receipt identifier: 144 bits, far beyond guessing."""

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
PNG_MEDIA_TYPE = "image/png"
TEXT_MEDIA_TYPE = "text/plain"

MEDIA_TYPE_BY_KIND = {
    "table": PARQUET_MEDIA_TYPE,
    "image": PNG_MEDIA_TYPE,
    "text": TEXT_MEDIA_TYPE,
}
"""The media type each output kind materializes as (section 13.2)."""

OUTPUT_SUFFIX_BY_KIND = {"table": ".parquet", "image": ".png", "text": ".txt"}
"""The file name suffix the SDK stages each output kind under."""

PROJECTION_KIND_OF_SOURCE_KIND = {"table": "table", "image": "plot", "text": "document"}
"""The projection kind that renders each source kind (section 8)."""

CURRENT = "current"
SUPERSEDED = "superseded"
REJECTED = "rejected"
NOT_PUBLISHABLE = "not-publishable"
"""The outcomes a publication attempt reports."""

RESULT_STATUSES = ("empty", "pending", "current", "stale", "error")
"""The status names an output group exposes (SPEC.md section 13.5)."""

_SOURCE_ID_MAX = 64
"""Entity identifiers hold at most this many characters (section 3)."""


def mint_publication_id() -> str:
    """Mint one opaque publication receipt identifier."""

    return PUBLICATION_ID_PREFIX + secrets.token_urlsafe(_ID_ENTROPY_BYTES)


def _diagnostic(
    code: str,
    path: str,
    message: str,
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(code=code, path=path, message=message, hint=hint)


# ---------------------------------------------------------------------------
# Step 1: independent verification of the staged files (section 13.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedOutput:
    """One staged output as the service verified and measured it.

    ``payload`` holds the bytes the service reread itself. The runner's
    staged-output report is never trusted input: publication measures
    the actual files (SPEC.md sections 13.2 and 13.4).
    """

    measured: MeasuredArtifact
    payload: bytes


@dataclass(frozen=True)
class PublicationOutcome:
    """The report of one publication attempt.

    ``outcome`` is ``current``, ``superseded``, ``rejected``, or
    ``not-publishable``. A ``not-publishable`` attempt changed nothing:
    the run never reached a successful terminal state, it holds no
    captured registration, or its finalization failed operationally —
    a full disk above all — which leaves the run eligible for a retry
    once the fault clears (SPEC.md section 18.4). ``refusal`` carries
    the diagnostics of a rejection; ``outputs`` carries the source
    references an archived or current publication allocated.
    """

    run_id: str
    outcome: str
    reason: str = ""
    refusal: tuple[Diagnostic, ...] = ()
    outputs: dict = field(default_factory=dict)
    cancellation_requested: bool = False
    commit_sequence: "int | None" = None


def staged_output_path(staging: Path, name: str, kind: str) -> Path:
    """Return the file one declared output stages under."""

    return Path(staging) / OUTPUTS_DIRECTORY_NAME / f"{name}{OUTPUT_SUFFIX_BY_KIND[kind]}"


def _non_finite_table_error(payload: bytes, name: str) -> "Diagnostic | None":
    """Return the diagnostic for a non-finite float a table carries.

    Non-finite table floats are rejected during publication in v0.1
    (SPEC.md section 5.1). Null cells stay allowed for nullable columns;
    only a present NaN or infinity refuses the output.
    """

    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(payload))
    except Exception:  # pragma: no cover - measure_artifact reported it first
        return None
    for field_item in table.schema:
        if not pa.types.is_floating(field_item.type):
            continue
        finite = pc.fill_null(pc.is_finite(table.column(field_item.name)), True)
        if bool(pc.any(pc.invert(finite)).as_py()):
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"/outputs/{name}",
                f"table output {name!r} carries a non-finite float in"
                f" column {field_item.name!r}; non-finite table floats"
                " are rejected during publication in v0.1",
                hint="Filter, coerce, or clamp the column before staging"
                " the output.",
            )
    return None


def verify_staged_outputs(
    staging: Path,
    declared_outputs: Mapping[str, str],
    *,
    staged_byte_limit: int,
) -> "tuple[dict[str, VerifiedOutput], tuple[Diagnostic, ...]]":
    """Verify every declared output of one run (section 13.4, step 1).

    Each staged file is reread, hashed, size-checked, and measured
    against its declared kind. One missing or invalid output rejects the
    entire publication, so the caller publishes nothing when any
    diagnostic returns (SPEC.md section 13.4).
    """

    verified: dict[str, VerifiedOutput] = {}
    errors: list[Diagnostic] = []
    total = 0
    for name, kind in sorted(declared_outputs.items()):
        path = staged_output_path(staging, name, kind)
        if not path.is_file():
            errors.append(
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    f"/outputs/{name}",
                    f"declared output {name!r} has no staged file at"
                    f" {path.relative_to(Path(staging))}",
                    hint="A missing output rejects the entire publication;"
                    " the previous results remain.",
                )
            )
            continue
        try:
            payload = path.read_bytes()
        except OSError as error:
            errors.append(
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    f"/outputs/{name}",
                    f"staged output {name!r} is unreadable: {error}",
                )
            )
            continue
        if kind not in MEDIA_TYPE_BY_KIND:
            errors.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"/outputs/{name}",
                    f"declared output {name!r} carries unknown kind"
                    f" {kind!r}",
                )
            )
            continue
        try:
            schema = measure_artifact(
                payload, kind=kind, media_type=MEDIA_TYPE_BY_KIND[kind]
            )
        except MeltImportError as error:
            errors.extend(error.diagnostics)
            continue
        if kind == "table":
            non_finite = _non_finite_table_error(payload, name)
            if non_finite is not None:
                errors.append(non_finite)
                continue
        total += len(payload)
        if total > staged_byte_limit:
            errors.append(
                _diagnostic(
                    DiagnosticCode.LIMIT,
                    f"/outputs/{name}",
                    f"staged output bytes exceed the allowed bound of"
                    f" {staged_byte_limit}",
                    hint="Stage smaller outputs; the staged-bytes bound is"
                    " service policy, not a field project code can raise"
                    " (SPEC.md section 15.2).",
                )
            )
            continue
        verified[name] = VerifiedOutput(
            measured=MeasuredArtifact(
                artifact=sha256_hash(payload),
                kind=kind,
                mediaType=MEDIA_TYPE_BY_KIND[kind],
                byteLength=len(payload),
                schema=schema,
            ),
            payload=payload,
        )
    return verified, tuple(sorted_diagnostics(errors))


# ---------------------------------------------------------------------------
# Step 4: the service-owned output source mapping (section 13.4)
# ---------------------------------------------------------------------------


def output_source_mapping(
    document: "Mapping | None", runs: Mapping[str, RunRecord]
) -> dict[tuple[str, str], str]:
    """Rebuild the ``(action ID, output name)`` to source mapping.

    Output source IDs are service-owned and stable: the first
    publication of one action output allocates the logical source, and
    every later publication appends to it (SPEC.md section 13.4). The
    mapping rebuilds from committed data alone, because every allocated
    version carries run provenance and every run record names its
    action, so a reopened workspace recovers the mapping without
    executing anything.
    """

    mapping: dict[tuple[str, str], str] = {}
    for source_id, source in sorted((document or {}).get("sources", {}).items()):
        for version in (source.get("versions") or {}).values():
            origin = version.get("origin") or {}
            if origin.get("kind") != "run":
                continue
            run = runs.get(origin.get("runId"))
            if run is None:
                continue
            mapping.setdefault((run.actionId, origin.get("output")), source_id)
    return mapping


def _candidate_source_id(action_id: str, output: str) -> str:
    """Return the deterministic source ID candidate of one output."""

    base = f"{action_id}-{output}"[:_SOURCE_ID_MAX].rstrip("-_") or "output"
    return base


def allocate_output_sources(
    document: "Mapping | None",
    runs: Mapping[str, RunRecord],
    action_id: str,
    outputs: Iterable[str],
) -> dict[str, str]:
    """Return the stable source ID of every output of one action.

    An output whose mapping already exists keeps its source. A new
    output derives its candidate from ``(action ID, output name)``,
    never from a title, and disambiguates against every existing source
    and every sibling allocation of this publication (SPEC.md section
    13.4).
    """

    existing = output_source_mapping(document, runs)
    occupied = set((document or {}).get("sources", {}))
    allocated: dict[str, str] = {}
    for output in sorted(outputs):
        source_id = existing.get((action_id, output))
        if source_id is None:
            candidate = _candidate_source_id(action_id, output)
            attempt = 2
            while candidate in occupied:
                suffix = f"-{attempt}"
                candidate = (
                    f"{action_id}-{output}"[: _SOURCE_ID_MAX - len(suffix)]
                ).rstrip("-_") + suffix
                attempt += 1
            source_id = candidate
            occupied.add(source_id)
        allocated[output] = source_id
    return allocated


def append_output_versions(
    document: Mapping,
    run_id: str,
    sources: Mapping[str, str],
    verified: Mapping[str, VerifiedOutput],
    *,
    become_current: bool,
) -> "tuple[dict, dict[str, dict]]":
    """Append one run-provenance version per verified output.

    The provenance of every version comes from the service hook alone:
    ``run_version`` attaches ``{kind: run, runId, output}`` inside the
    owning publication transaction, which no document command reaches
    (SPEC.md sections 5.1 and 13.4, step 4). ``become_current`` decides
    whether the appended versions advance each source's current
    reference.
    """

    candidate = dict(document)
    references: dict[str, dict] = {}
    for name in sorted(verified):
        version = run_version(verified[name].measured, run_id, name)
        candidate, number = append_source_version(
            candidate,
            sources[name],
            verified[name].measured,
            version,
            advance_current=become_current,
        )
        references[name] = {"sourceId": sources[name], "version": number}
    return candidate, references


def retarget_projections(
    document: Mapping, action: Mapping, references: Mapping[str, Mapping]
) -> dict:
    """Point every target projection at the references of one publication.

    All target references update together with the allocated source
    versions of the same transaction, so no reader can observe one new
    output beside one old one (SPEC.md section 13.4). The layout is left
    untouched: publication never changes it (section 10).
    """

    import copy

    candidate = copy.deepcopy(dict(document))
    projections = candidate["projections"]
    for name, target in sorted(action.get("targets", {}).items()):
        reference = references.get(name)
        if reference is None:  # pragma: no cover - verification covered it
            continue
        projections[target]["source"] = dict(reference)
    return candidate


# ---------------------------------------------------------------------------
# Step 3: the eligibility conditions (SPEC.md section 13.3)
# ---------------------------------------------------------------------------


def obsolescence_error(
    *,
    document: "Mapping | None",
    run: RunRecord,
    action: "Mapping | None",
    registration_entry,
    group: "OutputGroupRecord | None",
    parameter_states: Mapping[str, ParameterStateValue],
    action_revision_of: Callable[[str], int],
) -> "Diagnostic | None":
    """Return why one run went obsolete, or None while it stays eligible.

    This is the captured-dependency half of the section 13.3
    eligibility: the run must still hold its group's newest generation,
    and its captured action, registration, output group, parameter, and
    input revisions must remain current. A projection title or layout
    change never appears here. The same check reports the staleness of a
    current result, because a drifted current result is exactly a
    dependency that moved (section 13.5).
    """

    if group is None or group.generation != run.generation:
        newest = 0 if group is None else group.generation
        return _diagnostic(
            DiagnosticCode.CONFLICT,
            f"/runs/{run.runId}/generation",
            f"run {run.runId!r} captured generation {run.generation}, but"
            f" its output group {run.outputGroup!r} reserved generation"
            f" {newest}; a newer request superseded the run",
        )
    if action is None:
        return _diagnostic(
            DiagnosticCode.REFERENCE,
            f"/actions/{run.actionId}",
            f"action {run.actionId!r} no longer exists; the run captures a"
            " definition the document dropped",
        )
    if action_revision_of(run.actionId) != run.actionRevision:
        return _diagnostic(
            DiagnosticCode.CONFLICT,
            f"/actions/{run.actionId}",
            f"action {run.actionId!r} moved to revision"
            f" {action_revision_of(run.actionId)}; the run captured"
            f" revision {run.actionRevision}",
        )
    if registration_entry is None:
        return _diagnostic(
            DiagnosticCode.REGISTRATION,
            f"/actions/{run.actionId}/implementation/registrationId",
            f"registration {run.registrationId!r} is no longer available;"
            " the run captured a registration the registry dropped",
        )
    if registration_entry.revision != run.registrationRevision:
        return _diagnostic(
            DiagnosticCode.REGISTRATION,
            f"/actions/{run.actionId}/implementation/registrationId",
            f"registration {run.registrationId!r} moved to revision"
            f" {registration_entry.revision}; the run captured revision"
            f" {run.registrationRevision}",
        )
    if group.revision != run.outputGroupRevision:
        return _diagnostic(
            DiagnosticCode.CONFLICT,
            f"/actions/{run.actionId}/outputGroup",
            f"output group {run.outputGroup!r} moved to revision"
            f" {group.revision}; the run captured revision"
            f" {run.outputGroupRevision}",
        )
    for name, mapping in sorted(action.get("parameters", {}).items()):
        parameter_id = mapping["parameter"]
        stored = parameter_states.get(parameter_id)
        captured = run.parameters.get(name)
        if stored is None or captured is None or stored.revision != captured.revision:
            captured_revision = None if captured is None else captured.revision
            return _diagnostic(
                DiagnosticCode.CONFLICT,
                f"/parameters/{parameter_id}",
                f"parameter {parameter_id!r} moved to state revision"
                f" {0 if stored is None else stored.revision}; the run"
                f" captured revision {captured_revision}",
            )
    current_inputs = {
        name: dict(reference)
        for name, reference in sorted(action.get("inputs", {}).items())
    }
    captured_inputs = {
        name: {"sourceId": reference.sourceId, "version": reference.version}
        for name, reference in sorted(run.inputs.items())
    }
    if current_inputs != captured_inputs:
        return _diagnostic(
            DiagnosticCode.CONFLICT,
            f"/actions/{run.actionId}/inputs",
            f"the inputs of action {run.actionId!r} changed since the run"
            " captured them",
        )
    return None


def target_ineligibility(
    document: "Mapping | None", action_id: str, action: Mapping
) -> "Diagnostic | None":
    """Return the broken-target error of one action, or None.

    Every declared output needs a target projection that still exists
    with the expected kind and this action's ownership (SPEC.md section
    13.3). A committed document satisfies this by compilation; the check
    still guards the publication transaction, because it reads the
    document again inside that transaction.
    """

    projections = (document or {}).get("projections", {})
    outputs = action.get("outputs", {})
    targets = action.get("targets", {})
    for name in sorted(outputs):
        kind = outputs[name]
        target = targets.get(name)
        projection = projections.get(target) if target is not None else None
        if projection is None:
            return _diagnostic(
                DiagnosticCode.REFERENCE,
                f"/actions/{action_id}/targets/{name}",
                f"output {name!r} of action {action_id!r} targets"
                f" projection {target!r}, which the document no longer"
                " holds; the entire publication is rejected",
            )
        expected = PROJECTION_KIND_OF_SOURCE_KIND.get(kind)
        if expected is not None and projection.get("kind") != expected:
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"/actions/{action_id}/targets/{name}",
                f"output {name!r} of kind {kind!r} targets projection"
                f" {target!r} of kind {projection.get('kind')!r}; the"
                " target must be compatible with the output kind",
            )
    return None


# ---------------------------------------------------------------------------
# Result status (SPEC.md section 13.5)
# ---------------------------------------------------------------------------


def _captured_parameters(
    run: RunRecord, action: "Mapping | None"
) -> dict[str, dict]:
    """Return the captured parameters of one result with their IDs."""

    parameter_ids = {}
    if action is not None:
        parameter_ids = {
            name: mapping["parameter"]
            for name, mapping in action.get("parameters", {}).items()
        }
    return {
        name: {
            "parameter": parameter_ids.get(name, name),
            "value": captured.value,
            "stateRevision": captured.revision,
        }
        for name, captured in sorted(run.parameters.items())
    }


def _run_sort_key(run: RunRecord) -> tuple:
    return (run.generation, run.createdAt, run.runId)


def _failure_reason(run: RunRecord) -> str:
    if run.state == "cancelled":
        return "the newest request was cancelled"
    if run.state == "interrupted":
        return "the newest request was interrupted by a service restart"
    if run.state == "unknown":
        return "the newest request ended in an unknown state"
    return "the newest run failed"


def result_statuses(
    document: "Mapping | None",
    runs: Mapping[str, RunRecord],
    groups: Mapping[str, OutputGroupRecord],
    intents: Mapping[str, PendingIntentRecord],
    parameter_states: Mapping[str, ParameterStateValue],
    *,
    action_revision_of: Callable[[str], int],
    registry_entry_of: Callable[[str], object],
) -> dict[str, dict]:
    """Return the result status of every output group (section 13.5).

    The status order is fixed: ``pending`` while an active or
    approval-required intent or an in-flight run exists, then ``error``
    when the latest intent failed or was cancelled or interrupted, then
    ``empty`` when no result exists, then ``stale`` when the captured
    dependencies of the current result differ, otherwise ``current``.
    Every entry carries ``resultIsStale`` and the captured parameters of
    the retained result, so a host can show the parameter values that
    produced what is on screen when they differ from the current
    controls. Pending and error states retain an earlier result.
    """

    actions = (document or {}).get("actions", {})
    group_of: dict[str, list[RunRecord]] = {}
    for run in runs.values():
        group_of.setdefault(run.outputGroup, []).append(run)
    statuses: dict[str, dict] = {}
    for action_id, action in sorted(actions.items()):
        group_id = action.get("outputGroup")
        if group_id is None:
            continue
        group_runs = sorted(group_of.get(group_id, []), key=_run_sort_key)
        newest_run = group_runs[-1] if group_runs else None
        current_run = next(
            (
                run
                for run in reversed(group_runs)
                if run.disposition == "current"
            ),
            None,
        )
        group = groups.get(group_id)
        intent = intents.get(group_id)
        drift = (
            None
            if current_run is None
            else obsolescence_error(
                document=document,
                run=current_run,
                action=actions.get(current_run.actionId),
                registration_entry=registry_entry_of(current_run.registrationId),
                group=group,
                parameter_states=parameter_states,
                action_revision_of=action_revision_of,
            )
        )

        status = ""
        reason = ""
        if intent is not None and intent.status in ("pending", "approvalRequired"):
            status = "pending"
            reason = (
                "a debounce timer is running"
                if intent.status == "pending"
                else "an approval-required request waits for its host"
                " approval"
            )
        elif newest_run is not None and newest_run.state in ("queued", "running"):
            status = "pending"
            reason = "execution is in flight"
        elif (
            intent is not None
            and intent.status in ("expired", "interrupted")
            and (newest_run is None or newest_run.generation < intent.generation)
        ):
            status = "error"
            reason = (
                intent.reason
                if intent.reason
                else f"the newest request was {intent.status}"
            )
        elif newest_run is not None and newest_run.state in (
            "failed",
            "cancelled",
            "interrupted",
            "unknown",
        ):
            status = "error"
            reason = _failure_reason(newest_run)
        elif current_run is None:
            status = "empty"
        else:
            status = "stale" if drift is not None else "current"
            reason = "" if drift is None else drift.message

        entry: dict = {
            "action": action_id,
            "status": status,
            "resultIsStale": drift is not None,
            "generation": 0 if group is None else group.generation,
        }
        if reason:
            entry["reason"] = reason
        if current_run is not None:
            if drift is not None:
                entry["staleness"] = drift.message
            entry["result"] = {
                "runId": current_run.runId,
                "generation": current_run.generation,
                "outputs": {
                    name: {"sourceId": ref.sourceId, "version": ref.version}
                    for name, ref in sorted(current_run.outputs.items())
                },
                "capturedParameters": _captured_parameters(
                    current_run, actions.get(current_run.actionId)
                ),
            }
        if newest_run is not None:
            entry["latestRun"] = {
                "runId": newest_run.runId,
                "generation": newest_run.generation,
                "state": newest_run.state,
                "disposition": newest_run.disposition,
            }
        statuses[group_id] = entry
    return statuses


__all__ = [
    "CURRENT",
    "MEDIA_TYPE_BY_KIND",
    "NOT_PUBLISHABLE",
    "OUTPUT_SUFFIX_BY_KIND",
    "PUBLICATION_ID_PREFIX",
    "PROJECTION_KIND_OF_SOURCE_KIND",
    "PublicationOutcome",
    "RESULT_STATUSES",
    "REJECTED",
    "SUPERSEDED",
    "VerifiedOutput",
    "allocate_output_sources",
    "append_output_versions",
    "mint_publication_id",
    "obsolescence_error",
    "output_source_mapping",
    "result_statuses",
    "retarget_projections",
    "staged_output_path",
    "target_ineligibility",
    "verify_staged_outputs",
]
