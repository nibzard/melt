"""Artifact store and source import (T011, SPEC.md sections 5 and 12.6).

The tests prove the import loop through the real service and its
durable storage:

- A capture mints one opaque token; ``source.import`` redeems it,
  rereads the stored bytes, verifies their hash and size, measures the
  schema, and commits one immutable source version with import
  provenance.
- Versions append: earlier versions stay byte for byte, and
  ``currentVersion`` follows the newest version.
- ``expected`` carries the source record revision, or zero for a new
  source, so concurrent imports surface as conflicts.
- Imports use durable request receipts: replay returns the stored
  answer, reuse with different content fails with ``E_REQUEST_REUSE``.
- No document command can forge a source: the mutation operations
  address no source collection, and references to versions the service
  never issued are refused.
- A restart restores every committed version and rebuilds the trusted
  source catalog from the committed artifact records, without rereading
  any artifact bytes or executing project code.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from melt.schema.canonical import canonical_hash, model_to_wire
from melt.schema.commands import RegistrationContract, parse_command
from runtime.compiler.catalog import ActionRegistry, RegistrationEntry
from runtime.storage import PlatformAdapter, storage_worker_available
from runtime.workspace import ARTIFACT_KIND, WorkspaceService

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

pytestmark = requires_storage_worker

WORKSPACE = "sales-demo"

PARQUET = "application/vnd.apache.parquet"
PNG = "image/png"
MARKDOWN = "text/markdown"

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


def _load_builder():
    """Load scripts/build_fixture_artifacts.py as a module."""

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def sales_parquet() -> bytes:
    return BUILDER.sales_parquet_bytes()


def sales_parquet_v2() -> bytes:
    """A second, different sales table: one added row."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    months = ["2026-01", "2026-01", "2026-02", "2026-03"]
    revenues = [100.0, 300.0, 200.0, 50.0]
    table = pa.Table.from_arrays(
        [
            pa.array(months, type=pa.string()),
            pa.array(revenues, type=pa.float64()),
        ],
        schema=BUILDER.SALES_TABLE_SCHEMA,
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="snappy", version="2.6")
    return sink.getvalue().to_pybytes()


def revenue_png() -> bytes:
    return BUILDER.revenue_png_bytes()


def notes_markdown() -> bytes:
    return BUILDER.notes_markdown_bytes()


def import_envelope(
    request_id: str,
    source_id: str,
    token: str,
    label: str = "sales.csv",
    *,
    expected: "dict | None" = None,
):
    return parse_command(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": "source.import",
            "expected": expected,
            "payload": {
                "sourceId": source_id,
                "artifactToken": token,
                "originLabel": label,
            },
        }
    )


def apply_envelope(request_id: str, expected: dict, operations: list):
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


def raw_envelope(request_id: str, method: str, payload: dict) -> bytes:
    return json.dumps(
        {
            "protocolVersion": 1,
            "workspaceId": WORKSPACE,
            "requestId": request_id,
            "method": method,
            "payload": payload,
        }
    ).encode("utf-8")


def created_service(adapter: PlatformAdapter, document: "dict | None" = None):
    """One service whose workspace was created from a document."""

    service = WorkspaceService.open(adapter, WORKSPACE)
    created = service.create_workspace(
        document or json.loads(json.dumps(EMPTY_DOCUMENT)), "01-create"
    )
    assert created.ok, created.errors
    return service


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    """One opened service over a created workspace, closed with the test."""

    opened = created_service(service_adapter)
    yield opened
    opened.close()


def capture(service: WorkspaceService, data: bytes, media_type: str) -> str:
    return service.capture_artifact_token(data, media_type).token


def imported(service: WorkspaceService, request_id: str, source_id: str, data: bytes,
             media_type: str, label: str = "sales.csv", expected: int = 0):
    token = capture(service, data, media_type)
    return service.dispatch(
        import_envelope(
            request_id, source_id, token, label, expected={"sources/" + source_id: expected}
        )
    )


# ---------------------------------------------------------------------------
# Registration and append-only versions (SPEC.md sections 5.1 and 5.2)
# ---------------------------------------------------------------------------


