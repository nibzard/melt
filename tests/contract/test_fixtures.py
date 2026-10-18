"""Contract fixture suite (SPEC.md section 18.1, backlog task T004).

The fixtures under ``fixtures/`` are shared between every language
implementation. This module proves the fixture set itself:

- Every valid document and command fixture parses without diagnostics.
- The valid set covers the shapes that SPEC.md section 18.1 requires.
- Every invalid fixture in ``fixtures/invalid/manifest.json`` carries a
  known diagnostic code and a JSON Pointer. The entries that the schema
  layer enforces fail with exactly the expected diagnostic, the entries
  the compiler enforces fail through full compilation against
  ``fixtures/catalog.json`` (backlog task T005), and the entries the
  service enforces fail through a seeded workspace service (backlog task
  T009): each command envelope is applied against the workspace state
  its ``expected`` map addresses.
- The artifact hashes are real: the catalog matches the bytes on disk,
  the bytes regenerate identically, and every document that cites a
  catalog artifact agrees with its byte length and measured schema.
- Canonical document hashes of the valid fixtures are recorded, so other
  languages can compare against them (SPEC.md section 18.1).
"""

from __future__ import annotations

import importlib.util
import json
import struct
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from melt.schema.canonical import document_hash
from melt.schema.commands import (
    MeltCommandError,
    RegistrationContract,
    parse_command,
)
from melt.schema.diagnostics import DIAGNOSTIC_CODES
from melt.schema.document import MeltDocumentError, parse_document
from runtime.compiler import CompileRequest, compile_document, load_catalog
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import WorkspaceService

# ---------------------------------------------------------------------------
# Fixture set constants
# ---------------------------------------------------------------------------

REQUIRED_INVALID_CATEGORIES = frozenset(
    {
        "unknown-fields",
        "duplicate-keys",
        "invalid-ids",
        "unsupported-versions",
        "non-finite-numbers",
        "unsafe-integers",
        "invalid-defaults",
        "wrong-parameter-types",
        "missing-source-versions",
        "missing-controls",
        "renderer-mismatches",
        "layout-errors",
        "competing-writers",
        "incomplete-mappings",
        "invalid-migrations",
        "missing-expected-revisions",
        "registry-mismatches",
        "artifact-schema-drift",
    }
)
"""Every category that SPEC.md section 18.1 and task T004 require."""

REQUIRED_VALID_DOCUMENTS = frozenset(
    {
        "empty-workspace.json",
        "projection-table.json",
        "projection-plot.json",
        "projection-document.json",
        "hidden-panes.json",
        "nullable-parameters.json",
        "sales-full.json",
    }
)
REQUIRED_VALID_COMMANDS = frozenset({"commands/state-migration.json"})

SALES_ARTIFACT = "sha256:42cb3135d0370d5d9b0712adc0835b30c3d48843aa36d0df333851a42afa4598"

# Arrow types that the fixture table uses, in Melt column type names.
ARROW_TO_MELT_TYPES = {
    pa.string(): "string",
    pa.bool_(): "bool",
    pa.int64(): "int64",
    pa.float64(): "float64",
}


def load_builder(repo_root: Path):
    """Load scripts/build_fixture_artifacts.py as a module."""

    path = repo_root / "scripts" / "build_fixture_artifacts.py"
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder(repo_root: Path):
    return load_builder(repo_root)


