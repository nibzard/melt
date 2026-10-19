"""The explicit v0.1 migration (T027, RFC 001 sections 8.3 and 10.1).

The tests prove the migration contract through the real service and,
for the documented flow, through the command-line harness:

- The preview explains that pane removal no longer invalidates output
  publication, and writes nothing: no receipt, no backup, no revision.
- The migration refuses while runs or pending requests are active;
  quiescing them first is the caller's step.
- The committed migration validates the new document, saves a backup,
  and changes the language in one transaction. Source versions,
  projection IDs, parameter values, and layout are unchanged, and the
  former targets resolve the same saved outputs as following
  subscriptions.
- A failed migration leaves `melt/v0.1` current.
- After the migration, deleting a pane no longer invalidates a
  publication, and a second subscriber joins without rerunning the
  action (RFC 001 section 10.1).
- A `melt/v0.1` workspace keeps the v0.1 rule: deleting a target
  projection still rejects the entire publication.
- A restart restores the migrated workspace without executing
  anything.

Everything runs under the Looper resource prefix, never against the
user's data directory.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from melt.schema.commands import parse_command
from melt.schema.semantic import SEMANTIC_LANGUAGE_ID
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService, local_environment

from test_scheduling import (
    SPEC_SALES_SCRIPT,
    FakeClock,
    envelope,
    invoke,
    launch,
    requires_storage_worker,
    seed_workspace,
    set_parameter,
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

FILTERED_SOURCE = "analyze-sales-filtered"
MONTHLY_SOURCE = "analyze-sales-monthly"


def migrate_envelope(request_id: str, *, dry_run: bool = False):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": "workspace.migrate",
            "payload": {
                "targetLanguage": SEMANTIC_LANGUAGE_ID,
                "dryRun": dry_run,
            },
        }
    )


def seed_published(adapter: PlatformAdapter):
    """One completed v0.1 sales workspace: both outputs published."""

    seeded = seed_workspace(
        adapter,
        script=SPEC_SALES_SCRIPT,
        contract={
            "parameters": {
                "minimum": {
                    "type": "number",
                    "nullable": False,
                    "constraints": {"minimum": 0, "maximum": 1000000000},
                }
            },
            "inputs": {"sales": "table"},
            "outputs": {"filtered": "table", "monthly": "image"},
        },
        action_overrides={
            "outputs": {"filtered": "table", "monthly": "image"},
            "targets": {"filtered": "sales", "monthly": "revenue"},
        },
        extra_projections={
            "revenue": {
                "kind": "plot",
                "title": "Revenue by month",
                "source": None,
                "config": {"fit": "contain"},
                "controls": [],
            }
        },
    )
    service = seeded.service
    invoked = invoke(service, "05-invoke")
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    assert launch(service, run_id).state == "succeeded"
    publication = service.publish_run(run_id)
    assert publication.outcome == "current", publication.refusal
    return service, publication


def migrate(service: WorkspaceService, request_id: str = "m1", **kwargs):
    return service.dispatch(migrate_envelope(request_id, **kwargs))


def invoke_now(service: WorkspaceService, request_id: str):
    """Invoke the action against its currently observed revision."""

    return service.dispatch(
        envelope(
            request_id,
            method="action.invoke",
            expected={
                "actions/analyze-sales": service.state.entity_revisions[
                    "actions/analyze-sales"
                ]
            },
            payload={"actionId": "analyze-sales"},
        )
    )


def semantic_view(service: WorkspaceService) -> dict:
    response = service.dispatch(envelope("inspect", method="workspace.inspect"))
    assert response.ok, response.errors
    return response.result["semantic"]


def subscriber_of(service: WorkspaceService, projection_id: str) -> dict:
    for action in semantic_view(service)["outputs"].values():
        for output in action["outputs"].values():
            for subscriber in output["subscribers"]:
                if subscriber["projection"] == projection_id:
                    return subscriber
    raise AssertionError(f"projection {projection_id!r} does not subscribe")


def codes(response):
    return [(error.code, error.path) for error in response.errors]


# ---------------------------------------------------------------------------
# The preview (RFC 001 section 8.3)
# ---------------------------------------------------------------------------


def test_the_preview_explains_the_semantics_and_writes_nothing(service_adapter):
    service, _ = seed_published(service_adapter)
    sequence = service.commit_sequence
    document_before = json.loads(json.dumps(service.state.document))

    preview = migrate(service, "m-preview", dry_run=True)
    assert preview.ok, preview.errors
    result = preview.result
    assert result["fromLanguage"] == "melt/v0.1"
    assert result["toLanguage"] == SEMANTIC_LANGUAGE_ID
    assert "Pane removal no longer invalidates output publication" in (
        result["explanation"]
    )
    assert {entry["projection"] for entry in result["subscriptions"]} == {
        "sales",
        "revenue",
    }
    # The preview wrote nothing at all.
    assert service.commit_sequence == sequence
    assert service.state.language == "melt/v0.1"
    assert service.state.document == document_before
    assert service.receipt("m-preview") is None
    assert list((service.store.layout.backups).glob("*.sqlite")) == []


# ---------------------------------------------------------------------------
# Quiescing (RFC 001 section 8.3)
# ---------------------------------------------------------------------------


def test_the_migration_refuses_while_a_run_is_active(service_adapter):
    service, _ = seed_published(service_adapter)
    invoked = invoke(service, "05-invoke-2")
    assert invoked.ok, invoked.errors

    refused = migrate(service, "m-busy")
    assert not refused.ok
    assert ("E_CONFLICT", "/runs") in codes(refused)
    assert service.state.language == "melt/v0.1"


def test_the_migration_refuses_while_a_request_is_pending(service_adapter):
    service, _ = seed_published(service_adapter)
    changed = set_parameter(service, "05-set", value=200, expected_revision=1)
    assert changed.ok, changed.errors
    assert service.pending_intents["sales-results"].status == "pending"

    refused = migrate(service, "m-pending")
    assert not refused.ok
    assert ("E_CONFLICT", "/intents") in codes(refused)
    assert service.state.language == "melt/v0.1"


def test_an_extension_workspace_refuses_to_migrate_again(service_adapter):
    service, _ = seed_published(service_adapter)
    assert migrate(service, "m-once").ok
    again = migrate(service, "m-twice")
    assert not again.ok
    assert ("E_VERSION", "/language") in codes(again)


# ---------------------------------------------------------------------------
# The committed migration (RFC 001 sections 8.3 and 10.1)
# ---------------------------------------------------------------------------


def test_the_migration_preserves_the_saved_workspace(service_adapter):
    service, publication = seed_published(service_adapter)
    before = json.loads(json.dumps(service.state.document))
    parameters_before = {
        name: (state.value, state.revision)
        for name, state in service.parameter_state.items()
    }
    sequence = service.commit_sequence
    action_revision = service.state.entity_revisions["actions/analyze-sales"]

    migrated = migrate(service, "m-commit")
    assert migrated.ok, migrated.errors
    result = migrated.result
    assert migrated.commitSequence == sequence + 1
    assert result["backup"]["path"].endswith(
        f"pre-migration-{sequence}.sqlite"
    ), result["backup"]
    assert result["backup"]["bytes"] > 0

    document = service.state.document
    assert document["language"] == SEMANTIC_LANGUAGE_ID
    assert document["sources"] == before["sources"]
    assert document["parameters"] == before["parameters"]
    assert document["bindings"] == before["bindings"]
    assert document["layout"] == before["layout"]
    assert set(document["projections"]) == set(before["projections"])
    assert document["collections"] == {}
    assert document["state"] == {}
    action = document["actions"]["analyze-sales"]
    assert "targets" not in action
    assert action["outputs"] == before["actions"]["analyze-sales"]["outputs"]
    assert {
        name: (state.value, state.revision)
        for name, state in service.parameter_state.items()
    } == parameters_before
    # Every changed definition advanced its own revision once.
    assert (
        service.state.entity_revisions["actions/analyze-sales"]
        == action_revision + 1
    )

    # The former targets resolve the same saved sources.
    for projection_id, source_id in (
        ("sales", FILTERED_SOURCE),
        ("revenue", MONTHLY_SOURCE),
    ):
        subscriber = subscriber_of(service, projection_id)
        assert subscriber["follow"] == "current"
        assert subscriber["status"] == "current"
        assert subscriber["source"] == {
            "sourceId": source_id,
            "version": publication.outputs[
                "filtered" if projection_id == "sales" else "monthly"
            ]["version"],
        }
        subscription = document["projections"][projection_id]["source"]
        assert subscription["actionOutput"]["action"] == "analyze-sales"
        assert subscription["follow"] == "current"

    # The output group revision advanced: its targets dropped out of
    # the publication contract (SPEC.md section 13.3).
    assert (
        service.output_groups["sales-results"].revision
        >= service.output_groups["sales-results"].generation
    )

    # The request receipt replays: a retry with the same identifier
    # and payload returns the stored response without committing again
    # (SPEC.md section 12.5).
    replay = migrate(service, "m-commit")
    assert replay.ok, replay.errors
    assert replay.result["toLanguage"] == SEMANTIC_LANGUAGE_ID
    assert replay.commitSequence == migrated.commitSequence


def test_pane_removal_no_longer_invalidates_publication(service_adapter):
    service, _ = seed_published(service_adapter)
    assert migrate(service, "m-commit").ok

    # A new run executes and stages its outputs; the pane goes away in
    # the window between completion and publication, exactly where a
    # v0.1 publication would reject the entire group.
    invoked = invoke_now(service, "05-invoke-2")
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    assert launch(service, run_id).state == "succeeded"

    removed = service.apply(
        envelope(
            "06-remove-pane",
            method="workspace.apply",
            expected={
                "projections/revenue": service.state.entity_revisions[
                    "projections/revenue"
                ],
                "layout": service.state.entity_revisions["layout"],
            },
            operations=[
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "revenue",
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "sales"},
                        "hidden": [],
                    },
                },
            ],
        )
    )
    assert removed.ok, removed.errors

    publication = service.publish_run(run_id)
    assert publication.outcome == "current", publication.refusal
    assert publication.outputs["filtered"]["version"] == 2
    subscriber = subscriber_of(service, "sales")
    assert subscriber["source"]["version"] == 2


def test_a_second_subscriber_joins_without_rerunning(service_adapter):
    service, publication = seed_published(service_adapter)
    assert migrate(service, "m-commit").ok
    incarnation = semantic_view(service)["outputs"]["analyze-sales"][
        "incarnation"
    ]
    runs_before = set(service.runs)

    joined = service.apply(
        envelope(
            "07-second-subscriber",
            method="workspace.apply",
            expected={
                "projections/mirror": 0,
                "layout": service.state.entity_revisions["layout"],
            },
            operations=[
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "mirror",
                    "value": {
                        "kind": "table",
                        "title": "Mirror of the sales table",
                        "source": {
                            "actionOutput": {
                                "action": "analyze-sales",
                                "output": "filtered",
                                "incarnation": incarnation,
                            },
                            "follow": "current",
                        },
                        "config": {"columns": None, "selection": "none"},
                        "controls": [],
                    },
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {
                            "kind": "split",
                            "axis": "horizontal",
                            "ratio": 0.55,
                            "first": {"kind": "pane", "projection": "sales"},
                            "second": {"kind": "pane", "projection": "revenue"},
                        },
                        "hidden": ["mirror"],
                    },
                },
            ],
        )
    )
    assert joined.ok, joined.errors

    # No execution happened: the same runs remain and the new view
    # resolves the saved output (RFC 001 section 10.1).
    assert set(service.runs) == runs_before
    subscriber = subscriber_of(service, "mirror")
    assert subscriber["status"] == "current"
    assert subscriber["source"] == {
        "sourceId": FILTERED_SOURCE,
        "version": publication.outputs["filtered"]["version"],
    }


def test_a_failed_migration_leaves_v01_current(service_adapter, monkeypatch):
    service, _ = seed_published(service_adapter)
    document_before = json.loads(json.dumps(service.state.document))

    from runtime.storage.workspace import WorkspaceStore

    def refusing_backup(self, label):
        raise OSError("simulated full disk while saving the backup")

    monkeypatch.setattr(WorkspaceStore, "write_backup", refusing_backup)
    failed = migrate(service, "m-fail")
    assert not failed.ok
    assert ("E_ARTIFACT", "/backup") in codes(failed)

    # Nothing changed: the workspace keeps melt/v0.1 current with its
    # saved definitions, state, and history intact (RFC 001 8.3).
    assert service.state.language == "melt/v0.1"
    assert service.state.document == document_before
    assert service.commit_sequence  # untouched commits still stand


def test_a_migration_that_fails_validation_leaves_v01_current(
    service_adapter, monkeypatch
):
    service, _ = seed_published(service_adapter)
    sequence = service.commit_sequence
    document_before = json.loads(json.dumps(service.state.document))

    # Fault injection at the validation step: the candidate the
    # migration built does not compile, the way a drifted trusted
    # input would answer (RFC 001 section 8.3 validates the new
    # document before anything commits).
    from melt.schema.diagnostics import Diagnostic
    from runtime.compiler.compiler import CompileResult
    from runtime.workspace import service as service_module

    def refusing_compile(request):
        return CompileResult(
            None,
            (
                Diagnostic(
                    code="E_REGISTRATION",
                    path="/actions/analyze-sales/implementation/registrationId",
                    message="injected: the registration left the registry",
                ),
            ),
        )

    monkeypatch.setattr(service_module, "compile_document", refusing_compile)
    failed = migrate(service, "m-invalid")
    assert not failed.ok
    assert (
        "E_REGISTRATION",
        "/actions/analyze-sales/implementation/registrationId",
    ) in codes(failed)

    # The refusal committed nothing, stored no receipt, and saved no
    # backup: melt/v0.1 stays current exactly as it was.
    assert service.state.language == "melt/v0.1"
    assert service.state.document == document_before
    assert service.commit_sequence == sequence
    assert service.receipt("m-invalid") is None
    assert list((service.store.layout.backups).glob("*.sqlite")) == []


def test_the_migration_survives_a_restart(service_adapter):
    service, _ = seed_published(service_adapter)
    assert migrate(service, "m-commit").ok
    incarnation = semantic_view(service)["outputs"]["analyze-sales"][
        "incarnation"
    ]
    sequence = service.commit_sequence
    service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.state.language == SEMANTIC_LANGUAGE_ID
        assert reopened.commit_sequence == sequence
        assert reopened.state.output_incarnations == {
            "analyze-sales": incarnation
        }
        assert subscriber_of(reopened, "sales")["status"] == "current"
        # The old histories keep their records: the published run and
        # its receipt survive the language change unchanged.
        current_runs = [
            run
            for run in reopened.runs.values()
            if run.disposition == "current"
        ]
        assert len(current_runs) == 1
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# The v0.1 boundary (RFC 001 section 8.3)
# ---------------------------------------------------------------------------


def test_a_v01_target_removal_still_rejects_the_publication(service_adapter):
    service, _ = seed_published(service_adapter)
    assert service.state.language == "melt/v0.1"

    invoked = invoke(service, "05-invoke-2")
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    assert launch(service, run_id).state == "succeeded"

    # The pane removal rides with the action redefinition that keeps
    # the candidate compilable, exactly as the race suite drives it
    # (SPEC.md sections 12.2 and 18.3).
    from test_scheduling import LIGHT_CONTRACT, LIGHT_SALES_SCRIPT, PYTHON

    script_token = service.capture_artifact_token(
        LIGHT_SALES_SCRIPT.encode("utf-8"), PYTHON
    ).token
    registered = service.dispatch(
        envelope(
            "07-register-light",
            method="action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": LIGHT_CONTRACT,
            },
        )
    )
    assert registered.ok, registered.errors
    light_id = registered.result["registrationId"]

    removed = service.apply(
        envelope(
            "08-remove-target",
            method="workspace.apply",
            expected={
                "actions/analyze-sales": 1,
                "projections/revenue": service.state.entity_revisions[
                    "projections/revenue"
                ],
                "layout": service.state.entity_revisions["layout"],
            },
            operations=[
                {
                    "op": "entity.replace",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": {
                        "implementation": {"registrationId": light_id},
                        "parameters": {
                            "minimum": {"parameter": "minimum"}
                        },
                        "inputs": {
                            "sales": {"sourceId": "sales-input", "version": 1}
                        },
                        "outputs": {"filtered": "table"},
                        "outputGroup": "sales-results",
                        "targets": {"filtered": "sales"},
                    },
                },
                {
                    "op": "entity.delete",
                    "collection": "projections",
                    "id": "revenue",
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "sales"},
                        "hidden": [],
                    },
                },
            ],
        )
    )
    assert removed.ok, removed.errors

    # The v0.1 rule holds: removing a target projection invalidates
    # the whole output group publication, so not even the surviving
    # table target moved and the previous pair stays together.
    publication = service.publish_run(run_id)
    assert publication.outcome == "superseded"
    document = service.state.document
    assert "revenue" not in document["projections"]
    assert document["projections"]["sales"]["source"]["version"] == 1
    assert document["sources"][FILTERED_SOURCE]["currentVersion"] == 1
    assert document["sources"][MONTHLY_SOURCE]["currentVersion"] == 1


# ---------------------------------------------------------------------------
# The command-line harness (SPEC.md sections 2.2 and 18.2)
# ---------------------------------------------------------------------------


def test_the_harness_migrates_the_documented_flow(
    service_adapter, repo_root, service_root
):
    import os

    service, _ = seed_published(service_adapter)
    root = service_adapter.application_data_root
    service.close()

    environment = dict(os.environ)
    python_path = [str(repo_root / "python"), str(repo_root)]
    if existing := environment.get("PYTHONPATH"):
        python_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)

    def harness(*arguments):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "runtime.harness",
                "--root",
                str(root),
                "--workspace",
                WORKSPACE,
                *arguments,
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=120,
            env=environment,
        )
        assert completed.stderr == "", completed.stderr
        answer = (
            json.loads(completed.stdout) if completed.stdout.strip() else None
        )
        return completed.returncode, answer

    code, preview = harness("migrate", "--dry-run")
    assert code == 0, preview
    assert preview["ok"] is True
    assert "Pane removal no longer invalidates output publication" in (
        preview["result"]["explanation"]
    )

    code, committed = harness("migrate")
    assert code == 0, committed
    assert committed["ok"] is True
    assert committed["result"]["toLanguage"] == SEMANTIC_LANGUAGE_ID
    assert committed["result"]["backup"]["bytes"] > 0

    code, inspected = harness("inspect")
    assert code == 0, inspected
    document = inspected["result"]["document"]
    assert document["language"] == SEMANTIC_LANGUAGE_ID
    assert "targets" not in document["actions"]["analyze-sales"]
    outputs = inspected["result"]["semantic"]["outputs"]["analyze-sales"]
    assert outputs["incarnation"]
    assert outputs["outputs"]["filtered"]["subscribers"][0]["projection"] == "sales"
