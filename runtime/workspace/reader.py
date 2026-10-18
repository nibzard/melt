"""The trusted table reader (SPEC.md sections 8.2 and 12.7).

The service alone reads table artifacts for page requests. The reader
resolves the immutable source reference to stored artifact bytes, maps
the declared column identifiers to the labels the Parquet file carries,
and answers one bounded page:

- Operators are validated against the declared column types before any
  byte is read. Filters combine with logical AND, and a non-null
  comparison against a null cell is false; null membership uses
  ``isNull`` alone.
- Strings compare by Unicode code point, which the bytewise UTF-8
  comparison of the pinned Arrow build implements exactly.
- Filters apply before sorting and paging. Sorting places nulls last
  whatever the direction, and the original row position is the final
  tie-breaker, so equivalent inputs order rows deterministically.
- The response identifies the source reference and a query hash that
  covers the source, the sort keys, and the filters; it excludes the
  offset and the limit. Rows are arrays in the returned column order,
  and ``int64`` and ``decimal`` cells travel as strings (section 5.1).
- A table with more columns than the bound answers an explicit
  column-selection requirement instead of choosing columns silently.
  The encoded byte bound may reduce the row count, but a page always
  carries at least one display row or a structured limit error; an
  endless empty page with ``hasMore`` true is impossible by
  construction, because ``hasMore`` is true only while a further row
  exists to name in ``nextOffset``.

The reader trusts only approved immutable artifact bytes addressed by
their content hash (SPEC.md section 15.1); it never follows a
project-provided path or symlink.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from melt.schema.canonical import model_to_wire, table_query_hash
from melt.schema.diagnostics import Diagnostic, DiagnosticCode
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits
from melt.schema.records import RECORD_SCHEMA_VERSION, PageResponse

ORDER_OPERATORS = ("lt", "lte", "gt", "gte")
"""The operators that order values (SPEC.md section 12.7)."""

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z"
)

_STRING_TYPES = frozenset({"string", "date", "timestamp"})
"""Column types whose wire cells and filter values are strings."""

_NUMERIC_TYPES = frozenset({"int64", "float64", "decimal"})
"""Column types whose filter values are numbers."""


class MeltReaderError(Exception):
    """Raised when a page request names a column the table lacks.

    Operator-type mismatches answer structured diagnostics instead, so
    the service can reject the request the way section 12.7 describes.
    """

    def __init__(self, diagnostic: Diagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class PageQuery:
    """One parsed page request (SPEC.md section 12.7)."""

    source: Mapping[str, Any]
    sort: "Sequence[Mapping[str, Any]]" = ()
    filters: "Sequence[Mapping[str, Any]]" = ()
    offset: int = 0
    limit: int = DEFAULT_LIMITS.table_page_rows


def _diagnostic(
    code: str,
    path: str,
    message: str,
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(code=code, path=path, message=message, hint=hint)


def _kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    return "number"


def _column_types(schema: Mapping[str, Any]) -> "dict[str, Mapping[str, Any]]":
    """Map the declared column identifiers to their declarations."""

    return {
        column["id"]: column for column in (schema.get("columns") or ())
    }


def _parquet_name(column: Mapping[str, Any]) -> str:
    """The Parquet field name one declared column was imported under."""

    return column.get("label") or column["id"]


def _check_columns(schema: Mapping[str, Any], limits: ServiceLimits) -> "None | Diagnostic":
    """Refuse a table wider than the page column bound (section 12.7)."""

    count = len(schema.get("columns") or ())
    if count > limits.table_page_columns:
        return _diagnostic(
            DiagnosticCode.LIMIT,
            "/payload/source",
            f"the table holds {count} columns, which exceeds the allowed"
            f" bound of {limits.table_page_columns}; v0.1 pages carry every"
            " declared column, so the reader cannot choose columns"
            " silently",
            hint="Select columns explicitly through a narrower source;"
            " column selection arrives with a later language version.",
        )
    return None


def _filter_type_error(
    index: int, column: Mapping[str, Any], item: Mapping[str, Any]
) -> "None | Diagnostic":
    """Validate one filter against its declared column type."""

    path = f"/payload/filters/{index}"
    operator = item["operator"]
    value = item.get("value")
    kind = _kind_of(value)
    type_name = column["type"]

    if operator == "contains":
        if type_name != "string":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/operator",
                f"contains accepts only string columns, but column"
                f" {item['column']!r} has type {type_name!r}",
            )
        return None
    if operator == "isNull":
        return None
    if operator == "eq":
        if type_name == "bool" and kind != "boolean":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"eq accepts a value of the column type, but column"
                f" {item['column']!r} has type {type_name!r} and the"
                f" filter carries a {kind} value",
            )
        if type_name in _STRING_TYPES and kind != "string":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"eq accepts a value of the column type, but column"
                f" {item['column']!r} has type {type_name!r} and the"
                f" filter carries a {kind} value",
            )
        if type_name in _NUMERIC_TYPES and kind != "number":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"eq accepts a value of the column type, but column"
                f" {item['column']!r} has type {type_name!r} and the"
                f" filter carries a {kind} value",
            )
    if operator in ORDER_OPERATORS:
        if type_name == "bool":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/operator",
                f"operator {operator!r} does not order boolean column"
                f" {item['column']!r}; use eq or isNull",
            )
        if type_name in _STRING_TYPES and kind != "string":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"operator {operator!r} orders {type_name} values as"
                f" strings, but the filter carries a {kind} value",
            )
        if type_name in _NUMERIC_TYPES and kind != "number":
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"operator {operator!r} orders {type_name} values as"
                f" numbers, but the filter carries a {kind} value",
            )
    if type_name == "int64" and kind == "number":
        if float(value) != int(value):
            return _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/value",
                f"column {item['column']!r} has type int64, whose values"
                f" are whole numbers, but the filter carries {value!r}",
            )
    if type_name == "date" and kind == "string" and not _DATE_PATTERN.match(value):
        return _diagnostic(
            DiagnosticCode.TYPE,
            f"{path}/value",
            f"column {item['column']!r} has type date, whose values use"
            f" YYYY-MM-DD, but the filter carries {value!r}",
        )
    if (
        type_name == "timestamp"
        and kind == "string"
        and not _TIMESTAMP_PATTERN.match(value)
    ):
        return _diagnostic(
            DiagnosticCode.TYPE,
            f"{path}/value",
            f"column {item['column']!r} has type timestamp, whose values"
            " use UTC RFC 3339 strings with a Z suffix, but the filter"
            f" carries {value!r}",
        )
    return None


def validate_query(
    schema: Mapping[str, Any], query: PageQuery, *, limits: ServiceLimits = DEFAULT_LIMITS
) -> "tuple[dict[str, Mapping[str, Any]], None | list[Diagnostic]]":
    """Validate a page query against one declared table schema.

    Returns the declared columns by identifier and either the sorted
    diagnostics or None. Column references resolve against the
    declaration; operators resolve against the declared types before
    any artifact byte is read (SPEC.md section 12.7).
    """

    errors: list[Diagnostic] = []
    columns = _column_types(schema)
    limit_error = _check_columns(schema, limits)
    if limit_error is not None:
        errors.append(limit_error)
    for index, key in enumerate(query.sort):
        if key["column"] not in columns:
            errors.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"/payload/sort/{index}/column",
                    f"sort key names column {key['column']!r}, which the"
                    " declared table schema does not carry",
                )
            )
    for index, item in enumerate(query.filters):
        column = columns.get(item["column"])
        if column is None:
            errors.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"/payload/filters/{index}/column",
                    f"filter names column {item['column']!r}, which the"
                    " declared table schema does not carry",
                )
            )
            continue
        type_error = _filter_type_error(index, column, item)
        if type_error is not None:
            errors.append(type_error)
    if errors:
        return columns, errors
    return columns, None


def _filter_scalar(column: Mapping[str, Any], item: Mapping[str, Any], field_type):
    """Build the typed comparison value of one filter.

    The value takes the Parquet type of the column, so the comparison
    never rewrites cells: a timestamp column stored without a zone
    compares against its naive UTC equivalent, and a zoned column
    compares against the same instant (SPEC.md section 5.1).
    """

    value = item["value"]
    type_name = column["type"]
    if type_name == "int64":
        return int(value)
    if type_name == "decimal":
        return Decimal(repr(value))
    if type_name == "date":
        return date.fromisoformat(value)
    if type_name == "timestamp":
        moment = _parse_timestamp(value)
        if getattr(field_type, "tz", None):
            return moment
        return moment.replace(tzinfo=None)
    return value


def _parse_timestamp(value: str) -> datetime:
    """Parse one UTC RFC 3339 string into an aware datetime."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:  # pragma: no cover - the pattern demands a zone
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _encode_cell(value: Any, type_name: str, column: str) -> Any:
    """Encode one raw cell into its wire shape (SPEC.md section 5.1)."""

    if value is None:
        return None
    if type_name == "int64":
        return str(value)
    if type_name == "decimal":
        return str(value)
    if type_name == "date":
        return value.isoformat()
    if type_name == "timestamp":
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        text = moment.strftime("%Y-%m-%dT%H:%M:%S")
        if moment.microsecond:
            text += f".{moment.microsecond:06d}".rstrip("0")
        return text + "Z"
    if type_name == "float64":
        if not math.isfinite(value):
            raise MeltReaderError(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"/payload/source",
                    f"column {column!r} carries the non-finite float"
                    f" {value!r}, which no table page can encode;"
                    " non-finite table floats are rejected in v0.1",
                )
            )
        return float(value)
    return value


