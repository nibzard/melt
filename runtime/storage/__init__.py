"""Storage: the Pi Durable adapter and the artifact store (SPEC.md 14).

The service is the sole writer to each workspace database. Artifact
bytes stay outside the database. Backlog task T002 probed the pinned
adapter, T008 implements workspace storage and ownership, and T011
registers verified artifacts.

A workspace resolves its directory through the platform adapter,
creates the specification tree of section 14, takes the exclusive
``owner.lock``, and drives exactly one owning connection to
``state.sqlite`` inside a Node storage worker. The worker speaks the
framed channel of section 12.1: a four-byte big-endian length prefix
followed by UTF-8 JSON, rejected above the control frame limit.

Typical use:

```python
from runtime.storage import PlatformAdapter, WorkspaceStore

with WorkspaceStore.open(PlatformAdapter.with_root(root), "sales-demo") as store:
    sequence, records = store.append_records(
        [("melt.parameterState", {"parameterId": "minimum", "value": 0})]
    )
    artifact = store.write_artifact(b"...")
```
"""

from __future__ import annotations

from runtime.storage.artifacts import (
    ARTIFACT_HASH_PATTERN,
    ARTIFACT_NAME_PATTERN,
    ArtifactStore,
    fsync_directory,
)
from runtime.storage.errors import (
    MeltStorageError,
    PlatformResolutionError,
    StorageWorkerError,
    WorkspaceFormatError,
    WorkspaceOwnershipError,
)
from runtime.storage.layout import (
    BACKUPS_DIRECTORY,
    DATABASE_NAME,
    LOGS_DIRECTORY,
    OWNER_LOCK_NAME,
    REQUIRED_DIRECTORIES,
    STAGING_DIRECTORY,
    ARTIFACTS_DIRECTORY,
    WorkspaceLayout,
)
from runtime.storage.ownership import OwnerLock, is_locked, read_holder
from runtime.storage.platform import (
    PlatformAdapter,
    WORKSPACES_ROOT_PARTS,
    WORKSPACE_ID_PATTERN,
    validate_workspace_id,
)
from runtime.storage.worker import (
    PI_DURABLE_PIN,
    SYNCHRONOUS_FULL,
    StorageWorker,
    WORKER_SCRIPT,
    storage_worker_available,
)
from runtime.storage.workspace import (
    HEADER_KIND,
    STORAGE_FORMAT_VERSION,
    StoredRecord,
    WorkspaceStore,
    validate_record_kind,
)

__all__ = [
    "ARTIFACT_HASH_PATTERN",
    "ARTIFACT_NAME_PATTERN",
    "ARTIFACTS_DIRECTORY",
    "BACKUPS_DIRECTORY",
    "DATABASE_NAME",
    "HEADER_KIND",
    "LOGS_DIRECTORY",
    "MeltStorageError",
    "OWNER_LOCK_NAME",
    "PI_DURABLE_PIN",
    "PlatformAdapter",
    "PlatformResolutionError",
    "REQUIRED_DIRECTORIES",
    "STAGING_DIRECTORY",
    "STORAGE_FORMAT_VERSION",
    "StorageWorker",
    "StorageWorkerError",
    "SYNCHRONOUS_FULL",
    "WORKER_SCRIPT",
    "WORKSPACES_ROOT_PARTS",
    "WORKSPACE_ID_PATTERN",
    "WorkspaceFormatError",
    "WorkspaceLayout",
    "WorkspaceOwnershipError",
    "WorkspaceStore",
    "StoredRecord",
    "OwnerLock",
    "ArtifactStore",
    "fsync_directory",
    "is_locked",
    "read_holder",
    "storage_worker_available",
    "validate_record_kind",
    "validate_workspace_id",
]
