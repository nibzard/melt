"""Content-addressed artifact files (SPEC.md sections 5 and 14).

Artifact bytes stay outside the workspace database. Each artifact is an
immutable file named by its lowercase SHA-256 hash, written with the
publication ordering the storage probe proved: stage and fsync the
file, rename it into place, fsync the directory, and only then may a
database reference follow. A crash can leave an unreferenced file,
never a referenced missing one.
"""

from __future__ import annotations

from contextlib import contextmanager

import errno
import os
import re
import stat
import uuid
from pathlib import Path

from melt.schema.canonical import sha256_hash

ARTIFACT_NAME_PATTERN = re.compile(r"^[0-9a-f]{64}\Z")
"""Artifact file names are bare lowercase SHA-256 hex."""

ARTIFACT_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}\Z")
"""Artifact references add the `sha256:` prefix (SPEC.md section 3)."""


def _validate_hash(artifact: str) -> str:
    if not isinstance(artifact, str) or not ARTIFACT_HASH_PATTERN.match(artifact):
        raise ValueError(
            f"artifact reference {artifact!r} is not sha256:<lowercase hex>"
        )
    return artifact


def fsync_directory(path: Path) -> None:
    """fsync one directory so a rename inside it is durable."""

    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    """The `artifacts/` directory of one workspace."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path_for(self, artifact: str) -> Path:
        """The immutable file of one artifact reference."""

        _validate_hash(artifact)
        return self.directory / artifact[len("sha256:") :]

    def contains(self, artifact: str) -> bool:
        return self.path_for(artifact).is_file()

    def write(self, data: bytes) -> str:
        """Store bytes under their content hash and return the reference.

        Writing an artifact that already exists keeps the stored file:
        artifact versions are append-only, and two logical sources may
        refer to the same bytes.
        """

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("artifact data must be bytes")
        payload = bytes(data)
        artifact = sha256_hash(payload)
        final = self.path_for(artifact)
        if final.exists():
            return artifact
        self.directory.mkdir(parents=True, exist_ok=True)
        staged = self.directory / f".staging-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o444)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(staged, final)
        finally:
            if staged.exists():
                # A concurrent writer won the rename; this copy is junk.
                staged.unlink(missing_ok=True)
        fsync_directory(self.directory)
        return artifact

    @contextmanager
    def open(self, artifact: str):
        """Open a regular artifact file without following symbolic links."""

        path = self.path_for(artifact)
        # Nonblocking open lets the type check reject a pipe without waiting.
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "an artifact must be a regular file", path)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                yield handle
        finally:
            os.close(fd)

    def read(self, artifact: str) -> bytes:
        """Read a verified regular file handle."""
        with self.open(artifact) as handle:
            return handle.read()

    def verify(self, artifact: str) -> bool:
        """Check that a stored file still hashes to its reference."""

        try:
            payload = self.read(artifact)
        except OSError:
            return False
        return sha256_hash(payload) == artifact


__all__ = [
    "ARTIFACT_HASH_PATTERN",
    "ARTIFACT_NAME_PATTERN",
    "ArtifactStore",
    "fsync_directory",
]