def _apply_filters(table, columns: "dict[str, Mapping[str, Any]]", filters):
    """Return the table with every filter applied as one AND (12.7)."""

    import pyarrow.compute as pc

    for item in filters:
        column = columns[item["column"]]
        field = table.column(_parquet_name(column))
        operator = item["operator"]
        if operator == "isNull":
            mask = pc.is_null(field)
        elif operator == "contains":
            mask = pc.match_substring(field, item["value"])
        else:
            import pyarrow as pa

            scalar = pa.scalar(
                _filter_scalar(column, item, field.type), type=field.type
            )
            if operator == "eq":
                mask = pc.equal(field, scalar)
            elif operator == "lt":
                mask = pc.less(field, scalar)
            elif operator == "lte":
                mask = pc.less_equal(field, scalar)
            elif operator == "gt":
                mask = pc.greater(field, scalar)
            else:
                mask = pc.greater_equal(field, scalar)
        # A non-null comparison against a null cell is false: null
        # results of the comparison drop the row, and only isNull
        # selects it (SPEC.md section 12.7).
        table = table.filter(pc.fill_null(mask, False))
    return table


def _sort_indices(table, columns, sort) -> "Any":
    """Order rows by the sort keys, nulls last, position last (12.7)."""

    import pyarrow as pa
    import pyarrow.compute as pc

    if not sort:
        return None
    keys = [
        (
            _parquet_name(columns[key["column"]]),
            "descending" if key["direction"] == "desc" else "ascending",
        )
        for key in sort
    ]
    positions = pa.array(range(table.num_rows), type=pa.int64())
    keyed = table.append_column("__melt_position__", positions)
    keys.append(("__melt_position__", "ascending"))
    # The pinned Arrow build places nulls last for either direction by
    # default, which the row-position tie-breaker then orders fully.
    indices = pc.sort_indices(keyed, sort_keys=keys)
    return indices