@pytest.fixture(scope="module")
def catalog(repo_root: Path) -> dict:
    return json.loads(
        (repo_root / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def manifest(repo_root: Path) -> dict:
    return json.loads(
        (repo_root / "fixtures" / "invalid" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture(scope="module")
def compiler_inputs(repo_root: Path):
    """The trusted catalog and action registry for full compilation."""

    return load_catalog(
        (repo_root / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def seeded_service(service_root: Path):
    """A workspace service holding the state the command fixtures address.

    The workspace ``sales-demo`` holds ``parameters/minimum`` at entity
    revision 2 with stored value 0 at state revision 1, which is the
    state every service-enforced command fixture assumes. The service
    runs against the shared isolated root under the Looper resource
    prefix; it needs the storage worker of backlog task T008.
    """

    available, reason = storage_worker_available()
    if not available:
        pytest.skip(reason)
    adapter = PlatformAdapter.with_root(
        service_root / f"fixture-service-{uuid.uuid4().hex[:12]}"
    )
    service = WorkspaceService.open(adapter, "sales-demo")
    try:
        created = service.create_workspace(
            {
                "language": "melt/v0.1",
                "workspaceId": "sales-demo",
                "title": "Sales analysis",
                "sources": {},
                "parameters": {
                    "minimum": {
                        "type": "number",
                        "default": 0,
                        "nullable": False,
                        "constraints": {
                            "minimum": 0,
                            "maximum": 1000000000,
                        },
                    }
                },
                "actions": {},
                "projections": {},
                "bindings": {},
                "layout": {"root": None, "hidden": []},
            },
            "01-seed-create",
        )
        assert created.ok, created.errors
        replaced = service.apply(
            parse_command(
                json.dumps(
                    {
                        "protocolVersion": 1,
                        "workspaceId": "sales-demo",
                        "requestId": "01-seed-replace",
                        "method": "workspace.apply",
                        "expected": {"parameters/minimum": 1},
                        "operations": [
                            {
                                "op": "entity.replace",
                                "collection": "parameters",
                                "id": "minimum",
                                "value": {
                                    "type": "number",
                                    "default": 0,
                                    "nullable": False,
                                    "constraints": {"minimum": 0},
                                },
                            }
                        ],
                    }
                )
            )
        )
        assert replaced.ok, replaced.errors
        assert service.entity_revisions["parameters/minimum"] == 2
        assert service.parameter_state["minimum"].value == 0.0
        yield service
    finally:
        service.close()


@pytest.fixture(scope="module")
def valid_documents(repo_root: Path) -> dict[str, dict]:
    """Every valid document fixture keyed by file name."""

    directory = repo_root / "fixtures" / "valid"
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
        if path.name != "document-hashes.json"
    }


def document_fixture_paths(repo_root: Path) -> list[Path]:
    """Return every document fixture, valid and invalid."""

    paths = list((repo_root / "fixtures" / "valid").glob("*.json"))
    paths = [path for path in paths if path.name != "document-hashes.json"]
    paths.extend(
        path for path in (repo_root / "fixtures" / "invalid").glob("*.json")
        if path.name != "manifest.json"
    )
    return sorted(paths)


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Return the pixel size declared in the PNG header."""

    assert data[:8] == b"\x89PNG\r\n\x1a\n", "fixture is not a PNG"
    assert data[12:16] == b"IHDR", "PNG holds no IHDR chunk first"
    return struct.unpack(">II", data[16:24])


def measured_columns(table: pa.Table) -> list[dict]:
    """Return the Arrow schema as Melt column declarations."""

    columns = []
    for field in table.schema:
        melt_type = ARROW_TO_MELT_TYPES.get(field.type)
        assert melt_type is not None, f"unmapped Arrow type {field.type}"
        columns.append(
            {"id": field.name, "type": melt_type, "nullable": field.nullable}
        )
    return columns


# ---------------------------------------------------------------------------
# Deterministic artifact bytes
# ---------------------------------------------------------------------------


def test_committed_artifacts_match_regeneration(repo_root: Path, builder) -> None:
    """The committed bytes equal what the builder produces today."""

    problems = builder.check_all(repo_root / "fixtures")
    assert problems == []


def test_catalog_matches_artifact_bytes_on_disk(
    repo_root: Path, catalog: dict, builder
) -> None:
    """Every catalog artifact names its file, hash, and size exactly."""

    for artifact, entry in catalog["artifacts"].items():
        path = repo_root / "fixtures" / entry["path"]
        data = path.read_bytes()
        assert builder.sha256_of(data) == artifact, entry["path"]
        assert entry["byteLength"] == len(data), entry["path"]


def test_catalog_measured_schema_matches_the_real_bytes(
    repo_root: Path, catalog: dict
) -> None:
    """The measured schemas in the catalog come from the artifact bytes."""

    table_entry = next(
        entry
        for entry in catalog["artifacts"].values()
        if entry["kind"] == "table"
    )
    path = repo_root / "fixtures" / table_entry["path"]
    table = pq.read_table(pa.BufferReader(pa.py_buffer(path.read_bytes())))
    assert measured_columns(table) == table_entry["schema"]["columns"]
    assert table_entry["schema"]["rowKey"] is None

    image_entry = next(
        entry for entry in catalog["artifacts"].values() if entry["kind"] == "image"
    )
    image_path = repo_root / "fixtures" / image_entry["path"]
    width, height = png_dimensions(image_path.read_bytes())
    assert image_entry["schema"] == {"width": width, "height": height}

    text_entry = next(
        entry for entry in catalog["artifacts"].values() if entry["kind"] == "text"
    )
    text_path = repo_root / "fixtures" / text_entry["path"]
    text_path.read_bytes().decode("utf-8")
    assert text_entry["schema"] == {"encoding": "utf-8"}


def test_documents_agree_with_the_catalog(repo_root: Path, catalog: dict) -> None:
    """Every source version that cites a catalog artifact matches its size."""

    for path in document_fixture_paths(repo_root):
        document = json.loads(path.read_text(encoding="utf-8"))
        for source_id, source in document.get("sources", {}).items():
            for version_key, version in source["versions"].items():
                entry = catalog["artifacts"].get(version["artifact"])
                if entry is None:
                    continue
                assert version["byteLength"] == entry["byteLength"], (
                    f"{path.name}: /sources/{source_id}/versions/{version_key}"
                    " disagrees with the catalog byte length"
                )


def test_sales_fixture_declares_the_measured_schema(
    repo_root: Path, valid_documents: dict, catalog: dict
) -> None:
    """The full sales example declares the schema the bytes carry."""

    document = valid_documents["sales-full.json"]
    declared = document["sources"]["sales-input"]["versions"]["1"]
    assert declared["artifact"] == SALES_ARTIFACT
    path = repo_root / "fixtures" / "sales" / "sales.parquet"
    table = pq.read_table(pa.BufferReader(pa.py_buffer(path.read_bytes())))
    assert measured_columns(table) == declared["schema"]["columns"]
    assert declared["schema"] == catalog["artifacts"][SALES_ARTIFACT]["schema"]
    assert table.num_rows == 3


# ---------------------------------------------------------------------------
# Valid fixtures
# ---------------------------------------------------------------------------


def test_valid_documents_parse_without_diagnostics(valid_documents: dict) -> None:
    assert valid_documents, "fixtures/valid holds no documents"
    for name, document in valid_documents.items():
        parse_document(json.dumps(document))


def test_valid_commands_parse_without_diagnostics(repo_root: Path) -> None:
    directory = repo_root / "fixtures" / "valid" / "commands"
    paths = sorted(directory.glob("*.json"))
    assert paths, "fixtures/valid/commands holds no envelopes"
    for path in paths:
        parse_command(path.read_text(encoding="utf-8"))


def test_valid_set_covers_the_required_shapes(
    repo_root: Path, valid_documents: dict
) -> None:
    assert REQUIRED_VALID_DOCUMENTS <= set(valid_documents)
    commands = repo_root / "fixtures" / "valid" / "commands"
    present = {f"commands/{path.name}" for path in commands.glob("*.json")}
    assert REQUIRED_VALID_COMMANDS <= present


def test_recorded_document_hashes_are_current(valid_documents: dict) -> None:
    """document-hashes.json lists every valid document with its hash."""

    hash_file = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "valid"
        / "document-hashes.json"
    )
    recorded = json.loads(hash_file.read_text(encoding="utf-8"))["documents"]
    assert set(recorded) == set(valid_documents)
    for name, document in valid_documents.items():
        assert recorded[name] == document_hash(parse_document(json.dumps(document))), name


# ---------------------------------------------------------------------------
# Invalid fixtures
# ---------------------------------------------------------------------------


def test_manifest_covers_every_required_category(manifest: dict) -> None:
    categories = {entry["category"] for entry in manifest["fixtures"]}
    assert REQUIRED_INVALID_CATEGORIES <= categories


def test_manifest_entries_are_well_formed(repo_root: Path, manifest: dict) -> None:
    invalid_root = repo_root / "fixtures" / "invalid"
    listed: set[Path] = set()
    for entry in manifest["fixtures"]:
        path = invalid_root / entry["file"]
        assert path.is_file(), entry["file"]
        listed.add(path)
        assert entry["kind"] in {"document", "command"}, entry["file"]
        assert entry["enforcedBy"] in {"schema", "compiler", "service"}, entry["file"]
        assert entry["code"] in DIAGNOSTIC_CODES, entry["file"]
        assert entry["path"].startswith("/"), entry["file"]
        if entry["enforcedBy"] != "schema":
            assert entry.get("ownerTask"), entry["file"]
        if entry["enforcedBy"] == "schema":
            assert not entry.get("ownerTask"), entry["file"]
    orphans = {
        path
        for path in invalid_root.rglob("*.json")
        if path.name != "manifest.json" and path not in listed
    }
    assert not orphans, "fixtures absent from the manifest"


def pytest_generate_tests(metafunc) -> None:
    """Parametrize the invalid-fixture tests over manifest entries."""

    if "invalid_entry" not in metafunc.fixturenames:
        return
    manifest_path = (
        Path(__file__).resolve().parents[2] / "fixtures" / "invalid" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metafunc.parametrize(
        "invalid_entry",
        manifest["fixtures"],
        ids=lambda entry: Path(entry["file"]).stem,
    )


def read_fixture(entry: dict) -> str:
    root = Path(__file__).resolve().parents[2] / "fixtures" / "invalid"
    return (root / entry["file"]).read_text(encoding="utf-8")


def test_invalid_fixture_fails_with_the_expected_diagnostic(
    invalid_entry: dict, compiler_inputs, request
) -> None:
    """Schema entries fail at parse time; compiler entries fail at
    compilation against the catalog; service entries fail when the
    workspace service applies them to the state they address."""

    text = read_fixture(invalid_entry)
    if invalid_entry["enforcedBy"] == "compiler":
        source_catalog, action_registry = compiler_inputs
        result = compile_document(
            CompileRequest(
                document=text,
                source_catalog=source_catalog,
                action_registry=action_registry,
            )
        )
        assert not result.ok, invalid_entry["file"]
        pairs = [
            (diagnostic.code, diagnostic.path) for diagnostic in result.diagnostics
        ]
    elif invalid_entry["enforcedBy"] == "service":
        # Requested lazily so the entries the schema and compiler layers
        # enforce keep running where no storage worker is available.
        seeded_service = request.getfixturevalue("seeded_service")
        response = seeded_service.apply(parse_command(text))
        assert response.ok is False, invalid_entry["file"]
        pairs = [(error.code, error.path) for error in response.errors]
    elif invalid_entry["kind"] == "document":
        with pytest.raises(MeltDocumentError) as raised:
            parse_document(text)
        pairs = [
            (diagnostic.code, diagnostic.path) for diagnostic in raised.value.diagnostics
        ]
    else:
        with pytest.raises(MeltCommandError) as raised:
            parse_command(text)
        pairs = [
            (diagnostic.code, diagnostic.path) for diagnostic in raised.value.diagnostics
        ]
    expected = (invalid_entry["code"], invalid_entry["path"])
    assert expected in pairs, pairs
    if invalid_entry.get("exclusive", True):
        assert pairs == [expected], pairs


def test_pending_fixture_stays_well_formed_today(invalid_entry: dict) -> None:
    """Compiler and service entries are rejected later, not at decode time."""

    if invalid_entry["enforcedBy"] == "schema":
        pytest.skip("enforced by the schema layer today")
    text = read_fixture(invalid_entry)
    if invalid_entry["kind"] == "document":
        parse_document(text)
    else:
        parse_command(text)


def test_registry_mismatch_fixture_drifts_from_the_catalog(
    repo_root: Path, catalog: dict
) -> None:
    """The action maps a parameter the registration never declared."""

    path = repo_root / "fixtures" / "invalid" / "registry-mismatch.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    registration = catalog["registrations"]["sales-script-v1"]
    contract = RegistrationContract.model_validate(
        {key: value for key, value in registration.items() if key != "revision"}
    )
    action = document["actions"]["analyze-sales"]
    mapped = set(action["parameters"])
    declared = set(contract.parameters)
    assert mapped - declared == {"maximum"}
    assert set(action["outputs"]) == set(contract.outputs)
    assert set(action["inputs"]) == set(contract.inputs)


def test_sales_registration_satisfies_the_sales_example(
    valid_documents: dict, catalog: dict
) -> None:
    """The sales example maps exactly what sales-script-v1 declares."""

    registration = catalog["registrations"]["sales-script-v1"]
    contract = RegistrationContract.model_validate(
        {key: value for key, value in registration.items() if key != "revision"}
    )
    action = valid_documents["sales-full.json"]["actions"]["analyze-sales"]
    assert set(action["parameters"]) == set(contract.parameters)
    assert set(action["inputs"]) == set(contract.inputs)
    assert set(action["outputs"]) == set(contract.outputs)


def test_artifact_schema_drift_fixture_differs_from_the_bytes(
    repo_root: Path, catalog: dict
) -> None:
    """The declared int64 column disagrees with the measured float64."""

    path = repo_root / "fixtures" / "invalid" / "artifact-schema-drift.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    declared = document["sources"]["sales-input"]["versions"]["1"]["schema"]
    measured = catalog["artifacts"][SALES_ARTIFACT]["schema"]
    assert declared["columns"] != measured["columns"]
    declared_revenue = next(
        column for column in declared["columns"] if column["id"] == "revenue"
    )
    measured_revenue = next(
        column for column in measured["columns"] if column["id"] == "revenue"
    )
    assert declared_revenue["type"] == "int64"
    assert measured_revenue["type"] == "float64"