def test_import_registers_a_verified_table_version(service: WorkspaceService):
    data = sales_parquet()
    response = imported(service, "02-import", "sales-input", data, PARQUET)
    assert response.ok, response.errors
    source = service.document["sources"]["sales-input"]
    assert source["kind"] == "table"
    assert source["currentVersion"] == 1
    version = source["versions"]["1"]
    assert version["artifact"] == response.result["artifact"]
    assert version["artifact"].startswith("sha256:")
    assert version["artifact"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert version["mediaType"] == PARQUET
    assert version["byteLength"] == len(data)
    assert [column["id"] for column in version["schema"]["columns"]] == [
        "month",
        "revenue",
    ]
    assert version["origin"] == {"kind": "import", "label": "sales.csv"}
    # The answer is the new source reference (SPEC.md section 12.6).
    assert response.result["source"] == {"sourceId": "sales-input", "version": 1}
    assert response.result["sourceRevision"] == 1
    assert response.result["kind"] == "table"
    assert response.result["byteLength"] == len(data)
    # The record revision and the change notification name the source.
    assert service.entity_revisions["sources/sales-input"] == 1
    notification = service.notifications[-1]
    assert notification["changedRecords"] == ["sources/sales-input"]
    # The verified artifact record and the stored bytes both exist.
    records = service.store.records_of_kind(ARTIFACT_KIND)
    assert len(records) == 1
    assert records[0].data["artifact"] == version["artifact"]
    assert records[0].data["schema"]["columns"][0]["id"] == "month"
    assert service.store.read_artifact(version["artifact"]) == data


def test_imports_append_immutable_versions(service: WorkspaceService):
    first = imported(service, "02-import-1", "sales-input", sales_parquet(), PARQUET)
    assert first.ok, first.errors
    second = imported(
        service,
        "02-import-2",
        "sales-input",
        sales_parquet_v2(),
        PARQUET,
        label="sales-v2.csv",
        expected=1,
    )
    assert second.ok, second.errors
    assert second.result["source"] == {"sourceId": "sales-input", "version": 2}
    source = service.document["sources"]["sales-input"]
    assert source["currentVersion"] == 2
    assert sorted(source["versions"]) == ["1", "2"]
    # The first version is untouched: same artifact, schema, and origin.
    assert source["versions"]["1"] == {
        "artifact": first.result["artifact"],
        "mediaType": PARQUET,
        "byteLength": first.result["byteLength"],
        "schema": first.result["schema"],
        "origin": {"kind": "import", "label": "sales.csv"},
    }
    assert source["versions"]["2"]["origin"] == {
        "kind": "import",
        "label": "sales-v2.csv",
    }
    assert source["versions"]["2"]["artifact"] != source["versions"]["1"]["artifact"]
    assert service.entity_revisions["sources/sales-input"] == 2
    assert len(service.store.records_of_kind(ARTIFACT_KIND)) == 2


def test_two_sources_may_share_artifact_bytes(service: WorkspaceService):
    first = imported(service, "02-import-1", "sales-input", sales_parquet(), PARQUET)
    assert first.ok, first.errors
    second = imported(service, "02-import-2", "sales-copy", sales_parquet(), PARQUET)
    assert second.ok, second.errors
    assert second.result["artifact"] == first.result["artifact"]
    assert second.result["source"] == {"sourceId": "sales-copy", "version": 1}
    # The same bytes commit exactly one verified artifact record.
    assert len(service.store.records_of_kind(ARTIFACT_KIND)) == 1


def test_image_and_text_imports_measure_their_schemas(service: WorkspaceService):
    image = imported(
        service, "02-png", "revenue-plot", revenue_png(), PNG, label="revenue.png"
    )
    assert image.ok, image.errors
    assert image.result["kind"] == "image"
    assert image.result["schema"] == {"width": 64, "height": 48}
    source = service.document["sources"]["revenue-plot"]
    assert source["kind"] == "image"
    assert source["versions"]["1"]["schema"] == {"width": 64, "height": 48}

    text = imported(
        service, "03-notes", "notes", notes_markdown(), MARKDOWN, label="notes.md"
    )
    assert text.ok, text.errors
    assert text.result["kind"] == "text"
    assert text.result["schema"] == {"encoding": "utf-8"}
    assert service.document["sources"]["notes"]["versions"]["1"]["mediaType"] == MARKDOWN


def test_import_rejects_a_kind_change_of_an_existing_source(service: WorkspaceService):
    first = imported(service, "02-import", "sales-input", sales_parquet(), PARQUET)
    assert first.ok, first.errors
    second = imported(
        service, "03-import", "sales-input", revenue_png(), PNG, expected=1
    )
    assert second.ok is False
    assert [(error.code, error.path) for error in second.errors] == [
        ("E_ARTIFACT", "/payload/bytes")
    ]
    assert "fixed at creation" in second.errors[0].message
    # Nothing was appended.
    assert service.document["sources"]["sales-input"]["currentVersion"] == 1


# ---------------------------------------------------------------------------
# Expectations, tokens, and receipts (SPEC.md sections 12.2, 12.5, 12.6)
# ---------------------------------------------------------------------------


def test_import_rejects_stale_or_missing_expectations(service: WorkspaceService):
    assert imported(service, "02-import", "sales-input", sales_parquet(), PARQUET).ok

    cases = {
        "missing expected": None,
        "zero for an existing source": 0,
        "stale revision": 2,
    }
    for label, expected in cases.items():
        token = capture(service, sales_parquet_v2(), PARQUET)
        envelope = import_envelope(
            "03-import-" + label.replace(" ", "-"),
            "sales-input",
            token,
            expected=None if expected is None else {"sources/sales-input": expected},
        )
        answer = service.dispatch(envelope)
        assert answer.ok is False, label
        assert answer.errors[0].code == "E_CONFLICT", label
        assert answer.errors[0].path.startswith("/expected"), label
    assert service.document["sources"]["sales-input"]["currentVersion"] == 1


def test_import_rejects_unknown_and_script_tokens(service: WorkspaceService):
    unknown = service.dispatch(
        import_envelope(
            "02-unknown", "sales-input", "melttok_nobody", expected={"sources/sales-input": 0}
        )
    )
    assert unknown.ok is False
    assert [(error.code, error.path) for error in unknown.errors] == [
        ("E_ARTIFACT", "/payload/artifactToken")
    ]

    script = capture(service, b"def run(ui):\n    pass\n", "text/x-python")
    refused = service.dispatch(
        import_envelope(
            "02-script", "sales-input", script, expected={"sources/sales-input": 0}
        )
    )
    assert refused.ok is False
    assert refused.errors[0].code == "E_ARTIFACT"
    assert "script" in refused.errors[0].message
    assert service.document["sources"] == {}


def test_import_replays_its_receipt_and_refuses_reuse(service: WorkspaceService):
    token = capture(service, sales_parquet(), PARQUET)
    envelope = import_envelope(
        "02-import", "sales-input", token, expected={"sources/sales-input": 0}
    )
    first = service.dispatch(envelope)
    assert first.ok, first.errors
    # A retry with the same identifier and payload replays the stored
    # response and appends nothing, even though the token still lives.
    replay = service.dispatch(
        import_envelope(
            "02-import", "sales-input", token, expected={"sources/sales-input": 0}
        )
    )
    assert replay.ok, replay.errors
    assert model_to_wire(replay) == model_to_wire(first)
    assert service.document["sources"]["sales-input"]["currentVersion"] == 1
    # The same identifier with different content is refused.
    other = service.dispatch(
        import_envelope(
            "02-import",
            "sales-input",
            token,
            label="other.csv",
            expected={"sources/sales-input": 0},
        )
    )
    assert other.ok is False
    assert [(error.code, error.path) for error in other.errors] == [
        ("E_REQUEST_REUSE", "/requestId")
    ]


def test_import_requires_a_created_workspace(service_adapter: PlatformAdapter):
    service = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        token = capture(service, sales_parquet(), PARQUET)
        answer = service.dispatch(
            import_envelope(
                "02-import", "sales-input", token, expected={"sources/sales-input": 0}
            )
        )
        assert answer.ok is False
        assert [(error.code, error.path) for error in answer.errors] == [
            ("E_REFERENCE", "/workspaceId")
        ]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Documents cannot forge artifact records (SPEC.md sections 5.1 and 12.2)
# ---------------------------------------------------------------------------


def test_document_commands_cannot_address_sources(service: WorkspaceService):
    answer = service.handle_request(
        json.dumps(
            {
                "protocolVersion": 1,
                "workspaceId": WORKSPACE,
                "requestId": "02-forge",
                "method": "workspace.apply",
                "expected": {},
                "operations": [
                    {
                        "op": "entity.create",
                        "collection": "sources",
                        "id": "forged",
                        "value": {
                            "kind": "table",
                            "currentVersion": 1,
                            "versions": {},
                        },
                    }
                ],
            }
        ).encode("utf-8")
    )
    assert answer.ok is False
    codes = [error.code for error in answer.errors]
    assert "E_SCHEMA" in codes
    assert service.document["sources"] == {}


def test_definitions_reference_only_issued_versions(service_adapter: PlatformAdapter):
    service = created_service(service_adapter)
    try:
        table_projection = {
            "kind": "table",
            "title": "Sales",
            "source": {"sourceId": "sales-input", "version": 1},
            "config": {"columns": ["month", "revenue"], "selection": "none"},
            "controls": [],
        }

        def apply(request_id: str, projection: dict):
            return service.dispatch(
                apply_envelope(
                    request_id,
                    {"projections/sales": 0, "layout": 1},
                    [
                        {
                            "op": "entity.create",
                            "collection": "projections",
                            "id": "sales",
                            "value": projection,
                        },
                        {
                            "op": "layout.replace",
                            "value": {"root": None, "hidden": ["sales"]},
                        },
                    ],
                )
            )

        # A source the service never issued is refused.
        missing = apply("02-missing", table_projection)
        assert missing.ok is False
        assert [(error.code, error.path) for error in missing.errors] == [
            ("E_REFERENCE", "/projections/sales/source")
        ]

        # Version 2 does not exist yet either.
        ahead = apply(
            "02-ahead",
            {
                **table_projection,
                "source": {"sourceId": "sales-input", "version": 2},
            },
        )
        assert ahead.ok is False
        assert ahead.errors[0].code == "E_REFERENCE"

        # After the import, the same definition applies cleanly.
        assert imported(service, "03-import", "sales-input", sales_parquet(), PARQUET).ok
        applied = apply("04-apply", table_projection)
        assert applied.ok, applied.errors
        assert service.document["projections"]["sales"]["source"] == {
            "sourceId": "sales-input",
            "version": 1,
        }
    finally:
        service.close()


def test_imported_source_feeds_action_inputs(service_adapter: PlatformAdapter):
    contract = RegistrationContract.model_validate(
        {
            "parameters": {},
            "inputs": {"sales": "table"},
            "outputs": {"filtered": "table"},
        }
    )
    registry = ActionRegistry(
        {"sales-script-v1": RegistrationEntry("sales-script-v1", 1, contract)}
    )
    service = WorkspaceService.open(service_adapter, WORKSPACE, action_registry=registry)
    try:
        created = service.create_workspace(
            json.loads(json.dumps(EMPTY_DOCUMENT)), "01-create"
        )
        assert created.ok, created.errors
        assert imported(service, "02-import", "sales-input", sales_parquet(), PARQUET).ok
        action = {
            "implementation": {"registrationId": "sales-script-v1"},
            "parameters": {},
            "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
            "outputs": {"filtered": "table"},
            "outputGroup": "sales-results",
            "targets": {"filtered": "filtered"},
        }
        projection = {
            "kind": "table",
            "title": "Filtered",
            "source": None,
            "config": {"columns": None, "selection": "none"},
            "controls": [],
        }
        applied = service.dispatch(
            apply_envelope(
                "03-definitions",
                {"actions/analyze": 0, "projections/filtered": 0, "layout": 1},
                [
                    {
                        "op": "entity.create",
                        "collection": "actions",
                        "id": "analyze",
                        "value": action,
                    },
                    {
                        "op": "entity.create",
                        "collection": "projections",
                        "id": "filtered",
                        "value": projection,
                    },
                    {
                        "op": "layout.replace",
                        "value": {"root": None, "hidden": ["filtered"]},
                    },
                ],
            )
        )
        assert applied.ok, applied.errors
        # An inspection of the committed workspace reports no drift:
        # the imported catalog covers every source the document cites.
        inspect = service.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "04-inspect",
                    "method": "workspace.inspect",
                    "payload": {},
                }
            )
        )
        assert inspect.ok, inspect.errors
        assert inspect.result["diagnostics"] == []
        revisions = inspect.result["entityRevisions"]
        assert revisions["sources/sales-input"] == 1
        assert revisions["actions/analyze"] == 1
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Restart recovery (SPEC.md section 14, startup steps 2 and 3)
# ---------------------------------------------------------------------------


