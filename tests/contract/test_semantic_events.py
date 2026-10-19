"""Pure events, observers, and the renderer boundary (T026).

RFC 001 sections 5 through 7 add the interaction half of
`melt/semantic-draft-1`. These tests pin the language layer:

- The graph source carries a validated identity map: node and edge
  identifiers are stable and unique, and edges name existing nodes
  (section 7.1).
- The pure events bind only projections whose renderer declares them,
  only to `state.set` effects, with declared event fields or typed
  literals and no expressions (sections 5.1 and 5.2).
- Each event address carries at most one writer, and the assignment
  type-fits the target state (section 5.2).
- The compiled plans carry the extension: renderer slots, event
  addresses, the state-to-observer dependency index, and the graph
  identity map (section 9).
- A `melt/v0.1` document rejects every extension shape.
"""

from __future__ import annotations

import copy

import pytest

from melt.schema.canonical import document_hash
from melt.schema.semantic import (
    OBJECT_SELECT_EVENT,
    SELECTION_CLEAR_EVENT,
    GraphProjection,
    GraphSchema,
    MeltDocumentError,
    RecordProjection,
    parse_document_for_language,
)
from runtime.compiler import (
    CompileRequest,
    compile_document,
    renderer_events,
)
from runtime.compiler.catalog import CatalogArtifact, SourceCatalog

SUPPLIER_ARTIFACT = (
    "sha256:0b03b11a7e1c826a41f3bdc4a877b87487c254110a8fc54692c170c3f2afb99e"
)
GRAPH_ARTIFACT = (
    "sha256:5c2dae28729386d098e65aac600753940cf8475471415d239cece1763c9d5dc5"
)

SUPPLIER_SCHEMA = {
    "columns": [
        {"id": "supplier_id", "type": "string", "nullable": False},
        {"id": "name", "type": "string", "nullable": False},
    ],
    "rowKey": None,
}
GRAPH_SCHEMA = {
    "nodes": [
        {"nodeId": "s-42", "label": "Alpha Components", "collection": "suppliers", "key": "S-42"},
        {"nodeId": "s-73", "label": "Beta Systems", "collection": "suppliers", "key": "S-73"},
    ],
    "edges": [{"edgeId": "e-s42-s73", "from": "s-42", "to": "s-73"}],
}


def semantic_catalog() -> SourceCatalog:
    return SourceCatalog(
        {
            SUPPLIER_ARTIFACT: CatalogArtifact.model_validate(
                {
                    "path": "semantic/suppliers.parquet",
                    "kind": "table",
                    "mediaType": "application/vnd.apache.parquet",
                    "byteLength": 737,
                    "schema": copy.deepcopy(SUPPLIER_SCHEMA),
                }
            ),
            GRAPH_ARTIFACT: CatalogArtifact.model_validate(
                {
                    "path": "semantic/supplier-network.graph.json",
                    "kind": "graph",
                    "mediaType": "application/vnd.melt.graph+json",
                    "byteLength": 366,
                    "schema": copy.deepcopy(GRAPH_SCHEMA),
                }
            ),
        }
    )


def semantic_document() -> dict:
    """One fresh supplier workspace; the shared schemas are copied so a
    test that mutates its document never mutates the constants."""

    document = {
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
                        "schema": copy.deepcopy(SUPPLIER_SCHEMA),
                        "origin": {
                            "kind": "import",
                            "label": "suppliers.parquet",
                        },
                    }
                },
            },
            "supplier-network": {
                "kind": "graph",
                "currentVersion": 1,
                "versions": {
                    "1": {
                        "artifact": GRAPH_ARTIFACT,
                        "mediaType": "application/vnd.melt.graph+json",
                        "byteLength": 366,
                        "schema": copy.deepcopy(GRAPH_SCHEMA),
                        "origin": {
                            "kind": "import",
                            "label": "supplier-network.graph.json",
                        },
                    }
                },
            },
        },
        "parameters": {},
        "actions": {},
        "projections": {
            "network": {
                "kind": "graph",
                "title": "Supplier network",
                "source": {"sourceId": "supplier-network", "version": 1},
                "collection": "suppliers",
                "config": {"nodeLabels": "show"},
                "controls": [],
                "observe": {"highlight": {"state": "selected_supplier"}},
            },
            "inspector": {
                "kind": "record",
                "title": "Supplier inspector",
                "collection": "suppliers",
                "config": {"fields": None},
                "controls": [],
                "observe": {"record": {"state": "selected_supplier"}},
            },
        },
        "bindings": {
            "select-supplier": {
                "event": {"kind": "object.select", "projection": "network"},
                "effect": {
                    "kind": "state.set",
                    "target": "selected_supplier",
                    "value": {"eventField": "objectRef"},
                },
            },
            "clear-supplier": {
                "event": {"kind": "selection.clear", "projection": "network"},
                "effect": {
                    "kind": "state.set",
                    "target": "selected_supplier",
                    "value": {"literal": None},
                },
            },
        },
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
        "layout": {
            "root": {
                "kind": "split",
                "axis": "horizontal",
                "ratio": 0.6,
                "first": {"kind": "pane", "projection": "network"},
                "second": {"kind": "pane", "projection": "inspector"},
            },
            "hidden": [],
        },
    }
    return document


