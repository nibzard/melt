"""Observer availability while an artifact is missing (backlog task T046).

RFC 001 section 6 fixes the display conditions of a ``record``
selector: null shows an empty selection, a missing key shows the key,
and an unreadable source shows a recoverable source error. Section 10.5
adds the harness-level demand: an unavailable source must not prevent
inspection of the saved sources or the semantic state.

The review reproduced the opposite: ``workspace.inspect`` raised
``FileNotFoundError`` after the fixture lost the table artifact a
selected record observer pinned, because ``record_view`` called the
artifact reader directly while it built its page arguments. One damaged
file took the whole snapshot — and the framed channel with it — down.

These regressions pin the repaired contract:

- The affected observer answers ``unavailable`` with the stored
  reference, its revision, and a recoverable reason; no read error
  escapes the dispatch or the framed boundary.
- Unrelated observers, sources, and state stay inspectable, and the
  normal missing-artifact diagnostics and the recovery report still
  ride the same response.
- Restart with the artifact still missing keeps the workspace
  inspectable, and restoring the same content-addressed bytes lets the
  observer resolve again without a new selection or any Python run.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import json

from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

from test_semantic_harness import data_root, run_harness, supplier_workspace
from test_semantic_interactions import (
    WORKSPACE,
    apply,
    build_supplier_workspace,
    event_guards,
    import_source,
    inspect as inspect_workspace,
    requires_storage_worker,
    revision_of,
    select,
    suppliers_bytes,
    SUPPLIERS_V1,
)

pytestmark = requires_storage_worker

SUPPLIER_ARTIFACT_PATH = "/sources/supplier-records/versions/1/artifact"
"""The diagnostic path of the artifact the record observer pins."""

SITES = (("SITE-1", "North"), ("SITE-2", "South"))
"""An unrelated table the damaged artifact never touches."""


def table_bytes(rows, columns) -> bytes:
    """One two-column Parquet fixture with string columns."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [pa.field(name, pa.string(), nullable=False) for name in columns]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([row[index] for row in rows], type=pa.string())
            for index in range(len(columns))
        ],
        schema=schema,
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="snappy", version="2.6")
    return sink.getvalue().to_pybytes()


def artifact_file(service, source_id: str = "supplier-records", version: int = 1):
    """The stored file of one pinned source version."""

    document = service.state.document
    artifact = document["sources"][source_id]["versions"][str(version)][
        "artifact"
    ]
    return service.store.artifacts.path_for(artifact)


def framed(service, request_id: str, method: str, payload: dict) -> dict:
    """Send one request through the framed channel the host uses."""

    from melt.schema.commands import decode_frame, encode_frame

    body = json.dumps(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": method,
            "payload": payload,
        }
    ).encode("utf-8")
    answer = service.process_frame(encode_frame(body))
    raw, _ = decode_frame(answer)
    return json.loads(raw.decode("utf-8"))


def selected_supplier_workspace(service_adapter) -> tuple:
    """The fixture of the review before the damage: S-42 is selected."""

    service = build_supplier_workspace(service_adapter)
    assert select(service, "05-select", "S-42", event_guards(service)).ok
    target = artifact_file(service)
    assert target.is_file()
    return service, target


def selected_with_a_missing_table_artifact(service_adapter) -> tuple:
    """The fixture of the review: S-42 selected, its table artifact gone."""

    service, target = selected_supplier_workspace(service_adapter)
    target.unlink()
    return service, target


def codes_of(response) -> list:
    """The diagnostic code and path pairs of one inspection response."""

    return [
        (error["code"], error["path"]) for error in response.result["diagnostics"]
    ]


# ---------------------------------------------------------------------------
# The failing regression: inspection survives a missing artifact
# ---------------------------------------------------------------------------


