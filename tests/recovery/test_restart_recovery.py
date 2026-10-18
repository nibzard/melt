"""Recovery fault injection (T022, SPEC.md sections 14 and 18.4).

The suite kills real services and damages real durable state, then
proves the recovery contract:

- Kills land on both sides of every acknowledgement boundary of
  section 18.4: before and after artifact finalization, before and
  after the database commit, and after the commit but before the
  acknowledgement returns. The killed service runs in a separate
  process (``service_child.py``), so one ``SIGKILL`` takes the owner
  lock, the storage worker, and the in-flight transaction down
  together. Acknowledged references always point to complete files,
  and an explicit retry reconciles the unacknowledged work that may
  exist.
- Disk-full during artifact finalization aborts the publication
  without committing anything; the previous table and plot remain
  together and a retry after the fault clears publishes.
- Missing artifacts are reported individually (section 14, startup
  step 4) while saved views stay readable; a missing environment
  disables new execution, not saved projections; lost notifications
  and changed service epochs answer ``snapshot-required``; reused
  request identifiers replay the stored response or fail with
  ``E_REQUEST_REUSE``.
- Corrupt descriptors are skipped and reported while the last valid
  snapshot preserves the previous readable interface; incompatible
  saved versions — an unsupported workspace format, an unknown record
  kind, an unsupported record schema version, an unknown snapshot
  language — refuse to open with every file preserved.
- Cancelled runs stay cancelled across a restart, a kill during
  execution never reruns uncertain Python effects, and startup follows
  the seven recovery steps of section 14, ending with explicit rerun
  and recovery offers.
"""

from __future__ import annotations

import errno
import json
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from melt.schema.diagnostics import DiagnosticCode
from runtime.storage import (
    PlatformAdapter,
    WorkspaceStore,
    storage_worker_available,
)
from runtime.storage.artifacts import ArtifactStore
from runtime.storage.errors import WorkspaceFormatError, WorkspaceOwnershipError
from runtime.storage.layout import DATABASE_NAME
from runtime.storage.worker import StorageWorker
from runtime.storage.workspace import HEADER_KIND, STORAGE_FORMAT_VERSION
from runtime.workspace import (
    RUN_KIND,
    SNAPSHOT_KIND,
    KnownEnvironment,
    WorkspaceService,
)
from runtime.workspace.service import LANGUAGE_VERSION

from service_child import (
    FILTERED_SOURCE,
    MONTHLY_SOURCE,
    SERVICE_CHILD_POINTS_ALL,
    FakeClock,
    envelope,
    invoke_action,
    run_at_minimum,
    seed_sales_workspace,
    set_minimum,
)

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"
SERVICE_CHILD = Path(__file__).resolve().parent / "service_child.py"
PARQUET = "application/vnd.apache.parquet"
CHILD_TIMEOUT_SECONDS = 240
LINE_TIMEOUT_SECONDS = 180.0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def reopen(adapter: PlatformAdapter, *, clock: "FakeClock | None" = None):
    """Reopen the workspace a killed or closed service left behind."""

    return WorkspaceService.open(adapter, WORKSPACE, clock=clock or FakeClock())


def artifact_names(service: WorkspaceService) -> list[str]:
    """Every immutable artifact file currently in storage."""

    return sorted(path.name for path in service.store.layout.artifacts.iterdir())


def staging_path(adapter: PlatformAdapter, run_id: str) -> Path:
    """The staging tree of one run under the workspace data root."""

    return adapter.workspace_directory(WORKSPACE) / "staging" / run_id


def assert_acknowledged_references_complete(service: WorkspaceService) -> None:
    """Every current projection reference points to a complete file.

    The invariant of SPEC.md section 18.4: a reference a commit
    acknowledged resolves to stored bytes that hash to the reference
    and carry the declared length.
    """

    document = service.document
    for projection_id in sorted(document["projections"]):
        reference = document["projections"][projection_id]["source"]
        if reference is None:
            continue
        version = document["sources"][reference["sourceId"]]["versions"][
            str(reference["version"])
        ]
        artifact = version["artifact"]
        assert service.store.artifacts.verify(artifact), (
            f"the acknowledged reference of {projection_id!r} does not"
            f" hash to its file: {artifact}"
        )
        assert len(service.store.artifacts.read(artifact)) == version["byteLength"]


def table_page(service: WorkspaceService, source_id: str, version: int = 1):
    """Read one bounded table page through the command surface."""

    return service.dispatch(
        envelope(
            "page-read",
            method="table.page",
            payload={
                "source": {"sourceId": source_id, "version": version},
                "sort": [],
                "filters": [],
                "offset": 0,
                "limit": 16,
            },
        )
    )