def compile_semantic(document=None, **kwargs):
    return compile_document(
        CompileRequest(
            document=document or semantic_document(),
            source_catalog=semantic_catalog(),
            **kwargs,
        )
    )


def diagnostics_of(document: dict) -> "list[tuple[str, str]]":
    try:
        parse_document_for_language(document)
    except MeltDocumentError as error:
        return [(item.code, item.path) for item in error.diagnostics]
    return []


# ---------------------------------------------------------------------------
# The graph identity map (RFC 001 section 7.1)
# ---------------------------------------------------------------------------


def test_the_graph_schema_validates_its_identity_map():
    schema = GraphSchema.model_validate(GRAPH_SCHEMA)
    assert [node.nodeId for node in schema.nodes] == ["s-42", "s-73"]
    assert schema.edges[0].from_ == "s-42"
    duplicated = diagnostics_of(_with_graph_map(
        nodes=[
            {"nodeId": "s-42", "label": "A", "collection": "suppliers", "key": "S-42"},
            {"nodeId": "s-42", "label": "B", "collection": "suppliers", "key": "S-73"},
        ]
    ))
    assert ("E_SCHEMA", "/sources/supplier-network/versions/1/schema") in duplicated

    dangling_edge = diagnostics_of(_with_graph_map(
        nodes=[{"nodeId": "s-42", "label": "A", "collection": "suppliers", "key": "S-42"}],
        edges=[{"edgeId": "e-x", "from": "s-42", "to": "s-99"}],
    ))
    assert ("E_REFERENCE", "/sources/supplier-network/versions/1/schema") in dangling_edge


def _with_graph_map(*, nodes, edges=()) -> dict:
    document = semantic_document()
    document["sources"]["supplier-network"]["versions"]["1"]["schema"] = {
        "nodes": nodes,
        "edges": list(edges),
    }
    return document


# ---------------------------------------------------------------------------
# Bindings (RFC 001 sections 5.1 and 5.2)
# ---------------------------------------------------------------------------


def _with_binding(binding_id: str, binding: dict) -> dict:
    document = semantic_document()
    document["bindings"][binding_id] = binding
    return document


def test_a_pure_event_binding_parses_with_a_zero_debounce():
    document = parse_document_for_language(semantic_document())
    binding = document.bindings["select-supplier"]
    assert binding.event.kind == OBJECT_SELECT_EVENT
    assert binding.effect.value.eventField == "objectRef"
    assert binding.debounceMs == 0
    clear = document.bindings["clear-supplier"]
    assert clear.effect.value.literal is None


def test_the_events_come_only_from_renderers_that_declare_them():
    document = _with_binding(
        "inspect-supplier",
        {
            "event": {"kind": OBJECT_SELECT_EVENT, "projection": "inspector"},
            "effect": {
                "kind": "state.set",
                "target": "selected_supplier",
                "value": {"literal": None},
            },
        },
    )
    assert (
        "E_TYPE",
        "/bindings/inspect-supplier/event/projection",
    ) in diagnostics_of(document)


def test_the_renderer_contracts_declare_the_event_names():
    assert renderer_events("graph") == (
        OBJECT_SELECT_EVENT,
        SELECTION_CLEAR_EVENT,
    )
    assert renderer_events("record") == ()
    assert renderer_events("table") == ()
    assert renderer_events("unknown-kind") == ()


