"""Artifact measurement before registration (T011, SPEC.md section 5.1).

The service measures the bytes it captured before any source version
exists. These tests drive the measurement layer directly, with no
storage worker: an ``ArtifactStore`` over a temporary directory is the
whole fixture.

- A Parquet artifact measures its columns: Melt types, nullability,
  decimal precision and scale, and original labels retained when the
  mapped identifier differs.
- A PNG artifact measures its dimensions and enforces the png-dimension
  limits with ``E_LIMIT``.
- A text artifact confirms UTF-8 and answers the text schema.
- ``register_artifact`` rereads the stored bytes and verifies their
  content hash and size before measuring anything.
"""

from __future__ import annotations

import importlib.util
import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from melt.schema.diagnostics import MeltSchemaError
from runtime.storage.artifacts import ArtifactStore
from runtime.workspace import (
    MeasuredArtifact,
    MeltImportError,
    measure_artifact,
    register_artifact,
    run_version,
)
from runtime.workspace.tokens import CapturedArtifact, TokenStore

PARQUET = "application/vnd.apache.parquet"
PNG = "image/png"
PLAIN = "text/plain"
MARKDOWN = "text/markdown"

NOW = "2026-10-18T12:00:00Z"


def _load_builder():
    """Load scripts/build_fixture_artifacts.py as a module."""

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def parquet_bytes(schema_fields, row) -> bytes:
    """Return the Parquet encoding of one row under a given schema."""

    schema = pa.schema(schema_fields)
    arrays = [
        pa.array([None if field.nullable else row[field.name]], type=field.type)
        for field in schema
    ]
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), sink)
    return sink.getvalue().to_pybytes()


def captured(store: ArtifactStore, data: bytes, media_type: str) -> CapturedArtifact:
    """Capture bytes through the token store and return the capture."""

    return TokenStore("sales-demo", store).capture(data, media_type)


def import_error(call) -> MeltImportError:
    with pytest.raises(MeltImportError) as error:
        call()
    return error.value


# ---------------------------------------------------------------------------
# Table measurement
# ---------------------------------------------------------------------------


def test_table_measurement_maps_types_and_nullability():
    payload = parquet_bytes(
        [
            pa.field("month", pa.string(), nullable=False),
            pa.field("revenue", pa.float64(), nullable=False),
        ],
        {"month": "2026-01", "revenue": 100.0},
    )
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    assert schema == {
        "columns": [
            {
                "id": "month",
                "type": "string",
                "nullable": False,
                "label": None,
                "precision": None,
                "scale": None,
            },
            {
                "id": "revenue",
                "type": "float64",
                "nullable": False,
                "label": None,
                "precision": None,
                "scale": None,
            },
        ],
        "rowKey": None,
    }


