"""Storage errors (SPEC.md sections 11.3 and 14).

Storage failures that describe a workspace condition carry the shared
diagnostic shape, so callers report them like every other Melt failure.
Operational failures that belong to the helper process or the platform
raise plain exceptions instead: they are not document diagnostics.
"""

from __future__ import annotations

from pathlib import Path

from melt.schema.diagnostics import Diagnostic, DiagnosticCode, MeltSchemaError


class MeltStorageError(MeltSchemaError):
    """Base class for storage failures that carry diagnostics."""


class WorkspaceOwnershipError(MeltStorageError):
    """Another service process owns the workspace (SPEC.md section 14).

    The service is the sole writer to each workspace database, and the
    operating-system ``owner.lock`` enforces that rule. A second owner
    is refused instead of queued: an overlapping writer on the pinned
    adapter blocks its own event loop and fails with ``database is
    locked`` (probe finding F4).
    """

    def __init__(self, workspace_id: str, lock_path: Path, holder: str):
        self.workspace_id = workspace_id
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            (
                Diagnostic(
                    code=DiagnosticCode.CONFLICT,
                    path="/workspaceId",
                    message=(
                        f"workspace {workspace_id!r} is already owned by"
                        f" another service process"
                        + (f" ({holder})" if holder else "")
                    ),
                    hint=(
                        "Close the owning service, or remove its process,"
                        " and open the workspace again. The lock is"
                        " released by the operating system when the owner"
                        " exits."
                    ),
                ),
            )
        )


class WorkspaceFormatError(MeltStorageError):
    """The stored workspace uses a version this service cannot read.

    SPEC.md section 14 forbids silent migration: an unknown stored
    version opens a recovery screen and preserves the files.
    """

    def __init__(self, workspace_id: str, message: str, hint: str = ""):
        self.workspace_id = workspace_id
        super().__init__(
            (
                Diagnostic(
                    code=DiagnosticCode.VERSION,
                    path="/workspace",
                    message=message,
                    hint=hint
                    or "Open the workspace in recovery mode. Do not"
                    " overwrite the stored database.",
                ),
            )
        )


class PlatformResolutionError(RuntimeError):
    """The application data root cannot be resolved on this platform."""


class StorageWorkerError(RuntimeError):
    """The Node storage worker failed, died, or broke the frame protocol.

    This is an operational failure of the helper that owns the storage
    connection, not a diagnostic about a document.
    """


__all__ = [
    "MeltStorageError",
    "PlatformResolutionError",
    "StorageWorkerError",
    "WorkspaceFormatError",
    "WorkspaceOwnershipError",
]
