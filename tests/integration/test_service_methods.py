"""Service methods, preview, and inspection (T010, SPEC.md 12.4-12.6).

The tests drive the service methods of section 12.6 through the same
framed handlers a native host uses:

- ``session.hello`` negotiates protocol and language versions and
  reports the epoch and the limit table, without touching state.
- ``workspace.create`` answers the initial snapshot of an absent
  workspace whose sources and actions collections are empty.
- ``workspace.inspect`` returns the bounded document, entity
  revisions, parameter state, active runs, and drift diagnostics.
- ``workspace.preview`` plans the same transaction an apply would and
  reports diagnostics, affected records, controller replacements,
  state resets, invalidated runs, and the candidate hash, without
  writing anything.
- ``workspace.subscribe`` answers ordered changes or the successful
  snapshot-required status.
- ``control.activate`` routes a button press through its binding and
  answers a no-op or the successful approval-required admission
  result, which never grants permission.
- ``run.cancel`` and ``run.inspect`` answer and update run records
  idempotently.
- Artifact tokens are opaque, workspace-scoped, and expire after
  15 minutes (a companion file covers the token store itself).

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from melt.schema.canonical import canonical_hash, model_to_wire
from melt.schema.commands import RegistrationContract, parse_command
from runtime.compiler.catalog import ActionRegistry, RegistrationEntry
from runtime.storage import PlatformAdapter, WorkspaceStore, storage_worker_available
from runtime.workspace import (
    RUN_KIND,
    LANGUAGE_VERSION,
    SERVICE_IDENTIFIER,
    WorkspaceService,
)

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

THRESHOLD_PARAMETER = {
    "type": "string",
    "default": "all",
    "nullable": False,
    "constraints": {"maxLength": 16},
}

NOTES_PROJECTION = {
    "kind": "document",
    "title": "Notes",
    "source": None,
    "config": {"format": "markdown"},
    "controls": [{"id": "run", "kind": "button", "label": "Run"}],
}

MANUAL_BINDING = {
    "event": {"kind": "control.activate", "projection": "notes", "control": "run"},
    "effect": {"kind": "action.invoke", "action": "summarize"},
    "debounceMs": 0,
}


def notes_registry() -> ActionRegistry:
    contract = RegistrationContract.model_validate(
        {"parameters": {}, "inputs": {}, "outputs": {"notes": "text"}}
    )
    return ActionRegistry(
        {"notes-script-v1": RegistrationEntry("notes-script-v1", 1, contract)}
    )


def notes_action(**overrides) -> dict:
    action = {
        "implementation": {"registrationId": "notes-script-v1"},
        "parameters": {},
        "inputs": {},
        "outputs": {"notes": "text"},
        "outputGroup": "notes-group",
        "targets": {"notes": "notes"},
    }
    action.update(overrides)
    return action


def envelope(
    request_id: str,
    *,
    method: str,
    payload: "dict | None" = None,
    expected: "dict | None" = None,
    operations: "list | None" = None,
    workspace_id: str = WORKSPACE,
):
    body = {
        "protocolVersion": 1,
        "workspaceId": workspace_id,
        "requestId": request_id,
        "method": method,
        "expected": expected,
    }
    if operations is not None:
        body["operations"] = operations
    else:
        body["payload"] = payload
    return parse_command(body)


def apply_envelope(
    request_id: str,
    expected: "dict | None",
    operations: list,
    *,
    method: str = "workspace.apply",
):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": method,
            "expected": expected,
            "operations": operations,
        }
    )


def document_with(**collections) -> dict:
    document = json.loads(json.dumps(EMPTY_DOCUMENT))
    for name, entities in collections.items():
        document[name].update(entities)
    return document


def seed_definitions(
    adapter: PlatformAdapter,
    *,
    registry: ActionRegistry | None = None,
    action_value: "dict | None" = None,
    extra_projections: "dict[str, dict] | None" = None,
) -> WorkspaceService:
    """One service whose workspace holds a parameter, an action with a
    text output, its targeted document projection, and a button binding.

    ``extra_projections`` adds hidden projections the action may target,
    for workspaces whose registration declares more outputs.
    """

    if registry is None:
        registry = notes_registry()
    service = WorkspaceService.open(adapter, WORKSPACE, action_registry=registry)
    created = service.create_workspace(
        document_with(parameters={"threshold": THRESHOLD_PARAMETER}),
        "01-seed-create",
    )
    assert created.ok, created.errors
    extras = extra_projections or {}
    definitions = service.apply(
        apply_envelope(
            "01-seed-definitions",
            {
                "actions/summarize": 0,
                **{f"projections/{name}": 0 for name in extras},
                "projections/notes": 0,
                "bindings/manual-run": 0,
                "layout": 1,
            },
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "summarize",
                    "value": action_value or notes_action(),
                },
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "notes",
                    "value": NOTES_PROJECTION,
                },
                *(
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": name,
                        "value": value,
                    }
                    for name, value in extras.items()
                ),
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": "manual-run",
                    "value": MANUAL_BINDING,
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "notes"},
                        "hidden": sorted(extras),
                    },
                },
            ],
        )
    )
    assert definitions.ok, definitions.errors
    return service


def run_record_wire(
    run_id: str,
    *,
    action: str = "summarize",
    state: str = "queued",
    disposition: str = "none",
    outputs: "dict | None" = None,
) -> dict:
    """One complete run record wire (SPEC.md section 7.3)."""

    now = "2026-10-18T00:00:00Z"
    return {
        "recordType": "run",
        "schemaVersion": 1,
        "runId": run_id,
        "workspaceId": WORKSPACE,
        "actionId": action,
        "actionRevision": 1,
        "outputGroup": "notes-group",
        "outputGroupRevision": 1,
        "generation": 1,
        "registrationId": "notes-script-v1",
        "registrationRevision": 1,
        "scriptArtifact": "sha256:" + "1a" * 32,
        "environmentFingerprint": "cpython-3.12",
        "inputs": {},
        "parameters": {},
        "permissionDecision": None,
        "state": state,
        "disposition": disposition,
        "outputs": outputs or {},
        "createdAt": now,
        "updatedAt": now,
    }


def seed_run_record(adapter: PlatformAdapter, run_wire: dict) -> None:
    """Append one run record through the owning storage connection."""

    store = WorkspaceStore.open(adapter, WORKSPACE)
    try:
        store.append_records([(RUN_KIND, run_wire)])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# session.hello (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


def test_hello_negotiates_versions_and_reports_epoch_and_limits(
    service_adapter: PlatformAdapter,
):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        sequence = service.commit_sequence
        response = service.dispatch(
            envelope(
                "01-hello",
                method="session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/v0.1"],
                    "clientVersion": "harness/0.1",
                },
            )
        )
        assert response.ok, response.errors
        assert response.commitSequence == sequence, "a read observes its sequence"
        assert response.result["protocolVersion"] == 1
        assert response.result["language"] == LANGUAGE_VERSION
        assert response.result["serverVersion"] == SERVICE_IDENTIFIER
        assert response.result["clientVersion"] == "harness/0.1"
        assert response.result["epoch"] == service.epoch
        limits = response.result["limits"]
        assert limits["controlFrameBytes"] == 1024 * 1024
        assert limits["tablePageRows"] == 256
        assert limits["operationsPerMutation"] == 128
        # The handshake is a read: no receipt, no commit.
        assert service.receipt("01-hello") is None
        assert service.commit_sequence == sequence
        assert service.client["clientVersion"] == "harness/0.1"


def test_hello_rejects_versions_it_cannot_speak(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        protocol = service.dispatch(
            envelope(
                "01-hello",
                method="session.hello",
                payload={
                    "protocolVersions": [2, 3],
                    "languageVersions": ["melt/v0.1"],
                    "clientVersion": "harness/0.1",
                },
            )
        )
        assert protocol.ok is False
        assert [(error.code, error.path) for error in protocol.errors] == [
            ("E_VERSION", "/payload/protocolVersions")
        ]
        language = service.dispatch(
            envelope(
                "02-hello",
                method="session.hello",
                payload={
                    "protocolVersions": [1],
                    "languageVersions": ["melt/v0.2"],
                    "clientVersion": "harness/0.1",
                },
            )
        )
        assert language.ok is False
        assert [(error.code, error.path) for error in language.errors] == [
            ("E_VERSION", "/payload/languageVersions")
        ]


def test_hello_works_over_the_framed_channel(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        payload = json.dumps(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": "01-hello",
                "method": "session.hello",
                "payload": {
                    "protocolVersions": [1],
                    "languageVersions": ["melt/v0.1"],
                    "clientVersion": "harness/0.1",
                },
            }
        ).encode("utf-8")
        answer = service.handle_request(payload)
        assert answer.ok, answer.errors
        assert answer.result["protocolVersion"] == 1


# ---------------------------------------------------------------------------
# workspace.create (SPEC.md sections 12.6 and 12.4)
# ---------------------------------------------------------------------------


def test_create_answers_the_initial_snapshot(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = service.dispatch(
            envelope(
                "01-create",
                method="workspace.create",
                payload={
                    "document": document_with(
                        parameters={"minimum": MINIMUM_PARAMETER}
                    )
                },
            )
        )
        assert response.ok, response.errors
        result = response.result
        assert result["present"] is True
        assert result["workspaceId"] == WORKSPACE
        assert result["document"]["parameters"]["minimum"]["default"] == 0
        assert result["entityRevisions"] == {"parameters/minimum": 1, "layout": 1}
        assert result["parameterStates"]["minimum"] == {"value": 0.0, "revision": 1}
        assert result["incarnations"] == {"minimum": 1}
        assert result["activeRuns"] == []
        assert result["diagnostics"] == []
        # The same snapshot an inspection answers after the commit.
        inspected = service.dispatch(
            envelope("02-inspect", method="workspace.inspect", payload={})
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["document"] == result["document"]
        assert inspected.result["entityRevisions"] == result["entityRevisions"]


def test_create_rejects_an_expected_map(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = service.dispatch(
            envelope(
                "01-create",
                method="workspace.create",
                expected={"parameters/minimum": 0},
                payload={
                    "document": document_with(
                        parameters={"minimum": MINIMUM_PARAMETER}
                    )
                },
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_SCHEMA", "/expected")
        ]


# ---------------------------------------------------------------------------
# workspace.inspect (SPEC.md section 12.4)
# ---------------------------------------------------------------------------


def test_inspect_returns_the_committed_snapshot(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        sequence = service.commit_sequence
        response = service.dispatch(
            envelope("02-inspect", method="workspace.inspect", payload={})
        )
        assert response.ok, response.errors
        assert response.commitSequence == sequence, "a read observes its sequence"
        result = response.result
        assert result["present"] is True
        assert result["document"] == service.document
        assert result["entityRevisions"] == service.entity_revisions
        assert result["parameterStates"]["threshold"] == {
            "value": "all",
            "revision": 1,
        }
        assert result["activeRuns"] == []
        assert result["diagnostics"] == []
        assert service.receipt("02-inspect") is None
        assert service.commit_sequence == sequence, "inspection writes nothing"


def test_inspect_bounds_the_answer_by_collection_and_ids(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        response = service.dispatch(
            envelope(
                "02-inspect-parameters",
                method="workspace.inspect",
                payload={"collection": "parameters"},
            )
        )
        assert response.ok, response.errors
        result = response.result
        assert set(result["document"]) == {
            "language",
            "workspaceId",
            "title",
            "parameters",
        }
        assert result["entityRevisions"] == {"parameters/threshold": 1}
        assert result["parameterStates"] == {"threshold": {"value": "all", "revision": 1}}
        assert "layout" not in result["document"]

        filtered = service.dispatch(
            envelope(
                "03-inspect-ids",
                method="workspace.inspect",
                payload={"collection": "bindings", "ids": ["manual-run"]},
            )
        )
        assert filtered.ok, filtered.errors
        assert set(filtered.result["document"]["bindings"]) == {"manual-run"}
        assert filtered.result["entityRevisions"] == {"bindings/manual-run": 1}

        empty = service.dispatch(
            envelope(
                "04-inspect-absent-id",
                method="workspace.inspect",
                payload={"ids": ["ghost"]},
            )
        )
        assert empty.ok, empty.errors
        assert empty.result["document"]["parameters"] == {}
        assert empty.result["document"]["actions"] == {}
        assert empty.result["parameterStates"] == {}
        # The layout rides along with the unfiltered view; the entity
        # collections were bounded to identifiers that match nothing.
        assert empty.result["entityRevisions"] == {"layout": 2}


def test_inspect_on_an_absent_workspace_is_rejected(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = service.dispatch(
            envelope("01-inspect", method="workspace.inspect", payload={})
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_REFERENCE", "/workspaceId")
        ]


def test_inspect_reports_drift_and_stays_readable(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter, registry=notes_registry()):
        pass
    # The registration left the trusted inputs: the saved definitions
    # stay readable while the diagnostics name the disabled action.
    with WorkspaceService.open(service_adapter, WORKSPACE) as reopened:
        response = reopened.dispatch(
            envelope("02-inspect", method="workspace.inspect", payload={})
        )
        assert response.ok, response.errors
        pairs = [
            (item["code"], item["path"]) for item in response.result["diagnostics"]
        ]
        assert (
            "E_REGISTRATION",
            "/actions/summarize/implementation/registrationId",
        ) in pairs
        assert response.result["document"]["actions"]["summarize"] is not None
        assert response.result["present"] is True


def test_inspect_rejects_an_expected_map(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        response = service.dispatch(
            envelope(
                "02-inspect",
                method="workspace.inspect",
                expected={"parameters/threshold": 1},
                payload={},
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_SCHEMA", "/expected")
        ]


# ---------------------------------------------------------------------------
# workspace.preview (SPEC.md section 12.4)
# ---------------------------------------------------------------------------


def test_preview_reports_effects_without_writing(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        sequence = service.commit_sequence
        state_before = dict(service.parameter_state)
        request = apply_envelope(
            "02-preview-set",
            {"state/threshold": 1},
            [{"op": "parameter.set", "id": "threshold", "value": "recent"}],
            method="workspace.preview",
        )
        response = service.preview(request)
        assert response.ok, response.errors
        assert response.commitSequence == sequence, "a preview observes its sequence"
        result = response.result
        assert result["affectedRecords"] == ["state/threshold"]
        assert result["controllerReplacements"] == []
        assert result["stateResets"] == []
        assert result["invalidatedRuns"] == []
        assert result["parameterStateChanges"] == {
            "threshold": {"value": "recent", "revision": 2}
        }
        assert result["candidateHash"].startswith("sha256:")
        # No writes: no receipt, no state change, no sequence advance.
        assert service.receipt("02-preview-set") is None
        assert service.commit_sequence == sequence
        assert service.parameter_state == state_before
        # A preview reserves nothing: the same request id applies cleanly.
        applied = service.apply(
            apply_envelope(
                "02-preview-set",
                {"state/threshold": 1},
                [{"op": "parameter.set", "id": "threshold", "value": "recent"}],
            )
        )
        assert applied.ok, applied.errors
        assert service.parameter_state["threshold"].value == "recent"


def test_preview_matches_the_hash_of_the_applied_candidate(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        replace = {
            "op": "entity.replace",
            "collection": "parameters",
            "id": "threshold",
            "value": {
                "type": "string",
                "default": "all",
                "nullable": False,
                "constraints": {"maxLength": 8},
            },
        }
        previewed = service.preview(
            apply_envelope(
                "02-preview-replace",
                {"parameters/threshold": 1},
                [replace],
                method="workspace.preview",
            )
        )
        assert previewed.ok, previewed.errors
        assert previewed.result["affectedRecords"] == ["parameters/threshold"]
        applied = service.apply(
            apply_envelope("03-apply-replace", {"parameters/threshold": 1}, [replace])
        )
        assert applied.ok, applied.errors
        settled = service.preview(
            apply_envelope(
                "04-preview-noop", {"parameters/threshold": 2}, [], method="workspace.preview"
            )
        )
        assert settled.ok, settled.errors
        assert settled.result["candidateHash"] == previewed.result["candidateHash"]


def test_preview_rejects_invalid_candidates_and_stale_expectations(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        sequence = service.commit_sequence
        invalid = service.preview(
            apply_envelope(
                "02-preview-invalid",
                {"layout": 2},
                [
                    {
                        "op": "layout.replace",
                        "value": {
                            "root": {"kind": "pane", "projection": "ghost"},
                            "hidden": [],
                        },
                    }
                ],
                method="workspace.preview",
            )
        )
        assert invalid.ok is False
        pairs = [(error.code, error.path) for error in invalid.errors]
        assert ("E_LAYOUT", "/layout/root/projection") in pairs
        stale = service.preview(
            apply_envelope(
                "03-preview-stale",
                {"parameters/threshold": 9},
                [],
                method="workspace.preview",
            )
        )
        assert stale.ok is False
        assert [(error.code, error.path) for error in stale.errors] == [
            ("E_CONFLICT", "/expected/parameters~1threshold")
        ]
        assert service.commit_sequence == sequence, "neither preview wrote"


def test_preview_reports_controller_replacements(service_adapter: PlatformAdapter):
    contract = RegistrationContract.model_validate(
        {
            "parameters": {},
            "inputs": {},
            "outputs": {"notes": "text", "spare-out": "text", "rows": "table"},
        }
    )
    lean = RegistrationContract.model_validate(
        {"parameters": {}, "inputs": {}, "outputs": {"notes": "text", "rows": "table"}}
    )
    registry = ActionRegistry(
        {
            "notes-script-v1": RegistrationEntry("notes-script-v1", 1, contract),
            "lean-script-v1": RegistrationEntry("lean-script-v1", 1, lean),
        }
    )
    wide_action = notes_action(
        outputs={"notes": "text", "spare-out": "text", "rows": "table"},
        targets={"notes": "notes", "spare-out": "spare", "rows": "rows"},
    )
    with seed_definitions(
        service_adapter,
        registry=registry,
        action_value=wide_action,
        extra_projections={
            "spare": dict(NOTES_PROJECTION, title="Spare"),
            "rows": {
                "kind": "table",
                "title": "Rows",
                "source": None,
                "config": {"columns": None, "selection": "none"},
                "controls": [],
            },
        },
    ) as service:
        # The kind change of a surviving projection ID replaces its
        # controller; a same-kind recreation keeps it (SPEC.md 8.5).
        kind_change = [
            {
                "op": "entity.delete",
                "collection": "projections",
                "id": "notes",
            },
            {
                "op": "entity.create",
                "collection": "projections",
                "id": "notes",
                "value": {
                    "kind": "table",
                    "title": "Notes",
                    "source": None,
                    "config": {"columns": None, "selection": "none"},
                    # The bound button survives the kind change.
                    "controls": [{"id": "run", "kind": "button", "label": "Run"}],
                },
            },
            {
                "op": "entity.replace",
                "collection": "actions",
                "id": "summarize",
                "value": {
                    "implementation": {"registrationId": "lean-script-v1"},
                    "parameters": {},
                    "inputs": {},
                    "outputs": {"notes": "text", "rows": "table"},
                    "outputGroup": "notes-group",
                    "targets": {"notes": "spare", "rows": "notes"},
                },
            },
            {
                # The lean registration has one output fewer, so the
                # table it no longer targets leaves with it.
                "op": "entity.delete",
                "collection": "projections",
                "id": "rows",
            },
            {
                "op": "entity.delete",
                "collection": "projections",
                "id": "spare",
            },
            {
                "op": "entity.create",
                "collection": "projections",
                "id": "spare",
                "value": dict(NOTES_PROJECTION, title="Spare"),
            },
            {
                "op": "layout.replace",
                "value": {
                    "root": {"kind": "pane", "projection": "notes"},
                    "hidden": ["spare"],
                },
            },
        ]
        expectations = {
            "projections/notes": 1,
            "projections/spare": 1,
            "projections/rows": 1,
            "actions/summarize": 1,
            "layout": 2,
        }
        previewed = service.preview(
            apply_envelope(
                "03-preview-kinds",
                expectations,
                kind_change,
                method="workspace.preview",
            )
        )
        assert previewed.ok, previewed.errors
        assert previewed.result["controllerReplacements"] == [
            {"projection": "notes", "fromKind": "document", "toKind": "table"}
        ], "a surviving ID whose kind changed replaces its controller"
        assert "spare" not in [
            item["projection"] for item in previewed.result["controllerReplacements"]
        ], "a same-kind recreation keeps the controller"
        assert previewed.result["stateResets"] == []
        # The report is truthful: the same transaction applies.
        applied = service.apply(
            apply_envelope("04-apply-kinds", expectations, kind_change)
        )
        assert applied.ok, applied.errors
        assert service.document["projections"]["notes"]["kind"] == "table"


def test_preview_reports_state_resets(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        created = service.preview(
            apply_envelope(
                "02-preview-create",
                {"parameters/extra": 0},
                [
                    {
                        "op": "entity.create",
                        "collection": "parameters",
                        "id": "extra",
                        "value": MINIMUM_PARAMETER,
                    }
                ],
                method="workspace.preview",
            )
        )
        assert created.ok, created.errors
        assert created.result["stateResets"] == ["extra"]
        assert created.result["parameterStateChanges"] == {
            "extra": {"value": 0.0, "revision": 1}
        }
        deleted = service.preview(
            apply_envelope(
                "03-preview-delete",
                {"parameters/threshold": 1, "state/threshold": 1},
                [
                    {
                        "op": "entity.delete",
                        "collection": "parameters",
                        "id": "threshold",
                    }
                ],
                method="workspace.preview",
            )
        )
        assert deleted.ok, deleted.errors
        assert deleted.result["stateResets"] == ["threshold"]
        assert deleted.result["parameterStateChanges"] == {}


def test_preview_on_an_absent_workspace_is_rejected(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = service.preview(
            apply_envelope("01-preview", None, [], method="workspace.preview")
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_REFERENCE", "/workspaceId")
        ]


# ---------------------------------------------------------------------------
# workspace.subscribe (SPEC.md section 12.5)
# ---------------------------------------------------------------------------


def test_subscribe_answers_changes_or_requires_a_snapshot(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        assert service.apply(
            apply_envelope(
                "02-set",
                {"state/threshold": 1},
                [{"op": "parameter.set", "id": "threshold", "value": "recent"}],
            )
        ).ok
        response = service.dispatch(
            envelope(
                "03-subscribe",
                method="workspace.subscribe",
                payload={"afterSequence": 1, "epoch": service.epoch},
            )
        )
        assert response.ok, response.errors
        assert response.result["status"] == "changes"
        sequences = [item["commitSequence"] for item in response.result["changes"]]
        assert sequences == [2, 3, 4]  # creation, definitions, parameter set
        mismatched = service.dispatch(
            envelope(
                "04-subscribe-epoch",
                method="workspace.subscribe",
                payload={"afterSequence": 1, "epoch": service.epoch + 1},
            )
        )
        assert mismatched.ok, mismatched.errors
        assert mismatched.result["status"] == "snapshot-required"
        assert "epoch" in mismatched.result["reason"]
        beyond = service.dispatch(
            envelope(
                "05-subscribe-beyond",
                method="workspace.subscribe",
                payload={"afterSequence": 99, "epoch": service.epoch},
            )
        )
        assert beyond.ok, beyond.errors
        assert beyond.result["status"] == "snapshot-required"


# ---------------------------------------------------------------------------
# control.activate (SPEC.md sections 12.6, 13.1)
# ---------------------------------------------------------------------------


def test_control_activate_requires_a_button_of_a_known_projection(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        unknown_projection = service.dispatch(
            envelope(
                "02-activate",
                method="control.activate",
                payload={"projectionId": "ghost", "controlId": "run"},
            )
        )
        assert unknown_projection.ok is False
        assert [(error.code, error.path) for error in unknown_projection.errors] == [
            ("E_REFERENCE", "/payload/projectionId")
        ]
        unknown_control = service.dispatch(
            envelope(
                "03-activate",
                method="control.activate",
                payload={"projectionId": "notes", "controlId": "ghost"},
            )
        )
        assert unknown_control.ok is False
        assert [(error.code, error.path) for error in unknown_control.errors] == [
            ("E_REFERENCE", "/payload/controlId")
        ]
        # A document projection carries no non-button control here, so
        # add one and prove only buttons activate.
        added = service.apply(
            apply_envelope(
                "04-add-text-control",
                {"projections/notes": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "projections",
                        "id": "notes",
                        "value": {
                            "kind": "document",
                            "title": "Notes",
                            "source": None,
                            "config": {"format": "markdown"},
                            "controls": [
                                dict(NOTES_PROJECTION["controls"][0]),
                                {
                                    "id": "threshold",
                                    "kind": "text",
                                    "label": "Threshold",
                                    "parameter": "threshold",
                                },
                            ],
                        },
                    }
                ],
            )
        )
        assert added.ok, added.errors
        not_a_button = service.dispatch(
            envelope(
                "05-activate-text",
                method="control.activate",
                expected={"projections/notes": 2},
                payload={"projectionId": "notes", "controlId": "threshold"},
            )
        )
        assert not_a_button.ok is False
        assert [(error.code, error.path) for error in not_a_button.errors] == [
            ("E_TYPE", "/payload/controlId")
        ]


def test_control_activate_without_a_binding_is_a_reported_no_op(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        removed = service.apply(
            apply_envelope(
                "02-remove-binding",
                {"bindings/manual-run": 1},
                [{"op": "entity.delete", "collection": "bindings", "id": "manual-run"}],
            )
        )
        assert removed.ok, removed.errors
        sequence = service.commit_sequence
        response = service.dispatch(
            envelope(
                "03-activate",
                method="control.activate",
                expected={"projections/notes": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert response.ok, response.errors
        assert response.result["status"] == "no-op"
        assert response.result["origin"] == "user"
        assert service.commit_sequence == sequence + 1, "the receipt commits once"
        # The stored receipt replays the same answer.
        replay = service.dispatch(
            envelope(
                "03-activate",
                method="control.activate",
                expected={"projections/notes": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert replay.ok, replay.errors
        assert replay.commitSequence == response.commitSequence
        assert service.commit_sequence == sequence + 1


def test_control_activate_admits_to_approval_required(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter, registry=notes_registry()) as service:
        sequence = service.commit_sequence
        response = service.dispatch(
            envelope(
                "02-activate",
                method="control.activate",
                expected={"projections/notes": 1, "bindings/manual-run": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert response.ok is True, response.errors
        result = response.result
        # Approval-required is a successful admission result: it names
        # the missing grant, executes nothing, and grants nothing.
        assert result["status"] == "approvalRequired"
        assert result["action"] == "summarize"
        assert result["binding"] == "manual-run"
        assert result["requiredPermission"] == "execute_project_code"
        assert result["origin"] == "user"
        assert service.runs == {}, "no run was admitted or executed"
        assert service.commit_sequence == sequence + 1
        assert service.receipt("02-activate") is not None
        # Reuse of the request id with different content is refused.
        reused = service.dispatch(
            envelope(
                "02-activate",
                method="control.activate",
                expected={"projections/notes": 1, "bindings/manual-run": 1},
                payload={"projectionId": "notes", "controlId": "ghost"},
            )
        )
        assert reused.ok is False
        assert [(error.code, error.path) for error in reused.errors] == [
            ("E_REQUEST_REUSE", "/requestId")
        ]


def test_control_activate_needs_the_projection_and_binding_revisions(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        missing = service.dispatch(
            envelope(
                "02-activate",
                method="control.activate",
                expected={"projections/notes": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert missing.ok is False
        assert [(error.code, error.path) for error in missing.errors] == [
            ("E_CONFLICT", "/expected")
        ]
        assert "bindings/manual-run" in missing.errors[0].message
        stale = service.dispatch(
            envelope(
                "03-activate",
                method="control.activate",
                expected={"projections/notes": 9, "bindings/manual-run": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert stale.ok is False
        assert [(error.code, error.path) for error in stale.errors] == [
            ("E_CONFLICT", "/expected/projections~1notes")
        ]


def test_control_activate_reports_a_disabled_registration(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter, registry=notes_registry()):
        pass
    with WorkspaceService.open(service_adapter, WORKSPACE) as reopened:
        response = reopened.dispatch(
            envelope(
                "02-activate",
                method="control.activate",
                expected={"projections/notes": 1, "bindings/manual-run": 1},
                payload={"projectionId": "notes", "controlId": "run"},
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            (
                "E_REGISTRATION",
                "/actions/summarize/implementation/registrationId",
            )
        ]
        assert reopened.receipt("02-activate") is None


# ---------------------------------------------------------------------------
# run.cancel and run.inspect (SPEC.md sections 12.6, 13.5)
# ---------------------------------------------------------------------------


def seeded_run_service(
    adapter: PlatformAdapter,
    run_wire: dict,
    *,
    registry: "ActionRegistry | None" = None,
) -> WorkspaceService:
    """One reopened service whose record stream holds one run record."""

    seed_run_record(adapter, run_wire)
    return WorkspaceService.open(adapter, WORKSPACE, action_registry=registry)


def test_run_inspect_returns_the_run_and_its_outputs(service_adapter: PlatformAdapter):
    outputs = {"notes": {"sourceId": "notes-out", "version": 3}}
    with seed_definitions(service_adapter, registry=notes_registry()):
        pass
    service = seeded_run_service(
        service_adapter,
        run_record_wire(
            "01-run-a",
            state="succeeded",
            disposition="current",
            outputs=outputs,
        ),
    )
    try:
        response = service.dispatch(
            envelope(
                "02-inspect-run",
                method="run.inspect",
                payload={"runId": "01-run-a"},
            )
        )
        assert response.ok, response.errors
        assert response.result["run"]["runId"] == "01-run-a"
        assert response.result["run"]["state"] == "succeeded"
        assert response.result["run"]["disposition"] == "current"
        assert response.result["outputs"] == outputs
        assert service.receipt("02-inspect-run") is None, "reads store no receipt"
        unknown = service.dispatch(
            envelope(
                "03-inspect-run",
                method="run.inspect",
                payload={"runId": "ghost"},
            )
        )
        assert unknown.ok is False
        assert [(error.code, error.path) for error in unknown.errors] == [
            ("E_REFERENCE", "/payload/runId")
        ]
    finally:
        service.close()


def test_run_cancel_is_idempotent_and_needs_no_expectation(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter, registry=notes_registry()):
        pass
    service = seeded_run_service(
        service_adapter, run_record_wire("01-run-a", state="queued"), registry=notes_registry()
    )
    try:
        inspect = service.dispatch(
            envelope("02-inspect", method="workspace.inspect", payload={})
        )
        assert inspect.ok, inspect.errors
        assert [run["runId"] for run in inspect.result["activeRuns"]] == ["01-run-a"]

        cancelled = service.dispatch(
            envelope(
                "03-cancel",
                method="run.cancel",
                payload={"runId": "01-run-a"},
            )
        )
        assert cancelled.ok, cancelled.errors
        assert cancelled.result == {
            "runId": "01-run-a",
            "state": "cancelled",
            "cancellation": {"status": "cancelled"},
        }
        window = service.changes_after(cancelled.commitSequence - 1, service.epoch)
        assert window.changes[-1]["changedRecords"] == ["runs/01-run-a"]

        again = service.dispatch(
            envelope(
                "04-cancel-again",
                method="run.cancel",
                payload={"runId": "01-run-a"},
            )
        )
        assert again.ok, again.errors
        assert again.result["cancellation"]["status"] == "alreadyCancelled"
        assert again.result["state"] == "cancelled"

        # The replayed first request answers its stored response.
        replay = service.dispatch(
            envelope(
                "03-cancel",
                method="run.cancel",
                payload={"runId": "01-run-a"},
            )
        )
        assert replay.ok, replay.errors
        assert replay.commitSequence == cancelled.commitSequence
    finally:
        service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.runs["01-run-a"].state == "cancelled"
        inspected = reopened.dispatch(
            envelope(
                "05-inspect",
                method="workspace.inspect",
                payload={},
            )
        )
        assert inspected.ok, inspected.errors
        assert inspected.result["activeRuns"] == [], "a cancelled run is not active"
    finally:
        reopened.close()


def test_run_cancel_on_a_finished_run_reports_it_is_not_active(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter):
        pass
    service = seeded_run_service(
        service_adapter,
        run_record_wire("01-run-done", state="succeeded", disposition="current"),
    )
    try:
        response = service.dispatch(
            envelope(
                "02-cancel",
                method="run.cancel",
                payload={"runId": "01-run-done"},
            )
        )
        assert response.ok, response.errors
        assert response.result["cancellation"]["status"] == "notActive"
        assert response.result["state"] == "succeeded"
        assert service.runs["01-run-done"].state == "succeeded"
        unknown = service.dispatch(
            envelope(
                "03-cancel",
                method="run.cancel",
                payload={"runId": "ghost"},
            )
        )
        assert unknown.ok is False
        assert [(error.code, error.path) for error in unknown.errors] == [
            ("E_REFERENCE", "/payload/runId")
        ]
    finally:
        service.close()


def test_preview_reports_invalidated_runs(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter, registry=notes_registry()):
        pass
    service = seeded_run_service(
        service_adapter, run_record_wire("01-run-a", state="queued"), registry=notes_registry()
    )
    try:
        # Even an equivalent action replacement invalidates its
        # admitted runs (SPEC.md section 13.3).
        replaced = service.preview(
            apply_envelope(
                "02-preview-action",
                {"actions/summarize": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "actions",
                        "id": "summarize",
                        "value": notes_action(),
                    }
                ],
                method="workspace.preview",
            )
        )
        assert replaced.ok, replaced.errors
        assert replaced.result["invalidatedRuns"] == ["01-run-a"]
        untouched = service.preview(
            apply_envelope(
                "03-preview-set",
                {"state/threshold": 1},
                [{"op": "parameter.set", "id": "threshold", "value": "recent"}],
                method="workspace.preview",
            )
        )
        assert untouched.ok, untouched.errors
        assert untouched.result["invalidatedRuns"] == []
        # Make the queued run terminal before the historical check.
        cancelled = service.dispatch(
            envelope(
                "04-cancel",
                method="run.cancel",
                payload={"runId": "01-run-a"},
            )
        )
        assert cancelled.ok, cancelled.errors
    finally:
        service.close()

    seed_run_record(service_adapter, run_record_wire("01-run-b", state="succeeded"))
    historical_service = WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=notes_registry()
    )
    try:
        historical = historical_service.preview(
            apply_envelope(
                "05-preview-action",
                {"actions/summarize": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "actions",
                        "id": "summarize",
                        "value": notes_action(),
                    }
                ],
                method="workspace.preview",
            )
        )
        assert historical.ok, historical.errors
        assert historical.result["invalidatedRuns"] == []
    finally:
        historical_service.close()


# ---------------------------------------------------------------------------
# Artifact tokens through the service (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


def test_service_tokens_capture_redeem_and_stay_workspace_scoped(
    service_adapter: PlatformAdapter,
):
    png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        captured = service.capture_artifact_token(png, "image/png")
        assert captured.kind == "image"
        assert captured.byteLength == len(png)
        assert (
            captured.artifact
            == "sha256:" + hashlib.sha256(png).hexdigest()
        )
        assert service.store.artifacts.contains(captured.artifact)
        redeemed = service.redeem_artifact_token(captured.token)
        assert redeemed == captured
        assert service.redeem_artifact_token("melttok_unknown") is None

    with WorkspaceService.open(service_adapter, "other-workspace") as other:
        # A token is scoped to the workspace that minted it.
        assert other.redeem_artifact_token(captured.token) is None


# ---------------------------------------------------------------------------
# Envelope policy (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


def test_payload_methods_reject_operations_and_versa(
    service_adapter: PlatformAdapter,
):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        payload = json.dumps(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": "01-shape",
                "method": "workspace.inspect",
                "operations": [],
            }
        ).encode("utf-8")
        answer = service.handle_request(payload)
        assert answer.ok is False
        assert answer.errors[0].code == "E_SCHEMA"


def test_dispatch_rejects_a_foreign_workspace(service_adapter: PlatformAdapter):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        response = service.dispatch(
            envelope(
                "01-foreign",
                method="workspace.inspect",
                payload={},
                workspace_id="other-workspace",
            )
        )
        assert response.ok is False
        assert [(error.code, error.path) for error in response.errors] == [
            ("E_REFERENCE", "/workspaceId")
        ]


def test_receipt_hash_covers_the_whole_envelope(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        request = envelope(
            "02-activate",
            method="control.activate",
            expected={"projections/notes": 1, "bindings/manual-run": 1},
            payload={"projectionId": "notes", "controlId": "run"},
        )
        assert service.dispatch(request).ok
        stored = service.receipt("02-activate")
        assert stored["requestPayloadHash"] == canonical_hash(model_to_wire(request))
        assert stored["method"] == "control.activate"
