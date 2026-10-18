"""Single-writer ownership through an operating-system lock (SPEC.md 14).

The service is the sole writer to each workspace database. The pinned
adapter fences no second writer (probe finding F3): any connection that
holds the database file can commit to it. The ``owner.lock`` file closes
that gap. An exclusive ``flock`` is held for the whole ownership period
and released by the operating system when the owning process exits, so a
crashed service cannot wedge a workspace.

The lock file is never removed. Removing it would let a second writer
create a fresh file and lock that instead.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform as system_platform
import socket
from datetime import datetime, timezone
from pathlib import Path

from runtime.storage.errors import WorkspaceOwnershipError


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_holder(path: Path) -> str:
    """Return the recorded holder of the lock, for diagnostics."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        holder = json.loads(raw)
    except ValueError:
        return raw.strip()
    if not isinstance(holder, dict):
        return raw.strip()
    pid = holder.get("pid")
    host = holder.get("host")
    acquired_at = holder.get("acquiredAt")
    parts = []
    if isinstance(pid, int):
        parts.append(f"pid {pid}")
    if isinstance(host, str) and host:
        parts.append(f"on {host}")
    if isinstance(acquired_at, str) and acquired_at:
        parts.append(f"since {acquired_at}")
    return ", ".join(parts)


def is_locked(path: Path) -> bool:
    """Report whether another process currently holds the lock."""

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class OwnerLock:
    """The exclusive operating-system lock on one workspace."""

    def __init__(self, path: Path, fd: int, holder: dict):
        self.path = path
        self._fd: int | None = fd
        self.holder = holder

    @classmethod
    def acquire(cls, path: Path, *, workspace_id: str) -> "OwnerLock":
        """Take the lock or refuse with `E_CONFLICT`.

        ``flock`` conflicts per open file description, so a second open
        in the same process fails exactly like one in another process.
        """

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            holder = read_holder(path)
            os.close(fd)
            raise WorkspaceOwnershipError(workspace_id, path, holder) from error
        except OSError as error:
            os.close(fd)
            raise WorkspaceOwnershipError(workspace_id, path, str(error)) from error
        holder = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquiredAt": _utc_now(),
            "system": system_platform.system(),
        }
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(holder).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            # The fence is the lock, not the file body. Holder text is
            # advisory, so a failed write must not fail ownership.
            pass
        return cls(path=path, fd=fd, holder=holder)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Release the lock. Idempotent."""

        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "OwnerLock":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


__all__ = ["OwnerLock", "is_locked", "read_holder"]