def _row_bytes(row: "list[Any]") -> int:
    """The encoded byte cost of one response row."""

    import json

    return len(json.dumps(row, separators=(",", ":"), ensure_ascii=False))


def read_table_page(
    payload: bytes,
    schema: Mapping[str, Any],
    query: PageQuery,
    *,
    limits: ServiceLimits = DEFAULT_LIMITS,
) -> "tuple[dict, None] | tuple[None, list[Diagnostic]]":
    """Answer one page request over verified artifact bytes.

    Returns the wire page response, or the sorted diagnostics that
    reject the request. The declared schema is the one the service
    measured when it registered the artifact, so the reader trusts the
    column identifiers and types it carries.
    """

    columns, errors = validate_query(schema, query, limits=limits)
    if errors is not None:
        return None, errors

    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(payload))
    except Exception as error:
        return None, [
            _diagnostic(
                DiagnosticCode.ARTIFACT,
                "/payload/source",
                f"the stored artifact is no longer a readable Parquet"
                f" file: {error}",
            )
        ]
    try:
        table = _apply_filters(table, columns, query.filters)
        indices = _sort_indices(table, columns, query.sort)
        if indices is not None:
            table = table.take(indices)
    except MeltReaderError as error:
        return None, [error.diagnostic]

    total = table.num_rows
    start = min(int(query.offset), total)
    end = min(start + int(query.limit), total)
    window: "list[list[Any]]" = []
    try:
        window = _encode_window(table.slice(start, end - start), columns)
    except MeltReaderError as error:
        return None, [error.diagnostic]

    # The encoded byte bound may reduce the row count, but never below
    # one display row: a page that cannot carry a row answers a
    # structured limit error instead (SPEC.md section 12.7).
    kept = len(window)
    while (
        kept > 1
        and sum(_row_bytes(row) for row in window[:kept]) > limits.table_page_bytes
    ):
        kept -= 1
    if kept == 1 and _row_bytes(window[0]) > limits.table_page_bytes:
        return None, [
            _diagnostic(
                DiagnosticCode.LIMIT,
                "/payload/limit",
                "one row of this table exceeds the encoded page bound of"
                f" {limits.table_page_bytes} bytes, so no display row"
                " fits a page",
                hint="Widen the table-page-bytes service policy, or read"
                " the cell through bounded full-value retrieval.",
            )
        ]
    window = window[:kept]
    served_end = start + kept
    has_more = served_end < total
    response = PageResponse.model_validate(
        {
            "recordType": "pageResponse",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "source": dict(query.source),
            "queryHash": table_query_hash(query.source, query.sort, query.filters),
            "columns": tuple(column["id"] for column in schema["columns"]),
            "rows": [tuple(row) for row in window],
            "offset": start,
            "hasMore": has_more,
            "nextOffset": served_end if has_more else None,
        }
    )
    return model_to_wire(response), None