def test_the_renderer_contracts_declare_the_observer_slots():
    from runtime.compiler.compiler import registry_of_language

    registry = registry_of_language("melt/semantic-draft-1")
    assert registry["graph"].observer_slots == ("highlight",)
    assert registry["record"].observer_slots == ("record",)
    # The v0.1 renderers support no selector slot.
    for kind in ("table", "plot", "document"):
        assert registry[kind].observer_slots == ()


@pytest.mark.parametrize(
    ("value", "codes"),
    [
        ({"template": "${event.objectRef.key}"}, ("E_SCHEMA",)),
        ({"eventField": "objectRef", "literal": None}, ("E_SCHEMA", "E_TYPE")),
        ({"eventField": "rowIndex"}, ("E_SCHEMA", "E_TYPE")),
        ("objectRef", ("E_TYPE",)),
    ],
)
def test_only_declared_fields_and_literals_stand_as_values(value, codes):
    document = semantic_document()
    document["bindings"]["select-supplier"]["effect"]["value"] = value
    found = [
        (code, path)
        for code, path in diagnostics_of(document)
        if path.startswith("/bindings/select-supplier/effect/value")
    ]
    assert found, "the value selector must be rejected"
    assert all(code in codes for code, _ in found), found


def test_two_writers_share_no_event_address():
    document = _with_binding(
        "another-select",
        {
            "event": {"kind": OBJECT_SELECT_EVENT, "projection": "network"},
            "effect": {
                "kind": "state.set",
                "target": "selected_supplier",
                "value": {"literal": None},
            },
        },
    )
    # The later binding in sorted order carries the diagnostic.
    assert ("E_SCHEMA", "/bindings/select-supplier/event") in diagnostics_of(
        document
    )


def test_selection_never_invokes_a_capability():
    document = semantic_document()
    document["bindings"]["select-supplier"]["effect"] = {
        "kind": "action.invoke",
        "action": "analyze-risk",
    }
    assert (
        "E_TYPE",
        "/bindings/select-supplier/effect",
    ) in diagnostics_of(document)


def test_a_state_set_effect_never_fires_from_a_v01_event():
    document = semantic_document()
    document["bindings"]["reset-on-change"] = {
        "event": {"kind": "parameter.changed", "parameter": "minimum"},
        "effect": {
            "kind": "state.set",
            "target": "selected_supplier",
            "value": {"literal": None},
        },
    }
    assert (
        "E_TYPE",
        "/bindings/reset-on-change/effect",
    ) in diagnostics_of(document)


# ---------------------------------------------------------------------------
# Observers (RFC 001 section 6)
# ---------------------------------------------------------------------------


def test_an_observer_reads_only_state_of_its_collection():
    document = semantic_document()
    document["collections"]["parts"] = {
        "source": {"sourceId": "supplier-records", "version": 1},
        "key": "supplier_id",
    }
    document["state"]["selected_part"] = {
        "type": {"kind": "ref", "collection": "parts"},
        "nullable": True,
        "default": None,
        "scope": "workspace",
    }
    document["projections"]["network"]["observe"]["highlight"]["state"] = (
        "selected_part"
    )
    assert (
        "E_TYPE",
        "/projections/network/observe/highlight/state",
    ) in diagnostics_of(document)


def test_a_graph_carries_one_collection():
    document = semantic_document()
    document["sources"]["supplier-network"]["versions"]["1"]["schema"][
        "nodes"
    ][0]["collection"] = "parts"
    assert (
        "E_TYPE",
        "/projections/network/collection",
    ) in diagnostics_of(document)


def test_a_record_configures_columns_of_the_pinned_version():
    document = semantic_document()
    document["projections"]["inspector"]["config"] = {
        "fields": ["supplier_id", "risk_score"]
    }
    assert (
        "E_REFERENCE",
        "/projections/inspector/config/fields/1",
    ) in diagnostics_of(document)


def test_a_record_projection_reads_through_its_collection():
    document = parse_document_for_language(semantic_document())
    inspector = document.projections["inspector"]
    assert isinstance(inspector, RecordProjection)
    assert inspector.observe.record.state == "selected_supplier"
    network = document.projections["network"]
    assert isinstance(network, GraphProjection)
    assert network.collection == "suppliers"


# ---------------------------------------------------------------------------
# The compiled plans (RFC 001 section 9)
# ---------------------------------------------------------------------------


