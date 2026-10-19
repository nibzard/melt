"""Pane-independent outputs at the schema and compiler level
(T027, RFC 001 section 8).

These tests pin the language surface of the extension:

- An extension action declares output names, kinds, and a publication
  group and carries no projection targets; a `melt/v0.1` document
  still requires them.
- A projection source subscribes by the action's stable output
  incarnation, following the current pointer or pinning one version.
- The compiler verifies a subscription against the incarnations the
  service issued, lowers it into the render plan, and indexes it in
  the output-to-subscriber dependency of RFC 001 section 9.
- A `melt/v0.1` compile keeps exactly its v0.1 plans: no incarnation,
  no output index, targets unchanged.
"""

from __future__ import annotations

import json

import pytest

from melt.schema.canonical import document_hash
from melt.schema.commands import parse_command
from melt.schema.document import MeltDocumentError, parse_document
from melt.schema.semantic import (
    SEMANTIC_LANGUAGE_ID,
    OutputSubscription,
    SemanticAction,
    parse_document_for_language,
    parse_semantic_document,
)
from runtime.compiler import (
    ActionRegistry,
    CompileRequest,
    RegistrationEntry,
    compile_document,
)
from melt.schema.commands import RegistrationContract

INCARNATION = "out-test-1"

RISK_REGISTRY = ActionRegistry(
    {
        "risk-script-v1": RegistrationEntry(
            registration_id="risk-script-v1",
            revision=1,
            contract=RegistrationContract.model_validate(
                {
                    "parameters": {},
                    "inputs": {},
                    "outputs": {"risk-table": "table", "risk-plot": "image"},
                }
            ),
        ),
    }
)


def semantic_document() -> dict:
    """One extension document whose action owns two pane-free outputs."""

    return {
        "language": SEMANTIC_LANGUAGE_ID,
        "workspaceId": "risk-demo",
        "title": "Risk analysis",
        "sources": {},
        "parameters": {},
        "actions": {
            "analyze-risk": {
                "implementation": {"registrationId": "risk-script-v1"},
                "parameters": {},
                "inputs": {},
                "outputs": {"risk-table": "table", "risk-plot": "image"},
                "outputGroup": "risk-results",
            }
        },
        "projections": {
            "risk": {
                "kind": "table",
                "title": "Risk table",
                "source": {
                    "actionOutput": {
                        "action": "analyze-risk",
                        "output": "risk-table",
                        "incarnation": INCARNATION,
                    },
                    "follow": "current",
                },
                "config": {"columns": None, "selection": "none"},
                "controls": [],
            },
            "frozen-plot": {
                "kind": "plot",
                "title": "Frozen plot",
                "source": {
                    "actionOutput": {
                        "action": "analyze-risk",
                        "output": "risk-plot",
                        "incarnation": INCARNATION,
                    },
                    "follow": "pinned",
                    "version": 3,
                },
                "config": {"fit": "contain"},
                "controls": [],
            },
            "empty-notes": {
                "kind": "document",
                "title": "Notes",
                "source": None,
                "config": {"format": "plain"},
                "controls": [],
            },
        },
        "bindings": {},
        "collections": {},
        "state": {},
        "layout": {"root": None, "hidden": ["empty-notes", "risk", "frozen-plot"]},
    }


def parse_and_compile(document: dict, **request_fields):
    parsed = parse_document_for_language(document)
    fields = {"action_registry": RISK_REGISTRY}
    fields.update(request_fields)
    result = compile_document(CompileRequest(document=parsed, **fields))
    return parsed, result


# ---------------------------------------------------------------------------
# The extension action and its subscriptions (RFC 001 section 8.1)
# ---------------------------------------------------------------------------


def test_an_extension_action_declares_outputs_without_targets():
    document = parse_semantic_document(semantic_document())
    action = document.actions["analyze-risk"]
    assert isinstance(action, SemanticAction)
    assert action.outputs == {"risk-table": "table", "risk-plot": "image"}
    assert action.outputGroup == "risk-results"
    assert not hasattr(action, "targets")


def test_a_subscription_follows_or_pins_by_incarnation():
    document = parse_semantic_document(semantic_document())
    following = document.projections["risk"].source
    pinned = document.projections["frozen-plot"].source
    assert isinstance(following, OutputSubscription)
    assert following.follow == "current"
    assert following.version is None
    assert isinstance(pinned, OutputSubscription)
    assert pinned.follow == "pinned"
    assert pinned.version == 3
    assert pinned.actionOutput.incarnation == INCARNATION


def test_a_null_source_stays_an_empty_result_state():
    document = parse_semantic_document(semantic_document())
    assert document.projections["empty-notes"].source is None


def test_a_v01_document_still_requires_targets():
    document = semantic_document()
    document["language"] = "melt/v0.1"
    document.pop("collections")
    document.pop("state")
    document["projections"].pop("empty-notes")
    document["layout"]["hidden"].remove("empty-notes")
    document["actions"]["analyze-risk"]["targets"] = {
        "risk-table": "risk",
        "risk-plot": "frozen-plot",
    }
    document["projections"]["risk"]["source"] = None
    document["projections"]["frozen-plot"]["source"] = None
    parse_document(document)
    # The same action without targets no longer parses as v0.1.
    document["actions"]["analyze-risk"].pop("targets")
    with pytest.raises(MeltDocumentError) as raised:
        parse_document(document)
    assert ("E_SCHEMA", "/actions/analyze-risk/targets") in [
        (item.code, item.path) for item in raised.value.diagnostics
    ]