def poll_until(predicate, *, message: str, timeout: float = 90.0) -> None:
    """Wait until `predicate` holds, or fail with `message`."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {message}")


def _start_child(
    point: str,
    adapter: PlatformAdapter,
    *,
    side_effect: "Path | None" = None,
) -> subprocess.Popen:
    """Spawn the child service process for one kill scenario."""

    assert point in SERVICE_CHILD_POINTS_ALL
    command = [
        sys.executable,
        str(SERVICE_CHILD),
        "--root",
        str(adapter.application_data_root),
        "--point",
        point,
    ]
    if side_effect is not None:
        command += ["--side-effect", str(side_effect)]
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _read_child_line(process: subprocess.Popen) -> str:
    """Read the child's first stdout line under a deadline."""

    stdout = process.stdout
    readable, _, _ = select.select([stdout.fileno()], [], [], LINE_TIMEOUT_SECONDS)
    if not readable:
        process.kill()
        process.wait(timeout=30)
        pytest.fail("the child service never announced its armed point")
    return stdout.readline()


def kill_service_at(
    point: str, adapter: PlatformAdapter, *, side_effect: "Path | None" = None
) -> dict:
    """Run the child service until it kills itself at `point`.

    Returns the facts the child announced before the kill. The child
    must die from ``SIGKILL``: reaching the end of its scenario means
    the kill point never fired, which fails here.
    """

    process = _start_child(point, adapter, side_effect=side_effect)
    try:
        line = _read_child_line(process)
        if not line.strip():
            process.wait(timeout=60)
            pytest.fail(
                f"the child service exited before arming {point!r}:"
                f"\n{process.stderr.read()}"
            )
        header = json.loads(line)
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        stderr = process.stderr.read()
        assert process.returncode == -signal.SIGKILL, (
            f"the child at kill point {point!r} exited with"
            f" {process.returncode} instead of dying from SIGKILL:"
            f"\n{stderr}"
        )
        return header
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=30)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _raw_store(adapter: PlatformAdapter) -> WorkspaceStore:
    """Open the owning store directly, bypassing the service."""

    return WorkspaceStore.open(adapter, WORKSPACE)


def _raw_worker_records(adapter: PlatformAdapter) -> list[dict]:
    """Read every committed entry through a bare storage worker.

    Unlike the owning store, a bare worker negotiates nothing, so the
    file-preservation checks of the incompatible-version scenarios can
    count records even while the stored header refuses every store
    open.
    """

    worker = StorageWorker.start(
        adapter.workspace_directory(WORKSPACE) / DATABASE_NAME
    )
    try:
        entries: list[dict] = []
        cursor = None
        while True:
            batch, cursor = worker.scan_entries(cursor=cursor)
            entries.extend(batch)
            if cursor is None:
                return entries
    finally:
        worker.terminate()


def _commit_through_worker(adapter: PlatformAdapter, records: list) -> None:
    """Commit records through a bare worker, negotiating nothing."""

    worker = StorageWorker.start(
        adapter.workspace_directory(WORKSPACE) / DATABASE_NAME
    )
    try:
        worker.commit_entries(records)
    finally:
        worker.terminate()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Kills around the publication acknowledgement (SPEC.md section 18.4)
# ---------------------------------------------------------------------------