def test_table_measurement_covers_every_column_type():
    from decimal import Decimal

    payload = parquet_bytes(
        [
            pa.field("name", pa.string(), nullable=False),
            pa.field("qty", pa.int32(), nullable=False),
            pa.field("big", pa.int64(), nullable=True),
            pa.field("active", pa.bool_(), nullable=False),
            pa.field("ratio", pa.float32(), nullable=True),
            pa.field("price", pa.decimal128(12, 2), nullable=False),
            pa.field("day", pa.date32(), nullable=False),
            pa.field("seen_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ],
        {
            "name": "x",
            "qty": 2,
            "big": None,
            "active": True,
            "ratio": None,
            "price": Decimal("12.34"),
            "day": 0,
            "seen_at": 0,
        },
    )
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    columns = {column["id"]: column for column in schema["columns"]}
    assert columns["qty"]["type"] == "int64"
    assert columns["big"] == {
        "id": "big",
        "type": "int64",
        "nullable": True,
        "label": None,
        "precision": None,
        "scale": None,
    }
    assert columns["active"]["type"] == "bool"
    assert columns["ratio"]["type"] == "float64"
    assert columns["price"]["type"] == "decimal"
    assert columns["price"]["precision"] == 12
    assert columns["price"]["scale"] == 2
    assert columns["day"]["type"] == "date"
    assert columns["seen_at"]["type"] == "timestamp"


def test_column_labels_map_to_identifier_ids_and_are_retained():
    payload = parquet_bytes(
        [
            pa.field("Revenue (USD)", pa.string(), nullable=False),
            pa.field("2month", pa.string(), nullable=False),
            pa.field("Units  Shipped", pa.string(), nullable=False),
        ],
        {"Revenue (USD)": "a", "2month": "b", "Units  Shipped": "c"},
    )
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    assert [(column["id"], column["label"]) for column in schema["columns"]] == [
        ("revenue_usd", "Revenue (USD)"),
        ("col_2month", "2month"),
        ("units_shipped", "Units  Shipped"),
    ]


def test_mapped_identifiers_stay_unique_and_bounded():
    # Both labels lower-case to the same base, so the second gains a
    # suffix; the 70-character labels truncate to the 64-character cap.
    payload = parquet_bytes(
        [
            pa.field("A" * 70, pa.string(), nullable=False),
            pa.field("a" * 70 + "b", pa.string(), nullable=False),
        ],
        {"A" * 70: "a", "a" * 70 + "b": "b"},
    )
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    identifiers = [column["id"] for column in schema["columns"]]
    assert len(set(identifiers)) == 2
    assert all(len(identifier) <= 64 for identifier in identifiers)
    assert identifiers[0] == "a" * 64
    assert identifiers[1] == "a" * 62 + "_2"


def test_table_measurement_rejects_empty_column_names():
    payload = parquet_bytes(
        [pa.field("", pa.string(), nullable=False)], {"": "x"}
    )
    error = import_error(
        lambda: measure_artifact(payload, kind="table", media_type=PARQUET)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "non-empty name" in error.diagnostics[0].message


def test_table_measurement_rejects_unmappable_columns():
    from decimal import Decimal

    cases = {
        "an unsigned 64-bit integer": (
            [pa.field("n", pa.uint64(), nullable=False)],
            {"n": 0},
        ),
        "a binary column": (
            [pa.field("blob", pa.binary(), nullable=False)],
            {"blob": b""},
        ),
        "a list column": (
            [pa.field("tags", pa.list_(pa.string()), nullable=False)],
            {"tags": []},
        ),
        "an over-precise decimal": (
            [pa.field("d", pa.decimal256(50, 2), nullable=False)],
            {"d": Decimal("12.34")},
        ),
    }
    for label, (fields, row) in cases.items():
        payload = parquet_bytes(fields, row)
        error = import_error(
            lambda: measure_artifact(payload, kind="table", media_type=PARQUET)
        )
        assert error.diagnostics[0].code == "E_ARTIFACT", label
        assert error.diagnostics[0].path == "/payload/bytes"


def test_table_measurement_rejects_unreadable_parquet():
    # The capture guard accepts PAR1...PAR1 bytes; measurement reads on.
    payload = b"PAR1" + b"corrupt payload" + b"PAR1"
    error = import_error(
        lambda: measure_artifact(payload, kind="table", media_type=PARQUET)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "Parquet" in error.diagnostics[0].message


def test_column_name_longer_than_a_label_is_refused():
    payload = parquet_bytes(
        [pa.field("n" * 300, pa.string(), nullable=False)], {"n" * 300: "x"}
    )
    error = import_error(
        lambda: measure_artifact(payload, kind="table", media_type=PARQUET)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "label" in error.diagnostics[0].message


# ---------------------------------------------------------------------------
# Image and text measurement
# ---------------------------------------------------------------------------


def png_bytes(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return length + tag + data + crc

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def test_image_measurement_reads_png_dimensions():
    payload = BUILDER.revenue_png_bytes()
    schema = measure_artifact(payload, kind="image", media_type=PNG)
    assert schema == {"width": 64, "height": 48}


def test_image_measurement_enforces_png_limits():
    error = import_error(
        lambda: measure_artifact(png_bytes(20000, 4), kind="image", media_type=PNG)
    )
    assert error.diagnostics[0].code == "E_LIMIT"
    assert "16384" in error.diagnostics[0].message

    error = import_error(
        lambda: measure_artifact(png_bytes(10000, 9000), kind="image", media_type=PNG)
    )
    assert error.diagnostics[0].code == "E_LIMIT"
    assert "64000000" in error.diagnostics[0].message

    # A zero dimension carries no limit message; it is not a PNG image.
    error = import_error(
        lambda: measure_artifact(png_bytes(0, 4), kind="image", media_type=PNG)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"


def test_image_measurement_requires_an_ihdr_chunk():
    payload = b"\x89PNG\r\n\x1a\n" + b"junk that is not a png"
    error = import_error(
        lambda: measure_artifact(payload, kind="image", media_type=PNG)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "IHDR" in error.diagnostics[0].message


def test_text_measurement_confirms_utf_8():
    for media_type in (PLAIN, MARKDOWN):
        schema = measure_artifact(
            "revenue notes\n".encode("utf-8"), kind="text", media_type=media_type
        )
        assert schema == {"encoding": "utf-8"}
    error = import_error(
        lambda: measure_artifact(b"revenue \xff notes", kind="text", media_type=PLAIN)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "UTF-8" in error.diagnostics[0].message


def test_measurement_rejects_kind_and_media_type_mismatches():
    payload = BUILDER.revenue_png_bytes()
    error = import_error(
        lambda: measure_artifact(payload, kind="image", media_type=PARQUET)
    )
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert error.diagnostics[0].path == "/payload/mediaType"


# ---------------------------------------------------------------------------
# Registration verification
# ---------------------------------------------------------------------------


def test_register_artifact_verifies_hash_size_and_schema(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    data = BUILDER.sales_parquet_bytes()
    measured = register_artifact(store, captured(store, data, PARQUET))
    assert isinstance(measured, MeasuredArtifact)
    assert measured.kind == "table"
    assert measured.mediaType == PARQUET
    assert measured.byteLength == len(data)
    assert measured.schema["columns"][0]["id"] == "month"
    # The import version carries exactly the verified facts.
    version = measured.version({"kind": "import", "label": "sales.csv"})
    assert version == {
        "artifact": measured.artifact,
        "mediaType": PARQUET,
        "byteLength": len(data),
        "schema": measured.schema,
        "origin": {"kind": "import", "label": "sales.csv"},
    }


def test_register_artifact_rejects_tampered_bytes(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    capture = captured(store, BUILDER.sales_parquet_bytes(), PARQUET)
    # Overwrite the stored file under its hash name; the hash check
    # refuses the registration instead of measuring foreign bytes.
    # Artifact files are read-only by design, so the tamper first
    # widens the mode.
    stored = store.path_for(capture.artifact)
    stored.chmod(0o644)
    stored.write_bytes(b"PAR1tamperedPAR1")
    error = import_error(lambda: register_artifact(store, capture))
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "hash" in error.diagnostics[0].message


def test_register_artifact_rejects_a_wrong_recorded_length(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    capture = captured(store, BUILDER.sales_parquet_bytes(), PARQUET)
    lying = replace(capture, byteLength=capture.byteLength + 1)
    error = import_error(lambda: register_artifact(store, lying))
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "bytes" in error.diagnostics[0].message


def test_register_artifact_rejects_missing_files_and_scripts(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    capture = captured(store, b"def run(ui):\n    pass\n", "text/x-python")
    error = import_error(lambda: register_artifact(store, capture))
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert error.diagnostics[0].path == "/payload/artifactToken"

    text = captured(store, "notes".encode("utf-8"), PLAIN)
    store.path_for(text.artifact).unlink()
    error = import_error(lambda: register_artifact(store, text))
    assert error.diagnostics[0].code == "E_ARTIFACT"
    assert "unreadable" in error.diagnostics[0].message


def test_run_provenance_is_attached_by_the_service_alone():
    measured = MeasuredArtifact(
        artifact="sha256:" + "1a" * 32,
        kind="table",
        mediaType=PARQUET,
        byteLength=4200,
        schema={"columns": [], "rowKey": None},
    )
    version = run_version(measured, "run-9", "filtered")
    assert version["origin"] == {"kind": "run", "runId": "run-9", "output": "filtered"}
    # The mutation operations address no source collection, so no
    # document command can carry this origin into a document
    # (SPEC.md sections 5.1 and 12.2).
    from melt.schema.commands import EntityCreate

    with pytest.raises(Exception) as error:
        EntityCreate.model_validate(
            {
                "op": "entity.create",
                "collection": "sources",
                "id": "sales-input",
                "value": version,
            }
        )
    assert "sources" in str(error.value)


def test_melt_import_error_is_a_schema_error():
    assert issubclass(MeltImportError, MeltSchemaError)
