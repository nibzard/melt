"""The trusted table reader (T012/T018, SPEC.md sections 8.2 and 12.7).

The reader answers ``table.page`` over artifact bytes the service
registered. The suite proves the contract end to end:

- Operators validate against the declared column types before any
  byte is read; filters combine with logical AND, and a non-null
  comparison against a null cell is false.
- Strings compare by Unicode code point; sorting places nulls last
  whatever the direction and keeps the original row position as the
  final tie-breaker; filters apply before sorting and paging.
- Responses identify the source reference and a query hash that covers
  the source, sort, and filters while excluding offset and limit.
- ``int64`` and ``decimal`` cells travel as strings; dates and
  timestamps encode as their wire strings.
- The limits hold: 256 rows per request, an explicit
  column-selection requirement above 128 columns, and the encoded byte
  bound that may reduce rows but never answers an endless empty page
  with ``hasMore`` true.
- The service route resolves the immutable source reference to stored
  bytes and refuses unknown sources, wrong kinds, and missing versions
  with structured diagnostics.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits
from runtime.storage import PlatformAdapter
from runtime.workspace import PageQuery, read_table_page
from runtime.workspace.importer import measure_artifact

# The T016 scheduling harness: seeding, envelopes, parameters.
from test_scheduling import requires_storage_worker, seed_workspace

pytestmark = requires_storage_worker

PARQUET = "application/vnd.apache.parquet"

RICH_SCHEMA = pa.schema(
    [
        pa.field("month", pa.string(), nullable=True),
        pa.field("revenue", pa.float64(), nullable=True),
        pa.field("quantity", pa.int64(), nullable=True),
        pa.field("price", pa.decimal128(10, 2), nullable=True),
        pa.field("ok", pa.bool_(), nullable=True),
        pa.field("day", pa.date32(), nullable=True),
        pa.field("moment", pa.timestamp("us"), nullable=True),
        pa.field("Revenue Note", pa.string(), nullable=True),
    ]
)

RICH_ROWS = {
    "month": ["2026-01", "2026-01", "2026-02", "2026-03", "é-tag", None, "2026-01", "2026-02"],
    "revenue": [100.0, 300.0, 200.0, None, 50.0, 60.0, 300.0, 200.0],
    "quantity": [1, 2, None, 4, 5, 6, 2, None],
    "price": [
        None,
        "10.50",
        "20.00",
        "30.25",
        "40.00",
        "50.75",
        "10.50",
        "20.00",
    ],
    "ok": [True, False, None, True, False, True, False, None],
    "day": [
        date(2026, 1, 1),
        date(2026, 1, 2),
        None,
        date(2026, 3, 4),
        date(2026, 3, 5),
        date(2026, 3, 6),
        date(2026, 1, 2),
        None,
    ],
    "moment": [
        datetime(2026, 1, 1, 12, 0, 0),
        datetime(2026, 1, 2, 12, 30, 15, 500000),
        None,
        datetime(2026, 3, 4, 0, 0, 0),
        datetime(2026, 3, 5, 0, 0, 0),
        None,
        datetime(2026, 1, 2, 12, 30, 15, 500000),
        datetime(2026, 2, 1, 6, 0, 0),
    ],
    "Revenue Note": ["first", None, "third", "fourth", None, "sixth", "seventh", None],
}


def rich_table() -> pa.Table:
    columns = {
        name: list(values) for name, values in RICH_ROWS.items()
    }
    columns["price"] = [
        None if item is None else Decimal(item)
        for item in RICH_ROWS["price"]
    ]
    return pa.Table.from_pydict(columns, schema=RICH_SCHEMA)


def rich_bytes() -> bytes:
    import io

    sink = pa.BufferOutputStream()
    pq.write_table(rich_table(), sink)
    return sink.getvalue().to_pybytes()


def rich_schema_wire() -> dict:
    return measure_artifact(rich_bytes(), kind="table", media_type=PARQUET)


def page(sort=(), filters=(), offset=0, limit=50, source=("rich", 1)) -> PageQuery:
    return PageQuery(
        source={"sourceId": source[0], "version": source[1]},
        sort=list(sort),
        filters=list(filters),
        offset=offset,
        limit=limit,
    )


def read(query: PageQuery, *, limits: ServiceLimits = DEFAULT_LIMITS):
    return read_table_page(rich_bytes(), rich_schema_wire(), query, limits=limits)


def codes_of(errors):
    return [(error.code, error.path) for error in errors]


# ---------------------------------------------------------------------------
# Shape, encoding, and the query hash
# ---------------------------------------------------------------------------


def test_a_page_carries_the_source_query_hash_and_wire_cells():
    result, errors = read(page())
    assert errors is None
    assert result["source"] == {"sourceId": "rich", "version": 1}
    assert result["columns"] == [
        "month",
        "revenue",
        "quantity",
        "price",
        "ok",
        "day",
        "moment",
        "revenue_note",
    ]
    # The retained label maps the declared identifier back onto the
    # Parquet field (SPEC.md section 5.1).
    assert result["rows"][0] == [
        "2026-01",
        100.0,
        "1",
        None,
        True,
        "2026-01-01",
        "2026-01-01T12:00:00Z",
        "first",
    ]
    # int64 and decimal travel as strings; timestamps carry the Z suffix.
    assert result["rows"][1][2] == "2"
    assert result["rows"][1][3] == "10.50"
    assert result["rows"][1][6] == "2026-01-02T12:30:15.5Z"
    assert result["offset"] == 0
    assert result["hasMore"] is False
    assert result["nextOffset"] is None


def test_the_query_hash_covers_source_sort_and_filters_only():
    plain, _ = read(page())
    sorted_page, _ = read(page(sort=[{"column": "month", "direction": "asc"}]))
    assert plain["queryHash"] != sorted_page["queryHash"]
    filtered, _ = read(page(filters=[{"column": "month", "operator": "eq", "value": "2026-01"}]))
    assert filtered["queryHash"] != plain["queryHash"]
    # Offset and limit stay outside the hash: the same query pages.
    farther, _ = read(page(offset=3, limit=2))
    assert farther["queryHash"] == plain["queryHash"]


def test_paging_names_the_next_offset_and_stops_at_the_end():
    first, errors = read(page(limit=3))
    assert errors is None
    assert len(first["rows"]) == 3
    assert first["hasMore"] is True
    assert first["nextOffset"] == 3
    second, _ = read(page(offset=3, limit=3))
    assert second["offset"] == 3
    third, _ = read(page(offset=6, limit=3))
    assert len(third["rows"]) == 2
    assert third["hasMore"] is False
    assert third["nextOffset"] is None
    # An offset beyond the end answers one empty terminal page, never
    # an endless empty page with hasMore true.
    beyond, _ = read(page(offset=100, limit=5))
    assert beyond["rows"] == []
    assert beyond["hasMore"] is False
    assert beyond["nextOffset"] is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_filters_combine_with_logical_and():
    result, errors = read(
        page(
            filters=[
                {"column": "month", "operator": "eq", "value": "2026-01"},
                {"column": "revenue", "operator": "gte", "value": 200},
            ]
        )
    )
    assert errors is None
    assert [row[1] for row in result["rows"]] == [300.0, 300.0]


def test_a_non_null_comparison_against_a_null_cell_is_false():
    result, errors = read(
        page(filters=[{"column": "revenue", "operator": "lt", "value": 1000}])
    )
    assert errors is None
    # The null revenue row drops; isNull selects it.
    assert None not in [row[1] for row in result["rows"]]
    nulls, _ = read(page(filters=[{"column": "revenue", "operator": "isNull"}]))
    assert [row[1] for row in nulls["rows"]] == [None]


def test_strings_compare_by_unicode_code_point():
    result, errors = read(
        page(filters=[{"column": "month", "operator": "lt", "value": "2026-02"}])
    )
    assert errors is None
    assert sorted({row[0] for row in result["rows"]}) == ["2026-01"]
    # 'é' (U+00E9, 233) sorts after every ASCII letter (code points).
    after_z, _ = read(
        page(filters=[{"column": "month", "operator": "gt", "value": "z"}])
    )
    assert [row[0] for row in after_z["rows"]] == ["é-tag"]


def test_contains_matches_substrings_of_string_columns():
    result, errors = read(
        page(filters=[{"column": "revenue_note", "operator": "contains", "value": "irs"}])
    )
    assert errors is None
    assert [row[7] for row in result["rows"]] == ["first"]
    # The null note drops: a non-null comparison against a null cell is
    # false, and contains is one (SPEC.md section 12.7).
    assert None not in [row[7] for row in result["rows"]]


def test_operators_validate_against_column_types():
    _, errors = read(
        page(filters=[{"column": "ok", "operator": "lt", "value": True}])
    )
    assert ("E_TYPE", "/payload/filters/0/operator") in codes_of(errors)
    _, errors = read(
        page(filters=[{"column": "revenue", "operator": "contains", "value": "0"}])
    )
    assert ("E_TYPE", "/payload/filters/0/operator") in codes_of(errors)
    _, errors = read(
        page(filters=[{"column": "month", "operator": "eq", "value": 5}])
    )
    assert ("E_TYPE", "/payload/filters/0/value") in codes_of(errors)
    _, errors = read(
        page(filters=[{"column": "quantity", "operator": "eq", "value": 1.5}])
    )
    assert ("E_TYPE", "/payload/filters/0/value") in codes_of(errors)
    _, errors = read(
        page(filters=[{"column": "day", "operator": "gte", "value": "01-01-2026"}])
    )
    assert ("E_TYPE", "/payload/filters/0/value") in codes_of(errors)
    _, errors = read(
        page(
            filters=[
                {"column": "moment", "operator": "lt", "value": "2026-01-02"}
            ]
        )
    )
    assert ("E_TYPE", "/payload/filters/0/value") in codes_of(errors)
    # A number compares against numeric columns, whole against int64.
    result, errors = read(
        page(filters=[{"column": "quantity", "operator": "eq", "value": 2}])
    )
    assert errors is None
    assert len(result["rows"]) == 2
    priced, errors = read(
        page(filters=[{"column": "price", "operator": "gt", "value": 20}])
    )
    assert errors is None
    assert [row[3] for row in priced["rows"]] == ["30.25", "40.00", "50.75"]
    dated, errors = read(
        page(
            filters=[
                {"column": "day", "operator": "lte", "value": "2026-01-02"}
            ]
        )
    )
    assert errors is None
    assert len(dated["rows"]) == 3


def test_unknown_columns_are_reference_errors():
    _, errors = read(page(sort=[{"column": "nope", "direction": "asc"}]))
    assert ("E_REFERENCE", "/payload/sort/0/column") in codes_of(errors)
    _, errors = read(
        page(filters=[{"column": "nope", "operator": "eq", "value": 1}])
    )
    assert ("E_REFERENCE", "/payload/filters/0/column") in codes_of(errors)


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_sorting_places_nulls_last_and_keeps_row_position_order():
    result, errors = read(page(sort=[{"column": "revenue", "direction": "asc"}]))
    assert errors is None
    values = [row[1] for row in result["rows"]]
    assert values == [50.0, 60.0, 100.0, 200.0, 200.0, 300.0, 300.0, None]
    # Descending reverses values and still parks nulls last; equal
    # keys keep their original row order (position 1 before 6).
    descending, _ = read(page(sort=[{"column": "revenue", "direction": "desc"}]))
    assert [row[1] for row in descending["rows"]] == [
        300.0,
        300.0,
        200.0,
        200.0,
        100.0,
        60.0,
        50.0,
        None,
    ]
    assert [row[7] for row in descending["rows"]][:2] == [None, "seventh"]


def test_filters_apply_before_sorting_and_paging():
    result, errors = read(
        page(
            sort=[{"column": "revenue", "direction": "desc"}],
            filters=[{"column": "month", "operator": "eq", "value": "2026-01"}],
            offset=1,
            limit=2,
        )
    )
    assert errors is None
    # 2026-01 rows hold revenues 100, 300, 300 (original positions 0,
    # 1, 6); descending order is 300 (pos 1), 300 (pos 6), 100 (pos 0),
    # and the page serves positions one and two of that order.
    assert [row[1] for row in result["rows"]] == [300.0, 100.0]
    assert result["offset"] == 1
    assert result["hasMore"] is False


def test_multiple_sort_keys_apply_in_order():
    result, errors = read(
        page(
            sort=[
                {"column": "quantity", "direction": "asc"},
                {"column": "revenue", "direction": "desc"},
            ]
        )
    )
    assert errors is None
    assert [(row[2], row[1]) for row in result["rows"]] == [
        ("1", 100.0),
        ("2", 300.0),
        ("2", 300.0),
        ("4", None),
        ("5", 50.0),
        ("6", 60.0),
        (None, 200.0),
        (None, 200.0),
    ]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_the_row_limit_binds_the_request_window():
    result, errors = read(page(limit=DEFAULT_LIMITS.table_page_rows))
    assert errors is None
    assert len(result["rows"]) <= DEFAULT_LIMITS.table_page_rows


def test_a_table_wider_than_the_column_bound_requires_selection():
    wide_schema = {
        "columns": [
            {"id": f"c{index}", "type": "string", "nullable": True}
            for index in range(DEFAULT_LIMITS.table_page_columns + 1)
        ],
        "rowKey": None,
    }
    _, errors = read_table_page(
        rich_bytes(), wide_schema, page(), limits=DEFAULT_LIMITS
    )
    assert ("E_LIMIT", "/payload/source") in codes_of(errors)
    exactly_bound = {
        "columns": [
            {"id": f"c{index}", "type": "string", "nullable": True}
            for index in range(DEFAULT_LIMITS.table_page_columns)
        ],
        "rowKey": None,
    }
    _, errors = read_table_page(
        rich_bytes(), exactly_bound, page(), limits=DEFAULT_LIMITS
    )
    # The declared columns do not exist on these bytes; that is an
    # artifact drift, not a column-selection requirement.
    assert ("E_LIMIT", "/payload/source") not in codes_of(errors)


def test_the_byte_bound_reduces_rows_but_never_below_one():
    tiny = dataclasses.replace(DEFAULT_LIMITS, table_page_bytes=400)
    result, errors = read(page(limit=8), limits=tiny)
    assert errors is None
    assert len(result["rows"]) >= 1
    assert len(result["rows"]) < 8
    assert result["hasMore"] is True
    assert result["nextOffset"] == len(result["rows"])
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) <= tiny.table_page_bytes

    impossible = dataclasses.replace(DEFAULT_LIMITS, table_page_bytes=8)
    _, errors = read(page(limit=8), limits=impossible)
    assert ("E_LIMIT", "/payload/limit") in codes_of(errors)

    # Metadata also consumes bytes when the requested offset has no rows.
    _, errors = read(page(offset=100), limits=impossible)
    assert ("E_LIMIT", "/payload/limit") in codes_of(errors)


# ---------------------------------------------------------------------------
# The service route (SPEC.md section 12.6 table.page)
# ---------------------------------------------------------------------------


def _import_rich_source(service, source_id: str = "rich") -> None:
    token = service.capture_artifact_token(rich_bytes(), PARQUET).token
    imported = service.dispatch(
        _envelope(
            "10-import-rich",
            method="source.import",
            expected={f"sources/{source_id}": 0},
            payload={
                "sourceId": source_id,
                "artifactToken": token,
                "originLabel": "rich.parquet",
            },
        )
    )
    assert imported.ok, imported.errors


def _envelope(request_id: str, *, method: str, payload=None, expected=None):
    from test_scheduling import envelope

    return envelope(
        request_id, method=method, payload=payload, expected=expected
    )


def test_the_service_answers_table_page_for_an_imported_source(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    _import_rich_source(service)
    answer = service.dispatch(
        _envelope(
            "11-page",
            method="table.page",
            payload={
                "source": {"sourceId": "rich", "version": 1},
                "offset": 0,
                "limit": 3,
                "sort": [{"column": "revenue", "direction": "desc"}],
                "filters": [
                    {"column": "quantity", "operator": "gte", "value": 2}
                ],
            },
        )
    )
    assert answer.ok, answer.errors
    page_result = answer.result
    assert page_result["columns"][0] == "month"
    assert [row[1] for row in page_result["rows"]] == [300.0, 300.0, 60.0]
    # Reads answer the sequence they observed.
    assert answer.commitSequence == service.commit_sequence


def test_the_service_refuses_unknown_sources_wrong_kinds_and_versions(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    _import_rich_source(service)
    text_token = service.capture_artifact_token(
        b"# notes\n", "text/markdown"
    ).token
    imported = service.dispatch(
        _envelope(
            "12-import-text",
            method="source.import",
            expected={"sources/notes": 0},
            payload={
                "sourceId": "notes",
                "artifactToken": text_token,
                "originLabel": "notes.md",
            },
        )
    )
    assert imported.ok, imported.errors

    unknown = service.dispatch(
        _envelope(
            "13-unknown",
            method="table.page",
            payload={"source": {"sourceId": "ghost", "version": 1}, "offset": 0, "limit": 5},
        )
    )
    assert [
        (error.code, error.path) for error in unknown.errors
    ] == [("E_REFERENCE", "/payload/source/sourceId")]

    wrong_kind = service.dispatch(
        _envelope(
            "14-kind",
            method="table.page",
            payload={"source": {"sourceId": "notes", "version": 1}, "offset": 0, "limit": 5},
        )
    )
    assert [
        (error.code, error.path) for error in wrong_kind.errors
    ] == [("E_TYPE", "/payload/source/sourceId")]

    missing_version = service.dispatch(
        _envelope(
            "15-version",
            method="table.page",
            payload={"source": {"sourceId": "rich", "version": 9}, "offset": 0, "limit": 5},
        )
    )
    assert [
        (error.code, error.path) for error in missing_version.errors
    ] == [("E_REFERENCE", "/payload/source/version")]


def test_the_service_reads_published_output_sources(
    service_adapter: PlatformAdapter,
):
    """The hero numbers page through the published filtered table."""

    seeded = seed_workspace(service_adapter)
    service = seeded.service
    invoked = service.dispatch(
        _envelope(
            "20-invoke",
            method="action.invoke",
            expected={"actions/analyze-sales": 1},
            payload={"actionId": "analyze-sales"},
        )
    )
    assert invoked.ok, invoked.errors
    run_id = invoked.result["run"]["runId"]
    outcome = service.launch_admitted_run(run_id)
    assert outcome.launched, outcome.refusal
    published = service.publish_run(run_id)
    assert published.outcome == "current", published.refusal

    answer = service.dispatch(
        _envelope(
            "21-page",
            method="table.page",
            payload={
                "source": published.outputs["filtered"],
                "offset": 0,
                "limit": 10,
            },
        )
    )
    assert answer.ok, answer.errors
    assert answer.result["rows"] == [
        ["2026-01", 100.0],
        ["2026-01", 300.0],
        ["2026-02", 200.0],
    ]
    assert answer.result["hasMore"] is False


def test_unicode_rows_obey_the_encoded_byte_bound():
    table = pa.table({"text": ["😀" * 20, "😀" * 20]})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    limits = dataclasses.replace(DEFAULT_LIMITS, table_page_bytes=400)
    result, errors = read_table_page(payload, schema, page(), limits=limits)
    assert errors is None
    assert result["rows"] == [["😀" * 20]]
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= limits.table_page_bytes
    assert result["nextOffset"] == 1


def test_sorting_does_not_collide_with_imported_field_names():
    table = pa.table({"__melt_position__": ["second", "first"], "rank": [2, 1]})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    schema = measure_artifact(payload, kind="table", media_type=PARQUET)
    result, errors = read_table_page(
        payload, schema, page(sort=[{"column": "rank", "direction": "asc"}])
    )
    assert errors is None
    assert result["rows"] == [["first", "1"], ["second", "2"]]


@pytest.mark.parametrize(
    "column,value",
    [
        ("day", "2026-02-30"),
        ("day", "0000-01-01"),
        ("moment", "2026-13-01T00:00:00Z"),
        ("moment", "2026-01-01T25:00:00Z"),
        ("moment", "2026-01-01T00:00:00+01:99"),
        ("moment", "0001-01-01T00:00:00+01:00"),
        ("quantity", 1e30),
        ("quantity", -1e30),
        ("price", 1e30),
        ("price", 0.001),
    ],
)
def test_invalid_filter_values_return_diagnostics(service_adapter, column, value):
    from melt.schema.canonical import model_to_wire
    from melt.schema.commands import decode_frame, encode_frame

    result, errors = read(
        page(filters=[{"column": column, "operator": "eq", "value": value}])
    )
    assert result is None
    assert codes_of(errors) == [("E_TYPE", "/payload/filters/0/value")]

    service = seed_workspace(service_adapter, grant=False).service
    _import_rich_source(service)
    request = _envelope(
        "invalid-filter", method="table.page",
        payload={"source": {"sourceId": "rich", "version": 1},
                 "offset": 0, "limit": 3,
                 "filters": [{"column": column, "operator": "eq", "value": value}]},
    )
    answer = service.dispatch(request)
    assert not answer.ok
    assert codes_of(answer.errors) == codes_of(errors)

    frame = encode_frame(json.dumps(model_to_wire(request)).encode())
    response_frame = service.process_frame(frame)
    body, consumed = decode_frame(response_frame)
    assert consumed == len(response_frame)
    response = json.loads(body)
    assert response["ok"] is False
    assert [(e["code"], e["path"]) for e in response["errors"]] == codes_of(errors)

    # A bad filter must leave the channel usable for the next request.
    request = _envelope(
        "valid-page", method="table.page",
        payload={"source": {"sourceId": "rich", "version": 1}, "offset": 0, "limit": 3},
    )
    body, _ = decode_frame(service.process_frame(
        encode_frame(json.dumps(model_to_wire(request)).encode())
    ))
    assert json.loads(body)["ok"] is True