def test_a_missing_table_artifact_marks_the_record_observer_unavailable(
    service_adapter: PlatformAdapter,
):
    service, _ = selected_with_a_missing_table_artifact(service_adapter)
    try:
        response = inspect_workspace(service, "06-inspect")
        assert response.ok, response.errors

        # The affected observer answers unavailable with a recoverable
        # reason, and the stored reference rides the view untouched
        # (RFC 001 sections 3.3 and 6).
        observer = response.result["semantic"]["observers"]["inspector"]
        assert observer["slot"] == "record"
        assert observer["state"] == "selected_supplier"
        assert observer["display"] == "unavailable"
        assert observer["resolution"]["status"] == "unavailable"
        assert "unreadable" in observer["resolution"]["reason"]
        assert observer["value"] == {
            "collection": "suppliers",
            "incarnation": service.state.collection_incarnations["suppliers"],
            "key": "S-42",
        }
        assert "record" not in observer

        # The saved selection, its revision, the document, and every
        # entity revision survive the damage.
        stored = response.result["semantic"]["referenceStates"][
            "selected_supplier"
        ]
        assert stored["value"]["key"] == "S-42"
        assert stored["revision"] == 2
        assert stored["resolution"]["status"] == "unavailable"
        document = response.result["document"]
        assert set(document["sources"]) == {
            "supplier-records",
            "supplier-network",
        }
        assert set(document["projections"]) == {"inspector", "network"}
        assert response.result["entityRevisions"]["projections/inspector"] >= 1
        assert response.result["activeRuns"] == []

        # The normal missing-artifact diagnostic still rides the same
        # response, exactly as startup reported it (SPEC.md section 14).
        assert ("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH) in codes_of(response)

        # The graph observer still resolves its highlight from the
        # intact graph artifact; its reference resolution names the
        # unreadable collection index instead of failing the request.
        highlight = response.result["semantic"]["observers"]["network"]
        assert highlight["highlightedNodes"] == ["s-42"]
        assert highlight["resolution"]["status"] == "unavailable"
    finally:
        service.close()


def test_the_framed_channel_answers_the_same_inspection(
    service_adapter: PlatformAdapter,
):
    """No read error escapes the framed boundary the native host uses."""

    service, _ = selected_with_a_missing_table_artifact(service_adapter)
    try:
        body = framed(service, "06-inspect", "workspace.inspect", {})
        assert body["requestId"] == "06-inspect"
        assert body["ok"] is True
        observer = body["result"]["semantic"]["observers"]["inspector"]
        assert observer["display"] == "unavailable"
        assert observer["resolution"]["status"] == "unavailable"
        assert observer["value"]["key"] == "S-42"
        assert [
            (error["code"], error["path"]) for error in body["result"]["diagnostics"]
        ] == [("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH)]

        # A filtered read answers on the same channel too.
        bounded = framed(
            service, "07-filtered", "workspace.inspect", {"collection": "projections"}
        )
        assert bounded["ok"] is True
        assert set(bounded["result"]["document"]["projections"]) == {
            "inspector",
            "network",
        }
    finally:
        service.close()


def test_one_bad_observer_never_blocks_unrelated_content(
    service_adapter: PlatformAdapter,
):
    """A second collection keeps resolving while the first one is damaged."""

    service = build_supplier_workspace(service_adapter)
    try:
        assert select(service, "05-select", "S-42", event_guards(service)).ok
        assert (
            import_source(
                service,
                "05-sites",
                "site-records",
                table_bytes(SITES, ("site_id", "region")),
                "application/vnd.apache.parquet",
                0,
            ).ok
        )
        operations = [
            {
                "op": "entity.create",
                "collection": "collections",
                "id": "sites",
                "value": {
                    "source": {"sourceId": "site-records", "version": 1},
                    "key": "site_id",
                },
            },
            {
                "op": "entity.create",
                "collection": "state",
                "id": "selected_site",
                "value": {
                    "type": {"kind": "ref", "collection": "sites"},
                    "nullable": True,
                    "default": None,
                    "scope": "workspace",
                },
            },
            {
                "op": "entity.create",
                "collection": "projections",
                "id": "site-inspector",
                "value": {
                    "kind": "record",
                    "title": "Site inspector",
                    "collection": "sites",
                    "config": {"fields": None},
                    "controls": [],
                    "observe": {"record": {"state": "selected_site"}},
                },
            },
            {
                "op": "layout.replace",
                "value": {
                    "root": {
                        "kind": "split",
                        "axis": "horizontal",
                        "ratio": 0.6,
                        "first": {"kind": "pane", "projection": "network"},
                        "second": {"kind": "pane", "projection": "inspector"},
                    },
                    "hidden": ["site-inspector"],
                },
            },
        ]
        expected = {
            "collections/sites": 0,
            "semantic-state/selected_site": 0,
            "projections/site-inspector": 0,
            "layout": revision_of(service, "layout"),
        }
        assert apply(service, "06-sites", expected, operations).ok
        assigned = apply(
            service,
            "07-assign",
            {"refstate/selected_site": 1},
            [
                {
                    "op": "state.set",
                    "id": "selected_site",
                    "value": {
                        "collection": "sites",
                        "incarnation": service.state.collection_incarnations[
                            "sites"
                        ],
                        "key": "SITE-1",
                    },
                }
            ],
        )
        assert assigned.ok, assigned.errors

        artifact_file(service).unlink()
        response = inspect_workspace(service, "08-inspect")
        assert response.ok, response.errors

        # The damaged collection's observer is unavailable...
        damaged = response.result["semantic"]["observers"]["inspector"]
        assert damaged["display"] == "unavailable"
        assert damaged["value"]["key"] == "S-42"
        # ...while the unrelated collection resolves its record.
        healthy = response.result["semantic"]["observers"]["site-inspector"]
        assert healthy["display"] == "record"
        assert healthy["record"]["cells"] == {
            "site_id": "SITE-1",
            "region": "North",
        }
        states = response.result["semantic"]["referenceStates"]
        assert (
            states["selected_supplier"]["resolution"]["status"] == "unavailable"
        )
        assert states["selected_site"]["resolution"] == {"status": "resolved"}
        # Both sources stay in the bounded document.
        assert set(response.result["document"]["sources"]) == {
            "supplier-records",
            "supplier-network",
            "site-records",
        }
    finally:
        service.close()


def test_corrupt_artifact_bytes_answer_unavailable(
    service_adapter: PlatformAdapter,
):
    """Bytes that read but no longer decode stay a recoverable error."""

    service, target = selected_supplier_workspace(service_adapter)
    try:
        target.chmod(0o644)
        target.write_bytes(b"not a parquet file at all")
        response = inspect_workspace(service, "06-inspect")
        assert response.ok, response.errors
        observer = response.result["semantic"]["observers"]["inspector"]
        assert observer["display"] == "unavailable"
        assert observer["resolution"]["status"] == "unavailable"
        assert "Parquet" in observer["resolution"]["reason"]
        assert observer["value"]["key"] == "S-42"
        # The stored size no longer matches the declared version, so the
        # normal artifact diagnostic names the damage as well.
        assert ("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH) in codes_of(response)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Restart and restoration (SPEC.md section 14, RFC 001 section 10.5)
# ---------------------------------------------------------------------------


def test_restart_with_a_missing_artifact_keeps_the_workspace_inspectable(
    service_adapter: PlatformAdapter,
):
    service, _ = selected_with_a_missing_table_artifact(service_adapter)
    committed = inspect_workspace(service).result
    service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        response = inspect_workspace(reopened, "06-inspect")
        assert response.ok, response.errors
        observer = response.result["semantic"]["observers"]["inspector"]
        assert observer["display"] == "unavailable"
        assert observer["value"]["key"] == "S-42"

        # The committed document, revisions, and selection are the ones
        # the closed service saved; nothing reran or rewrote them.
        assert response.result["document"] == committed["document"]
        stored = response.result["semantic"]["referenceStates"][
            "selected_supplier"
        ]
        assert stored["revision"] == 2
        assert reopened.runs == {}

        # Startup reported the missing artifact individually, and the
        # recovery report carries the same damage (SPEC.md section 14).
        assert ("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH) in codes_of(response)
        missing = response.result["recovery"]["missingArtifacts"]
        assert [
            (item["code"], item["path"]) for item in missing
        ] == [("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH)]
    finally:
        reopened.close()


def test_restoring_the_same_bytes_resolves_the_observer_again(
    service_adapter: PlatformAdapter,
):
    service, _ = selected_with_a_missing_table_artifact(service_adapter)
    try:
        damaged = inspect_workspace(service, "06-inspect")
        assert damaged.ok, damaged.errors
        assert (
            damaged.result["semantic"]["observers"]["inspector"]["display"]
            == "unavailable"
        )

        # The same content-addressed bytes return under the same
        # reference: no new source version, no selection change, and no
        # Python execution anywhere on the path.
        assert (
            service.store.artifacts.write(suppliers_bytes(SUPPLIERS_V1))
            == service.state.document["sources"]["supplier-records"]["versions"][
                "1"
            ]["artifact"]
        )
        restored = inspect_workspace(service, "07-inspect")
        assert restored.ok, restored.errors
        observer = restored.result["semantic"]["observers"]["inspector"]
        assert observer["display"] == "record"
        assert observer["record"]["cells"] == {
            "supplier_id": "S-42",
            "name": "Alpha Components",
        }
        assert observer["value"]["key"] == "S-42"
        state = restored.result["semantic"]["referenceStates"][
            "selected_supplier"
        ]
        assert state["revision"] == 2
        assert state["resolution"] == {"status": "resolved"}
        assert codes_of(restored) == []
        assert service.runs == {}
        assert service.pending_intents == {}
        assert list(service.store.layout.staging.iterdir()) == []
        assert list(service.store.layout.logs.iterdir()) == []
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The command-line harness path
# ---------------------------------------------------------------------------


def test_the_harness_inspects_and_renders_through_the_damage(
    repo_root, supplier_workspace
):
    """The harness process answers instead of dying on the damaged file."""

    document_root = supplier_workspace / "melt" / "workspaces" / WORKSPACE
    code, selected = run_harness(
        repo_root,
        supplier_workspace,
        "select",
        "--projection",
        "network",
        "--key",
        "S-42",
    )
    assert code == 0, selected
    assert selected["ok"] is True

    code, snapshot = run_harness(repo_root, supplier_workspace, "inspect")
    assert code == 0, snapshot
    artifact = snapshot["result"]["document"]["sources"]["supplier-records"][
        "versions"
    ]["1"]["artifact"]
    (document_root / "artifacts" / artifact[len("sha256:") :]).unlink()

    code, answer = run_harness(repo_root, supplier_workspace, "inspect")
    assert code == 0, answer
    assert answer["ok"] is True
    observer = answer["result"]["semantic"]["observers"]["inspector"]
    assert observer["display"] == "unavailable"
    assert observer["value"]["key"] == "S-42"
    assert ("E_ARTIFACT", SUPPLIER_ARTIFACT_PATH) in [
        (item["code"], item["path"]) for item in answer["result"]["diagnostics"]
    ]

    # The test renderer draws the same recoverable state the inspection
    # reports; the rendered answer and the inspected answer agree.
    code, rendered = run_harness(repo_root, supplier_workspace, "render")
    assert code == 0, rendered
    panes = {pane["projection"]: pane for pane in rendered["panes"]}
    pane = panes["inspector"]
    assert pane["display"] == "unavailable"
    assert pane["resolution"]["status"] == "unavailable"
    assert pane["value"]["key"] == "S-42"
