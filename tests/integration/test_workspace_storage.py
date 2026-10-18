"""Workspace storage and ownership (SPEC.md section 14, backlog task T008).

The tests prove the directory layout of section 14, the platform
adapter that resolves the data root, single-writer ownership through an
operating-system lock, the one owning connection per workspace
`state.sqlite`, identifier minting on that connection, and artifact
bytes that stay outside the database.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's real data directory.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from melt.schema.commands import MeltCommandError
from runtime.storage import (
    ArtifactStore,
    OwnerLock,
    PlatformAdapter,
    StorageWorker,
    StorageWorkerError,
    WorkspaceFormatError,
    WorkspaceLayout,
    WorkspaceOwnershipError,
    WorkspaceStore,
    is_locked,
    read_holder,
    storage_worker_available,
)

WORKSPACE_ROOT_SUFFIX = "-workspace-storage"

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)


# ---------------------------------------------------------------------------
# Private temporary root, named with the Looper resource prefix
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def storage_root() -> Path:
    """A private root outside the repository, removed by Looper cleanup."""

    prefix = os.environ.get("LOOPER_RESOURCE_PREFIX", "melt")
    root = Path(tempfile.gettempdir()) / f"{prefix}{WORKSPACE_ROOT_SUFFIX}"
    _register_cleanup(root, prefix)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _register_cleanup(root: Path, prefix: str) -> None:
    """Register idempotent removal of the private root with Looper cleanup.

    The guard removes the directory only when its path still carries
    this run's resource prefix, so cleanup never removes a directory it
    does not own. Cleanup registered by setup is preserved: entries are
    appended, and an already registered root is left alone.
    """

    cleanup_file = os.environ.get("LOOPER_RESOURCE_CLEANUP_FILE")
    if not cleanup_file:
        return
    path = Path(cleanup_file)
    marker = f"# workspace-storage-root: {root}"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if marker in existing:
        return
    header = existing or (
        "#!/usr/bin/env bash\n"
        "# Looper resource cleanup. Registered resources only; idempotent.\n"
        "set -u\n"
    )
    quoted = shlex.quote(str(root))
    path.write_text(
        header
        + f"\n{marker}\n"
        + f"if [ -d {quoted} ]; then\n"
        + f"  case {quoted} in\n"
        + f"    *{shlex.quote(prefix)}{WORKSPACE_ROOT_SUFFIX}) rm -rf -- {quoted} ;;\n"
        + "  esac\n"
        + "fi\n",
        encoding="utf-8",
    )


@pytest.fixture
def adapter(storage_root: Path) -> PlatformAdapter:
    """One isolated data root per test."""

    return PlatformAdapter.with_root(
        storage_root / f"case-{uuid.uuid4().hex[:12]}"
    )


@pytest.fixture
def store(adapter: PlatformAdapter) -> WorkspaceStore:
    """One opened workspace, closed with the test."""

    opened = WorkspaceStore.open(adapter, "sales-demo")
    yield opened
    opened.close()


# ---------------------------------------------------------------------------
# Platform adapter (SPEC.md sections 2.2 and 14)
# ---------------------------------------------------------------------------


def test_data_root_uses_xdg_data_home(tmp_path: Path) -> None:
    adapter = PlatformAdapter(env={"XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert adapter.application_data_root == tmp_path / "xdg"


def test_data_root_falls_back_to_home_local_share(tmp_path: Path) -> None:
    adapter = PlatformAdapter(env={"HOME": str(tmp_path / "home")})
    assert adapter.application_data_root == tmp_path / "home" / ".local" / "share"


def test_a_relative_xdg_data_home_is_ignored(tmp_path: Path) -> None:
    adapter = PlatformAdapter(env={"XDG_DATA_HOME": "relative/path", "HOME": str(tmp_path)})
    assert adapter.application_data_root == tmp_path / ".local" / "share"


def test_missing_home_and_xdg_cannot_resolve_a_root() -> None:
    from runtime.storage import PlatformResolutionError

    adapter = PlatformAdapter(env={})
    with pytest.raises(PlatformResolutionError):
        _ = adapter.application_data_root


def test_macos_data_root_uses_application_support(tmp_path: Path) -> None:
    adapter = PlatformAdapter(
        system="darwin", env={"HOME": str(tmp_path / "home")}
    )
    assert (
        adapter.application_data_root
        == tmp_path / "home" / "Library" / "Application Support"
    )


def test_workspace_directory_validates_identifiers(adapter: PlatformAdapter) -> None:
    assert adapter.workspace_directory("sales-demo") == (
        adapter.application_data_root / "melt" / "workspaces" / "sales-demo"
    )
    for bad in ("Sales", "1sales", "sales/demo", "", "sales demo", "s" * 65):
        with pytest.raises(ValueError):
            adapter.workspace_directory(bad)


# ---------------------------------------------------------------------------
# Directory layout (SPEC.md section 14)
# ---------------------------------------------------------------------------


def test_layout_creates_the_specification_tree(adapter: PlatformAdapter) -> None:
    directory = adapter.workspace_directory("layout-demo")
    layout = WorkspaceLayout.create(directory, "layout-demo")
    assert layout.database == directory / "state.sqlite"
    assert layout.owner_lock == directory / "owner.lock"
    for name in ("artifacts", "staging", "logs", "backups"):
        assert (directory / name).is_dir(), f"missing {name}/"
    # Creating again is idempotent.
    WorkspaceLayout.create(directory, "layout-demo")


def test_run_directories_are_named_and_validated(adapter: PlatformAdapter) -> None:
    layout = WorkspaceLayout.create(adapter.workspace_directory("runs"), "runs")
    staging = layout.staging_directory("run-000001")
    logs = layout.log_directory("run-000001")
    assert staging.parent == layout.staging
    assert logs.parent == layout.logs
    assert staging.is_dir() and logs.is_dir()
    for bad in ("..", ".", "run/1", "", "run 1"):
        with pytest.raises(ValueError):
            layout.staging_directory(bad)


# ---------------------------------------------------------------------------
# Ownership (SPEC.md section 14)
# ---------------------------------------------------------------------------


def test_owner_lock_refuses_a_second_holder_in_the_same_process(
    adapter: PlatformAdapter,
) -> None:
    path = adapter.workspace_directory("locked") / "owner.lock"
    with OwnerLock.acquire(path, workspace_id="locked") as first:
        assert first.held
        assert is_locked(path)
        with pytest.raises(WorkspaceOwnershipError) as conflict:
            OwnerLock.acquire(path, workspace_id="locked")
        diagnostic = conflict.value.diagnostics[0]
        assert diagnostic.code == "E_CONFLICT"
        assert "pid" in conflict.value.holder


def test_owner_lock_release_allows_reacquisition(adapter: PlatformAdapter) -> None:
    path = adapter.workspace_directory("recycled") / "owner.lock"
    first = OwnerLock.acquire(path, workspace_id="recycled")
    first.release()
    first.release()  # idempotent
    assert not is_locked(path)
    with OwnerLock.acquire(path, workspace_id="recycled") as second:
        assert "pid" in read_holder(path)
        assert second.holder["pid"] == os.getpid()


def test_owner_lock_is_released_when_the_process_dies(
    adapter: PlatformAdapter, repo_root: Path
) -> None:
    path = adapter.workspace_directory("dying") / "owner.lock"
    # A child process takes the lock and is killed without cleanup. The
    # operating system must release the lock; this is the property that
    # makes owner.lock a real fence and not a stale marker.
    code = (
        "import time\n"
        "from runtime.storage import OwnerLock\n"
        f"lock = OwnerLock.acquire({str(path)!r}, workspace_id='dying')\n"
        "print('HELD', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(repo_root),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "HELD"
        assert is_locked(path)
    finally:
        child.kill()
        child.wait(timeout=10)
    assert not is_locked(path)


# ---------------------------------------------------------------------------
# Artifact store (SPEC.md sections 5 and 14)
# ---------------------------------------------------------------------------


def test_artifact_store_writes_content_addressed_files(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"melt artifact bytes\n" + b"y" * 4096
    artifact = store.write(payload)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert artifact == expected
    assert store.contains(artifact)
    assert store.read(artifact) == payload
    # The staged temporary file is gone; only the hashed name remains.
    names = [entry.name for entry in (tmp_path / "artifacts").iterdir()]
    assert names == [artifact[len("sha256:") :]]
    assert store.verify(artifact)


def test_artifact_store_dedupes_equal_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.write(b"same bytes")
    second = store.write(b"same bytes")
    assert first == second
    assert len(list((tmp_path / "artifacts").iterdir())) == 1


def test_artifact_store_rejects_foreign_references(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        store.path_for("not-a-hash")
    with pytest.raises(ValueError):
        store.path_for("sha256:" + "A" * 64)
    assert not store.verify("sha256:" + "0" * 64)


def test_artifact_store_detects_corruption(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.write(b"payload")
    target = store.path_for(artifact)
    target.chmod(0o644)
    target.write_bytes(b"tampered")
    assert not store.verify(artifact)


# ---------------------------------------------------------------------------
# Workspace storage through the Node helper (SPEC.md sections 12.1 and 14)
# ---------------------------------------------------------------------------


@requires_storage_worker
def test_open_creates_the_specification_files(adapter: PlatformAdapter) -> None:
    store = WorkspaceStore.open(adapter, "sales-demo")
    try:
        directory = adapter.application_data_root / "melt" / "workspaces" / "sales-demo"
        assert store.directory == directory
        assert (directory / "state.sqlite").is_file()
        assert (directory / "owner.lock").is_file()
        for name in ("artifacts", "staging", "logs", "backups"):
            assert (directory / name).is_dir()
        assert is_locked(directory / "owner.lock")
    finally:
        store.close()


@requires_storage_worker
def test_owning_connection_reports_verified_durability(store: WorkspaceStore) -> None:
    facts = store.storage_facts
    assert facts["synchronous"] == 2, "owning connection is not synchronous=FULL"
    assert facts["journalMode"] == "wal"
    assert facts["adapterVersion"] == "1.3.0"


@requires_storage_worker
def test_second_service_cannot_open_the_same_workspace(
    adapter: PlatformAdapter,
) -> None:
    first = WorkspaceStore.open(adapter, "sales-demo")
    try:
        with pytest.raises(WorkspaceOwnershipError) as conflict:
            WorkspaceStore.open(adapter, "sales-demo")
        assert conflict.value.diagnostics[0].code == "E_CONFLICT"
    finally:
        first.close()
    # Releasing ownership hands the workspace to the next service.
    second = WorkspaceStore.open(adapter, "sales-demo")
    second.close()


@requires_storage_worker
def test_the_helper_holds_exactly_one_owning_connection(
    adapter: PlatformAdapter,
) -> None:
    layout = WorkspaceLayout.create(adapter.workspace_directory("one-connection"), "one-connection")
    worker = StorageWorker.start(layout.database)
    try:
        with pytest.raises(StorageWorkerError, match="E_CONFLICT"):
            worker.call("open", {"path": str(layout.database)})
        assert worker.call("ping", {}) == {"ok": True, "pong": True}
    finally:
        worker.terminate()


@requires_storage_worker
def test_ids_are_minted_on_one_connection_without_collisions(
    store: WorkspaceStore,
) -> None:
    ids = [store.mint_id() for _ in range(8)]
    assert len(set(ids)) == len(ids), "minted identifiers collide"
    assert ids == sorted(ids), "minted identifiers are not increasing"


@requires_storage_worker
def test_append_records_commits_atomically_with_one_sequence(
    store: WorkspaceStore,
) -> None:
    first_sequence, first_records = store.append_records(
        [
            ("melt.parameterState", {"parameterId": "minimum", "value": 200, "revision": 2}),
            ("melt.commandReceipt", {"requestId": "01-request", "ok": True}),
        ]
    )
    assert [record.kind for record in first_records] == [
        "melt.parameterState",
        "melt.commandReceipt",
    ]
    assert all(record.commit_sequence == first_sequence for record in first_records)
    assert store.commit_sequence == first_sequence

    second_sequence, _ = store.append_records(
        [("melt.parameterState", {"parameterId": "minimum", "value": 0, "revision": 3})]
    )
    assert second_sequence == first_sequence + 1, "a commit must advance the sequence once"

    stored = {record.id: record for record in store.records()}
    assert len(stored) == 4  # three appended records plus the workspace header
    by_kind = {}
    for record in stored.values():
        by_kind.setdefault(record.kind, []).append(record)
    assert by_kind["melt.parameterState"][1].data["value"] == 0
    assert store.get_record(first_records[0].id) is not None
    assert store.get_record(999999) is None


@requires_storage_worker
def test_records_and_epoch_survive_a_clean_restart(adapter: PlatformAdapter) -> None:
    first = WorkspaceStore.open(adapter, "sales-demo")
    _, records = first.append_records(
        [("melt.parameterState", {"parameterId": "minimum", "value": 200})]
    )
    last_sequence, _ = first.append_records(
        [("melt.parameterState", {"parameterId": "minimum", "value": 0})]
    )
    epoch = first.epoch
    header = first.header
    first.close()

    second = WorkspaceStore.open(adapter, "sales-demo")
    try:
        assert second.epoch == epoch, "the workspace epoch changed across a restart"
        assert second.header["workspaceId"] == "sales-demo"
        assert second.header["formatVersion"] == 1
        assert second.get_record(records[0].id) is not None
        assert second.get_record(records[0].id).data["value"] == 200
        assert [record.kind for record in second.records_of_kind("melt.parameterState")] == [
            "melt.parameterState",
            "melt.parameterState",
        ]
        assert second.header["entryId"] == header["entryId"]
        assert second.commit_sequence == last_sequence, (
            "a reopened store must resume from the last committed sequence"
        )
    finally:
        second.close()


@requires_storage_worker
def test_two_workspaces_own_separate_connections(adapter: PlatformAdapter) -> None:
    first = WorkspaceStore.open(adapter, "alpha")
    try:
        second = WorkspaceStore.open(adapter, "beta")
        try:
            # One owning connection per workspace database, not one for
            # the whole service.
            assert first.worker_pid != second.worker_pid
            first.append_records([("melt.commandReceipt", {"requestId": "a"})])
            second.append_records([("melt.commandReceipt", {"requestId": "b"})])
            assert [
                record.data["requestId"]
                for record in first.records_of_kind("melt.commandReceipt")
            ] == ["a"]
            assert [
                record.data["requestId"]
                for record in second.records_of_kind("melt.commandReceipt")
            ] == ["b"]
        finally:
            second.close()
        with pytest.raises(RuntimeError, match="closed"):
            second.records()
    finally:
        first.close()


@requires_storage_worker
def test_a_killed_helper_leaves_committed_records_intact(
    adapter: PlatformAdapter,
) -> None:
    store = WorkspaceStore.open(adapter, "sales-demo")
    _, records = store.append_records(
        [("melt.commandReceipt", {"requestId": "01-crash", "ok": True})]
    )
    store.worker.kill()  # crash model of the storage probe: SIGKILL
    store.close()

    reopened = WorkspaceStore.open(adapter, "sales-demo")
    try:
        recovered = reopened.get_record(records[0].id)
        assert recovered is not None, "an acknowledged record vanished after the kill"
        assert recovered.data["requestId"] == "01-crash"
        facts = reopened.durability_facts()
        assert facts["integrityCheck"] == "ok"
        assert facts["journalMode"] == "wal"
        # The store stays writable after recovery.
        reopened.append_records([("melt.commandReceipt", {"requestId": "02-after"})])
    finally:
        reopened.close()


@requires_storage_worker
def test_the_helper_exits_when_the_service_pipe_closes(
    adapter: PlatformAdapter,
) -> None:
    layout = WorkspaceLayout.create(adapter.workspace_directory("pipe-close"), "pipe-close")
    worker = StorageWorker.start(layout.database)
    worker.close_input()
    assert worker.wait(timeout=15) == 0, "the helper must exit when its parent goes away"


@requires_storage_worker
def test_frame_limit_is_enforced_on_both_sides(
    adapter: PlatformAdapter,
) -> None:
    layout = WorkspaceLayout.create(adapter.workspace_directory("frames"), "frames")
    worker = StorageWorker.start(layout.database)
    try:
        # The client refuses to encode an oversized request at all.
        with pytest.raises(MeltCommandError) as encode_error:
            worker.call("ping", {"padding": "x" * (1024 * 1024)})
        assert encode_error.value.diagnostics[0].code == "E_LIMIT"
        # The channel stays usable after the refused request.
        assert worker.call("ping", {})["pong"] is True
    finally:
        worker.terminate()

    # The helper rejects a declared length above the limit before it
    # allocates or reads the payload, then exits because the stream can
    # no longer be resynchronized (SPEC.md section 12.1).
    worker = StorageWorker.start(layout.database)
    try:
        declared = (1024 * 1024) + 1
        worker.send_raw(declared.to_bytes(4, "big") + b"truncated payload")
        response = worker.read_response()
        assert response["ok"] is False
        assert response["code"] == "E_LIMIT"
        assert worker.wait(timeout=15) == 1
    finally:
        worker.terminate()


@requires_storage_worker
def test_artifact_bytes_stay_outside_the_database(
    store: WorkspaceStore, adapter: PlatformAdapter
) -> None:
    payload = b"parquet-shaped bytes with a unique marker 6f2c91" + b"z" * 8192
    artifact = store.write_artifact(payload)
    assert artifact.startswith("sha256:")
    stored_path = store.artifact_path(artifact)
    assert stored_path.parent == store.layout.artifacts
    assert stored_path.read_bytes() == payload

    database_bytes = store.layout.database.read_bytes()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(store.layout.database) + suffix)
        if extra.exists():
            database_bytes += extra.read_bytes()
    assert b"unique marker 6f2c91" not in database_bytes, (
        "artifact bytes leaked into state.sqlite"
    )
    # Restarting does not lose the artifact, and equal bytes dedupe.
    store.close()
    reopened = WorkspaceStore.open(adapter, "sales-demo")
    try:
        assert reopened.read_artifact(artifact) == payload
        assert reopened.write_artifact(payload) == artifact
    finally:
        reopened.close()


@requires_storage_worker
def test_an_unknown_stored_format_is_reported_not_migrated(
    adapter: PlatformAdapter,
) -> None:
    layout = WorkspaceLayout.create(adapter.workspace_directory("future"), "future")
    forged = StorageWorker.start(layout.database)
    forged.commit_entries(
        [
            (
                "melt.workspaceHeader",
                {
                    "formatVersion": 99,
                    "workspaceId": "future",
                    "epoch": 1,
                    "createdAt": "2026-10-18T00:00:00Z",
                },
            )
        ]
    )
    forged.terminate()

    with pytest.raises(WorkspaceFormatError) as error:
        WorkspaceStore.open(adapter, "future")
    assert error.value.diagnostics[0].code == "E_VERSION"
    # The stored bytes are preserved: the failed open changed nothing.
    reopened = StorageWorker.start(layout.database)
    try:
        entries, _ = reopened.scan_entries()
        assert entries[0]["data"]["formatVersion"] == 99
    finally:
        reopened.terminate()


@requires_storage_worker
def test_the_workspace_header_cannot_be_forged(store: WorkspaceStore) -> None:
    with pytest.raises(ValueError, match="workspace header"):
        store.append_records(
            [("melt.workspaceHeader", {"formatVersion": 2, "workspaceId": "other"})]
        )
    with pytest.raises(ValueError):
        store.append_records([("not-melt-prefix", {})])


def _child_environment(repo_root: Path) -> dict:
    """The test environment, with the repository import paths."""

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "python"), str(repo_root)]
    )
    return environment


@requires_storage_worker
def test_service_process_death_releases_ownership(
    adapter: PlatformAdapter, repo_root: Path
) -> None:
    code = "\n".join(
        [
            "import sys, time",
            "from runtime.storage import PlatformAdapter, WorkspaceStore",
            "store = WorkspaceStore.open(PlatformAdapter.with_root(sys.argv[1]), sys.argv[2])",
            "store.append_records([('melt.commandReceipt', {'requestId': '01-owner', 'ok': True})])",
            "print(f'READY {store.epoch} {store.worker_pid}', flush=True)",
            "time.sleep(60)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(adapter.application_data_root), "sales-demo"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(repo_root),
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().strip()
        assert line.startswith("READY"), f"the child service did not start: {line}"
        _, epoch_text, worker_pid_text = line.split()
        epoch, worker_pid = int(epoch_text), int(worker_pid_text)
    except BaseException:
        child.kill()
        child.wait(timeout=10)
        raise

    child.kill()
    child.wait(timeout=10)

    # The orphaned helper owns the connection; it must exit once its
    # parent's pipe closes, before another service opens the workspace.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the storage helper outlived its service process")

    lock_path = adapter.workspace_directory("sales-demo") / "owner.lock"
    assert not is_locked(lock_path), "the operating system did not release owner.lock"

    successor = WorkspaceStore.open(adapter, "sales-demo")
    try:
        assert successor.epoch == epoch
        receipts = successor.records_of_kind("melt.commandReceipt")
        assert [record.data["requestId"] for record in receipts] == ["01-owner"]
        # The recovered workspace accepts new work.
        successor.append_records(
            [("melt.commandReceipt", {"requestId": "02-successor", "ok": True})]
        )
        assert [record.data["requestId"] for record in successor.records_of_kind("melt.commandReceipt")] == [
            "01-owner",
            "02-successor",
        ]
    finally:
        successor.close()


@requires_storage_worker
def test_json_record_data_is_validated_before_commit(store: WorkspaceStore) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        store.append_records([("melt.commandReceipt", {"value": float("nan")})])
    with pytest.raises(ValueError):
        store.append_records([("melt.commandReceipt", {"value": object()})])
    with pytest.raises(ValueError, match="at least one record"):
        store.append_records([])
