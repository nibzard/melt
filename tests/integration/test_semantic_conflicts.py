"""The conflicts and recovery suite of RFC 001 section 10.5 (backlog
task T029).

The section names its conflict and recovery shapes, then two
invariants that must hold through all of them:

1. Duplicate events replay their stored receipt.
2. Reused request IDs with changed payloads are refused.
3. Stale state revisions conflict without committing.
4. A recreated collection ID issues another incarnation an old
   reference cannot satisfy.
5. A failed commit is never acknowledged; the retry reconciles.
6. A lost acknowledgement replays the durable receipt, across a
   restart, and the notification stream still carries the commit.
7. Unavailable renderer versions degrade the host display only.

The invariants:

- An optimistic selection is never reported durable before commit.
  The host display (:class:`OptimisticSelection`) shows the attempted
  key immediately, while every durable read — taken inside the commit
  window, before the store append lands — still reports the previous
  committed value, and a failed append leaves nothing saved.
- An unavailable graph renderer never blocks inspection of saved
  sources or semantic state: `workspace.inspect` keeps answering with
  the full snapshot, and the host renders a fallback pane instead.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import pytest

from melt.schema.canonical import model_to_wire
from runtime.harness import test_renderer
from runtime.workspace import WorkspaceService

from test_semantic_interactions import (
    WORKSPACE,
    apply,
    build_supplier_workspace,
    codes,
    envelope,
    event_guards,
    inspect as inspect_workspace,
    requires_storage_worker,
    revision_of,
    select,
    semantic_view,
)

pytestmark = requires_storage_worker

# The renderer registry a host ships that lacks the graph renderer:
# every v0.1 renderer plus the record renderer, no `melt.graph`.
HOST_WITHOUT_GRAPH = {
    "melt.table": 1,
    "melt.plot": 1,
    "melt.document": 1,
    "melt.record": 1,
}

# A host that ships a graph renderer built for another contract
# version: it must not silently reinterpret the saved plan.
HOST_WITH_INCOMPATIBLE_GRAPH = {
    **HOST_WITHOUT_GRAPH,
    "melt.graph": 2,
}


class ServiceSession:
    """The channel protocol the host stand-ins need, over one service.

    ``snapshot`` and ``request`` mirror `HarnessSession`
    (runtime/harness/cli.py), so the test renderer and the optimistic
    display run against the same interface the command-line harness
    uses, over the same service handlers the native host will use.
    """

    def __init__(self, service: WorkspaceService, workspace: str = WORKSPACE):
        self._service = service
        self.workspace = workspace

    def snapshot(self) -> dict:
        answer = self._service.dispatch(
            envelope("host-inspect", "workspace.inspect", payload={})
        )
        assert answer.ok, answer.errors
        return answer.result

    def request(
        self,
        method: str,
        *,
        payload: "dict | None" = None,
        expected: "dict | None" = None,
        request_id: "str | None" = None,
        origin: str = "user",
    ):
        return model_to_wire(
            self._service.dispatch_as(
                envelope(
                    request_id or "host-request",
                    method,
                    expected=expected,
                    payload=payload,
                ),
                origin=origin,
            )
        )


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: duplicate events and reused IDs
# ---------------------------------------------------------------------------


def test_a_duplicate_event_replays_its_receipt_and_commits_once(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        first = select(service, "evt-duplicate", "S-42", guards)
        assert first.ok, first.errors
        replay = select(service, "evt-duplicate", "S-42", guards)
        assert replay.ok
        assert replay.result == first.result
        assert replay.commitSequence == first.commitSequence
        # Exactly one commit: the stored revision moved once and the
        # commit sequence stands where the first acknowledgement left it.
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["revision"] == 2
        assert stored["value"]["key"] == "S-42"
        assert service.commit_sequence == first.commitSequence
        assert service.receipt("evt-duplicate") is not None
    finally:
        service.close()


def test_a_reused_id_with_a_changed_payload_is_refused(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        assert select(service, "evt-reuse", "S-42", guards).ok
        reused = select(service, "evt-reuse", "S-73", guards)
        assert not reused.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(reused)
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["revision"] == 2
    finally:
        service.close()

    # Receipts live for the workspace lifetime (SPEC.md section 12.5),
    # so the refusal outlives a restart.
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert semantic_view(reopened)["referenceStates"][
            "selected_supplier"
        ]["value"]["key"] == "S-42"
        refused = select(reopened, "evt-reuse", "S-73", guards)
        assert not refused.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(refused)
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: stale state revisions
# ---------------------------------------------------------------------------


def test_a_stale_state_revision_conflicts_then_a_refreshed_gesture_commits(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "evt-first", "S-42", event_guards(service)).ok
        stale = dict(event_guards(service))
        stale["refstate/selected_supplier"] = 1
        rejected = select(service, "evt-stale", "S-73", stale)
        assert not rejected.ok
        assert ("E_CONFLICT", "/expected/refstate~1selected_supplier") in codes(
            rejected
        )
        # The rejection committed nothing and stored no receipt, so the
        # same request ID stays usable and the stored value never moved.
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["revision"] == 2
        assert service.receipt("evt-stale") is None
        # A refreshed gesture commits normally.
        refreshed = select(service, "evt-stale", "S-73", event_guards(service))
        assert refreshed.ok, refreshed.errors
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-73"
        assert stored["revision"] == 3
    finally:
        service.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: a recreated collection ID
# ---------------------------------------------------------------------------


def test_a_recreated_collection_id_never_inherits_the_old_identity(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "evt-old", "S-42", event_guards(service)).ok
        old_incarnation = service.state.collection_incarnations["suppliers"]
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["incarnation"] == old_incarnation

        recreate = [
            {
                "op": "entity.delete",
                "collection": "collections",
                "id": "suppliers",
            },
            {
                "op": "entity.create",
                "collection": "collections",
                "id": "suppliers",
                "value": {
                    "source": {"sourceId": "supplier-records", "version": 1},
                    "key": "supplier_id",
                },
            },
        ]
        expected = {
            "collections/suppliers": revision_of(
                service, "collections/suppliers"
            ),
            "refstate/selected_supplier": revision_of(
                service, "refstate/selected_supplier"
            ),
        }
        # The stored selection still names the previous incarnation, so
        # the same transaction must clear or replace it (RFC 001 3.3).
        refused = apply(service, "tx-refused", expected, recreate)
        assert not refused.ok
        assert any(
            code == "E_STATE_MIGRATION" for code, _ in codes(refused)
        ), codes(refused)
        assert service.state.collection_incarnations["suppliers"] == (
            old_incarnation
        )

        committed = apply(
            service,
            "tx-recreate",
            expected,
            [*recreate, {"op": "state.set", "id": "selected_supplier",
                         "value": None}],
        )
        assert committed.ok, committed.errors
        new_incarnation = service.state.collection_incarnations["suppliers"]
        assert new_incarnation != old_incarnation
        cleared = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert cleared["value"] is None

        # An event carrying the old incarnation cannot select an object
        # in the new collection, even with fresh guards (RFC 001 3.2).
        rejected = service.dispatch(
            envelope(
                "evt-old-incarnation",
                "object.select",
                expected=event_guards(service),
                payload={
                    "projectionId": "network",
                    "objectRef": {
                        "collection": "suppliers",
                        "incarnation": old_incarnation,
                        "key": "S-73",
                    },
                },
            )
        )
        assert not rejected.ok
        assert ("E_CONFLICT", "/payload/objectRef") in codes(rejected)
        # A gesture carrying the new incarnation commits.
        selected = select(service, "evt-new-incarnation", "S-73",
                          event_guards(service))
        assert selected.ok, selected.errors
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-73"
        assert stored["value"]["incarnation"] == new_incarnation
        # Two commits moved the record before this one: the original
        # selection and the clearing state.set of the recreation.
        assert stored["revision"] == 4
    finally:
        service.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: failed commits
# ---------------------------------------------------------------------------


def test_a_failed_commit_is_never_acknowledged_and_the_retry_reconciles(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        sequence = service.commit_sequence
        notifications_before = service.notifications

        def failing_append(records):
            raise OSError("simulated storage failure inside the commit")

        service._store.append_records = failing_append
        try:
            failed = select(service, "evt-failed", "S-42", event_guards(service))
            assert not failed.ok
        except OSError:
            # The service refuses to acknowledge a commit that did not
            # land; the failure surfaces instead of a false receipt.
            pass
        finally:
            del service._store.append_records
        # Nothing moved and nothing was receipted.
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"] is None
        assert stored["revision"] == 1
        assert service.commit_sequence == sequence
        assert service.receipt("evt-failed") is None
        assert service.notifications == notifications_before
        # The retry reconciles: with no stored receipt the same request
        # processes again and commits exactly once (SPEC.md 18.4).
        retried = select(service, "evt-failed", "S-42", event_guards(service))
        assert retried.ok, retried.errors
        assert service.commit_sequence == sequence + 1
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["revision"] == 2
    finally:
        service.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: lost acknowledgements
# ---------------------------------------------------------------------------


def test_a_lost_acknowledgement_reconciles_through_the_receipt(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        guards = event_guards(service)
        before = service.commit_sequence
        committed = select(service, "evt-lost", "S-42", guards)
        assert committed.ok, committed.errors
        # The host never saw the acknowledgement. The retry with the
        # same ID and payload replays the stored receipt and commits
        # nothing a second time (SPEC.md section 12.5).
        replay = select(service, "evt-lost", "S-42", guards)
        assert replay.ok
        assert replay.result == committed.result
        assert replay.commitSequence == committed.commitSequence
        assert service.commit_sequence == before + 1
        # A retry that rebuilds its guards from the new snapshot is
        # different content: it answers E_REQUEST_REUSE, which tells the
        # host to take the snapshot path instead of replaying (SPEC.md
        # sections 12.5 and 18.4).
        drifted = select(service, "evt-lost", "S-42", event_guards(service))
        assert not drifted.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(drifted)
        assert semantic_view(service)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 2
        # The notification stream still carries the one commit, so a
        # host that lost the acknowledgement can resynchronize.
        window = service.dispatch(
            envelope(
                "sub-lost",
                "workspace.subscribe",
                payload={"afterSequence": before, "epoch": service.epoch},
            )
        )
        assert window.ok, window.errors
        assert [change["commitSequence"] for change in
                window.result["changes"]] == [before + 1]
        assert window.result["changes"][0]["changedRecords"] == [
            "refstate/selected_supplier"
        ]
        # A new gesture replayed against the pre-commit guards
        # conflicts: the host refreshes instead (RFC 001 section 5.3).
        stale = select(service, "evt-after-lost", "S-73", guards)
        assert not stale.ok
        assert ("E_CONFLICT", "/expected/refstate~1selected_supplier") in codes(
            stale
        )
    finally:
        service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        # The receipt outlives the restart: a retry after reconnect
        # still replays exactly the committed answer, and reuse with
        # different content stays refused.
        replay = select(reopened, "evt-lost", "S-42", guards)
        assert replay.ok
        assert replay.result == committed.result
        reused = select(reopened, "evt-lost", "S-73", guards)
        assert not reused.ok
        assert ("E_REQUEST_REUSE", "/requestId") in codes(reused)
        assert semantic_view(reopened)["referenceStates"][
            "selected_supplier"
        ]["revision"] == 2
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: the optimistic selection is never durable
# ---------------------------------------------------------------------------


def test_the_optimistic_selection_never_reads_as_durable_before_commit(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        host = test_renderer.OptimisticSelection(session)
        assert host.view() == {
            "value": None,
            "status": "committed",
            "conflict": None,
        }
        window: dict = {}
        real_append = service._store.append_records

        def probing_append(records):
            # Inside the commit window: the append has not landed, so
            # the durable view must still report the previous value
            # while the host displays the optimistic one.
            window["durable"] = session.snapshot()["semantic"][
                "referenceStates"
            ]["selected_supplier"]
            window["host"] = host.view()
            return real_append(records)

        service._store.append_records = probing_append
        try:
            answer = host.select("S-42", request_id="evt-optimistic")
        finally:
            del service._store.append_records
        assert answer["ok"] is True
        assert window["host"] == {
            "value": "S-42",
            "status": "optimistic",
            "conflict": None,
        }
        assert window["durable"]["value"] is None
        assert window["durable"]["revision"] == 1
        # After the acknowledgement the display is the durable value.
        assert host.view() == {
            "value": "S-42",
            "status": "committed",
            "conflict": None,
        }
        value, revision = host.committed()
        assert value["key"] == "S-42"
        assert revision == 2
    finally:
        service.close()


def test_a_failed_commit_leaves_the_optimistic_selection_unsaved(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        host = test_renderer.OptimisticSelection(session)
        sequence = service.commit_sequence

        def failing_append(records):
            raise OSError("simulated storage failure inside the commit")

        service._store.append_records = failing_append
        try:
            with pytest.raises(OSError):
                host.select("S-42", request_id="evt-unsaved")
        finally:
            del service._store.append_records
        # No acknowledgement: the display restored the committed value
        # and reports the gesture as unanswered, never as saved.
        view = host.view()
        assert view["status"] == "unacknowledged"
        assert view["value"] is None
        value, revision = host.committed()
        assert value is None
        assert revision == 1
        assert service.commit_sequence == sequence
        assert service.receipt("evt-unsaved") is None
        # The retry commits and only then turns the display committed.
        answer = host.select("S-42", request_id="evt-unsaved")
        assert answer["ok"] is True
        assert host.view() == {
            "value": "S-42",
            "status": "committed",
            "conflict": None,
        }
        assert service.commit_sequence == sequence + 1
    finally:
        service.close()


def test_a_rejected_gesture_restores_the_committed_display(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        host = test_renderer.OptimisticSelection(session)
        assert host.select("S-42", request_id="evt-reject-1")["ok"] is True
        assert host.select("S-73", request_id="evt-reject-2")["ok"] is True
        assert host.view() == {
            "value": "S-73",
            "status": "committed",
            "conflict": None,
        }
        # The host races itself: the next gesture carries the guards of
        # the snapshot taken before the last commit (RFC 001 5.3 keeps
        # one pending selection event per state record per host).
        revision = host.committed()[1]
        rejected = host.select(
            "S-42",
            request_id="evt-reject-3",
            if_revisions={"refstate/selected_supplier": revision - 1},
        )
        assert rejected["ok"] is False
        view = host.view()
        assert view["status"] == "conflict"
        assert view["value"] == "S-73"
        assert any(
            item["code"] == "E_CONFLICT" for item in view["conflict"]
        ), view["conflict"]
        # The durable value never moved through the rejection.
        value, durable_revision = host.committed()
        assert value["key"] == "S-73"
        assert durable_revision == revision
    finally:
        service.close()


# ---------------------------------------------------------------------------
# RFC 001 section 10.5: unavailable renderer versions
# ---------------------------------------------------------------------------


def test_an_unavailable_graph_renderer_never_blocks_inspection(service_adapter):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        committed = test_renderer.OptimisticSelection(session)
        assert committed.select("S-42", request_id="evt-renderer")["ok"] is True
        sequence = service.commit_sequence

        without_graph = test_renderer.TestRenderer(session, installed_renderers=HOST_WITHOUT_GRAPH)
        # Inspection stays available and complete: the saved sources,
        # the committed semantic state with its resolution, the
        # observer views, and the pinned renderer versions the host
        # needs to detect the gap (RFC 001 sections 7.2 and 9).
        snapshot = session.snapshot()
        assert set(snapshot["document"]["sources"]) == {
            "supplier-records",
            "supplier-network",
        }
        stored = snapshot["semantic"]["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["resolution"] == {"status": "resolved"}
        assert set(snapshot["semantic"]["observers"]) == {
            "inspector",
            "network",
        }
        assert snapshot["semantic"]["renderers"]["network"] == {
            "id": "melt.graph",
            "contractVersion": 1,
        }
        # The rendered answer degrades to the fallback pane only: the
        # graph pane carries its source details and diagnostics, the
        # inspector keeps resolving the committed record, and the
        # semantic state stays visible (RFC 001 section 7.2).
        rendered = without_graph.render()
        graph = next(
            pane for pane in rendered["panes"] if pane["projection"] == "network"
        )
        assert graph["rendererStatus"] == "unavailable"
        assert graph["renderer"] == "melt.graph"
        assert "no 'melt.graph' renderer" in graph["reason"]
        assert graph["source"] == {
            "sourceId": "supplier-network",
            "version": 1,
        }
        assert graph["diagnostics"][0]["code"] == "E_VERSION"
        assert graph["diagnostics"][0]["path"] == "/projections/network/renderer"
        inspector = next(
            pane
            for pane in rendered["panes"]
            if pane["projection"] == "inspector"
        )
        assert inspector["display"] == "record"
        assert inspector["record"]["cells"]["supplier_id"] == "S-42"
        assert rendered["selected"]["selected_supplier"]["value"]["key"] == "S-42"
        # The host cannot originate a gesture from a renderer it
        # cannot instantiate; it does not reinterpret the contract.
        with pytest.raises(test_renderer.TestRendererError) as error:
            without_graph.event_request(
                projection="network", event="object.select", key="S-73"
            )
        assert "unavailable" in str(error.value)
        # Reads committed nothing.
        assert service.commit_sequence == sequence
    finally:
        service.close()


def test_an_incompatible_renderer_contract_version_degrades_the_same_way(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        newer_host = test_renderer.TestRenderer(
            session, installed_renderers=HOST_WITH_INCOMPATIBLE_GRAPH
        )
        snapshot = session.snapshot()
        assert snapshot["semantic"]["referenceStates"]["selected_supplier"][
            "value"
        ] is None
        assert snapshot["semantic"]["renderers"]["network"]["id"] == "melt.graph"
        rendered = newer_host.render()
        graph = next(
            pane for pane in rendered["panes"] if pane["projection"] == "network"
        )
        assert graph["rendererStatus"] == "unavailable"
        assert "contract version 1" in graph["reason"]
        assert "ships 2" in graph["reason"]
        with pytest.raises(test_renderer.TestRendererError):
            newer_host.event_request(
                projection="network", event="object.select", key="S-42"
            )
        # The saved sources and semantic state remain inspectable
        # through the whole degraded session.
        assert set(session.snapshot()["document"]["sources"]) == {
            "supplier-records",
            "supplier-network",
        }
    finally:
        service.close()


def test_a_host_that_ships_the_pinned_renderers_renders_every_pane(
    service_adapter,
):
    service = build_supplier_workspace(service_adapter)
    try:
        session = ServiceSession(service)
        host = test_renderer.OptimisticSelection(session)
        assert host.select("S-42", request_id="evt-full")["ok"] is True
        installed = dict(HOST_WITH_INCOMPATIBLE_GRAPH, **{"melt.graph": 1})
        rendered = test_renderer.TestRenderer(session, installed_renderers=installed).render()
        by_projection = {
            pane["projection"]: pane for pane in rendered["panes"]
        }
        assert set(by_projection) == {"inspector", "network"}
        assert "rendererStatus" not in by_projection["network"]
        assert by_projection["inspector"]["record"]["cells"] == {
            "supplier_id": "S-42",
            "name": "Alpha Components",
        }
        assert by_projection["network"]["highlightedNodes"] == ["s-42"]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The inspection the whole suite leans on
# ---------------------------------------------------------------------------


def test_every_conflict_above_left_the_committed_workspace_readable(service_adapter):
    """The workspace survives the whole conflict battery intact.

    Drives the six shapes of section 10.5 in sequence against one
    workspace, then proves the committed state stays inspectable and
    internally consistent afterwards.
    """

    service = build_supplier_workspace(service_adapter)
    try:
        # 1. duplicate events, 2. reused IDs with changed payloads.
        guards = event_guards(service)
        assert select(service, "seq-1", "S-42", guards).ok
        assert select(service, "seq-1", "S-42", guards).result["status"] == (
            "committed"
        )
        reused = select(service, "seq-1", "S-73", guards)
        assert ("E_REQUEST_REUSE", "/requestId") in codes(reused)
        # 3. stale state revisions.
        stale = dict(event_guards(service))
        stale["refstate/selected_supplier"] = 1
        assert ("E_CONFLICT", "/expected/refstate~1selected_supplier") in codes(
            select(service, "seq-2", "S-73", stale)
        )
        # 4. a recreated collection ID.
        expected = {
            "collections/suppliers": revision_of(
                service, "collections/suppliers"
            ),
            "refstate/selected_supplier": revision_of(
                service, "refstate/selected_supplier"
            ),
        }
        recreated = apply(
            service,
            "seq-3",
            expected,
            [
                {
                    "op": "entity.delete",
                    "collection": "collections",
                    "id": "suppliers",
                },
                {
                    "op": "entity.create",
                    "collection": "collections",
                    "id": "suppliers",
                    "value": {
                        "source": {
                            "sourceId": "supplier-records",
                            "version": 1,
                        },
                        "key": "supplier_id",
                    },
                },
                {"op": "state.set", "id": "selected_supplier", "value": None},
            ],
        )
        assert recreated.ok, recreated.errors
        # 5. a failed commit.
        def failing_append(records):
            raise OSError("simulated storage failure inside the commit")

        service._store.append_records = failing_append
        try:
            failed = select(service, "seq-4", "S-73", event_guards(service))
            assert not failed.ok
        except OSError:
            pass
        finally:
            del service._store.append_records
        # 6. a lost acknowledgement: the event commits, the receipt
        #    answers the verbatim retry of the still-pending request.
        pending = event_guards(service)
        lost = select(service, "seq-5", "S-42", pending)
        assert lost.ok, lost.errors
        replayed = select(service, "seq-5", "S-42", pending)
        assert replayed.result == lost.result
        # The committed workspace is still fully readable.
        snapshot = inspect_workspace(service).result
        assert snapshot["present"] is True
        assert set(snapshot["document"]["sources"]) == {
            "supplier-records",
            "supplier-network",
        }
        stored = semantic_view(service)["referenceStates"]["selected_supplier"]
        assert stored["value"]["key"] == "S-42"
        assert stored["resolution"]["status"] == "resolved"
        assert snapshot["document"]["projections"]["network"]["kind"] == "graph"
    finally:
        service.close()
