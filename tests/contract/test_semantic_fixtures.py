"""The isolated fixture set of `melt/semantic-draft-1` (T025).

The fixtures under `fixtures/semantic/` stay outside the v0.1 contract
suite on purpose (RFC 001 section 1). This module proves them:

- The committed supplier artifact is real: the catalog entry matches
  the bytes on disk, and regeneration is byte-identical.
- The valid workspace document parses and fully compiles against the
  semantic catalog.
- Every invalid fixture in the manifest fails with exactly its
  recorded diagnostic code and JSON Pointer.
- The v0.1 suites never scan this directory: the v0.1 fixture set is
  unchanged by the extension.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from melt.schema.canonical import document_hash
from melt.schema.document import MeltDocumentError, parse_document
from melt.schema.semantic import (
    SEMANTIC_LANGUAGE_ID,
    parse_document_for_language,
    parse_semantic_document,
)
from runtime.compiler import CompileRequest, compile_document, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
SEMANTIC_DIR = REPO_ROOT / "fixtures" / "semantic"

SUPPLIER_ARTIFACT = (
    "sha256:0b03b11a7e1c826a41f3bdc4a877b87487c254110a8fc54692c170c3f2afb99e"
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(
        (SEMANTIC_DIR / "invalid" / "manifest.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def catalog():
    source_catalog, registry = load_catalog(
        (SEMANTIC_DIR / "catalog.json").read_text(encoding="utf-8")
    )
    return source_catalog


def test_committed_supplier_artifact_matches_its_catalog_entry(catalog):
    data = (SEMANTIC_DIR / "suppliers.parquet").read_bytes()
    entry = catalog.entry(SUPPLIER_ARTIFACT)
    assert entry is not None
    assert entry.byteLength == len(data)
    assert "sha256:" + hashlib.sha256(data).hexdigest() == SUPPLIER_ARTIFACT


def test_supplier_artifact_regenerates_identically():
    import importlib.util

    path = REPO_ROOT / "scripts" / "build_fixture_artifacts.py"
    spec = importlib.util.spec_from_file_location("builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = (SEMANTIC_DIR / "suppliers.parquet").read_bytes()
    assert committed == module.suppliers_parquet_bytes()


def test_valid_semantic_fixture_parses_and_compiles(catalog):
    wire = json.loads(
        (SEMANTIC_DIR / "supplier-workspace.json").read_text(encoding="utf-8")
    )
    document = parse_semantic_document(wire)
    assert document.language == SEMANTIC_LANGUAGE_ID
    result = compile_document(
        CompileRequest(document=wire, source_catalog=catalog)
    )
    assert result.ok, result.diagnostics
    assert result.plan.language == SEMANTIC_LANGUAGE_ID
    assert result.plan.state.references["selected_supplier"].key == "supplier_id"


def test_valid_fixture_hash_is_stable(catalog):
    wire = json.loads(
        (SEMANTIC_DIR / "supplier-workspace.json").read_text(encoding="utf-8")
    )
    assert document_hash(parse_document_for_language(wire)) == document_hash(
        parse_semantic_document(wire)
    )


@pytest.mark.parametrize(
    "entry",
    json.loads(
        (SEMANTIC_DIR / "invalid" / "manifest.json").read_text(encoding="utf-8")
    )["fixtures"],
    ids=lambda entry: entry["file"],
)
def test_invalid_fixture_fails_with_the_recorded_diagnostic(entry):
    wire = json.loads(
        (SEMANTIC_DIR / "invalid" / entry["file"]).read_text(encoding="utf-8")
    )
    with pytest.raises(MeltDocumentError) as raised:
        parse_document_for_language(wire)
    recorded = (entry["code"], entry["path"])
    assert recorded in [
        (item.code, item.path) for item in raised.value.diagnostics
    ], f"{entry['file']} must fail with {recorded}"


def test_the_v01_fixture_set_stays_free_of_the_extension():
    """The extension fixtures never leak into the v0.1 directories."""

    for path in (REPO_ROOT / "fixtures" / "valid").glob("*.json"):
        if path.name == "document-hashes.json":
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document.get("language") == "melt/v0.1", path.name
        assert "collections" not in document, path.name
        assert "state" not in document, path.name
        parse_document(document)


def test_the_extension_fixture_declares_the_extension_language():
    """Every top-level fixture is a document or a named non-document.

    The documents all declare the extension language; `catalog.json`
    is the trusted catalog, `*.graph.json` are artifact bytes, the
    list-shaped files (`definitions.json`) are mutation payloads a
    harness applies, not documents of their own, and
    `document-hashes.json` is the canonical hash index the generated
    language models verify (backlog task T007).
    """

    for path in SEMANTIC_DIR.glob("*.json"):
        if (
            path.name == "catalog.json"
            or path.name == "document-hashes.json"
            or path.name.endswith(".graph.json")
        ):
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            assert all(isinstance(item, dict) for item in value), path.name
            continue
        assert value["language"] == SEMANTIC_LANGUAGE_ID, path.name


# ---------------------------------------------------------------------------
# The command line (SPEC.md section 11.3)
# ---------------------------------------------------------------------------


def test_validate_accepts_the_extension_document(capsys):
    from melt.cli import EXIT_OK, main

    code = main(["validate", str(SEMANTIC_DIR / "supplier-workspace.json")])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "valid" in captured.out


def test_compile_fully_resolves_the_extension_document(capsys):
    from melt.cli import EXIT_OK, main

    code = main(
        [
            "compile",
            str(SEMANTIC_DIR / "supplier-workspace.json"),
            "--catalog",
            str(SEMANTIC_DIR / "catalog.json"),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["plan"]["language"] == SEMANTIC_LANGUAGE_ID
    assert result["plan"]["state"]["references"]["selected_supplier"]


def test_validate_rejects_a_v01_document_with_extension_fields(capsys):
    from melt.cli import EXIT_INVALID, main

    fixture = SEMANTIC_DIR / "invalid" / "v01-document-with-collections.json"
    code = main(["validate", str(fixture)])
    captured = capsys.readouterr()
    assert code == EXIT_INVALID
    assert "E_SCHEMA /collections" in captured.err
