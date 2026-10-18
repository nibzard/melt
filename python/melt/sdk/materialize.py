"""Materialization of staged outputs (SPEC.md section 13.2).

`ui.output` accepts exactly three value shapes:

- a pandas DataFrame for a `table` output, written as Parquet;
- a matplotlib Figure for an `image` output, written as PNG;
- a `str` for a `text` output, written as UTF-8 bytes.

Every other shape is refused with a message that names the accepted
shape. Each file lands through a temporary name and one rename, so a
partial write never sits under its final name. The materializer checks
the magic bytes it produced, because the service validates the actual
files independently during publication (SPEC.md section 13.4).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from melt.schema.canonical import sha256_hash
from melt.sdk.errors import MeltSDKError

TABLE_KIND = "table"
IMAGE_KIND = "image"
TEXT_KIND = "text"

OUTPUT_KINDS = frozenset({TABLE_KIND, IMAGE_KIND, TEXT_KIND})
"""The output kinds a manifest may declare (SPEC.md section 7.1)."""

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
PNG_MEDIA_TYPE = "image/png"
TEXT_MEDIA_TYPE = "text/plain"

_KIND_FILES: dict[str, "tuple[str, str]"] = {
    TABLE_KIND: (".parquet", PARQUET_MEDIA_TYPE),
    IMAGE_KIND: (".png", PNG_MEDIA_TYPE),
    TEXT_KIND: (".txt", TEXT_MEDIA_TYPE),
}

PARQUET_MAGIC = b"PAR1"
"""Parquet files start and end with this magic."""

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
"""Every PNG starts with this signature."""


@dataclass(frozen=True)
class StagedOutput:
    """One staged output file with its measured facts.

    The hash and length are a report for the service, not proof. The
    service rereads, rehashes, and remeasures every staged file before
    anything publishes (SPEC.md section 13.4).
    """

    name: str
    kind: str
    mediaType: str
    path: Path
    byteLength: int
    sha256: str

    def to_wire(self) -> dict:
        """Return the index entry of this staged output."""

        return {
            "kind": self.kind,
            "mediaType": self.mediaType,
            "path": str(self.path),
            "byteLength": self.byteLength,
            "sha256": self.sha256,
        }


def _describe(value: object) -> str:
    """Return the type name of one value for an error message."""

    return type(value).__name__ or "value"


def _stage_table(name: str, value: object, handle: BinaryIO) -> None:
    """Write one DataFrame as Parquet."""

    import pandas as pd

    if isinstance(value, pd.Series):
        raise MeltSDKError(
            f"table output {name!r} requires a pandas DataFrame, not a"
            " Series; a Series carries part of its data in its index,"
            " so call reset_index() or reset_index(drop=True) first"
        )
    if not isinstance(value, pd.DataFrame):
        raise MeltSDKError(
            f"table output {name!r} requires a pandas DataFrame, not a"
            f" {_describe(value)}"
        )
    value.to_parquet(handle)


def _stage_image(name: str, value: object, handle: BinaryIO) -> None:
    """Write one matplotlib Figure as PNG."""

    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

    if isinstance(value, Axes):
        raise MeltSDKError(
            f"image output {name!r} requires a matplotlib Figure, not an"
            " Axes; pass the figure that holds the axes"
        )
    if not isinstance(value, Figure):
        raise MeltSDKError(
            f"image output {name!r} requires a matplotlib Figure, not a"
            f" {_describe(value)}"
        )
    value.savefig(handle, format="png")


def _stage_text(name: str, value: object, handle: BinaryIO) -> None:
    """Write one string as UTF-8 bytes."""

    if not isinstance(value, str):
        raise MeltSDKError(
            f"text output {name!r} requires a str, not a"
            f" {_describe(value)}; encode bytes in the script itself"
        )
    handle.write(value.encode("utf-8"))


_STAGERS: dict[str, Callable[[str, object, BinaryIO], None]] = {
    TABLE_KIND: _stage_table,
    IMAGE_KIND: _stage_image,
    TEXT_KIND: _stage_text,
}

def _check_table_bytes(data: bytes) -> None:
    """Confirm the written bytes hold the Parquet magic at both ends."""

    if (
        len(data) < 8
        or data[:4] != PARQUET_MAGIC
        or data[-4:] != PARQUET_MAGIC
    ):
        raise MeltSDKError("the Parquet writer produced no readable Parquet file")


def _check_image_bytes(data: bytes) -> None:
    """Confirm the written bytes carry the PNG signature."""

    if not data.startswith(PNG_SIGNATURE):
        raise MeltSDKError("the PNG writer produced no readable PNG file")


def _check_text_bytes(data: bytes) -> None:
    """Text bytes came from one encoded string, so they hold no check."""


_MAGIC_CHECKS: dict[str, Callable[[bytes], None]] = {
    TABLE_KIND: _check_table_bytes,
    IMAGE_KIND: _check_image_bytes,
    TEXT_KIND: _check_text_bytes,
}


def fsync_directory(path: Path) -> None:
    """fsync one directory so a rename inside it is durable.

    This mirrors the storage-layer helper. The SDK cannot import the
    service runtime, because project environments load it too.
    """

    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_value(kind: str, name: str, value: object, directory: Path) -> StagedOutput:
    """Materialize one declared output inside its staging directory.

    The caller has already checked that `name` is declared and not yet
    staged. This function refuses values that do not match the declared
    kind, writes the file through a temporary name, and returns the
    staged record with its measured hash and length.
    """

    if kind not in OUTPUT_KINDS:
        raise MeltSDKError(
            f"output {name!r} declares unknown kind {kind!r}; declared"
            f" kinds are {', '.join(sorted(OUTPUT_KINDS))}"
        )
    suffix, media_type = _KIND_FILES[kind]
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{name}{suffix}"
    staged = directory / f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with open(staged, "wb") as handle:
            _STAGERS[kind](name, value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        data = staged.read_bytes()
        _MAGIC_CHECKS[kind](data)
        os.replace(staged, final)
    finally:
        if staged.exists():
            staged.unlink(missing_ok=True)
    fsync_directory(directory)
    return StagedOutput(
        name=name,
        kind=kind,
        mediaType=media_type,
        path=final,
        byteLength=len(data),
        sha256=sha256_hash(data),
    )


__all__ = [
    "IMAGE_KIND",
    "OUTPUT_KINDS",
    "PARQUET_MAGIC",
    "PNG_SIGNATURE",
    "StagedOutput",
    "TABLE_KIND",
    "TEXT_KIND",
    "fsync_directory",
    "stage_value",
]