def test_the_render_plan_carries_the_observer_slots():
    result = compile_semantic(collection_incarnations={"suppliers": "inc-1"})
    assert result.ok, result.diagnostics
    plan = result.plan
    network = plan.render.projections["network"]
    assert network.renderer.id == "melt.graph"
    assert network.renderer.contractVersion == 1
    assert network.collection == "suppliers"
    assert network.observers["highlight"].state == "selected_supplier"
    inspector = plan.render.projections["inspector"]
    assert inspector.renderer.id == "melt.record"
    assert inspector.source is None
    assert inspector.observers["record"].collection == "suppliers"


def test_the_interaction_plan_carries_the_event_addresses():
    result = compile_semantic()
    assert result.ok
    assert sorted(result.plan.interaction.eventAddresses) == [
        "object.select/network",
        "selection.clear/network",
    ]
    entry = result.plan.interaction.bindings["select-supplier"]
    assert entry.event.kind == OBJECT_SELECT_EVENT
    assert entry.effect.kind == "state.set"


def test_the_dependency_plan_indexes_state_to_observers():
    result = compile_semantic()
    assert result.ok
    observers = result.plan.dependency.referenceStates["selected_supplier"]
    assert [(o.projection, o.slot) for o in observers.observers] == [
        ("inspector", "record"),
        ("network", "highlight"),
    ]
    graph_readers = result.plan.dependency.sources["supplier-network"]["1"]
    assert graph_readers.readers == ("projections/network",)


def test_the_identity_plan_carries_the_graph_nodes():
    result = compile_semantic()
    assert result.ok
    nodes = result.plan.identity.nodes
    assert nodes["network/s-42"].key == "S-42"
    assert nodes["network/s-42"].collection == "suppliers"
    assert nodes["network/s-73"].key == "S-73"


def test_the_semantic_renderers_ride_only_the_extension_plans():
    semantic = compile_semantic().plan
    assert set(semantic.renderers) == {
        "document",
        "graph",
        "plot",
        "record",
        "table",
    }
    v01 = compile_semantic(document=_without_extension_maps()).plan
    assert set(v01.renderers) == {"document", "plot", "table"}


def _without_extension_maps() -> dict:
    document = semantic_document()
    document.pop("collections")
    document.pop("state")
    document["projections"] = {}
    document["bindings"] = {}
    document["sources"].pop("supplier-network")
    document["language"] = "melt/v0.1"
    document["layout"] = {"root": None, "hidden": []}
    return document


def test_an_unrelated_observer_adds_no_state_revision():
    """Adding an observer changes the plan, not the observed state.

    The document hashes differ — the definition changed — but nothing
    about the observed state entry did (RFC 001 section 6).
    """

    with_observer = semantic_document()
    without = semantic_document()
    without["projections"].pop("inspector")
    without["layout"]["root"] = {"kind": "pane", "projection": "network"}
    first = compile_semantic(document=without)
    second = compile_semantic(document=with_observer)
    assert first.ok and second.ok
    assert document_hash(
        parse_document_for_language(without)
    ) != document_hash(parse_document_for_language(with_observer))
    assert (
        second.plan.dependency.referenceStates["selected_supplier"].observers[
            0
        ].projection
        == "inspector"
    )


# ---------------------------------------------------------------------------
# The v0.1 boundary (RFC 001 section 1)
# ---------------------------------------------------------------------------


def test_a_v01_document_rejects_every_extension_shape():
    for path in ("/collections", "/bindings/select-supplier"):
        document = _without_extension_maps()
        if path == "/collections":
            document["collections"] = {
                "suppliers": {
                    "source": {"sourceId": "supplier-records", "version": 1},
                    "key": "supplier_id",
                }
            }
        else:
            document["bindings"] = {
                "select-supplier": {
                    "event": {
                        "kind": OBJECT_SELECT_EVENT,
                        "projection": "network",
                    },
                    "effect": {
                        "kind": "state.set",
                        "target": "selected_supplier",
                        "value": {"literal": None},
                    },
                }
            }
        found = diagnostics_of(document)
        assert found, "the v0.1 document must reject the extension shape"
        assert any(
            found_path == path or found_path.startswith(path + "/")
            for _, found_path in found
        ), found


def test_a_v01_document_rejects_the_graph_source_kind():
    document = _without_extension_maps()
    document["sources"]["supplier-network"] = semantic_document()["sources"][
        "supplier-network"
    ]
    found = diagnostics_of(document)
    assert ("E_TYPE", "/sources/supplier-network/kind") in found
