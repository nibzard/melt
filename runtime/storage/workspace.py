"""Workspace storage: layout, ownership, and the owning connection.

SPEC.md section 14 requires the service to be the sole writer to each
workspace database, with ownership enforced through an operating-system
lock and artifact bytes kept outside the database. A ``WorkspaceStore``
ties the three pieces together for one workspace:

1. resolve the directory through the platform adapter;
2. create the specification tree of section 14;
3. take the exclusive ``owner.lock`` for the whole ownership period;
4. drive one owning connection to ``state.sqlite`` inside the Node
   storage worker, where every identifier is minted and every commit
   happens (probe findings F3 through F5).

Opening a store negotiates the stored format version from a committed
workspace header. An unknown version is reported, never migrated.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.storage.artifacts import ArtifactStore
from runtime.storage.errors import WorkspaceFormatError
from runtime.storage.layout import WorkspaceLayout
from runtime.storage.ownership import OwnerLock
from runtime.storage.platform import PlatformAdapter, validate_workspace_id
from runtime.storage.worker import PI_DURABLE_PIN, StorageWorker

STORAGE_FORMAT_VERSION = 1
"""The workspace storage format of `melt/v0.1`."""

HEADER_KIND = "melt.workspaceHeader"
"""The committed record that identifies a workspace database."""

RECORD_KIND_PATTERN = re.compile(r"^melt\.[a-z][A-Za-z0-9_-]{0,63}\Z")
"""Service record kinds live under the reserved `melt.` prefix."""

_EPOCH_FLOOR = 1
_EPOCH_CEILING = 2**31 - 1


@dataclass(frozen=True)
class StoredRecord:
    """One committed workspace record."""

    id: int
    kind: str
    data: dict
    commit_sequence: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_record_kind(kind: str) -> str:
    """Validate one service record kind."""

    if not isinstance(kind, str) or not RECORD_KIND_PATTERN.match(kind):
        raise ValueError(
            f"record kind {kind!r} does not match melt.<name> with the"
            " entity identifier syntax"
        )
    if kind == HEADER_KIND:
        raise ValueError(
            "the workspace header is written by the service alone and"
            " cannot be appended"
        )
    return kind


class WorkspaceStore:
    """The storage of one workspace under single-writer ownership."""

    def __init__(
        self,
        *,
        workspace_id: str,
        layout: WorkspaceLayout,
        lock: OwnerLock,
        worker: StorageWorker,
        header: Mapping[str, Any],
        commit_sequence: int,
    ):
        self._workspace_id = workspace_id
        self._layout = layout
        self._lock = lock
        self._worker = worker
        self._header = dict(header)
        self._commit_sequence = int(commit_sequence)
        self._artifacts = ArtifactStore(layout.artifacts)
        self._closed = False

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        platform: PlatformAdapter,
        workspace_id: str,
        *,
        node: str | None = None,
    ) -> "WorkspaceStore":
        """Take ownership of one workspace and its storage.

        The call fails with `E_CONFLICT` when another service process
        owns the workspace, and with `E_VERSION` when the stored
        workspace header records a format this service cannot read.
        """

        validate_workspace_id(workspace_id)
        directory = platform.workspace_directory(workspace_id)
        layout = WorkspaceLayout.create(directory, workspace_id)
        lock = OwnerLock.acquire(layout.owner_lock, workspace_id=workspace_id)
        try:
            worker = StorageWorker.start(layout.database, node=node)
        except BaseException:
            lock.release()
            raise
        try:
            header, commit_sequence = cls._negotiate(worker, workspace_id)
        except BaseException:
            worker.terminate()
            lock.release()
            raise
        return cls(
            workspace_id=workspace_id,
            layout=layout,
            lock=lock,
            worker=worker,
            header=header,
            commit_sequence=commit_sequence,
        )

    @classmethod
    def _negotiate(
        cls, worker: StorageWorker, workspace_id: str
    ) -> tuple[dict, int]:
        """Load or create the workspace header (SPEC.md section 14 step 1)."""

        committed = _scan_all(worker)
        headers = [record for record in committed if record.kind == HEADER_KIND]
        latest = max(headers, key=lambda record: record.id) if headers else None
        if latest is None:
            epoch = random.SystemRandom().randint(_EPOCH_FLOOR, _EPOCH_CEILING)
            header = {
                "formatVersion": STORAGE_FORMAT_VERSION,
                "workspaceId": workspace_id,
                "epoch": epoch,
                "createdAt": _utc_now(),
                "adapter": "pi-durable",
                "adapterVersion": worker.facts.get(
                    "adapterVersion", PI_DURABLE_PIN
                ),
            }
            sequence, entries = worker.commit_entries(
                [(HEADER_KIND, header)]
            )
            header = dict(header, entryId=entries[0]["id"])
            return header, sequence
        stored = latest.data
        stored_version = stored.get("formatVersion")
        if stored_version != STORAGE_FORMAT_VERSION:
            raise WorkspaceFormatError(
                workspace_id,
                f"stored workspace format {stored_version!r} is not"
                f" supported; this service writes format"
                f" {STORAGE_FORMAT_VERSION}",
            )
        if stored.get("workspaceId") != workspace_id:
            raise WorkspaceFormatError(
                workspace_id,
                "the stored workspace header names another workspace:"
                f" {stored.get('workspaceId')!r}",
            )
        epoch = stored.get("epoch")
        if not isinstance(epoch, int) or epoch < 1:
            raise WorkspaceFormatError(
                workspace_id,
                "the stored workspace header carries no usable epoch",
            )
        header = dict(stored, entryId=latest.id)
        # The newest committed sequence, not the header's own one: the
        # store must resume counting from the last durable transaction.
        commit_sequence = max(
            (record.commit_sequence for record in committed), default=0
        )
        return header, commit_sequence

    # ------------------------------------------------------------------
    # State of the open store
    # ------------------------------------------------------------------

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def layout(self) -> WorkspaceLayout:
        return self._layout

    @property
    def directory(self) -> Path:
        return self._layout.directory

    @property
    def header(self) -> dict:
        return dict(self._header)

    @property
    def epoch(self) -> int:
        """The stable epoch of this workspace's committed history."""

        return int(self._header["epoch"])

    @property
    def commit_sequence(self) -> int:
        """The commit sequence of the last durable transaction."""

        return self._commit_sequence

    @property
    def storage_facts(self) -> dict:
        """The durability facts verified on the owning connection."""

        return self._worker.facts

    @property
    def worker_pid(self) -> int:
        return self._worker.pid

    @property
    def worker(self) -> StorageWorker:
        """The helper that owns the connection. Tests use it to crash."""

        return self._worker

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def mint_id(self) -> int:
        """Mint one identifier through the owning connection."""

        self._require_open()
        return self._worker.mint_id()

    def append_records(
        self, records: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> tuple[int, list[StoredRecord]]:
        """Commit records in one atomic transaction.

        Every identifier is minted on the owning connection and the
        whole batch commits together, so the result is either every
        record or none of them.
        """

        self._require_open()
        if not records:
            raise ValueError("append_records requires at least one record")
        prepared: list[tuple[str, dict]] = []
        for kind, data in records:
            validate_record_kind(kind)
            if not isinstance(data, Mapping):
                raise ValueError(f"record {kind!r} requires a data object")
            payload = dict(data)
            try:
                json.dumps(payload, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"record {kind!r} data is not valid JSON: {error}"
                ) from error
            prepared.append((kind, payload))
        sequence, entries = self._worker.commit_entries(prepared)
        stored = [
            StoredRecord(
                id=int(entry["id"]),
                kind=str(entry["kind"]),
                data=dict(payload),
                commit_sequence=sequence,
            )
            for entry, (_kind, payload) in zip(entries, prepared)
        ]
        self._commit_sequence = max(self._commit_sequence, sequence)
        return sequence, stored

    def get_record(self, entry_id: int) -> StoredRecord | None:
        """Read one committed record by its identifier."""

        self._require_open()
        entry = self._worker.get_entry(int(entry_id))
        if entry is None:
            return None
        return _record_from_entry(entry)

    def records(self) -> list[StoredRecord]:
        """Every committed record, oldest first."""

        self._require_open()
        return _scan_all(self._worker)

    def records_of_kind(self, kind: str) -> list[StoredRecord]:
        """Every committed record of one kind, oldest first."""

        return [record for record in self.records() if record.kind == kind]

    def durability_facts(self) -> dict:
        """Re-read the durability settings of the owning connection."""

        self._require_open()
        return self._worker.durability_facts()

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def write_artifact(self, data: bytes) -> str:
        """Store artifact bytes outside the database (SPEC.md section 14)."""

        return self._artifacts.write(data)

    def read_artifact(self, artifact: str) -> bytes:
        return self._artifacts.read(artifact)

    def artifact_path(self, artifact: str) -> Path:
        return self._artifacts.path_for(artifact)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the owning connection and release the lock. Idempotent."""

        if self._closed:
            return
        self._closed = True
        try:
            self._worker.terminate()
        finally:
            self._lock.release()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                f"workspace {self._workspace_id!r} storage is closed"
            )

    def __enter__(self) -> "WorkspaceStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - last-resort cleanup
        try:
            self.close()
        except Exception:
            pass


def _record_from_entry(entry: Mapping[str, Any]) -> StoredRecord:
    return StoredRecord(
        id=int(entry["id"]),
        kind=str(entry["kind"]),
        data=dict(entry.get("data") or {}),
        commit_sequence=int(entry.get("commitSequence", 0)),
    )


def _scan_all(worker: StorageWorker) -> list[StoredRecord]:
    """Read the whole record stream of an existing conversation."""

    records: list[StoredRecord] = []
    if not worker.root_conversation_committed:
        return records
    cursor: Mapping[str, Any] | None = None
    while True:
        entries, next_cursor = worker.scan_entries(cursor=cursor)
        records.extend(_record_from_entry(entry) for entry in entries)
        if next_cursor is None:
            return records
        cursor = next_cursor


__all__ = [
    "HEADER_KIND",
    "STORAGE_FORMAT_VERSION",
    "StoredRecord",
    "WorkspaceStore",
    "validate_record_kind",
]