def _encode_window(
    window, columns: "dict[str, Mapping[str, Any]]"
) -> "list[list[Any]]":
    """Encode the cells of one sliced window into wire rows."""

    declared = _declared_order(columns, window)
    encoded = [
        [
            _encode_cell(value, column["type"], column["id"])
            for value in window.column(_parquet_name(column)).to_pylist()
        ]
        for column in declared
    ]
    return [
        [cells[position] for cells in encoded]
        for position in range(window.num_rows)
    ]


def _declared_order(
    columns: "dict[str, Mapping[str, Any]]", window
) -> "list[Mapping[str, Any]]":
    """Return the declared columns in schema order, verified on bytes.

    A stored artifact whose fields no longer match the measured
    declaration answers an artifact diagnostic: the saved schema and
    the bytes drifted apart, and the reader never guesses a mapping
    (SPEC.md sections 5.1 and 14).
    """

    ordered: "list[Mapping[str, Any]]" = []
    for column in columns.values():
        name = _parquet_name(column)
        try:
            window.column(name)
        except KeyError as error:
            raise MeltReaderError(
                _diagnostic(
                    DiagnosticCode.ARTIFACT,
                    "/payload/source",
                    f"the stored artifact carries no Parquet field"
                    f" {name!r} for declared column {column['id']!r};"
                    " the measured schema and the bytes disagree",
                )
            ) from error
        ordered.append(column)
    return ordered


__all__ = [
    "MeltReaderError",
    "ORDER_OPERATORS",
    "PageQuery",
    "read_table_page",
    "validate_query",
]