def test_kill_before_artifact_finalization_recovers_on_retry(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("publish-before-finalization", service_adapter)
    run_id = header["runId"]

    with reopen(service_adapter) as service:
        run = service.runs[run_id]
        assert run.state == "succeeded"
        assert run.disposition == "none"
        # Nothing was allocated and no reference moved.
        document = service.document
        assert document["projections"]["sales"]["source"] is None
        assert document["projections"]["revenue"]["source"] is None
        assert FILTERED_SOURCE not in document["sources"]
        # Storage still holds only the seeded import and script bytes.
        assert len(artifact_names(service)) == 2
        # The staged outputs survived: recovery kept them for a retry.
        assert staging_path(service_adapter, run_id).is_dir()
        report = service.recovery_report
        verdicts = {item["runId"]: item["action"] for item in report["staging"]}
        assert verdicts[run_id] == "kept-for-retry"
        assert any(
            item["kind"] == "retry-publication" and item["runId"] == run_id
            for item in report["offeredActions"]
        )

        # The retry reconciles the unacknowledged work.
        outcome = service.publish_run(run_id)
        assert outcome.outcome == "current"
        assert_acknowledged_references_complete(service)
        page = table_page(service, FILTERED_SOURCE)
        assert page.ok, page.errors
        assert len(page.result["rows"]) == 2, "minimum 200 keeps two rows"

    # A later restart removes the staging tree the retry concluded.
    with reopen(service_adapter) as service:
        assert not staging_path(service_adapter, run_id).exists()
        verdicts = {
            item["runId"]: item["action"]
            for item in service.recovery_report["staging"]
        }
        assert verdicts[run_id] == "removed-concluded"
        assert service.runs[run_id].disposition == "current"
        assert_acknowledged_references_complete(service)


def test_kill_between_artifact_writes_leaves_one_unreferenced_file(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("publish-mid-finalization", service_adapter)
    run_id = header["runId"]

    with reopen(service_adapter) as service:
        # Exactly one output finalized before the kill: an unreferenced
        # file, never a referenced missing one (SPEC.md section 13.4).
        assert len(artifact_names(service)) == 3
        assert service.runs[run_id].disposition == "none"
        assert service.document["projections"]["sales"]["source"] is None
        assert staging_path(service_adapter, run_id).is_dir()

        outcome = service.publish_run(run_id)
        assert outcome.outcome == "current"
        assert len(artifact_names(service)) == 4
        assert_acknowledged_references_complete(service)


def test_kill_after_artifact_finalization_before_commit_recovers_on_retry(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("publish-after-finalization", service_adapter)
    run_id = header["runId"]

    with reopen(service_adapter) as service:
        # Both immutable files finalized, but no transaction ever
        # acknowledged them: the document still knows nothing.
        assert len(artifact_names(service)) == 4
        assert service.runs[run_id].disposition == "none"
        assert service.publications == {}
        assert service.document["projections"]["sales"]["source"] is None
        assert staging_path(service_adapter, run_id).is_dir()

        # The retry reuses the finalized bytes and commits.
        outcome = service.publish_run(run_id)
        assert outcome.outcome == "current"
        assert_acknowledged_references_complete(service)
        (receipt,) = service.publications.values()
        assert receipt.runId == run_id
        assert receipt.outcome == "current"
        page = table_page(service, FILTERED_SOURCE)
        assert page.ok, page.errors


def test_kill_after_publication_commit_before_acknowledgement_replays(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("publish-after-commit", service_adapter)
    run_id = header["runId"]

    with reopen(service_adapter) as service:
        # The commit landed before the kill: the publication is durable
        # even though no caller was ever acknowledged.
        run = service.runs[run_id]
        assert run.disposition == "current"
        assert run.outputs["filtered"].sourceId == FILTERED_SOURCE
        (receipt,) = service.publications.values()
        assert receipt.runId == run_id
        assert service.document["projections"]["sales"]["source"] == {
            "sourceId": FILTERED_SOURCE,
            "version": 1,
        }
        assert_acknowledged_references_complete(service)
        # Startup reconciliation removed the concluded staging tree.
        assert not staging_path(service_adapter, run_id).exists()
        verdicts = {
            item["runId"]: item["action"]
            for item in service.recovery_report["staging"]
        }
        assert verdicts[run_id] == "removed-concluded"

        # The retry replays the stored outcome without a new commit.
        sequence = service.commit_sequence
        outcome = service.publish_run(run_id)
        assert outcome.outcome == "current"
        assert outcome.commit_sequence == sequence
        assert len(service.publications) == 1
        assert_acknowledged_references_complete(service)
        # The acknowledged result still pages.
        page = table_page(service, FILTERED_SOURCE)
        assert page.ok, page.errors


# ---------------------------------------------------------------------------
# Kills around a mutation acknowledgement (SPEC.md sections 12.5 and 18.4)
# ---------------------------------------------------------------------------


def test_kill_before_mutation_commit_leaves_the_change_unapplied(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("apply-before-commit", service_adapter)
    request_id = header["requestId"]

    with reopen(service_adapter) as service:
        # The transaction never committed: the parameter, its state
        # revision, and the request receipt are all absent.
        stored = service.parameter_state["minimum"]
        assert stored.value == 0
        assert stored.revision == 1
        assert service.receipt(request_id) is None

        # The retry applies the change the crash lost.
        response = set_minimum(service, request_id, value=200, expected_revision=1)
        assert response.ok, response.errors
        assert service.parameter_state["minimum"].value == 200
        assert service.parameter_state["minimum"].revision == 2
        assert service.receipt(request_id) is not None


def test_kill_after_mutation_commit_before_acknowledgement_replays(
    service_adapter: PlatformAdapter,
):
    header = kill_service_at("apply-after-commit", service_adapter)
    request_id = header["requestId"]

    with reopen(service_adapter) as service:
        # The commit landed: the stored value and the receipt exist.
        stored = service.parameter_state["minimum"]
        assert stored.value == 200
        assert stored.revision == 2
        receipt = service.receipt(request_id)
        assert receipt is not None
        acknowledged_sequence = receipt["commitSequence"]

        # A retry of the identical request replays the stored response
        # and commits nothing new (SPEC.md section 12.5).
        sequence = service.commit_sequence
        replay = set_minimum(service, request_id, value=200, expected_revision=1)
        assert replay.ok, replay.errors
        assert replay.commitSequence == acknowledged_sequence
        assert service.commit_sequence == sequence
        assert service.parameter_state["minimum"].revision == 2

        # Reuse of the identifier with different content is refused.
        reused = set_minimum(service, request_id, value=300, expected_revision=2)
        assert not reused.ok
        assert reused.errors[0].code == DiagnosticCode.REQUEST_REUSE
        assert service.parameter_state["minimum"].value == 200

        # A fresh identifier carries the next change.
        fresh = set_minimum(service, "21-set", value=300, expected_revision=2)
        assert fresh.ok, fresh.errors
        assert service.parameter_state["minimum"].revision == 3


# ---------------------------------------------------------------------------
# A kill during execution never reruns uncertain effects (section 18.4)
# ---------------------------------------------------------------------------


def test_kill_during_execution_never_reruns_uncertain_effects(
    service_adapter: PlatformAdapter,
):
    side_effect = service_adapter.application_data_root / "side-effect.log"
    process = _start_child(
        "run-during-execution", service_adapter, side_effect=side_effect
    )
    try:
        line = _read_child_line(process)
        header = json.loads(line)
        run_id = header["runId"]
        # Wait until the project process is genuinely mid-execution:
        # its external effect line exists and its trailing sleep still
        # holds it open. Then kill the service alone; the owned runner
        # keeps running, the way an orphaned child does.
        poll_until(
            lambda: side_effect.is_file()
            and side_effect.read_text(encoding="utf-8") == "ran\n",
            message="the project process to reach mid-execution",
        )
        process.kill()
        process.wait(timeout=60)
        assert process.returncode == -signal.SIGKILL
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    # The orphaned runner finishes its external effect on its own.
    finished_index = staging_path(service_adapter, run_id) / "staged-outputs.json"
    poll_until(
        lambda: finished_index.is_file(),
        message="the orphaned project process to finish",
    )
    time.sleep(1.0)
    assert side_effect.read_text(encoding="utf-8") == "ran\n"

    clock = FakeClock()
    with reopen(service_adapter, clock=clock) as service:
        run = service.runs[run_id]
        assert run.state == "interrupted"
        assert run.disposition == "none"
        # Startup removed the abandoned staging tree and reported the
        # interrupted run (SPEC.md section 14, steps 5 and 6).
        assert not staging_path(service_adapter, run_id).exists()
        report = service.recovery_report
        assert list(report["interruptedRuns"]) == [run_id]
        verdicts = {item["runId"]: item["action"] for item in report["staging"]}
        assert verdicts[run_id] == "removed-abandoned"

        # Recovery executes nothing: firing every timer runs no Python.
        assert service.dispatch_due_intents(clock.advance(10_000)) == []
        assert side_effect.read_text(encoding="utf-8") == "ran\n"

        # The interrupted run can never be relaunched.
        refusal = service.launch_admitted_run(run_id)
        assert not refusal.launched
        assert refusal.refusal[0].code == DiagnosticCode.CONFLICT
        assert side_effect.read_text(encoding="utf-8") == "ran\n"

        # An explicit user rerun is the only way forward, and it runs
        # exactly once more.
        second = run_at_minimum(service, clock, 300, "06-set", expected_revision=2)
        assert side_effect.read_text(encoding="utf-8") == "ran\nran\n"
        assert service.publish_run(second).outcome == "current"
        assert_acknowledged_references_complete(service)


# ---------------------------------------------------------------------------
# Disk-full during finalization (SPEC.md section 18.4)
# ---------------------------------------------------------------------------


def test_disk_full_during_finalization_preserves_the_previous_pair(
    service_adapter: PlatformAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    first = run_at_minimum(service, clock, 200, "05-set")
    assert service.publish_run(first).outcome == "current"
    document_before = json.dumps(service.document, sort_keys=True)
    second = run_at_minimum(service, clock, 300, "06-set", expected_revision=2)

    original = ArtifactStore.write
    writes = {"count": 0}

    def failing_write(self, data):
        writes["count"] += 1
        if writes["count"] > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(self, data)

    monkeypatch.setattr(ArtifactStore, "write", failing_write)
    outcome = service.publish_run(second)
    monkeypatch.undo()
    assert outcome.outcome == "not-publishable"
    assert outcome.refusal[0].code == DiagnosticCode.ARTIFACT
    assert "No space left on device" in outcome.refusal[0].message
    # Nothing committed: the previous table and plot remain together
    # and the run stays eligible for a retry.
    assert json.dumps(service.document, sort_keys=True) == document_before
    assert service.runs[second].disposition == "none"
    assert len(service.publications) == 1
    assert_acknowledged_references_complete(service)

    # The fault clears and the retry reconciles the same staged outputs.
    outcome = service.publish_run(second)
    assert outcome.outcome == "current"
    assert service.document["sources"][FILTERED_SOURCE]["currentVersion"] == 2
    assert_acknowledged_references_complete(service)


# ---------------------------------------------------------------------------
# Missing artifacts and missing environments (SPEC.md sections 14 and 18.4)
# ---------------------------------------------------------------------------


def test_missing_artifacts_are_reported_individually(
    service_adapter: PlatformAdapter,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    run_id = run_at_minimum(service, clock, 200, "05-set")
    assert service.publish_run(run_id).outcome == "current"
    document = json.loads(json.dumps(service.document))
    filtered_artifact = document["sources"][FILTERED_SOURCE]["versions"]["1"][
        "artifact"
    ]
    monthly_artifact = document["sources"][MONTHLY_SOURCE]["versions"]["1"][
        "artifact"
    ]
    service.close()

    # One artifact file vanishes; a second is truncated short of its
    # declared length. Stored artifacts are read-only, so the damage
    # lifts the mode first, the way a real disk fault would.
    artifacts = service_adapter.workspace_directory(WORKSPACE) / "artifacts"
    (artifacts / monthly_artifact[len("sha256:") :]).unlink()
    filtered_file = artifacts / filtered_artifact[len("sha256:") :]
    filtered_file.chmod(0o644)
    filtered_file.write_bytes(filtered_file.read_bytes()[:3])

    with reopen(service_adapter) as reopened:
        report = reopened.recovery_report
        missing = {
            item["path"]: item
            for item in report["missingArtifacts"]
        }
        assert set(missing) == {
            f"/sources/{FILTERED_SOURCE}/versions/1/artifact",
            f"/sources/{MONTHLY_SOURCE}/versions/1/artifact",
        }
        for item in missing.values():
            assert item["code"] == "E_ARTIFACT"
        assert monthly_artifact in missing[
            f"/sources/{MONTHLY_SOURCE}/versions/1/artifact"
        ]["message"], "the diagnostic names the missing artifact itself"
        # The saved definitions survive: the document still lists both
        # sources and their references (SPEC.md section 14 step 4).
        assert reopened.document == document
        inspected = reopened.dispatch(
            envelope("06-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        diagnostics = inspected.result["diagnostics"]
        assert sorted(item["path"] for item in diagnostics) == sorted(missing)
        assert inspected.result["recovery"]["missingArtifacts"] == report[
            "missingArtifacts"
        ]
        # A damaged artifact never blocks the whole workspace: the
        # reader reports the unreadable source by name.
        refused = table_page(reopened, FILTERED_SOURCE)
        assert not refused.ok


def test_missing_environment_disables_new_work_not_saved_views(
    service_adapter: PlatformAdapter,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    run_id = run_at_minimum(service, clock, 200, "05-set")
    assert service.publish_run(run_id).outcome == "current"
    service.close()

    drifted = KnownEnvironment(
        environment_id="python-local",
        fingerprint="cpython-0.0.0-drifted-linux",
        description="An interpreter upgrade the registration never saw",
    )
    monkeypatch.setattr(
        "runtime.workspace.service.local_environment", lambda: drifted
    )
    with reopen(service_adapter) as service:
        # Saved views stay readable and the published result survives.
        inspected = service.dispatch(
            envelope("06-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["diagnostics"] == []
        assert (
            inspected.result["outputGroups"]["sales-results"]["status"] == "current"
        )
        assert_acknowledged_references_complete(service)

        # The environment the registration resolved is gone: new
        # registrations and launches refuse, and nothing executes
        # (SPEC.md sections 13.1 and 14).
        token = service.capture_artifact_token(
            b"import pass\n", "text/x-python"
        ).token
        unknown = service.dispatch(
            envelope(
                "07-register",
                method="action.register",
                payload={
                    "scriptToken": token,
                    "environmentId": "python-vanished",
                    "contract": {
                        "parameters": {},
                        "inputs": {},
                        "outputs": {"filtered": "table"},
                    },
                },
            )
        )
        assert not unknown.ok
        assert unknown.errors[0].code == DiagnosticCode.REFERENCE

        response = invoke_action(service, "08-invoke")
        assert response.ok, response.errors
        run_id = response.result["run"]["runId"]
        refusal = service.launch_admitted_run(run_id)
        assert not refusal.launched
        assert refusal.refusal[0].code == DiagnosticCode.REGISTRATION
        assert "environment repair" in refusal.refusal[0].message
        assert not staging_path(service_adapter, run_id).exists()
        assert service.runs[run_id].state == "queued"

    # The environment returns: repair is a separate operation, not a
    # lost workspace. The refused run was interrupted by the restart
    # in between, so an explicit new invocation is the way forward.
    monkeypatch.undo()
    with reopen(service_adapter) as service:
        assert service.runs[run_id].state == "interrupted"
        response = invoke_action(service, "09-invoke")
        assert response.ok, response.errors
        repaired = response.result["run"]["runId"]
        outcome = service.launch_admitted_run(repaired)
        assert outcome.launched, outcome.refusal
        assert outcome.state == "succeeded"
        assert service.publish_run(repaired).outcome == "current"
        assert_acknowledged_references_complete(service)


# ---------------------------------------------------------------------------
# Lost notifications and changed epochs (SPEC.md sections 12.5 and 18.4)
# ---------------------------------------------------------------------------


def _subscribe(service: WorkspaceService, after_sequence: int, epoch: int):
    return service.dispatch(
        envelope(
            "subscribe-read",
            method="workspace.subscribe",
            payload={"afterSequence": after_sequence, "epoch": epoch},
        )
    )


def _foreign_artifact_record() -> tuple[str, dict]:
    """One valid artifact record a rogue writer committed."""

    return (
        "melt.artifact",
        {
            "recordType": "artifact",
            "schemaVersion": 1,
            "artifact": "sha256:" + "5f" * 32,
            "kind": "table",
            "mediaType": PARQUET,
            "byteLength": 5,
            "schema": {
                "columns": [
                    {"id": "month", "type": "string", "nullable": False}
                ],
                "rowKey": None,
            },
            "createdAt": _utc_now(),
        },
    )


def test_lost_notifications_require_a_snapshot(service_adapter: PlatformAdapter):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    before = service.commit_sequence
    service.close()

    # A rogue writer commits a record without its notification: the
    # notification log loses the entry for that sequence.
    with _raw_store(service_adapter) as store:
        sequence, _ = store.append_records([_foreign_artifact_record()])
    assert sequence == before + 1

    with reopen(service_adapter) as service:
        # A further real commit follows the hole.
        response = set_minimum(service, "06-set", value=300, expected_revision=1)
        assert response.ok, response.errors
        after = service.commit_sequence
        window = _subscribe(service, before, service.epoch)
        assert window.ok, window.errors
        assert window.result["status"] == "snapshot-required"
        assert str(before + 1) in window.result["reason"]
        assert "notification" in window.result["reason"]
        # Replay after the gap still answers ordered changes.
        caught_up = _subscribe(service, after, service.epoch)
        assert caught_up.ok, caught_up.errors
        assert caught_up.result["status"] == "changes"
        # The host falls back to a full snapshot, which stays readable.
        inspected = service.dispatch(
            envelope("07-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["parameterStates"]["minimum"]["value"] == 300


def test_changed_service_epoch_requires_a_snapshot(service_adapter: PlatformAdapter):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    subscribed_epoch = service.epoch
    service.close()

    # A replacement service rotates the epoch in a new header record.
    with _raw_store(service_adapter) as store:
        store.worker.commit_entries(
            [
                (
                    HEADER_KIND,
                    {
                        "formatVersion": STORAGE_FORMAT_VERSION,
                        "workspaceId": WORKSPACE,
                        "epoch": subscribed_epoch + 7,
                        "createdAt": _utc_now(),
                        "adapter": "pi-durable",
                        "adapterVersion": "1.3.0",
                    },
                )
            ]
        )

    with reopen(service_adapter) as service:
        assert service.epoch == subscribed_epoch + 7
        window = _subscribe(service, 0, subscribed_epoch)
        assert window.ok, window.errors
        assert window.result["status"] == "snapshot-required"
        assert "epoch" in window.result["reason"]
        # The rotated service replays its ordered history instead: the
        # notification log starts after the header-only first commit,
        # so a subscriber anchored there catches up cleanly.
        first_notification = service.notifications[0]["commitSequence"]
        fresh = _subscribe(service, first_notification - 1, service.epoch)
        assert fresh.ok, fresh.errors
        assert fresh.result["status"] == "changes"
        assert fresh.result["changes"], "the rotated service kept its history"


# ---------------------------------------------------------------------------
# Corrupt descriptors and incompatible saved versions (section 18.4)
# ---------------------------------------------------------------------------


def test_corrupt_descriptors_skip_and_keep_the_previous_readable_interface(
    service_adapter: PlatformAdapter,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    response = invoke_action(service, "05-invoke")
    assert response.ok, response.errors
    run_id = response.result["run"]["runId"]
    assert set_minimum(service, "06-set", value=300, expected_revision=1).ok
    before = service.commit_sequence
    service.close()

    # Two damaged records: a run record stripped of its fields and a
    # snapshot record with no document at all.
    with _raw_store(service_adapter) as store:
        store.append_records(
            [
                (
                    RUN_KIND,
                    {"recordType": "run", "schemaVersion": 1, "runId": "broken"},
                ),
                (
                    SNAPSHOT_KIND,
                    {
                        "recordType": "snapshot",
                        "schemaVersion": 1,
                        "workspaceId": WORKSPACE,
                        "language": LANGUAGE_VERSION,
                        "epoch": 1,
                        "commitSequence": 0,
                    },
                ),
            ]
        )

    with reopen(service_adapter) as reopened:
        # The damage is reported, not hidden.
        corrupt = reopened.recovery_report["corruptRecords"]
        assert [item["kind"] for item in corrupt] == [
            "melt.run",
            "melt.workspaceSnapshot",
        ]
        assert all(item["code"] == "E_SCHEMA" for item in corrupt)
        assert all(item["entryId"] > 0 for item in corrupt)
        # The previous readable interface survived: the last valid
        # snapshot still restores its values (SPEC.md section 18.4).
        stored = reopened.parameter_state["minimum"]
        assert stored.value == 300
        assert stored.revision == 2
        # The queued run the restart marked interrupted stays known.
        assert reopened.runs[run_id].state == "interrupted"
        inspected = reopened.dispatch(
            envelope("07-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["diagnostics"] == []
        # The damaged sequences have no notifications, so ordered
        # replay after the last good sequence demands a snapshot.
        window = _subscribe(reopened, before, reopened.epoch)
        assert window.ok, window.errors
        assert window.result["status"] == "snapshot-required"
        # The workspace still accepts new work.
        moved = set_minimum(reopened, "08-set", value=400, expected_revision=2)
        assert moved.ok, moved.errors
        assert reopened.parameter_state["minimum"].revision == 3


def test_incompatible_saved_versions_refuse_to_open_and_preserve_files(
    service_adapter: PlatformAdapter,
):
    workspace_dir = service_adapter.workspace_directory(WORKSPACE)

    def reseed() -> None:
        """Start over from a fresh, healthy workspace."""

        shutil.rmtree(workspace_dir, ignore_errors=True)
        seeded = seed_sales_workspace(service_adapter)
        seeded.service.close()

    def refuse_and_preserve(fragment: str) -> None:
        before = len(_raw_worker_records(service_adapter))
        with pytest.raises(WorkspaceFormatError) as refusal:
            reopen(service_adapter)
        assert refusal.value.diagnostics[0].code == DiagnosticCode.VERSION
        assert fragment in refusal.value.diagnostics[0].message
        assert len(_raw_worker_records(service_adapter)) == before, (
            "the refusal preserved every committed record"
        )

    # An unsupported workspace storage format refuses and never
    # migrates (SPEC.md section 14).
    reseed()
    _commit_through_worker(
        service_adapter,
        [
            (
                HEADER_KIND,
                {
                    "formatVersion": STORAGE_FORMAT_VERSION + 1,
                    "workspaceId": WORKSPACE,
                    "epoch": 5,
                    "createdAt": _utc_now(),
                },
            )
        ],
    )
    refuse_and_preserve("format")
    with pytest.raises(WorkspaceFormatError):
        reopen(service_adapter)  # still refuses on a second attempt

    # A record stored by a newer service generation.
    reseed()
    _commit_through_worker(
        service_adapter,
        [
            (
                RUN_KIND,
                {"recordType": "run", "schemaVersion": 99, "runId": "future"},
            )
        ],
    )
    refuse_and_preserve("99")

    # A record kind this service has never heard of: silently dropping
    # it would lose stored data, so the open refuses instead.
    reseed()
    _commit_through_worker(
        service_adapter,
        [("melt.prefetch", {"recordType": "prefetch", "schemaVersion": 1})],
    )
    refuse_and_preserve("melt.prefetch")

    # A snapshot stored by a newer language version of Melt.
    reseed()
    _commit_through_worker(
        service_adapter,
        [
            (
                SNAPSHOT_KIND,
                {
                    "recordType": "snapshot",
                    "schemaVersion": 1,
                    "workspaceId": WORKSPACE,
                    "language": "melt/v0.2",
                },
            )
        ],
    )
    refuse_and_preserve("melt/v0.2")


# ---------------------------------------------------------------------------
# Cancelled runs across a restart (SPEC.md sections 13.3 and 18.4)
# ---------------------------------------------------------------------------


def test_cancelled_runs_stay_cancelled_across_a_restart(
    service_adapter: PlatformAdapter,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    response = invoke_action(service, "05-invoke")
    run_id = response.result["run"]["runId"]
    cancelled = service.dispatch(
        envelope("06-cancel", method="run.cancel", payload={"runId": run_id})
    )
    assert cancelled.ok, cancelled.errors
    service.close()

    with reopen(service_adapter) as service:
        assert service.runs[run_id].state == "cancelled"
        # Recovery did not resurrect the run into a queued state.
        assert service.queued_run_ids() == []
        refused = service.publish_run(run_id)
        assert refused.outcome == "not-publishable"
        assert service.runs[run_id].disposition == "none"
        inspected = service.dispatch(
            envelope("07-inspect", method="workspace.inspect")
        )
        status = inspected.result["outputGroups"]["sales-results"]
        assert status["status"] == "error"
        assert status["reason"] == "the newest request was cancelled"
        # An explicit new invocation works after the restart.
        response = invoke_action(service, "08-invoke")
        assert response.ok, response.errors
        again = response.result["run"]["runId"]
        outcome = service.launch_admitted_run(again)
        assert outcome.launched, outcome.refusal
        assert service.publish_run(again).outcome == "current"
        assert_acknowledged_references_complete(service)


# ---------------------------------------------------------------------------
# The seven startup steps of SPEC.md section 14
# ---------------------------------------------------------------------------


def test_startup_follows_the_seven_recovery_steps(
    service_adapter: PlatformAdapter,
):
    clock = FakeClock()
    seeded = seed_sales_workspace(service_adapter, clock=clock)
    service = seeded.service
    # A succeeded run whose publication never committed, a pending
    # intent that never dispatched, an orphan staging tree, and a
    # missing input artifact: one workspace holding every recovery
    # condition at once.
    run_id = run_at_minimum(service, clock, 200, "05-set")
    assert set_minimum(service, "06-set", value=300, expected_revision=2).ok
    orphan = staging_path(service_adapter, "run-000000-no-such-record")
    orphan.mkdir(parents=True)
    (orphan / "outputs").mkdir()
    (orphan / "outputs" / "filtered.parquet").write_bytes(b"junk")
    document = json.loads(json.dumps(service.document))
    layout = document["layout"]
    import_reference = document["sources"]["sales-input"]["versions"]["1"][
        "artifact"
    ]
    # An operator keeps a copy, the way the diagnostic hint suggests.
    backup = service.store.artifacts.read(import_reference)
    import_file = (
        service_adapter.workspace_directory(WORKSPACE)
        / "artifacts"
        / import_reference[len("sha256:") :]
    )
    service.close()
    import_file.unlink()

    with reopen(service_adapter) as service:
        # Step 1: the operating-system lock fences every overlapping
        # writer, so a second owner is refused outright.
        with pytest.raises(WorkspaceOwnershipError) as ownership:
            reopen(service_adapter)
        assert ownership.value.diagnostics[0].code == DiagnosticCode.CONFLICT

        report = service.recovery_report
        # Steps 2 and 3: the last committed definitions and state load
        # and validate without executing anything.
        assert service.document == document
        assert service.parameter_state["minimum"].value == 300
        assert service.parameter_state["minimum"].revision == 3
        assert service.document["layout"] == layout
        assert report["corruptRecords"] == []
        # Step 4: the missing artifact is reported individually.
        (missing,) = report["missingArtifacts"]
        assert missing["code"] == "E_ARTIFACT"
        assert missing["path"] == "/sources/sales-input/versions/1/artifact"
        assert import_reference in missing["message"]
        # Step 5: the pending timer is interrupted, never executed.
        assert list(report["interruptedIntents"]) == [
            service.pending_intents["sales-results"].intentRef
        ]
        assert service.pending_intents["sales-results"].status == "interrupted"
        # Step 6: the orphan tree is removed, the retryable one kept.
        verdicts = {item["runId"]: item["action"] for item in report["staging"]}
        assert verdicts[run_id] == "kept-for-retry"
        assert verdicts["run-000000-no-such-record"] == "removed-unknown"
        assert not orphan.exists()
        assert staging_path(service_adapter, run_id).is_dir()
        # Step 7: the explicit rerun and recovery offers are on record.
        kinds = sorted(item["kind"] for item in report["offeredActions"])
        assert kinds == ["rerun-action", "retry-publication"]
        offered_runs = [
            item["runId"]
            for item in report["offeredActions"]
            if item["kind"] == "retry-publication"
        ]
        assert offered_runs == [run_id]

        # The offers ride the inspection surface a host renders from.
        inspected = service.dispatch(
            envelope("07-inspect", method="workspace.inspect")
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["recovery"] == report
        assert [item["path"] for item in inspected.result["diagnostics"]] == [
            missing["path"]
        ]

        # The offered publication retry reconciles the staged outputs.
        # The run captured revision 2 while the world moved to 3, so
        # it archives as superseded history, exactly as eligibility
        # demands (SPEC.md section 13.3).
        outcome = service.publish_run(run_id)
        assert outcome.outcome == "superseded"
        assert service.runs[run_id].disposition == "superseded"
        assert service.document["projections"]["sales"]["source"] is None
        assert_acknowledged_references_complete(service)

        # The operator restores the artifact and takes the rerun offer:
        # a fresh invocation becomes the current result.
        import_file.write_bytes(backup)
        second = run_at_minimum(service, clock, 400, "08-set", expected_revision=3)
        assert service.publish_run(second).outcome == "current"
        assert service.document["sources"][FILTERED_SOURCE]["currentVersion"] == 2
        assert_acknowledged_references_complete(service)
