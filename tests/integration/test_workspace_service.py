"""Workspace service: mutations, receipts, and notifications (T009).

The tests prove the service core of SPEC.md section 12 against the real
durable storage path of T008:

- The framed IPC channel: four-byte big-endian length prefix, UTF-8
  JSON payload, and rejection above the frame limit before allocation
  (section 12.1).
- ``workspace.apply`` transactions: operations evaluated in order
  against a private candidate, one validation, all-or-nothing commit,
  expected-revision conflicts, compiler read-set checks, and state
  migrations (sections 12.2 and 12.3).
- Request receipts: replay of the stored response, ``E_REQUEST_REUSE``
  on different content, and retention across restart (section 12.5).
- One commit sequence per transaction, changed records incrementing
  their own revision once, and ordered epoch-guarded notifications.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from melt.schema.canonical import canonical_hash, model_to_wire
from melt.schema.commands import (
    RegistrationContract,
    decode_frame,
    encode_frame,
    parse_command,
    read_frame,
)
from runtime.compiler.catalog import ActionRegistry, RegistrationEntry
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import ParameterStateValue, WorkspaceService, plan_mutation

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

EMPTY_DOCUMENT = {
    "language": "melt/v0.1",
    "workspaceId": WORKSPACE,
    "title": "Sales analysis",
    "sources": {},
    "parameters": {},
    "actions": {},
    "projections": {},
    "bindings": {},
    "layout": {"root": None, "hidden": []},
}

MINIMUM_PARAMETER = {
    "type": "number",
    "default": 0,
    "nullable": False,
    "constraints": {"minimum": 0, "maximum": 1000000000},
}


def document_with(**collections) -> dict:
    document = json.loads(json.dumps(EMPTY_DOCUMENT))
    for name, entities in collections.items():
        document[name].update(entities)
    return document


def envelope(
    request_id: str,
    expected: "dict | None" = None,
    operations: "list | None" = None,
    *,
    method: str = "workspace.apply",
    workspace_id: str = WORKSPACE,
):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": workspace_id,
            "requestId": request_id,
            "method": method,
            "expected": expected,
            "operations": operations or [],
        }
    )


def minimum_parameter(**overrides) -> dict:
    value = json.loads(json.dumps(MINIMUM_PARAMETER))
    value.update(overrides)
    if "constraints" in overrides:
        value["constraints"] = json.loads(json.dumps(overrides["constraints"]))
    return value


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    """One opened service, closed with the test."""

    opened = WorkspaceService.open(service_adapter, WORKSPACE)
    yield opened
    opened.close()


def seeded_service(
    adapter: PlatformAdapter, *, catalog=None, registry=None
) -> WorkspaceService:
    """One service whose workspace holds the minimum parameter."""

    service = WorkspaceService.open(
        adapter, WORKSPACE, source_catalog=catalog, action_registry=registry
    )
    created = service.create_workspace(
        document_with(parameters={"minimum": minimum_parameter()}),
        "01-seed-create",
    )
    assert created.ok, created.errors
    return service


def seed_parameter(service: WorkspaceService) -> None:
    """Bring parameters/minimum to entity revision 2, state revision 1."""

    replaced = service.apply(
        envelope(
            "01-seed-replace",
            {"parameters/minimum": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "parameters",
                    "id": "minimum",
                    "value": minimum_parameter(
                        constraints={"minimum": 0}
                    ),
                }
            ],
        )
    )
    assert replaced.ok, replaced.errors
    assert service.entity_revisions["parameters/minimum"] == 2


# ---------------------------------------------------------------------------
# Workspace creation (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


def test_create_seeds_revisions_and_default_state(service: WorkspaceService):
    response = service.create_workspace(
        document_with(parameters={"minimum": minimum_parameter()}), "01-create"
    )
    assert response.ok, response.errors
    assert response.commitSequence == 2  # the header commit took sequence 1
    assert service.entity_revisions == {"parameters/minimum": 1, "layout": 1}
    assert service.parameter_state["minimum"].value == 0.0
    assert service.parameter_state["minimum"].revision == 1
    assert service.document["parameters"]["minimum"]["default"] == 0


def test_create_stores_a_receipt_and_replays_it(service: WorkspaceService):
    document = document_with(parameters={"minimum": minimum_parameter()})
    first = service.create_workspace(document, "01-create")
    assert first.ok
    replay = service.create_workspace(document, "01-create")
    assert replay.ok
    assert replay.commitSequence == first.commitSequence
    assert service.commit_sequence == 2, "the replay committed nothing"
    other = service.create_workspace(
        document_with(parameters={"minimum": minimum_parameter(constraints={})}),
        "01-create",
    )
    assert other.ok is False
    assert [(error.code, error.path) for error in other.errors] == [
        ("E_REQUEST_REUSE", "/requestId")
    ]


def test_create_is_valid_only_for_an_absent_workspace(service: WorkspaceService):
    assert service.create_workspace(
        document_with(), "01-create"
    ).ok
    second = service.create_workspace(document_with(), "02-create")
    assert second.ok is False
    assert [(error.code, error.path) for error in second.errors] == [
        ("E_CONFLICT", "/workspaceId")
    ]


def test_create_requires_empty_sources_and_actions(service: WorkspaceService):
    document = document_with()
    document["sources"]["sales-input"] = {
        "kind": "text",
        "currentVersion": 1,
        "versions": {
            "1": {
                "artifact": "sha256:" + "1a" * 32,
                "mediaType": "text/plain",
                "byteLength": 4,
                "schema": {"encoding": "utf-8"},
                "origin": {"kind": "import", "label": "sales.csv"},
            }
        },
    }
    response = service.create_workspace(document, "01-create")
    assert response.ok is False
    pairs = [(error.code, error.path) for error in response.errors]
    assert ("E_SCHEMA", "/sources") in pairs


def test_apply_on_an_absent_workspace_is_rejected(service: WorkspaceService):
    response = service.apply(
        envelope("01-create", {"parameters/minimum": 0}, [])
    )
    assert response.ok is False
    assert [(error.code, error.path) for error in response.errors] == [
        ("E_REFERENCE", "/workspaceId")
    ]


def test_apply_rejects_a_foreign_workspace_id(service: WorkspaceService):
    response = service.apply(
        envelope("01-foreign", None, [], workspace_id="other-workspace")
    )
    assert response.ok is False
    assert [(error.code, error.path) for error in response.errors] == [
        ("E_REFERENCE", "/workspaceId")
    ]


# ---------------------------------------------------------------------------
# Mutation transactions (SPEC.md sections 12.2 and 12.3)
# ---------------------------------------------------------------------------


def test_parameter_set_commits_once_and_increments_once(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        before = service.commit_sequence
        response = service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert response.ok, response.errors
        assert response.commitSequence == before + 1
        assert service.commit_sequence == before + 1
        assert response.result["changedRecords"] == ["state/minimum"]
        assert response.result["revisions"] == {"state/minimum": 2}
        assert service.parameter_state["minimum"] == ParameterStateValue(
            value=200.0, revision=2
        )
        # Every record of this transaction shares the one commit
        # sequence; the snapshot, the receipt, and the notification
        # committed atomically.
        transaction_records = [
            record
            for record in service.store.records()
            if record.commit_sequence == response.commitSequence
        ]
        assert {record.kind for record in transaction_records} == {
            "melt.workspaceSnapshot",
            "melt.commandReceipt",
            "melt.changeNotification",
        }


def test_equal_value_set_is_a_no_op(service_adapter: PlatformAdapter):
    with seeded_service(service_adapter) as service:
        assert service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        ).ok
        before = service.commit_sequence
        response = service.apply(
            envelope(
                "03-set-again",
                {"state/minimum": 2},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert response.ok, response.errors
        assert response.result["changedRecords"] == []
        assert service.commit_sequence == before + 1
        assert service.parameter_state["minimum"].revision == 2
        # The no-op transaction stored its receipt but no notification.
        assert service.notifications[-1]["commitSequence"] == before


def test_stale_expectation_conflicts_without_applying_anything(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        before_sequence = service.commit_sequence
        before_state = dict(service.parameter_state)
        before_revisions = dict(service.entity_revisions)
        response = service.apply(
            envelope(
                "02-stale",
                {"parameters/minimum": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": minimum_parameter(constraints={"minimum": 5}),
                    }
                ],
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_CONFLICT", "/expected/parameters~1minimum")
        ]
        assert service.commit_sequence == before_sequence
        assert service.parameter_state == before_state
        assert service.entity_revisions == before_revisions


def test_parameter_set_requires_its_state_expectation(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        response = service.apply(
            envelope(
                "02-missing",
                {"parameters/minimum": 2},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert response.ok is False
        errors = [(error.code, error.path) for error in response.errors]
        assert errors == [("E_CONFLICT", "/expected")]
        assert "state/minimum" in response.errors[0].message


def test_invalid_state_migration_is_rejected_at_the_operation(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        before_sequence = service.commit_sequence
        response = service.apply(
            envelope(
                "02-migrate",
                {"parameters/minimum": 2},
                [
                    {
                        "op": "entity.replace",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": minimum_parameter(
                            constraints={"minimum": 200}
                        ),
                    }
                ],
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_STATE_MIGRATION", "/operations/0")
        ]
        assert service.commit_sequence == before_sequence
        assert service.parameter_state["minimum"].value == 0.0


def test_valid_state_migration_needs_the_expected_state_revision(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        response = service.apply(
            envelope(
                "02-migrate",
                {"parameters/minimum": 2, "state/minimum": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": minimum_parameter(
                            default=200, constraints={"minimum": 200}
                        ),
                    },
                    {
                        "op": "parameter.set",
                        "id": "minimum",
                        "value": 250,
                    },
                ],
            )
        )
        assert response.ok, response.errors
        assert service.parameter_state["minimum"].value == 250.0
        assert service.parameter_state["minimum"].revision == 2
        assert sorted(response.result["changedRecords"]) == [
            "parameters/minimum",
            "state/minimum",
        ]


def test_invalid_parameter_set_value_is_rejected(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        response = service.apply(
            envelope(
                "02-bad-value",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": -5}],
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_TYPE", "/operations/0/value")
        ]


def test_transaction_commits_all_or_nothing(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        before_sequence = service.commit_sequence
        before_state = dict(service.parameter_state)
        before_revisions = dict(service.entity_revisions)
        response = service.apply(
            envelope(
                "02-all-or-nothing",
                {"parameters/extra": 0, "layout": 1},
                [
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "extra",
                        "value": minimum_parameter(),
                    },
                    {
                        "op": "layout.replace",
                        # The layout references a projection that does
                        # not exist, so the final candidate is invalid.
                        "value": {
                            "root": {"kind": "pane", "projection": "ghost"},
                            "hidden": [],
                        },
                    },
                ],
            )
        )
        assert response.ok is False
        pairs = [(error.code, error.path) for error in response.errors]
        assert ("E_LAYOUT", "/layout/root/projection") in pairs
        assert service.commit_sequence == before_sequence
        assert service.parameter_state == before_state
        assert service.entity_revisions == before_revisions
        assert "extra" not in service.document["parameters"]


def test_replace_bumps_the_revision_even_when_equivalent(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        current = service.document["parameters"]["minimum"]
        response = service.apply(
            envelope(
                "02-replace",
                {"parameters/minimum": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": current,
                    }
                ],
            )
        )
        assert response.ok, response.errors
        assert service.entity_revisions["parameters/minimum"] == 2
        assert service.parameter_state["minimum"].revision == 1


def test_delete_and_create_in_one_transaction_replaces_identity(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        response = service.apply(
            envelope(
                "02-recreate",
                {"parameters/minimum": 1},
                [
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "minimum",
                    },
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": {
                            "type": "string",
                            "default": "all",
                            "nullable": False,
                            "constraints": {"maxLength": 16},
                        },
                    },
                ],
            )
        )
        assert response.ok, response.errors
        assert service.entity_revisions["parameters/minimum"] == 2
        assert service.parameter_state["minimum"].value == "all"
        assert service.parameter_state["minimum"].revision == 1
        assert service.state.incarnations["minimum"] == 2


def test_create_requires_absence_and_replace_requires_presence(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        created = service.apply(
            envelope(
                "02-create-existing",
                {"parameters/minimum": 1},
                [
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "minimum",
                        "value": minimum_parameter(),
                    }
                ],
            )
        )
        assert created.ok is False
        assert [(error.code, error.path) for error in created.errors] == [
            ("E_CONFLICT", "/operations/0/id")
        ]
        deleted = service.apply(
            envelope(
                "03-delete",
                {"parameters/minimum": 1},
                [
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "minimum",
                    }
                ],
            )
        )
        assert deleted.ok, deleted.errors
        missing = service.apply(
            envelope(
                "04-delete-again",
                {"parameters/minimum": 0},
                [
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "minimum",
                    }
                ],
            )
        )
        assert missing.ok is False
        assert [(error.code, error.path) for error in missing.errors] == [
            ("E_REFERENCE", "/operations/0/id")
        ]
        # Deleting removed the stored state; recreating starts over.
        assert "minimum" not in service.parameter_state


def test_read_set_divergence_rejects_at_commit(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        request = envelope(
            "02-read-set",
            {"parameters/minimum": 1},
            [
                {
                    "op": "entity.replace",
                    "collection": "parameters",
                    "id": "minimum",
                    "value": minimum_parameter(constraints={"minimum": 0}),
                }
            ],
        )
        plan = plan_mutation(service.state, request)
        assert plan.ok
        # An intervening writer changed a record the planning read. The
        # commit-time read-set check must refuse without writing.
        service.state.entity_revisions["parameters/minimum"] = 7
        payload_hash = canonical_hash(model_to_wire(request))
        response = service._commit(
            request.requestId, request.method, payload_hash, plan
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_CONFLICT", "/expected/parameters~1minimum")
        ]
        assert service.entity_revisions["parameters/minimum"] == 7
        assert service.store.commit_sequence == 2, "nothing was committed"


# ---------------------------------------------------------------------------
# Full definitions through the compiler (SPEC.md sections 7 and 11)
# ---------------------------------------------------------------------------


def notes_registry() -> ActionRegistry:
    contract = RegistrationContract.model_validate(
        {"parameters": {}, "inputs": {}, "outputs": {"notes": "text"}}
    )
    return ActionRegistry(
        {
            "notes-script-v1": RegistrationEntry(
                "notes-script-v1", 1, contract
            )
        }
    )


NOTES_ACTION = {
    "implementation": {"registrationId": "notes-script-v1"},
    "parameters": {},
    "inputs": {},
    "outputs": {"notes": "text"},
    "outputGroup": "notes-group",
    "targets": {"notes": "notes"},
}

NOTES_PROJECTION = {
    "kind": "document",
    "title": "Notes",
    "source": None,
    "config": {"format": "markdown"},
    "controls": [
        {
            "id": "threshold",
            "kind": "text",
            "label": "Threshold",
            "parameter": "threshold",
        }
    ],
}

NOTES_BINDING = {
    "event": {"kind": "parameter.changed", "parameter": "threshold"},
    "effect": {"kind": "action.invoke", "action": "summarize"},
    "debounceMs": 250,
}


def test_full_definitions_apply_through_the_compiler(
    service_adapter: PlatformAdapter,
):
    registry = notes_registry()
    with seeded_service(
        service_adapter, registry=registry
    ) as service:
        response = service.apply(
            envelope(
                "02-definitions",
                {
                    "parameters/threshold": 0,
                    "actions/summarize": 0,
                    "projections/notes": 0,
                    "bindings/rerun": 0,
                    "layout": 1,
                },
                [
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "threshold",
                        "value": {
                            "type": "string",
                            "default": "all",
                            "nullable": False,
                            "constraints": {"maxLength": 16},
                        },
                    },
                    {
                        "op": "entity.create",
                        "collection": "actions",
                        "id": "summarize",
                        "value": NOTES_ACTION,
                    },
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": "notes",
                        "value": NOTES_PROJECTION,
                    },
                    {
                        "op": "entity.create",
                        "collection": "bindings",
                        "id": "rerun",
                        "value": NOTES_BINDING,
                    },
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {"kind": "pane", "projection": "notes"},
                            "hidden": [],
                        },
                    },
                ],
            )
        )
        assert response.ok, response.errors
        assert sorted(response.result["changedRecords"]) == [
            "actions/summarize",
            "bindings/rerun",
            "layout",
            "parameters/threshold",
            "projections/notes",
            "state/threshold",
        ]
        assert service.parameter_state["threshold"].value == "all"
        assert service.entity_revisions["layout"] == 2


def test_unknown_registration_rejects_the_candidate(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        action = json.loads(json.dumps(NOTES_ACTION))
        action["implementation"]["registrationId"] = "ghost-script-v1"
        action["targets"] = {}
        action["outputs"] = {}
        response = service.apply(
            envelope(
                "02-ghost-registration",
                {"actions/summarize": 0},
                [
                    {
                        "op": "entity.create",
                        "collection": "actions",
                        "id": "summarize",
                        "value": action,
                    }
                ],
            )
        )
        assert response.ok is False
        pairs = [(error.code, error.path) for error in response.errors]
        assert (
            "E_REGISTRATION",
            "/actions/summarize/implementation/registrationId",
        ) in pairs


def test_deleting_a_parameter_requires_removing_its_references(
    service_adapter: PlatformAdapter,
):
    registry = notes_registry()
    with seeded_service(
        service_adapter, registry=registry
    ) as service:
        definitions = service.apply(
            envelope(
                "02-definitions",
                {
                    "parameters/threshold": 0,
                    "actions/summarize": 0,
                    "projections/notes": 0,
                    "bindings/rerun": 0,
                    "layout": 1,
                },
                [
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "threshold",
                        "value": {
                            "type": "string",
                            "default": "all",
                            "nullable": False,
                            "constraints": {"maxLength": 16},
                        },
                    },
                    {
                        "op": "entity.create",
                        "collection": "actions",
                        "id": "summarize",
                        "value": NOTES_ACTION,
                    },
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": "notes",
                        "value": NOTES_PROJECTION,
                    },
                    {
                        "op": "entity.create",
                        "collection": "bindings",
                        "id": "rerun",
                        "value": NOTES_BINDING,
                    },
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {"kind": "pane", "projection": "notes"},
                            "hidden": [],
                        },
                    },
                ],
            )
        )
        assert definitions.ok, definitions.errors
        orphaned = service.apply(
            envelope(
                "03-delete-orphaned",
                {"parameters/threshold": 1, "state/threshold": 1},
                [
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "threshold",
                    }
                ],
            )
        )
        assert orphaned.ok is False
        pairs = [(error.code, error.path) for error in orphaned.errors]
        assert any(
            code == "E_REFERENCE"
            and path in (
                "/projections/notes/controls/0/parameter",
                "/bindings/rerun/event/parameter",
            )
            for code, path in pairs
        ), pairs
        cleaned_projection = json.loads(json.dumps(NOTES_PROJECTION))
        cleaned_projection["controls"] = []
        removed = service.apply(
            envelope(
                "03-delete-clean",
                {
                    "parameters/threshold": 1,
                    "state/threshold": 1,
                    "projections/notes": 1,
                    "bindings/rerun": 1,
                },
                [
                    {
                        "op": "entity.delete",
                        "collection": "bindings",
                        "id": "rerun",
                    },
                    {
                        "op": "entity.replace",
                        "collection": "projections",
                        "id": "notes",
                        "value": cleaned_projection,
                    },
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "threshold",
                    },
                ],
            )
        )
        assert removed.ok, removed.errors
        assert "threshold" not in service.parameter_state
        assert "threshold" not in service.document["parameters"]


# ---------------------------------------------------------------------------
# Receipts (SPEC.md section 12.5)
# ---------------------------------------------------------------------------


def test_receipts_replay_and_reject_reuse(service_adapter: PlatformAdapter):
    with seeded_service(service_adapter) as service:
        first = service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert first.ok
        sequence_after_first = service.commit_sequence
        replay = service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert replay.ok
        assert replay.commitSequence == first.commitSequence
        assert service.commit_sequence == sequence_after_first
        reuse = service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 999}],
            )
        )
        assert reuse.ok is False
        assert [(error.code, error.path) for error in reuse.errors] == [
            ("E_REQUEST_REUSE", "/requestId")
        ]
        stored = service.receipt("02-set")
        assert stored["requestPayloadHash"] == canonical_hash(
            model_to_wire(
                envelope(
                    "02-set",
                    {"state/minimum": 1},
                    [{"op": "parameter.set", "id": "minimum", "value": 200}],
                )
            )
        )
        assert service.receipt("missing") is None


def test_rejected_requests_store_no_receipt(service_adapter: PlatformAdapter):
    with seeded_service(service_adapter) as service:
        response = service.apply(
            envelope("02-rejected", {"state/minimum": 99}, [])
        )
        assert response.ok is False
        assert service.receipt("02-rejected") is None
        # The same request id remains usable with corrected content.
        corrected = service.apply(
            envelope(
                "02-rejected",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 5}],
            )
        )
        assert corrected.ok, corrected.errors


# ---------------------------------------------------------------------------
# Notifications (SPEC.md section 12.5)
# ---------------------------------------------------------------------------


def test_notifications_are_ordered_and_epoch_guarded(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        epoch = service.epoch
        for value in (10, 20, 30):
            request = service.apply(
                envelope(
                    f"02-set-{value}",
                    {"state/minimum": service.parameter_state["minimum"].revision},
                    [{"op": "parameter.set", "id": "minimum", "value": value}],
                )
            )
            assert request.ok, request.errors
        window = service.changes_after(1, epoch)
        assert window.status == "changes"
        sequences = [item["commitSequence"] for item in window.changes]
        assert sequences == [2, 3, 4, 5]  # creation plus three sets
        assert window.changes[0]["changedRecords"] == [
            "layout",
            "parameters/minimum",
            "state/minimum",
        ]
        current = service.changes_after(service.commit_sequence, epoch)
        assert current.status == "changes"
        assert current.changes == ()
        stale_epoch = service.changes_after(1, epoch + 1)
        assert stale_epoch.status == "snapshot-required"
        beyond = service.changes_after(service.commit_sequence + 5, epoch)
        assert beyond.status == "snapshot-required"
        # A gap in the log forces a snapshot: the no-op transaction at
        # sequence 6 changed nothing and left no notification.
        assert service.apply(
            envelope(
                "05-noop",
                {"state/minimum": 4},
                [{"op": "parameter.set", "id": "minimum", "value": 30}],
            )
        ).ok
        assert service.apply(
            envelope(
                "06-set",
                {"state/minimum": 4},
                [{"op": "parameter.set", "id": "minimum", "value": 40}],
            )
        ).ok
        gapped = service.changes_after(4, epoch)
        assert gapped.status == "snapshot-required"
        assert "sequence 5" in gapped.reason or "sequence 6" in gapped.reason


# ---------------------------------------------------------------------------
# Restart recovery (SPEC.md section 14, steps 2 and 3)
# ---------------------------------------------------------------------------


def test_restart_restores_state_receipts_and_notifications(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        assert service.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        ).ok
        closed_sequence = service.commit_sequence
        closed_epoch = service.epoch
        closed_state = dict(service.parameter_state)
        closed_revisions = dict(service.entity_revisions)
        closed_document = service.document
        notification_count = len(service.notifications)

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.epoch == closed_epoch
        assert reopened.commit_sequence == closed_sequence
        assert reopened.parameter_state == closed_state
        assert reopened.entity_revisions == closed_revisions
        assert reopened.document == closed_document
        assert len(reopened.notifications) == notification_count
        # The stored receipt still replays after the restart.
        replay = reopened.apply(
            envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
        )
        assert replay.ok
        assert reopened.commit_sequence == closed_sequence
        # New work continues the sequence without a gap.
        follow_up = reopened.apply(
            envelope(
                "03-after-restart",
                {"state/minimum": 2},
                [{"op": "parameter.set", "id": "minimum", "value": 250}],
            )
        )
        assert follow_up.ok, follow_up.errors
        assert follow_up.commitSequence == closed_sequence + 1
        window = reopened.changes_after(closed_sequence, closed_epoch)
        assert [item["commitSequence"] for item in window.changes] == [
            closed_sequence + 1
        ]
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Framed channel (SPEC.md section 12.1)
# ---------------------------------------------------------------------------


def recv_exact(sock: socket.socket, count: int) -> bytes:
    parts: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def test_framed_channel_serves_requests(service_adapter: PlatformAdapter):
    with seeded_service(service_adapter) as service:
        seed_parameter(service)
        server_sock, client_sock = socket.socketpair()
        client_sock.settimeout(10)
        thread = threading.Thread(
            target=_serve_and_close, args=(service, server_sock), daemon=True
        )
        thread.start()
        try:
            request = envelope(
                "02-set",
                {"state/minimum": 1},
                [{"op": "parameter.set", "id": "minimum", "value": 200}],
            )
            payload = json.dumps(model_to_wire(request)).encode("utf-8")
            client_sock.sendall(encode_frame(payload))
            answer = read_frame(lambda count: recv_exact(client_sock, count))
            assert json.loads(answer.decode("utf-8")) == {
                "requestId": "02-set",
                "ok": True,
                "commitSequence": service.commit_sequence,
                "result": {
                    "changedRecords": ["state/minimum"],
                    "revisions": {"state/minimum": 2},
                },
            }
            # A rejected request answers with its request id and errors.
            bad = envelope("03-missing", None, [
                {"op": "parameter.set", "id": "minimum", "value": 1}
            ])
            client_sock.sendall(
                encode_frame(json.dumps(model_to_wire(bad)).encode("utf-8"))
            )
            answer = read_frame(lambda count: recv_exact(client_sock, count))
            body = json.loads(answer.decode("utf-8"))
            assert body["requestId"] == "03-missing"
            assert body["ok"] is False
            assert body["errors"][0]["code"] == "E_CONFLICT"
        finally:
            client_sock.close()
            thread.join(timeout=10)


def _serve_and_close(service: WorkspaceService, sock: socket.socket) -> None:
    try:
        service.serve(sock.recv, sock.sendall)
    finally:
        sock.close()


def test_oversized_frames_are_rejected_before_allocation(
    service_adapter: PlatformAdapter,
):
    with seeded_service(service_adapter) as service:
        # A single frame with a declared length above the limit answers
        # with one E_LIMIT error and no payload is read.
        declared = (1024 * 1024) + 1
        data = declared.to_bytes(4, "big") + b"never read"
        answer = service.process_frame(data)
        payload, _ = decode_frame(answer)
        body = json.loads(payload.decode("utf-8"))
        assert body["ok"] is False
        assert body["errors"][0]["code"] == "E_LIMIT"
        assert str(declared) in body["errors"][0]["message"]

        # Over the channel the declared length alone ends the session:
        # the service answers one error frame and stops reading, so the
        # stream cannot be resynchronized (SPEC.md section 12.1). No
        # payload bytes are sent or read.
        server_sock, client_sock = socket.socketpair()
        client_sock.settimeout(10)
        thread = threading.Thread(
            target=_serve_and_close, args=(service, server_sock), daemon=True
        )
        thread.start()
        try:
            client_sock.sendall(data[:4])
            answer = read_frame(lambda count: recv_exact(client_sock, count))
            body = json.loads(answer.decode("utf-8"))
            assert body["ok"] is False
            assert body["errors"][0]["code"] == "E_LIMIT"
            # The channel ends: the stream cannot be resynchronized.
            assert client_sock.recv(1) == b""
        finally:
            client_sock.close()
            thread.join(timeout=10)


def test_invalid_envelopes_answer_with_salvaged_request_ids(
    service: WorkspaceService,
):
    # A payload that is not JSON has no request id at all.
    answer = service.handle_request(b"not json at all")
    assert answer["ok"] is False
    assert answer["errors"][0]["code"] == "E_PARSE"
    # A structurally invalid envelope keeps its request id.
    payload = json.dumps(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": "01-bad",
            "method": "workspace.apply",
            "expected": None,
            "operations": [{"op": "parameter.set", "id": "x"}],
        }
    ).encode("utf-8")
    answer = service.handle_request(payload)
    assert answer.requestId == "01-bad"
    assert answer.ok is False
    assert answer.errors[0].code == "E_SCHEMA"
    # An unimplemented method is refused, not silently accepted.
    payload = json.dumps(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": "01-import",
            "method": "source.import",
            "payload": {
                "sourceId": "sales-input",
                "artifactToken": "melttok_example",
                "originLabel": "sales.csv",
            },
        }
    ).encode("utf-8")
    answer = service.handle_request(payload)
    assert answer.ok is False
    assert [(error.code, error.path) for error in answer.errors] == [
        ("E_SCHEMA", "/method")
    ]
