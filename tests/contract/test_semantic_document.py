"""Semantic document schema of `melt/semantic-draft-1` (T025).

RFC 001 sections 3 and 4 add two top-level maps to the document:
typed identity indexes over table sources, and nullable reference
state with workspace scope. These tests pin the schema layer:

- The extension document parses under its own identifier and keeps
  every v0.1 field meaning and check.
- A `melt/v0.1` document rejects both fields: the v0.1 model accepts
  no unknown fields, so the extension never leaks into the stable
  language (RFC 001 section 1).
- The collection checks name a table source, a pinned version, and a
  unique non-null string key column.
- The state checks name a declared collection and stay nullable.
- Any other language identifier reports `E_VERSION` at `/language`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from melt.schema.canonical import document_hash
from melt.schema.document import (
    LANGUAGE_ID,
    MeltDocumentError,
    parse_document,
)
from melt.schema.semantic import (
    SEMANTIC_LANGUAGE_ID,
    SemanticDocument,
    SemanticReference,
    document_diagnostics,
    document_model_of,
    parse_document_for_language,
    parse_semantic_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SUPPLIER_ARTIFACT = (
    "sha256:0b03b11a7e1c826a41f3bdc4a877b87487c254110a8fc54692c170c3f2afb99e"
)


def semantic_document(**overrides) -> dict:
    """One valid extension document, ready for mutation in a test."""

    document = {
        "language": SEMANTIC_LANGUAGE_ID,
        "workspaceId": "supplier-demo",
        "title": "Supplier risk",
        "sources": {
            "supplier-records": {
                "kind": "table",
                "currentVersion": 1,
                "versions": {
                    "1": {
                        "artifact": SUPPLIER_ARTIFACT,
                        "mediaType": "application/vnd.apache.parquet",
                        "byteLength": 737,
                        "schema": {
                            "columns": [
                                {
                                    "id": "supplier_id",
                                    "type": "string",
                                    "nullable": False,
                                },
                                {
                                    "id": "name",
                                    "type": "string",
                                    "nullable": False,
                                },
                            ],
                            "rowKey": None,
                        },
                        "origin": {
                            "kind": "import",
                            "label": "suppliers.parquet",
                        },
                    }
                },
            }
        },
        "parameters": {},
        "actions": {},
        "projections": {},
        "bindings": {},
        "collections": {
            "suppliers": {
                "source": {"sourceId": "supplier-records", "version": 1},
                "key": "supplier_id",
            }
        },
        "state": {
            "selected_supplier": {
                "type": {"kind": "ref", "collection": "suppliers"},
                "nullable": True,
                "default": None,
                "scope": "workspace",
            }
        },
        "layout": {"root": None, "hidden": []},
    }
    document.update(overrides)
    return document


def errors_of(call) -> list[tuple[str, str]]:
    with pytest.raises(MeltDocumentError) as raised:
        call()
    return [(item.code, item.path) for item in raised.value.diagnostics]


# ---------------------------------------------------------------------------
# Parsing under the extension identifier
# ---------------------------------------------------------------------------


def test_semantic_document_parses_with_both_extension_maps():
    document = parse_semantic_document(semantic_document())
    assert isinstance(document, SemanticDocument)
    assert document.language == SEMANTIC_LANGUAGE_ID
    assert document.collections["suppliers"].key == "supplier_id"
    assert document.collections["suppliers"].source.version == 1
    entry = document.state["selected_supplier"]
    assert entry.type.kind == "ref"
    assert entry.type.collection == "suppliers"
    assert entry.nullable is True
    assert entry.default is None
    assert entry.scope == "workspace"


def test_extension_maps_are_required_not_optional():
    document = semantic_document()
    del document["collections"]
    assert ("E_SCHEMA", "/collections") in errors_of(
        lambda: parse_semantic_document(document)
    )
    document = semantic_document()
    del document["state"]
    assert ("E_SCHEMA", "/state") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_empty_extension_maps_are_valid():
    document = dict(
        semantic_document(), collections={}, state={}
    )
    parsed = parse_semantic_document(document)
    assert parsed.collections == {}
    assert parsed.state == {}


def test_v01_fields_keep_their_checks_in_the_extension():
    document = semantic_document()
    document["parameters"]["minimum"] = {
        "type": "number",
        "default": 0,
        "nullable": False,
        "constraints": {"minimum": 0, "maximum": 10},
    }
    document["parameters"]["broken"] = {
        "type": "number",
        "default": 99,
        "nullable": False,
        "constraints": {"minimum": 0, "maximum": 10},
    }
    assert ("E_TYPE", "/parameters/broken/default") in errors_of(
        lambda: parse_semantic_document(document)
    )


# ---------------------------------------------------------------------------
# The v0.1 boundary (RFC 001 section 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["collections", "state"])
def test_v01_documents_reject_the_extension_fields(field: str):
    document = semantic_document(language=LANGUAGE_ID)
    assert (field in document) is True
    assert ("E_SCHEMA", f"/{field}") in errors_of(
        lambda: parse_document(document)
    )


def test_extension_maps_are_absent_from_a_v01_document_wire():
    parsed = parse_document(
        {
            "language": LANGUAGE_ID,
            "workspaceId": "empty",
            "title": "Empty",
            "sources": {},
            "parameters": {},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        }
    )
    assert "collections" not in parsed.model_dump()
    assert "state" not in parsed.model_dump()


@pytest.mark.parametrize("language", ["melt/semantic-draft-2", "melt/v0.2"])
def test_unknown_language_reports_version(language: str):
    assert ("E_VERSION", "/language") in errors_of(
        lambda: parse_document_for_language(semantic_document(language=language))
    )


# ---------------------------------------------------------------------------
# Collection checks (RFC 001 section 3.1)
# ---------------------------------------------------------------------------


def test_collection_must_pin_a_table_source():
    document = semantic_document()
    document["sources"]["log"] = {
        "kind": "text",
        "currentVersion": 1,
        "versions": {
            "1": {
                "artifact": "sha256:" + "0c" * 32,
                "mediaType": "text/plain",
                "byteLength": 12,
                "schema": {"encoding": "utf-8"},
                "origin": {"kind": "import", "label": "log.txt"},
            }
        },
    }
    document["collections"]["suppliers"]["source"] = {
        "sourceId": "log",
        "version": 1,
    }
    assert ("E_TYPE", "/collections/suppliers/source") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_collection_must_pin_an_existing_version():
    document = semantic_document()
    document["collections"]["suppliers"]["source"]["version"] = 7
    assert ("E_REFERENCE", "/collections/suppliers/source/version") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_collection_key_must_exist_in_the_pinned_schema():
    document = semantic_document()
    document["collections"]["suppliers"]["key"] = "supplier_code"
    assert ("E_REFERENCE", "/collections/suppliers/key") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_collection_key_must_be_a_string_column():
    document = semantic_document()
    document["sources"]["supplier-records"]["versions"]["1"]["schema"][
        "columns"
    ] = [
        {"id": "supplier_id", "type": "int64", "nullable": False},
        {"id": "name", "type": "string", "nullable": False},
    ]
    assert ("E_TYPE", "/collections/suppliers/key") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_collection_key_must_be_non_nullable():
    document = semantic_document()
    document["sources"]["supplier-records"]["versions"]["1"]["schema"][
        "columns"
    ] = [
        {"id": "supplier_id", "type": "string", "nullable": True},
        {"id": "name", "type": "string", "nullable": False},
    ]
    assert ("E_TYPE", "/collections/suppliers/key") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_collection_count_limit():
    document = semantic_document()
    binding = document["collections"]["suppliers"]
    for index in range(65):
        document["collections"][f"c{index:02d}"] = dict(binding)
    assert ("E_LIMIT", "/collections") in errors_of(
        lambda: parse_semantic_document(document)
    )


# ---------------------------------------------------------------------------
# Reference state checks (RFC 001 section 4)
# ---------------------------------------------------------------------------


def test_state_must_name_a_declared_collection():
    document = semantic_document()
    document["state"]["selected_supplier"]["type"]["collection"] = "products"
    assert (
        "E_REFERENCE",
        "/state/selected_supplier/type/collection",
    ) in errors_of(lambda: parse_semantic_document(document))


def test_reference_state_is_nullable_in_this_extension():
    document = semantic_document()
    document["state"]["selected_supplier"]["nullable"] = False
    assert ("E_TYPE", "/state/selected_supplier/nullable") in errors_of(
        lambda: parse_semantic_document(document)
    )


def test_reference_state_default_must_be_null():
    document = semantic_document()
    document["state"]["selected_supplier"]["default"] = {
        "collection": "suppliers",
        "incarnation": "inc-chosen-by-client",
        "key": "S-42",
    }
    # A non-null default cannot validate as a reference the client
    # picked: the schema layer reports the shape violation.
    assert errors_of(lambda: parse_semantic_document(document))


def test_reference_state_count_limit():
    document = semantic_document()
    entry = document["state"]["selected_supplier"]
    for index in range(257):
        document["state"][f"s{index:03d}"] = json.loads(json.dumps(entry))
    assert ("E_LIMIT", "/state") in errors_of(
        lambda: parse_semantic_document(document)
    )


# ---------------------------------------------------------------------------
# Dispatch helpers and canonical form
# ---------------------------------------------------------------------------


def test_document_model_dispatches_on_language():
    semantic = document_model_of(semantic_document())
    assert isinstance(semantic, SemanticDocument)
    v01 = document_model_of(
        {
            "language": LANGUAGE_ID,
            "workspaceId": "empty",
            "title": "Empty",
            "sources": {},
            "parameters": {},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        }
    )
    assert type(v01).__name__ == "Document"


def test_document_diagnostics_dispatch_on_language():
    semantic = parse_semantic_document(semantic_document())
    assert document_diagnostics(semantic) == []


def test_reference_model_rejects_missing_incarnation():
    with pytest.raises(Exception):
        SemanticReference.model_validate(
            {"collection": "suppliers", "key": "S-42"}
        )


def test_canonical_hash_covers_the_extension_maps():
    document = parse_semantic_document(semantic_document())
    stripped = json.loads(json.dumps(semantic_document()))
    stripped.pop("collections")
    stripped.pop("state")
    stripped["language"] = LANGUAGE_ID
    baseline = document_hash(document_model_of(stripped))
    assert document_hash(document) != baseline