def test_restart_restores_versions_and_rebuilds_the_catalog(
    service_adapter: PlatformAdapter,
):
    service = created_service(service_adapter)
    assert imported(service, "02-import-1", "sales-input", sales_parquet(), PARQUET).ok
    assert (
        imported(
            service,
            "02-import-2",
            "sales-input",
            sales_parquet_v2(),
            PARQUET,
            expected=1,
        ).ok
    )
    stale_token = capture(service, notes_markdown(), MARKDOWN)
    service.close()

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        # Every committed version and revision came back.
        source = reopened.document["sources"]["sales-input"]
        assert sorted(source["versions"]) == ["1", "2"]
        assert source["currentVersion"] == 2
        assert reopened.entity_revisions["sources/sales-input"] == 2
        # The rebuilt catalog keeps the saved sources readable: the
        # recompiled document reports no drift.
        assert reopened.source_catalog.entry(source["versions"]["1"]["artifact"]) is not None
        inspect = reopened.dispatch(
            parse_command(
                {
                    "protocolVersion": 1,
                    "workspaceId": WORKSPACE,
                    "requestId": "03-inspect",
                    "method": "workspace.inspect",
                    "payload": {},
                }
            )
        )
        assert inspect.ok, inspect.errors
        assert inspect.result["diagnostics"] == []
        # Receipts survive the restart: the reused request identifier
        # with new content (a fresh capture holds a fresh token) is
        # refused with E_REQUEST_REUSE (SPEC.md section 12.5).
        token = capture(reopened, sales_parquet(), PARQUET)
        reuse = reopened.dispatch(
            import_envelope(
                "02-import-1",
                "sales-input",
                token,
                expected={"sources/sales-input": 1},
            )
        )
        assert reuse.ok is False
        assert [(error.code, error.path) for error in reuse.errors] == [
            ("E_REQUEST_REUSE", "/requestId")
        ]
        # A restart withdraws every outstanding token.
        refused = reopened.dispatch(
            import_envelope(
                "04-stale",
                "notes",
                stale_token,
                expected={"sources/notes": 0},
            )
        )
        assert refused.ok is False
        assert refused.errors[0].code == "E_ARTIFACT"
        # And a fresh capture appends the next version.
        third = imported(
            reopened,
            "05-import-3",
            "sales-input",
            sales_parquet(),
            PARQUET,
            expected=2,
        )
        assert third.ok, third.errors
        assert third.result["source"]["version"] == 3
        assert reopened.document["sources"]["sales-input"]["currentVersion"] == 3
    finally:
        reopened.close()


def test_restart_without_imports_starts_with_an_empty_catalog(
    service_adapter: PlatformAdapter,
):
    service = created_service(service_adapter)
    service.close()
    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.source_catalog.artifacts == {}
        assert reopened.document is not None
        assert reopened.document["sources"] == {}
    finally:
        reopened.close()
