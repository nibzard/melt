"""Artifact registration and source import (SPEC.md sections 5 and 12.6).

The service alone registers artifacts. Before any source version
exists, it verifies the bytes the host captured: the stored file still
hashes to its content hash, its size matches, and its kind, media type,
and schema measure up against the actual bytes:

- a table artifact is a readable Parquet file whose column types map to
  the Melt column vocabulary;
- an image artifact is a PNG whose dimensions fit the limit table;
- a text artifact is valid UTF-8;
- a graph artifact is one JSON object whose nodes and edges satisfy the
  minimal graph contract, and its measured schema is the identity map
  itself (RFC 001 section 7.1).

The import adapter maps original Parquet column labels to entity-ID
column identifiers and retains each original label that differs
(SPEC.md section 5.1).

Source versions are immutable and append-only. Import builds one
version with ``{"kind":"import","label":...}`` provenance; only the
publication path of section 13.4 may attach run provenance, through
:func:`run_version`, which no document command can reach.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Mapping

from melt.schema.canonical import model_to_wire, parse_json, sha256_hash
from melt.schema.diagnostics import Diagnostic, DiagnosticCode, MeltSchemaError
from melt.schema.document import (
    SOURCE_KIND_MEDIA_TYPES,
    ImageSchema,
    TableSchema,
    TextSchema,
)
from melt.schema.limits import DEFAULT_LIMITS
from melt.schema.semantic import (
    GRAPH_MEDIA_TYPE,
    GraphSchema,
    SEMANTIC_SOURCE_KIND_MEDIA_TYPES,
)
from runtime.storage.artifacts import ArtifactStore
from runtime.workspace.tokens import CapturedArtifact

SOURCE_KINDS_MEDIA_TYPES: dict[str, frozenset[str]] = {
    **SOURCE_KIND_MEDIA_TYPES,
    **SEMANTIC_SOURCE_KIND_MEDIA_TYPES,
}
"""The media types each source kind accepts, graph included."""

IMPORT_KINDS = frozenset(SOURCE_KINDS_MEDIA_TYPES)
"""The capture kinds a source import accepts (`script` is excluded)."""

ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}\Z")
"""Column identifiers use the entity identifier syntax (section 5.1)."""

_ID_INVALID = re.compile(r"[^a-z0-9_-]+")
_ID_COLLAPSE = re.compile(r"_+")

_LABEL_MAX = 256


class MeltImportError(MeltSchemaError):
    """Raised when an artifact cannot be registered as a source version."""


@dataclass(frozen=True)
class MeasuredArtifact:
    """One artifact as the service verified and measured it."""

    artifact: str
    kind: str
    mediaType: str
    byteLength: int
    schema: dict

    def version(self, origin: Mapping[str, Any]) -> dict:
        """Return the source version wire entry of this measurement."""

        return {
            "artifact": self.artifact,
            "mediaType": self.mediaType,
            "byteLength": self.byteLength,
            "schema": self.schema,
            "origin": dict(origin),
        }


def _fail(
    message: str,
    path: str = "/payload/bytes",
    hint: str = "",
    code: str = DiagnosticCode.ARTIFACT,
) -> None:
    raise MeltImportError(
        (
            Diagnostic(
                code=code,
                path=path,
                message=message,
                hint=hint,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Measurement of each source kind (SPEC.md section 5.1)
# ---------------------------------------------------------------------------


def _column_identifier(name: str, used: set[str]) -> str:
    """Return one entity-ID column identifier for an original label.

    A label that already is a valid, unused identifier passes through.
    Any other label is lowercased, reduced to the identifier alphabet,
    and prefixed or suffixed until it is unique; the original label
    stays available for retention.
    """

    if ENTITY_ID_PATTERN.match(name) and name not in used:
        return name
    base = _ID_COLLAPSE.sub("_", _ID_INVALID.sub("_", name.lower())).strip("_")
    if not base:
        base = "column"
    if not base[0].isalpha():
        base = "col_" + base
    base = base[:64].rstrip("_") or "column"
    candidate, attempt = base, 2
    while candidate in used:
        suffix = f"_{attempt}"
        candidate = base[: 64 - len(suffix)] + suffix
        attempt += 1
    return candidate


def _table_column_type(arrow_type: Any) -> "tuple[str, int | None, int | None]":
    """Map one Arrow type to a Melt column type.

    Returns the type name with the decimal precision and scale, or
    raises through the caller for a type Melt tables cannot carry.
    """

    import pyarrow as pa
    from pyarrow import types

    if types.is_boolean(arrow_type):
        return ("bool", None, None)
    if types.is_string(arrow_type) or types.is_large_string(arrow_type):
        return ("string", None, None)
    if types.is_integer(arrow_type):
        if types.is_unsigned_integer(arrow_type) and arrow_type.bit_width == 64:
            _fail(
                "an unsigned 64-bit column cannot map to the int64 column"
                " type; Melt table integers stay inside signed 64 bits",
            )
        return ("int64", None, None)
    if types.is_floating(arrow_type):
        return ("float64", None, None)
    if types.is_decimal(arrow_type):
        return ("decimal", arrow_type.precision, arrow_type.scale)
    if types.is_date(arrow_type):
        return ("date", None, None)
    if types.is_timestamp(arrow_type):
        return ("timestamp", None, None)
    _fail(
        f"column type {str(arrow_type)!r} maps to no Melt column type;"
        " supported types are string, bool, int64, float64, decimal,"
        " date, and timestamp",
        hint="Cast the column before import, or drop it from the export.",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _measure_table(payload: bytes) -> dict:
    """Measure the table schema of one Parquet artifact."""

    import pyarrow.parquet as pq

    try:
        reader = pq.ParquetFile(io.BytesIO(payload))
        arrow_schema = reader.schema_arrow
    except Exception as error:
        _fail(
            f"bytes are not a readable Parquet file: {error}",
            hint="Capture the artifact with the parquet media type only"
            " when the bytes are a complete Parquet file.",
        )
    columns: list[dict] = []
    used: set[str] = set()
    for field in arrow_schema:
        name = field.name
        if not isinstance(name, str) or not name:
            _fail("every Parquet column needs a non-empty name")
        if len(name) > _LABEL_MAX:
            _fail(
                f"column name of {len(name)} characters cannot be retained"
                f" as a label; labels hold at most {_LABEL_MAX} characters",
            )
        identifier = _column_identifier(name, used)
        used.add(identifier)
        type_name, precision, scale = _table_column_type(field.type)
        column: dict[str, Any] = {
            "id": identifier,
            "type": type_name,
            "nullable": bool(field.nullable),
        }
        if identifier != name:
            column["label"] = name
        if precision is not None:
            column["precision"] = precision
            column["scale"] = scale
        columns.append(column)
    if not columns:
        _fail("a table artifact requires at least one column")
    try:
        schema = TableSchema.model_validate({"columns": columns, "rowKey": None})
    except Exception:
        # The column mapping above only produces shapes the model takes;
        # a decimal precision above 38 is the one reachable case.
        _fail(
            "measured columns do not fit the Melt table schema; decimal"
            " precision stays between 1 and 38",
        )
    return model_to_wire(schema)


def _measure_image(payload: bytes) -> dict:
    """Measure the dimensions of one PNG artifact."""

    if len(payload) < 24 or payload[12:16] != b"IHDR":
        _fail("png bytes carry no IHDR header chunk")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    limits = DEFAULT_LIMITS
    if width > limits.png_max_axis or height > limits.png_max_axis:
        _fail(
            "png axis length exceeds the allowed bound of"
            f" {limits.png_max_axis} per axis",
            hint="Import a smaller image; the png-dimensions limit is"
            " service policy, not a field project code can raise"
            " (SPEC.md section 15.2).",
            code=DiagnosticCode.LIMIT,
        )
    if width * height > limits.png_max_total_pixels:
        _fail(
            "png total pixels exceed the allowed bound of"
            f" {limits.png_max_total_pixels}",
            hint="Import a smaller image; the png-dimensions limit is"
            " service policy, not a field project code can raise"
            " (SPEC.md section 15.2).",
            code=DiagnosticCode.LIMIT,
        )
    try:
        schema = ImageSchema.model_validate({"width": width, "height": height})
    except Exception:
        _fail(
            f"png dimensions {width}x{height} do not fit the image schema",
        )
    return model_to_wire(schema)


def _measure_text(payload: bytes) -> dict:
    """Confirm one text artifact decodes as UTF-8."""

    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(f"text bytes are not valid UTF-8: {error}")
    return model_to_wire(TextSchema.model_validate({"encoding": "utf-8"}))


def _measure_graph(payload: bytes) -> dict:
    """Measure the identity map of one graph artifact (RFC 001 7.1).

    The measured schema is the validated identity map itself, stored
    with the source version, so node identities never depend on labels
    or element order.
    """

    try:
        value = parse_json(payload)
    except MeltSchemaError as error:
        _fail(
            "graph bytes are not one JSON document: " + error.diagnostics[0].message,
        )
    try:
        return model_to_wire(GraphSchema.model_validate(value))
    except Exception:
        _fail(
            "graph bytes do not satisfy the minimal graph contract: nodes"
            " with stable ids, labels, and semantic references, and edges"
            " between existing nodes",
        )


_MEASURERS = {
    "table": _measure_table,
    "image": _measure_image,
    "text": _measure_text,
    "graph": _measure_graph,
}


def measure_artifact(
    payload: bytes, *, kind: str, media_type: str
) -> dict:
    """Measure the source schema of verified artifact bytes."""

    allowed = SOURCE_KINDS_MEDIA_TYPES.get(kind)
    if allowed is None or media_type not in allowed:
        _fail(
            f"kind {kind!r} with media type {media_type!r} matches no"
            " source kind and media type pair",
            path="/payload/mediaType",
        )
    return _MEASURERS[kind](payload)


def register_artifact(
    artifacts: ArtifactStore, captured: CapturedArtifact
) -> MeasuredArtifact:
    """Verify one captured artifact against its stored bytes and measure it.

    The service runs this before any source version exists (SPEC.md
    section 5.1): the stored file must still hash to the captured
    reference, its size must match, and its bytes must measure up to the
    captured kind and media type.
    """

    if captured.kind not in IMPORT_KINDS:
        _fail(
            f"a {captured.kind!r} capture cannot import a source; source"
            " kinds are table, image, text, and graph",
            path="/payload/artifactToken",
        )
    try:
        payload = artifacts.read(captured.artifact)
    except OSError as error:
        _fail(
            f"captured artifact {captured.artifact} is unreadable: {error}",
            path="/payload/artifactToken",
            hint="Capture the artifact again; a restart withdraws every"
            " outstanding token.",
        )
    digest = sha256_hash(payload)
    if digest != captured.artifact:
        _fail(
            f"stored bytes hash to {digest}, not to the captured"
            f" reference {captured.artifact}",
        )
    if len(payload) != captured.byteLength:
        _fail(
            f"stored bytes hold {len(payload)} bytes, but the capture"
            f" recorded {captured.byteLength}",
        )
    schema = measure_artifact(
        payload, kind=captured.kind, media_type=captured.mediaType
    )
    return MeasuredArtifact(
        artifact=captured.artifact,
        kind=captured.kind,
        mediaType=captured.mediaType,
        byteLength=captured.byteLength,
        schema=schema,
    )


def import_version(measured: MeasuredArtifact, label: str) -> dict:
    """Return one import-provenance source version (SPEC.md section 5.1)."""

    return measured.version({"kind": "import", "label": label})


def run_version(
    measured: MeasuredArtifact, run_id: str, output: str
) -> dict:
    """Return one run-provenance source version.

    Run provenance is attached by the service alone, inside the
    publication transaction of SPEC.md section 13.4. No document command
    reaches this function: the mutation operations address no source
    collection, and ``workspace.create`` requires empty sources.
    """

    return measured.version({"kind": "run", "runId": run_id, "output": output})


# ---------------------------------------------------------------------------
# Append-only source versions (SPEC.md section 5.2)
# ---------------------------------------------------------------------------


def next_version_number(source: Mapping[str, Any]) -> int:
    """Return the next version number of one existing source."""

    return max(int(key) for key in source.get("versions", {})) + 1


def append_source_version(
    document: Mapping[str, Any],
    source_id: str,
    measured: MeasuredArtifact,
    version: Mapping[str, Any],
    *,
    advance_current: bool = True,
) -> "tuple[dict, int]":
    """Return the document with one new immutable version appended.

    The input document is left untouched. A new logical source starts at
    version 1 with the measured kind; an existing source keeps every
    earlier version byte for byte and advances ``currentVersion`` to the
    appended version. An existing source also refuses a version of
    another kind: the kind of a logical source is fixed at creation.

    ``advance_current=False`` appends one historical version: the
    appended version stays inspectable through its number while
    ``currentVersion`` keeps naming the version the last current
    publication produced. Publication uses this for a run that verified
    but may not become current, so an obsolete result never overwrites
    the current reference of its source (SPEC.md section 13.3). A new
    source still starts at ``currentVersion`` 1, because the field must
    refer to an included version.
    """

    import copy

    sources = copy.deepcopy(dict(document.get("sources", {})))
    existing = sources.get(source_id)
    if existing is None:
        number = 1
        sources[source_id] = {
            "kind": measured.kind,
            "currentVersion": number,
            "versions": {str(number): dict(version)},
        }
    else:
        if existing.get("kind") != measured.kind:
            _fail(
                f"source {source_id!r} holds kind {existing.get('kind')!r}"
                f" but the capture measures {measured.kind!r}; the kind of"
                " a logical source is fixed at creation",
            )
        number = next_version_number(existing)
        versions = dict(existing.get("versions", {}))
        versions[str(number)] = dict(version)
        sources[source_id] = {
            "kind": existing["kind"],
            "currentVersion": (
                existing.get("currentVersion", number)
                if not advance_current
                else number
            ),
            "versions": versions,
        }
    candidate = copy.deepcopy(dict(document))
    candidate["sources"] = sources
    return candidate, number


__all__ = [
    "IMPORT_KINDS",
    "MeasuredArtifact",
    "MeltImportError",
    "append_source_version",
    "import_version",
    "measure_artifact",
    "next_version_number",
    "register_artifact",
    "run_version",
]
