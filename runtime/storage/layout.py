"""Workspace directory layout (SPEC.md section 14).

```text
<application-data-root>/melt/workspaces/<workspace-id>/
  state.sqlite
  owner.lock
  artifacts/<sha256-hex>
  staging/<run-id>/
  logs/<run-id>/
  backups/
```

The layout object only creates and names directories. The database
connection belongs to the storage worker, the lock to the owner, and
artifact bytes to the artifact store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from runtime.storage.platform import validate_workspace_id

DATABASE_NAME = "state.sqlite"
OWNER_LOCK_NAME = "owner.lock"
ARTIFACTS_DIRECTORY = "artifacts"
STAGING_DIRECTORY = "staging"
LOGS_DIRECTORY = "logs"
BACKUPS_DIRECTORY = "backups"
REQUIRED_DIRECTORIES = (
    ARTIFACTS_DIRECTORY,
    STAGING_DIRECTORY,
    LOGS_DIRECTORY,
    BACKUPS_DIRECTORY,
)

_RUN_DIRECTORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _run_directory_name(run_id: str) -> str:
    """Validate one service-issued run id before it becomes a path."""

    if not isinstance(run_id, str) or not _RUN_DIRECTORY_PATTERN.match(run_id):
        raise ValueError(
            f"run identifier {run_id!r} does not match"
            " [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
    if run_id in {".", ".."}:
        raise ValueError(f"run identifier {run_id!r} is not a directory name")
    return run_id


@dataclass(frozen=True)
class WorkspaceLayout:
    """The paths of one workspace directory."""

    directory: Path
    workspace_id: str

    @classmethod
    def create(cls, directory: Path, workspace_id: str) -> "WorkspaceLayout":
        """Create the specification tree for one workspace, idempotently."""

        validate_workspace_id(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_DIRECTORIES:
            (directory / name).mkdir(exist_ok=True)
        return cls(directory=directory, workspace_id=workspace_id)

    @property
    def database(self) -> Path:
        return self.directory / DATABASE_NAME

    @property
    def owner_lock(self) -> Path:
        return self.directory / OWNER_LOCK_NAME

    @property
    def artifacts(self) -> Path:
        return self.directory / ARTIFACTS_DIRECTORY

    @property
    def staging(self) -> Path:
        return self.directory / STAGING_DIRECTORY

    @property
    def logs(self) -> Path:
        return self.directory / LOGS_DIRECTORY

    @property
    def backups(self) -> Path:
        return self.directory / BACKUPS_DIRECTORY

    def staging_directory(self, run_id: str) -> Path:
        """The private staging directory of one run."""

        path = self.staging / _run_directory_name(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_directory(self, run_id: str) -> Path:
        """The log directory of one run."""

        path = self.logs / _run_directory_name(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path


__all__ = [
    "BACKUPS_DIRECTORY",
    "DATABASE_NAME",
    "LOGS_DIRECTORY",
    "OWNER_LOCK_NAME",
    "REQUIRED_DIRECTORIES",
    "STAGING_DIRECTORY",
    "ARTIFACTS_DIRECTORY",
    "WorkspaceLayout",
]