def test_the_extension_document_hash_stable_across_parse_paths():
    document = semantic_document()
    assert document_hash(parse_document_for_language(document)) == (
        document_hash(parse_semantic_document(document))
    )


# ---------------------------------------------------------------------------
# Entity operations carry the extension shapes (SPEC.md section 12.2)
# ---------------------------------------------------------------------------


def _operation(collection: str, value: dict) -> dict:
    return {
        "protocolVersion": 1,
        "workspaceId": "risk-demo",
        "requestId": "op-1",
        "method": "workspace.apply",
        "operations": [
            {"op": "entity.create", "collection": collection, "id": "x", "value": value}
        ],
    }


def test_an_entity_create_accepts_the_extension_action():
    command = parse_command(
        _operation("actions", semantic_document()["actions"]["analyze-risk"])
    )
    assert isinstance(command.operations[0].value, SemanticAction)


def test_an_entity_create_accepts_the_subscribing_projection():
    command = parse_command(
        _operation(
            "projections", semantic_document()["projections"]["risk"]
        )
    )
    value = command.operations[0].value
    assert isinstance(value.source, OutputSubscription)
    assert value.source.follow == "current"


def test_an_entity_create_still_accepts_the_v01_action():
    action = semantic_document()["actions"]["analyze-risk"]
    action["targets"] = {"risk-table": "risk"}
    command = parse_command(_operation("actions", action))
    assert type(command.operations[0].value).__name__ == "Action"


# ---------------------------------------------------------------------------
# The compiled plans (RFC 001 section 9)
# ---------------------------------------------------------------------------


def compiled(document: dict, incarnations: "dict[str, str] | None" = None):
    _, result = parse_and_compile(
        document, output_incarnations=incarnations or {}
    )
    assert result.ok, result.diagnostics
    return result.plan


def test_the_action_plan_carries_the_stable_output_identity():
    plan = compiled(semantic_document(), {("analyze-risk"): INCARNATION})
    entry = plan.action.actions["analyze-risk"]
    assert entry.targets == {}
    assert entry.incarnation == INCARNATION
    assert entry.outputs == {"risk-table": "table", "risk-plot": "image"}
    group = plan.action.outputGroups["risk-results"]
    assert group.action == "analyze-risk"
    assert group.targets == {}


def test_the_dependency_plan_indexes_output_to_subscriber():
    plan = compiled(semantic_document(), {"analyze-risk": INCARNATION})
    table = plan.dependency.outputs["analyze-risk/risk-table"]
    assert table.action == "analyze-risk"
    assert table.output == "risk-table"
    assert table.kind == "table"
    assert table.incarnation == INCARNATION
    assert [item.projection for item in table.subscribers] == ["risk"]
    assert table.subscribers[0].follow == "current"
    plot = plan.dependency.outputs["analyze-risk/risk-plot"]
    assert [item.projection for item in plot.subscribers] == ["frozen-plot"]
    assert plot.subscribers[0].follow == "pinned"
    assert plot.subscribers[0].version == 3
    # A subscribing projection reads through its output, so the static
    # source index carries no version for it.
    assert "risk" not in plan.dependency.sources


def test_the_render_plan_carries_the_subscription_source():
    plan = compiled(semantic_document())
    source = plan.render.projections["risk"].source
    assert isinstance(source, OutputSubscription)
    assert source.actionOutput.output == "risk-table"


def test_the_identity_plan_places_a_subscriber_in_its_group():
    plan = compiled(semantic_document())
    assert plan.identity.panes["risk"].outputGroup == "risk-results"
    assert plan.identity.panes["frozen-plot"].outputGroup == "risk-results"
    assert plan.identity.panes["empty-notes"].outputGroup is None


def test_a_stale_incarnation_conflicts():
    document = semantic_document()
    _, result = parse_and_compile(
        document, output_incarnations={"analyze-risk": "out-another"}
    )
    assert not result.ok
    assert (
        "E_CONFLICT",
        "/projections/risk/source/actionOutput/incarnation",
    ) in [(item.code, item.path) for item in result.diagnostics]


def test_a_service_less_compile_cannot_verify_an_incarnation():
    _, result = parse_and_compile(semantic_document())
    assert result.ok, result.diagnostics


# ---------------------------------------------------------------------------
# The v0.1 boundary (RFC 001 section 8.3)
# ---------------------------------------------------------------------------


def v01_sales_document() -> dict:
    with open("fixtures/valid/sales-full.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_a_v01_plan_keeps_targets_and_carries_no_output_index():
    from runtime.compiler import load_catalog

    catalog_text = open("fixtures/catalog.json", encoding="utf-8").read()
    source_catalog, action_registry = load_catalog(catalog_text)
    document = parse_document(v01_sales_document())
    result = compile_document(
        CompileRequest(
            document=document,
            source_catalog=source_catalog,
            action_registry=action_registry,
        )
    )
    assert result.ok, result.diagnostics
    plan = result.plan
    entry = plan.action.actions["analyze-sales"]
    assert entry.targets == {"filtered": "sales", "monthly": "revenue"}
    assert entry.incarnation is None
    assert plan.dependency.outputs == {}
    assert plan.identity.panes["sales"].outputGroup == "sales-results"
