"""Execution permissions and the permission rechecks (T014, SPEC.md 15.1).

Document validity is not execution authority. These tests prove the
grant machinery end to end against an isolated workspace:

- Specific-run and project execution grants are stored in the service
  record stream, outside every exported document, and survive restart.
- Only the authenticated host permission endpoint mints and revokes
  them: no command envelope method, document field, or registration
  contract can grant anything.
- Admission rechecks the grant set: a missing grant answers the
  successful approval-required result with the captured intent the
  host approval binds to; a covering grant answers the authority
  decision without admitting a run.
- A specific-run grant covers exactly its captured intent and stops
  covering when the definitions it pinned change.
- The launch and publication rechecks deny a run whose authority was
  revoked, which blocks publication and requests cancellation.
- Grant and revoke transactions advance the commit sequence once and
  notify the permissions record.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from melt.schema.commands import (
    RegistrationContract,
    parse_command,
)
from melt.schema.document import parse_document
from runtime.compiler.catalog import ActionRegistry, RegistrationEntry
from runtime.storage import PlatformAdapter, WorkspaceStore, storage_worker_available
from runtime.workspace import (
    GRANT_KINDS,
    INTENT_ID_PREFIX,
    PERMISSION_KIND,
    RUN_KIND,
    WorkspaceService,
    authority_at_admission,
    is_live,
    revocation_blocks,
)
from runtime.workspace.permissions import MeltPermissionError

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

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
    """The registry the seeded action maps onto, with a parameter slot.

    The fixture entries cite the admission facts of SPEC.md section 7.3
    — a script hash and an environment fingerprint — so an admission
    against them captures a full invocation snapshot. The stored bytes
    do not exist, so such an admission can never launch; launching
    belongs to the scheduling suite, which registers real scripts.
    """

    contract = RegistrationContract.model_validate(
        {
            "parameters": {
                "threshold": {
                    "type": "string",
                    "nullable": False,
                    "constraints": {"maxLength": 16},
                }
            },
            "inputs": {},
            "outputs": {"notes": "text"},
        }
    )
    lean = RegistrationContract.model_validate(
        {"parameters": {}, "inputs": {}, "outputs": {"notes": "text"}}
    )
    return ActionRegistry(
        {
            "notes-script-v1": RegistrationEntry(
                "notes-script-v1",
                1,
                contract,
                script_artifact="sha256:" + "2b" * 32,
                environment_fingerprint="cpython-3.12",
            ),
            "lean-script-v1": RegistrationEntry(
                "lean-script-v1",
                1,
                lean,
                script_artifact="sha256:" + "3c" * 32,
                environment_fingerprint="cpython-3.12",
            ),
        }
    )


def notes_action(**overrides) -> dict:
    action = {
        "implementation": {"registrationId": "notes-script-v1"},
        "parameters": {"threshold": {"parameter": "threshold"}},
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
):
    body = {
        "protocolVersion": 1,
        "workspaceId": WORKSPACE,
        "requestId": request_id,
        "method": method,
        "expected": expected,
    }
    if payload is not None:
        body["payload"] = payload
    return parse_command(body)


def apply_envelope(request_id: str, expected: "dict | None", operations: list):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": "workspace.apply",
            "expected": expected,
            "operations": operations,
        }
    )


def seed_definitions(adapter: PlatformAdapter) -> WorkspaceService:
    """One service holding a parameter, an action, its targeted
    projection, and the button binding that invokes the action."""

    service = WorkspaceService.open(adapter, WORKSPACE, action_registry=notes_registry())
    created = service.create_workspace(
        {
            "language": "melt/v0.1",
            "workspaceId": WORKSPACE,
            "title": "Sales analysis",
            "sources": {},
            "parameters": {"threshold": THRESHOLD_PARAMETER},
            "actions": {},
            "projections": {},
            "bindings": {},
            "layout": {"root": None, "hidden": []},
        },
        "01-seed-create",
    )
    assert created.ok, created.errors
    definitions = service.apply(
        apply_envelope(
            "01-seed-definitions",
            {
                "actions/summarize": 0,
                "projections/notes": 0,
                "bindings/manual-run": 0,
                "layout": 1,
            },
            [
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "summarize",
                    "value": notes_action(),
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
                    "id": "manual-run",
                    "value": MANUAL_BINDING,
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
    return service


def activate(service: WorkspaceService, request_id: str):
    """Dispatch one button activation with current revisions."""

    return service.dispatch(
        envelope(
            request_id,
            method="control.activate",
            expected={"projections/notes": 1, "bindings/manual-run": 1},
            payload={"projectionId": "notes", "controlId": "run"},
        )
    )


def grant_project(service: WorkspaceService, registration_id: str = "notes-script-v1"):
    return service.grant_execution_permission(
        kind="projectExecution",
        registration_id=registration_id,
        granted_by="host-permission-control/run-approval",
    )


def run_record_wire(
    run_id: str,
    *,
    permission_decision: "str | None",
    registration_id: str = "notes-script-v1",
) -> dict:
    now = "2026-10-18T00:00:00Z"
    return {
        "recordType": "run",
        "schemaVersion": 1,
        "runId": run_id,
        "workspaceId": WORKSPACE,
        "actionId": "summarize",
        "actionRevision": 1,
        "outputGroup": "notes-group",
        "outputGroupRevision": 1,
        "generation": 1,
        "registrationId": registration_id,
        "registrationRevision": 1,
        "scriptArtifact": "sha256:" + "1a" * 32,
        "environmentFingerprint": "cpython-3.12",
        "inputs": {},
        "parameters": {},
        "permissionDecision": permission_decision,
        "state": "running",
        "disposition": "none",
        "outputs": {},
        "createdAt": now,
        "updatedAt": now,
    }


def seed_run_record(adapter: PlatformAdapter, run_wire: dict) -> None:
    store = WorkspaceStore.open(adapter, WORKSPACE)
    try:
        store.append_records([(RUN_KIND, run_wire)])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Grants live outside documents and survive restart (SPEC.md 15.1)
# ---------------------------------------------------------------------------


def test_grants_live_outside_the_document_and_survive_restart(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        document_before = service.document
        grant = grant_project(service)
        assert grant.grantId.startswith("meltgrant_")
        assert grant.kind in GRANT_KINDS
        assert grant.requiredPermission == "execute_project_code"
        # The grant never enters the document or a snapshot view.
        assert service.document == document_before
        assert "permissions" not in service.document
        inspected = service.dispatch(
            envelope("02-inspect", method="workspace.inspect", payload={})
        )
        assert inspected.ok, inspected.errors
        assert "permissions" not in inspected.result["document"]

    with WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=notes_registry()
    ) as reopened:
        restored = reopened.grants[grant.grantId]
        assert restored == grant, "the grant rebuilt from its record"
        assert is_live(restored)
        # And the restored grant still covers admission.
        response = activate(reopened, "03-activate")
        assert response.ok, response.errors
        assert response.result["status"] == "granted"
        assert response.result["permissionDecision"] == grant.grantId


def test_grant_records_rebuild_from_the_permission_record_kind(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        grant = grant_project(service)
    store = WorkspaceStore.open(service_adapter, WORKSPACE)
    try:
        kinds = [record.kind for record in store.records()]
        payloads = [
            record.data for record in store.records()
            if record.kind == PERMISSION_KIND
        ]
    finally:
        store.close()
    assert PERMISSION_KIND in kinds
    assert [payload["grantId"] for payload in payloads] == [grant.grantId]


# ---------------------------------------------------------------------------
# Admission recheck (SPEC.md sections 13.1 and 15.1)
# ---------------------------------------------------------------------------


def test_admission_without_a_grant_captures_the_intent(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        sequence = service.commit_sequence
        response = activate(service, "02-activate")
        assert response.ok, response.errors
        result = response.result
        assert result["status"] == "approvalRequired"
        assert result["reason"] == "missing-grant"
        assert result["requiredPermission"] == "execute_project_code"
        intent = result["intent"]
        assert intent["intentRef"].startswith(INTENT_ID_PREFIX)
        assert intent["actionId"] == "summarize"
        assert intent["actionRevision"] == 1
        assert intent["registrationId"] == "notes-script-v1"
        assert intent["registrationRevision"] == 1
        assert intent["outputGroup"] == "notes-group"
        assert intent["capturedParameters"] == {
            "threshold": {"parameter": "threshold", "value": "all", "stateRevision": 1}
        }
        assert intent["capturedInputs"] == {}
        # The admission state executes and grants nothing.
        assert service.runs == {}
        assert service.commit_sequence == sequence + 1
        assert service.receipt("02-activate") is not None


def test_a_project_grant_answers_the_authority_decision(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        grant = grant_project(service)
        sequence = service.commit_sequence
        response = activate(service, "02-activate")
        assert response.ok, response.errors
        result = response.result
        assert result["status"] == "granted"
        assert result["permissionDecision"] == grant.grantId
        assert result["requiredPermission"] == "execute_project_code"
        # The granted activation admits a real queued run in the same
        # transaction: a full invocation snapshot, a reserved
        # generation, and the permission decision on the record
        # (SPEC.md sections 7.3 and 13.1).
        run_summary = result["run"]
        assert run_summary["state"] == "queued"
        assert run_summary["generation"] == 1
        assert run_summary["permissionDecision"] == grant.grantId
        run = service.runs[run_summary["runId"]]
        assert run.actionId == "summarize"
        assert run.actionRevision == 1
        assert run.registrationId == "notes-script-v1"
        assert run.registrationRevision == 1
        assert run.scriptArtifact == "sha256:" + "2b" * 32
        assert run.environmentFingerprint == "cpython-3.12"
        assert run.outputGroup == "notes-group"
        assert run.outputGroupRevision == 1
        assert run.permissionDecision == grant.grantId
        assert service.output_groups["notes-group"].generation == 1
        assert service.commit_sequence == sequence + 1
        # The receipt replays the same admission answer: one admitted
        # run, never two (SPEC.md section 12.6).
        replay = activate(service, "02-activate")
        assert replay.ok, replay.errors
        assert replay.result["permissionDecision"] == grant.grantId
        assert service.commit_sequence == sequence + 1
        assert list(service.runs) == [run.runId]


def test_a_project_grant_covers_only_its_registration(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        grant_project(service)
        assert activate(service, "02-activate").result["status"] == "granted"
        # An edited script is a new registration the grant does not
        # cover (SPEC.md section 7.2).
        switched = service.apply(
            apply_envelope(
                "03-switch-registration",
                {"actions/summarize": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "actions",
                        "id": "summarize",
                        "value": notes_action(
                            implementation={"registrationId": "lean-script-v1"},
                            parameters={},
                        ),
                    }
                ],
            )
        )
        assert switched.ok, switched.errors
        response = activate(service, "04-activate")
        assert response.ok, response.errors
        assert response.result["status"] == "approvalRequired"
        assert response.result["intent"]["registrationId"] == "lean-script-v1"


def test_a_specific_run_grant_covers_exactly_its_captured_intent(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        first = activate(service, "02-activate")
        intent = first.result["intent"]
        grant = service.grant_execution_permission(
            kind="specificRun",
            registration_id="notes-script-v1",
            granted_by="host-permission-control/run-approval",
            action_id=intent["actionId"],
            action_revision=intent["actionRevision"],
            registration_revision=intent["registrationRevision"],
            output_group=intent["outputGroup"],
            intent_id=intent["intentRef"],
        )
        decision = authority_at_admission(
            service.grants,
            required_permission="execute_project_code",
            registration_id="notes-script-v1",
            registration_revision=1,
            action_id="summarize",
            action_revision=1,
            output_group="notes-group",
            intent_id=intent["intentRef"],
        )
        assert decision.granted and decision.grant_id == grant.grantId
        # A newer request is a new intent: the approval of the older
        # one never executes it (SPEC.md section 12.6).
        second = activate(service, "03-activate")
        assert second.result["status"] == "approvalRequired"
        assert (
            second.result["intent"]["intentRef"] != intent["intentRef"]
        ), "each admission captures its own intent"
        # A changed action revision expires the pinned approval.
        replaced = service.apply(
            apply_envelope(
                "04-replace-action",
                {"actions/summarize": 1},
                [
                    {
                        "op": "entity.replace",
                        "collection": "actions",
                        "id": "summarize",
                        "value": notes_action(),
                    }
                ],
            )
        )
        assert replaced.ok, replaced.errors
        # A changed action revision expires the pinned approval: the
        # captured intent rechecked against the current definitions no
        # longer fits its grant (SPEC.md section 12.6).
        drifted = authority_at_admission(
            service.grants,
            required_permission="execute_project_code",
            registration_id="notes-script-v1",
            registration_revision=1,
            action_id="summarize",
            action_revision=2,
            output_group="notes-group",
            intent_id=intent["intentRef"],
        )
        assert not drifted.granted
        assert drifted.reason == "grant-no-longer-covers"
        # A fresh admission after the change captures the new revision
        # and needs its own approval.
        third = activate(service, "05-activate")
        assert third.ok, third.errors
        assert third.result["status"] == "approvalRequired"
        assert third.result["intent"]["actionRevision"] == 2
        assert third.result["intent"]["intentRef"] != intent["intentRef"]


def test_a_live_project_grant_outlives_a_revoked_specific_grant(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        intent = activate(service, "02-activate").result["intent"]
        specific = service.grant_execution_permission(
            kind="specificRun",
            registration_id="notes-script-v1",
            granted_by="host-permission-control/run-approval",
            action_id=intent["actionId"],
            action_revision=intent["actionRevision"],
            registration_revision=intent["registrationRevision"],
            output_group=intent["outputGroup"],
            intent_id=intent["intentRef"],
        )
        project = grant_project(service)
        service.revoke_execution_permission(
            specific.grantId, revoked_by="host-permission-control/revoke"
        )
        # Revoking the narrow grant revokes no project authority.
        decision = authority_at_admission(
            service.grants,
            required_permission="execute_project_code",
            registration_id="notes-script-v1",
            registration_revision=1,
            action_id="summarize",
            action_revision=1,
            output_group="notes-group",
            intent_id=intent["intentRef"],
        )
        assert decision.granted and decision.grant_id == project.grantId
        # With every grant revoked, the denial names the revocation.
        service.revoke_execution_permission(
            project.grantId, revoked_by="host-permission-control/revoke"
        )
        denied = authority_at_admission(
            service.grants,
            required_permission="execute_project_code",
            registration_id="notes-script-v1",
            registration_revision=1,
            action_id="summarize",
            action_revision=1,
            output_group="notes-group",
            intent_id=intent["intentRef"],
        )
        assert not denied.granted
        assert denied.reason == "permission-revoked"


# ---------------------------------------------------------------------------
# Revocation (SPEC.md sections 13.3, 13.5, and 15.1)
# ---------------------------------------------------------------------------


def test_revocation_stops_covering_immediately_and_durably(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        grant = grant_project(service)
        assert activate(service, "02-activate").result["status"] == "granted"
        revoked = service.revoke_execution_permission(
            grant.grantId, revoked_by="host-permission-control/revoke"
        )
        assert revoked.revokedAt is not None
        assert revoked.revokedBy == "host-permission-control/revoke"
        denied = activate(service, "03-activate")
        assert denied.ok, denied.errors
        assert denied.result["status"] == "approvalRequired"
        assert denied.result["reason"] == "permission-revoked"

    with WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=notes_registry()
    ) as reopened:
        assert not is_live(reopened.grants[grant.grantId]), (
            "revocation survived the restart"
        )
        still_denied = activate(reopened, "04-activate")
        assert still_denied.result["status"] == "approvalRequired"


def test_launch_and_publication_recheck_the_captured_grant(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter):
        pass
    grant = None
    with WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=notes_registry()
    ) as service:
        grant = grant_project(service)
    seed_run_record(
        service_adapter,
        run_record_wire("01-run-a", permission_decision=grant.grantId),
    )
    seed_run_record(
        service_adapter,
        run_record_wire(
            "01-run-b",
            permission_decision=None,
            registration_id="lean-script-v1",
        ),
    )
    with WorkspaceService.open(
        service_adapter, WORKSPACE, action_registry=notes_registry()
    ) as service:
        launch = service.execution_authority_for_run("01-run-a", at="launch")
        assert launch.granted and launch.grant_id == grant.grantId
        publication = service.execution_authority_for_run(
            "01-run-a", at="publication"
        )
        assert publication.granted
        # A run of a registration no grant covers has no authority, with
        # or without a captured decision reference.
        ungranted = service.execution_authority_for_run("01-run-b")
        assert not ungranted.granted
        assert ungranted.reason == "missing-grant"

        service.revoke_execution_permission(
            grant.grantId, revoked_by="host-permission-control/revoke"
        )
        blocked = service.execution_authority_for_run("01-run-a", at="publication")
        assert not blocked.granted
        assert blocked.reason == "permission-revoked"
        assert revocation_blocks(blocked), (
            "revocation blocks publication and requests cancellation"
        )
        still_launchable = service.execution_authority_for_run("01-run-a")
        assert not still_launchable.granted
        assert still_launchable.reason == "permission-revoked"
        with pytest.raises(MeltPermissionError):
            service.execution_authority_for_run("ghost-run")


# ---------------------------------------------------------------------------
# Only host permission controls grant authority (SPEC.md sections 12.6, 15.1)
# ---------------------------------------------------------------------------


def test_no_command_envelope_grants_or_revokes_permission(
    service_adapter: PlatformAdapter,
):
    with WorkspaceService.open(service_adapter, WORKSPACE) as service:
        sequence = service.commit_sequence
        for method in ("permission.grant", "permission.revoke"):
            payload = json.dumps(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "01-envelope",
                    "method": method,
                    "payload": {"kind": "projectExecution"},
                }
            ).encode("utf-8")
            answer = service.handle_request(payload)
            assert answer.ok is False, method
            assert answer.errors[0].path == "/method"
        assert service.grants == {}
        assert service.commit_sequence == sequence, "nothing was committed"


def test_documents_and_contracts_cannot_declare_grants(
    service_adapter: PlatformAdapter, sales_document_copy: dict
):
    # A document field never carries a grant (SPEC.md sections 4.1 and
    # 15.1): the model rejects the unknown collection outright.
    sales_document_copy["permissions"] = {
        "run-once": {"kind": "specificRun", "registrationId": "sales-script-v1"}
    }
    with pytest.raises(Exception) as error:
        parse_document(sales_document_copy)
    assert "permissions" in str(error.value)
    # A registration contract cannot declare a permission either
    # (SPEC.md section 12.6).
    with pytest.raises(ValidationError):
        RegistrationContract.model_validate(
            {
                "parameters": {},
                "inputs": {},
                "outputs": {},
                "permission": "execute_project_code",
            }
        )


def test_the_permission_endpoint_validates_its_input(service_adapter: PlatformAdapter):
    with seed_definitions(service_adapter) as service:
        with pytest.raises(MeltPermissionError) as bad_kind:
            service.grant_execution_permission(
                kind="forever",
                registration_id="notes-script-v1",
                granted_by="host-permission-control/run-approval",
            )
        assert bad_kind.value.diagnostics[0].code == "E_PERMISSION"
        assert bad_kind.value.diagnostics[0].path == "/kind"

        with pytest.raises(MeltPermissionError) as unknown:
            service.grant_execution_permission(
                kind="projectExecution",
                registration_id="ghost-registration",
                granted_by="host-permission-control/run-approval",
            )
        assert unknown.value.diagnostics[0].code == "E_REFERENCE"
        assert unknown.value.diagnostics[0].path == "/registrationId"

        with pytest.raises(MeltPermissionError) as mismatch:
            service.grant_execution_permission(
                kind="projectExecution",
                registration_id="notes-script-v1",
                required_permission="something-else",
                granted_by="host-permission-control/run-approval",
            )
        assert mismatch.value.diagnostics[0].code == "E_PERMISSION"

        with pytest.raises(MeltPermissionError) as unpinned:
            service.grant_execution_permission(
                kind="specificRun",
                registration_id="notes-script-v1",
                granted_by="host-permission-control/run-approval",
                # No intent pins: the record model refuses the grant.
            )
        assert unpinned.value.diagnostics[0].code == "E_SCHEMA"

        with pytest.raises(MeltPermissionError) as unknown_revoke:
            service.revoke_execution_permission(
                "meltgrant_ghost", revoked_by="host-permission-control/revoke"
            )
        assert unknown_revoke.value.diagnostics[0].code == "E_PERMISSION"
        assert service.grants == {}, "no invalid grant was committed"


# ---------------------------------------------------------------------------
# Grant transactions (SPEC.md sections 12.5 and 15.1)
# ---------------------------------------------------------------------------


def test_grant_transactions_advance_the_sequence_and_notify_once(
    service_adapter: PlatformAdapter,
):
    with seed_definitions(service_adapter) as service:
        before = service.commit_sequence
        grant = grant_project(service)
        assert service.commit_sequence == before + 1
        window = service.changes_after(before, service.epoch)
        assert window.status == "changes"
        assert window.changes[-1]["changedRecords"] == [
            f"permissions/{grant.grantId}"
        ]
        revoked = service.revoke_execution_permission(
            grant.grantId, revoked_by="host-permission-control/revoke"
        )
        assert service.commit_sequence == before + 2
        # Revocation is idempotent: the second call changes nothing.
        again = service.revoke_execution_permission(
            grant.grantId, revoked_by="host-permission-control/revoke"
        )
        assert again.revokedAt == revoked.revokedAt
        assert service.commit_sequence == before + 2
        window = service.changes_after(before, service.epoch)
        assert [
            notification["changedRecords"] for notification in window.changes
        ].count([f"permissions/{grant.grantId}"]) == 2
