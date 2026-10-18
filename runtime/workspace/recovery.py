"""Startup recovery (SPEC.md section 14, backlog task T022).

Opening a workspace follows the seven startup steps of section 14. The
storage layer owns steps 1 and 2 (ownership and version negotiation in
:meth:`runtime.storage.WorkspaceStore.open`, state loading in the
service loader), and this module carries the pieces the recovery pass
adds on top of the restored records:

- Step 3, validate saved data: a committed record that no longer parses
  is a corrupt descriptor. The loader skips it, keeps the last snapshot
  that still validates, and reports the entry here, so the previous
  readable interface survives a damaged tail of the record stream.
- Step 4, report missing artifacts individually: every source version
  and every captured registration script must still resolve to a
  complete file under ``artifacts/``. Each missing or truncated file
  produces its own ``E_ARTIFACT`` diagnostic; saved projections stay
  readable and nothing blocks on the damage.
- Step 5, mark previous active jobs interrupted: the service marks
  queued or running runs and timer-running intents ``interrupted``
  durably; this module only records what the marking touched.
- Step 6, reconcile staging files and publication receipts: every
  ``staging/<run-id>`` tree is matched against its run record and the
  committed publication receipts. A concluded or unpublishable run
  leaves its staging behind as abandoned (SPEC.md section 5.2 permits
  removing abandoned staging files after recovery); a run that
  succeeded while its publication transaction never committed keeps its
  staged outputs, because an explicit retry must be able to reconcile
  them (section 18.4).
- Step 7, offer explicit rerun or recovery actions: the report lists
  the interrupted intents a user may invoke again and the
  succeeded-but-unpublished runs whose publication a caller may retry.
  Recovery never reruns uncertain Python effects on its own.

Nothing in this module executes project code, and nothing it does can
turn an unreferenced file into a referenced missing one: staging trees
are removed only when no receipt and no retry path can ever claim them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from melt.schema.canonical import model_to_wire
from melt.schema.diagnostics import Diagnostic, DiagnosticCode
from melt.schema.records import (
    PendingIntentRecord,
    PublicationRecord,
    RegistrationRecord,
    RunRecord,
)
from runtime.storage.artifacts import ArtifactStore
from runtime.storage.layout import WorkspaceLayout

STAGING_REMOVED_CONCLUDED = "removed-concluded"
"""The run holds a committed publication outcome; its staging is spent."""

STAGING_REMOVED_ABANDONED = "removed-abandoned"
"""The run ended in a state that can never publish; its staging is junk."""

STAGING_REMOVED_UNKNOWN = "removed-unknown"
"""No committed run record matches the directory; nothing can claim it."""

STAGING_KEPT_FOR_RETRY = "kept-for-retry"
"""The run succeeded unpublished; an explicit retry may still claim it."""

ABANDONED_RUN_STATES = frozenset(
    {"queued", "running", "failed", "cancelled", "interrupted"}
)
"""Run states whose staged outputs can never become a publication."""


@dataclass(frozen=True)
class CorruptRecordNote:
    """One committed record the loader could not validate.

    ``entry_id`` is the storage entry identifier, so an operator can
    find the damaged record in the database. The record was skipped,
    never rewritten: recovery preserves files (SPEC.md section 14).
    """

    entry_id: int
    kind: str
    code: str
    message: str

    def wire(self) -> dict:
        return {
            "entryId": self.entry_id,
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class StagingVerdict:
    """What startup reconciliation decided about one staging tree."""

    run_id: str
    action: str
    reason: str

    def wire(self) -> dict:
        return {"runId": self.run_id, "action": self.action, "reason": self.reason}


@dataclass(frozen=True)
class RecoveryReport:
    """What the startup recovery pass found and did (SPEC.md section 14).

    ``missing_artifacts`` and ``corrupt_records`` describe damage the
    pass reports but never repairs. ``staging`` describes the trees the
    pass removed or kept. ``offered_actions`` names the explicit
    actions a host can offer next; recovery performs none of them
    itself.
    """

    corrupt_records: tuple[CorruptRecordNote, ...] = ()
    missing_artifacts: tuple[Diagnostic, ...] = ()
    interrupted_runs: tuple[str, ...] = ()
    interrupted_intents: tuple[str, ...] = ()
    staging: tuple[StagingVerdict, ...] = ()
    offered_actions: tuple[dict, ...] = field(default_factory=tuple)

    def wire(self) -> dict:
        return {
            "corruptRecords": [note.wire() for note in self.corrupt_records],
            "missingArtifacts": [
                model_to_wire(item) for item in self.missing_artifacts
            ],
            "interruptedRuns": list(self.interrupted_runs),
            "interruptedIntents": list(self.interrupted_intents),
            "staging": [verdict.wire() for verdict in self.staging],
            "offeredActions": [dict(item) for item in self.offered_actions],
        }


def empty_report() -> RecoveryReport:
    """The report of a startup that found nothing to recover."""

    return RecoveryReport()


def _missing_artifact(
    artifacts: ArtifactStore,
    artifact: "str | None",
    declared_length: "int | None",
    path: str,
    owner: str,
) -> "Diagnostic | None":
    """Return the diagnostic for one unreadable artifact, or None."""

    if not isinstance(artifact, str):
        return Diagnostic(
            code=DiagnosticCode.ARTIFACT,
            path=path,
            message=f"the artifact reference of {owner} is not a string",
            hint="Restore the artifact file or re-import the source"
            " (SPEC.md section 14, startup step 4).",
        )
    target = artifacts.path_for(artifact)
    if not target.is_file():
        return Diagnostic(
            code=DiagnosticCode.ARTIFACT,
            path=path,
            message=f"the artifact of {owner} is missing from storage:"
            f" {artifact}",
            hint="Restore the artifact file or re-import the source;"
            " saved projections stay readable (SPEC.md section 14,"
            " startup step 4).",
        )
    if declared_length is not None:
        stored = target.stat().st_size
        if stored != declared_length:
            return Diagnostic(
                code=DiagnosticCode.ARTIFACT,
                path=path,
                message=f"the artifact of {owner} stores {stored} bytes"
                f" but its version declares {declared_length}:"
                f" {artifact}",
                hint="Restore the complete artifact file; a truncated"
                " artifact never satisfies its reference (SPEC.md"
                " sections 5.1 and 14).",
            )
    return None


def missing_artifact_diagnostics(
    document: "Mapping | None",
    artifacts: ArtifactStore,
    registrations: "Mapping[str, RegistrationRecord] | None" = None,
) -> list[Diagnostic]:
    """Report every artifact a saved reference names but storage lacks.

    Each damaged reference produces its own diagnostic, so a host can
    report the damage individually while the remaining projections
    stay readable (SPEC.md section 14, startup step 4). Sources come
    from the committed document; scripts come from the committed
    registration records.
    """

    notes: list[Diagnostic] = []
    sources = (document or {}).get("sources", {})
    for source_id, source in sorted(sources.items()):
        versions = source.get("versions", {})
        for version_key in sorted(versions, key=lambda item: int(item)):
            version = versions[version_key]
            note = _missing_artifact(
                artifacts,
                version.get("artifact"),
                version.get("byteLength"),
                f"/sources/{source_id}/versions/{version_key}/artifact",
                f"source {source_id!r} version {version_key}",
            )
            if note is not None:
                notes.append(note)
    for registration_id, record in sorted((registrations or {}).items()):
        note = _missing_artifact(
            artifacts,
            record.script.artifact,
            record.script.byteLength,
            f"/registrations/{registration_id}/script/artifact",
            f"registration {registration_id!r}",
        )
        if note is not None:
            notes.append(note)
    return sorted(
        notes, key=lambda item: (item.path, str(item.code))
    )


def reconcile_staging(
    layout: WorkspaceLayout,
    runs: Mapping[str, RunRecord],
    publications: Mapping[str, PublicationRecord],
) -> list[StagingVerdict]:
    """Match every staging tree against receipts and run records.

    Walks ``staging/<run-id>`` after restart (SPEC.md section 14,
    startup step 6) and removes the trees no path can ever claim again:

    - a run whose publication concluded (a receipt or a disposition
      exists) spent its staging when the transaction committed;
    - a run in an unpublishable state (``failed``, ``cancelled``,
      ``interrupted``, or still active) can never validate its staged
      outputs, so the tree is abandoned (SPEC.md section 5.2);
    - a directory no committed run record names is an orphan.

    A succeeded run whose publication never committed keeps its staged
    outputs: an explicit retry must reconcile that unacknowledged work
    (SPEC.md section 18.4). Removal is idempotent and a missing file
    inside a removed tree never fails the pass.
    """

    staging = Path(layout.staging)
    if not staging.is_dir():
        return []
    concluded = {publication.runId for publication in publications.values()}
    verdicts: list[StagingVerdict] = []
    for entry in sorted(staging.iterdir()):
        if not entry.is_dir():
            # A stray file is not a run staging tree; leave it for an
            # operator, because recovery cannot name its owner.
            continue
        run_id = entry.name
        run = runs.get(run_id)
        if run is None:
            _remove_tree(entry)
            verdicts.append(
                StagingVerdict(
                    run_id,
                    STAGING_REMOVED_UNKNOWN,
                    "no committed run record matches the staging"
                    " directory",
                )
            )
        elif run.disposition != "none" or run_id in concluded:
            _remove_tree(entry)
            verdicts.append(
                StagingVerdict(
                    run_id,
                    STAGING_REMOVED_CONCLUDED,
                    "a committed publication receipt concluded the run",
                )
            )
        elif run.state == "succeeded":
            verdicts.append(
                StagingVerdict(
                    run_id,
                    STAGING_KEPT_FOR_RETRY,
                    "the run succeeded while its publication never"
                    " committed; an explicit retry may reconcile it",
                )
            )
        else:
            _remove_tree(entry)
            verdicts.append(
                StagingVerdict(
                    run_id,
                    STAGING_REMOVED_ABANDONED,
                    f"the run is {run.state!r}, so its staged outputs"
                    " can never publish",
                )
            )
    return verdicts


def recovery_offers(
    interrupted_intents: Sequence[PendingIntentRecord],
    verdicts: Sequence[StagingVerdict],
    runs: Mapping[str, RunRecord],
) -> list[dict]:
    """Name the explicit rerun and recovery actions a host may offer.

    Startup step 7 offers actions; it never performs them. An
    interrupted intent is rerun by a fresh invocation of its action,
    never by resuming the interrupted request (SPEC.md sections 9.3 and
    18.4). Kept staged outputs are reconciled by retrying the
    publication of the run that staged them.
    """

    offers: list[dict] = []
    for intent in sorted(interrupted_intents, key=lambda item: item.intentRef):
        offers.append(
            {
                "kind": "rerun-action",
                "action": intent.actionId,
                "outputGroup": intent.outputGroup,
                "intentRef": intent.intentRef,
                "generation": intent.generation,
            }
        )
    for verdict in sorted(verdicts, key=lambda item: item.run_id):
        if verdict.action != STAGING_KEPT_FOR_RETRY:
            continue
        run = runs.get(verdict.run_id)
        if run is None:  # pragma: no cover - kept implies a run record
            continue
        offers.append(
            {
                "kind": "retry-publication",
                "runId": run.runId,
                "action": run.actionId,
            }
        )
    return offers


def _remove_tree(path: Path) -> None:
    """Remove one staging tree, tolerating a concurrent removal."""

    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "ABANDONED_RUN_STATES",
    "CorruptRecordNote",
    "RecoveryReport",
    "STAGING_KEPT_FOR_RETRY",
    "STAGING_REMOVED_ABANDONED",
    "STAGING_REMOVED_CONCLUDED",
    "STAGING_REMOVED_UNKNOWN",
    "StagingVerdict",
    "empty_report",
    "missing_artifact_diagnostics",
    "reconcile_staging",
    "recovery_offers",
]
