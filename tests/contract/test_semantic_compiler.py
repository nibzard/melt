"""Compiler stages and plans of `melt/semantic-draft-1` (T025).

RFC 001 section 9 extends the existing plans rather than adding a
second compiler. These tests pin the additions:

- Stage 2 dispatches on the language identifier: the extension
  document validates, and a `melt/v0.1` document still compiles with
  an empty reference state plan.
- The state plan carries the reference types, the collection
  incarnation, and the resolution inputs (collection, key, pinned
  source).
- The dependency plan indexes the source version each collection
  reads, so an import can invalidate what depends on it.
- Stage 5 rejects a stored selection that names the wrong collection
  or a stale incarnation, the same continuity rule the service
  applies at assignment time.
"""

from __future__ import annotations

import pytest

from runtime.compiler import CompileRequest, CompileResult, compile_document
from runtime.compiler.catalog import CatalogArtifact, SourceCatalog

SUPPLIER_ARTIFACT = (
    "sha256:0b03b11a7e1c826a41f3bdc4a877b87487c254110a8fc54692c170c3f2afb99e"
)

SUPPLIER_SCHEMA = {
    "columns": [
        {"id": "supplier_id", "type": "string", "nullable": False},
        {"id": "name", "type": "string", "nullable": False},
    ],
    "rowKey": None,
}


def supplier_catalog() -> SourceCatalog:
    entry = CatalogArtifact.model_validate(
        {
            "path": "semantic/suppliers.parquet",
            "kind": "table",
            "mediaType": "application/vnd.apache.parquet",
            "byteLength": 737,
            "schema": SUPPLIER_SCHEMA,
        }
    )
    return SourceCatalog({SUPPLIER_ARTIFACT: entry})


def semantic_document() -> dict:
    return {
        "language": "melt/semantic-draft-1",
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
                        "schema": SUPPLIER_SCHEMA,
                        "origin": {"kind": "import", "label": "suppliers.parquet"},
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


def v01_document() -> dict:
    document = semantic_document()
    document.pop("collections")
    document.pop("state")
    document["language"] = "melt/v0.1"
    return document


def compile_semantic(document=None, **kwargs) -> CompileResult:
    request = CompileRequest(
        document=document or semantic_document(),
        source_catalog=supplier_catalog(),
        **kwargs,
    )
    return compile_document(request)


def test_semantic_document_compiles_with_extension_plans():
    result = compile_semantic(collection_incarnations={"suppliers": "inc-1"})
    assert result.ok, result.diagnostics
    plan = result.plan
    assert plan.language == "melt/semantic-draft-1"
    entry = plan.state.references["selected_supplier"]
    assert entry.collection == "suppliers"
    assert entry.key == "supplier_id"
    assert entry.source.sourceId == "supplier-records"
    assert entry.source.version == 1
    assert entry.incarnation == "inc-1"
    assert entry.nullable is True
    assert entry.scope == "workspace"


def test_incarnation_stays_null_without_service_state():
    result = compile_semantic()
    assert result.ok
    assert result.plan.state.references["selected_supplier"].incarnation is None


def test_dependency_plan_indexes_the_pinned_version():
    result = compile_semantic()
    readers = result.plan.dependency.sources["supplier-records"]["1"].readers
    assert readers == ("collections/suppliers",)


def test_v01_plans_keep_their_exact_shape():
    result = compile_semantic(document=v01_document())
    assert result.ok
    assert result.plan.language == "melt/v0.1"
    assert result.plan.state.references == {}


def test_plan_key_separates_the_extension_document():
    with_extension = compile_semantic().plan.planKey
    without = compile_semantic(document=v01_document()).plan.planKey
    assert with_extension != without


def test_equivalent_inputs_produce_equivalent_plans():
    first = compile_semantic(collection_incarnations={"suppliers": "inc-1"}).plan
    second = compile_semantic(collection_incarnations={"suppliers": "inc-1"}).plan
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_stored_selection_with_current_incarnation_is_continuous():
    result = compile_semantic(
        collection_incarnations={"suppliers": "inc-1"},
        reference_states={
            "selected_supplier": {
                "value": {
                    "collection": "suppliers",
                    "incarnation": "inc-1",
                    "key": "S-42",
                },
                "revision": 2,
            }
        },
    )
    assert result.ok


def test_stored_selection_with_stale_incarnation_fails():
    result = compile_semantic(
        collection_incarnations={"suppliers": "inc-2"},
        reference_states={
            "selected_supplier": {
                "value": {
                    "collection": "suppliers",
                    "incarnation": "inc-1",
                    "key": "S-42",
                },
                "revision": 2,
            }
        },
    )
    assert not result.ok
    diagnostic = result.diagnostics[0]
    assert diagnostic.code == "E_STATE_MIGRATION"
    assert diagnostic.path == "/state/selected_supplier"
    assert "incarnation" in diagnostic.message


def test_stored_selection_for_the_wrong_collection_fails():
    result = compile_semantic(
        collection_incarnations={"suppliers": "inc-1"},
        reference_states={
            "selected_supplier": {
                "value": {
                    "collection": "products",
                    "incarnation": "inc-1",
                    "key": "P-1",
                },
                "revision": 1,
            }
        },
    )
    assert not result.ok
    assert result.diagnostics[0].code == "E_STATE_MIGRATION"
    assert "products" in result.diagnostics[0].message


def test_null_selection_is_always_continuous():
    result = compile_semantic(
        collection_incarnations={"suppliers": "inc-9"},
        reference_states={
            "selected_supplier": {"value": None, "revision": 4},
        },
    )
    assert result.ok


def test_state_for_an_undefined_entry_fails():
    result = compile_semantic(
        collection_incarnations={"suppliers": "inc-1"},
        reference_states={
            "removed_entry": {
                "value": {
                    "collection": "suppliers",
                    "incarnation": "inc-1",
                    "key": "S-42",
                },
                "revision": 1,
            }
        },
    )
    assert not result.ok
    assert result.diagnostics[0].path == "/state/removed_entry"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["collections"]["suppliers"].update(
            {"key": "supplier_code"}
        ),
        lambda document: document["collections"]["suppliers"]["source"].update(
            {"version": 2}
        ),
    ],
)
def test_collection_drift_fails_compilation(mutation):
    document = semantic_document()
    mutation(document)
    result = compile_semantic(document=document)
    assert not result.ok
    assert result.diagnostics[0].path.startswith("/collections/suppliers")
