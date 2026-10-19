"""Diagnostic codes and the shared diagnostic shape (SPEC.md section 11.3).

Every layer reports failures with the same shape: a code, a JSON Pointer
path, a message, optional related paths, and a hint. The compiler, the
schema models, and the service records all emit this shape. Messages
never contain secrets or full source contents.

A field that fails a scalar union, such as `JsonScalar`, reports one
decisive diagnostic, not one per union branch. The same holds for a
scalar union at an integer list index, such as a `TableCell` of
`PageResponse.rows`, and under a map key, such as a captured value of
`RunManifest.parameters`. Validation entry points convert pydantic
errors through `diagnostics_from_pydantic_errors`, which collapses
that branch noise.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator


class DiagnosticCode:
    """The diagnostic codes that SPEC.md section 11.3 requires."""

    PARSE = "E_PARSE"
    VERSION = "E_VERSION"
    SCHEMA = "E_SCHEMA"
    LIMIT = "E_LIMIT"
    REFERENCE = "E_REFERENCE"
    TYPE = "E_TYPE"
    LAYOUT = "E_LAYOUT"
    OUTPUT_OWNER = "E_OUTPUT_OWNER"
    STATE_MIGRATION = "E_STATE_MIGRATION"
    REGISTRATION = "E_REGISTRATION"
    ARTIFACT = "E_ARTIFACT"
    CONFLICT = "E_CONFLICT"
    PERMISSION = "E_PERMISSION"
    REQUEST_REUSE = "E_REQUEST_REUSE"


DIAGNOSTIC_CODES = frozenset(
    value
    for name, value in vars(DiagnosticCode).items()
    if not name.startswith("_") and isinstance(value, str)
)

# Custom validators raise ValueError with this prefix to pick their own
# diagnostic code. The mapping below reads the prefix back out.
_CODE_PREFIX = re.compile(r"^\[(E_[A-Z_]+)\]\s*(.*)$", re.DOTALL)


def with_code(code: str, message: str) -> str:
    """Return a validator message that carries an explicit code."""
    return f"[{code}] {message}"


def split_code(message: str) -> tuple[str | None, str]:
    """Split a `[E_CODE] message` string into its code and message."""
    match = _CODE_PREFIX.match(message)
    if match is None:
        return None, message
    return match.group(1), match.group(2)


# Pydantic prefixes custom ValueError messages with a fixed label.
_PYDANTIC_MESSAGE_PREFIXES = ("Value error, ", "Assertion failed, ")


def _strip_message_prefix(message: str) -> str:
    """Remove the label pydantic puts before a validator message."""
    for prefix in _PYDANTIC_MESSAGE_PREFIXES:
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def json_pointer(path: object) -> str:
    """Build a JSON Pointer from a pydantic error location."""
    parts: list[str] = []
    for part in path if isinstance(path, (tuple, list)) else (path,):
        if part is None:
            continue
        text = part if isinstance(part, str) else str(part)
        parts.append(text.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(parts)


class Diagnostic(BaseModel):
    """One diagnostic in the shape that SPEC.md section 11.3 defines."""

    model_config = ConfigDict(extra="forbid")

    code: StrictStr
    path: StrictStr
    message: StrictStr
    relatedPaths: tuple[StrictStr, ...] = ()
    hint: StrictStr = ""

    @field_validator("code")
    @classmethod
    def _code_is_known(cls, value: str) -> str:
        from melt.schema.diagnostics import DIAGNOSTIC_CODES

        if value not in DIAGNOSTIC_CODES:
            raise ValueError(f"unknown diagnostic code {value!r}")
        return value


def diagnostic_sort_key(diagnostic: Diagnostic) -> tuple[str, str]:
    """Sort by path, then code, as SPEC.md section 11.3 requires."""
    return diagnostic.path, diagnostic.code


def sorted_diagnostics(
    diagnostics: "list[Diagnostic] | tuple[Diagnostic, ...]",
) -> list[Diagnostic]:
    """Return diagnostics ordered by path, then code."""
    return sorted(diagnostics, key=diagnostic_sort_key)


class MeltSchemaError(Exception):
    """Base error carrying one or more diagnostics.

    Validation entry points raise this instead of leaking pydantic
    errors. The diagnostics are sorted by path, then code.
    """

    def __init__(self, diagnostics: "list[Diagnostic] | tuple[Diagnostic, ...]"):
        self.diagnostics: tuple[Diagnostic, ...] = tuple(
            sorted_diagnostics(list(diagnostics))
        )
        summary = "; ".join(
            f"{diagnostic.code} at {diagnostic.path}: {diagnostic.message}"
            for diagnostic in self.diagnostics
        )
        super().__init__(summary or "schema validation failed")

    def __str__(self) -> str:
        return super().__str__()


# Map a pydantic error type onto a diagnostic code. Types that are not
# listed fall back to E_SCHEMA, or to E_TYPE for value shape mismatches.
_DEFAULT_CODE_BY_TYPE = {
    "extra_forbidden": DiagnosticCode.SCHEMA,
    "missing": DiagnosticCode.SCHEMA,
    "literal_type": DiagnosticCode.TYPE,
    "literal_error": DiagnosticCode.TYPE,
    "enum": DiagnosticCode.TYPE,
    "string_type": DiagnosticCode.TYPE,
    "int_type": DiagnosticCode.TYPE,
    "float_type": DiagnosticCode.TYPE,
    "bool_type": DiagnosticCode.TYPE,
    "list_type": DiagnosticCode.TYPE,
    "dict_type": DiagnosticCode.TYPE,
    "model_type": DiagnosticCode.TYPE,
    "model_attributes_type": DiagnosticCode.TYPE,
    "union_tag_invalid": DiagnosticCode.TYPE,
    "union_tag_not_found": DiagnosticCode.TYPE,
    "string_pattern_mismatch": DiagnosticCode.SCHEMA,
    "string_too_short": DiagnosticCode.SCHEMA,
    "string_too_long": DiagnosticCode.SCHEMA,
    "greater_than": DiagnosticCode.SCHEMA,
    "greater_than_equal": DiagnosticCode.SCHEMA,
    "less_than": DiagnosticCode.SCHEMA,
    "less_than_equal": DiagnosticCode.SCHEMA,
    "multiple_of": DiagnosticCode.SCHEMA,
    "too_long": DiagnosticCode.SCHEMA,
    "too_short": DiagnosticCode.SCHEMA,
    "value_error": DiagnosticCode.SCHEMA,
    "finite_number": DiagnosticCode.TYPE,
    "string_unicode": DiagnosticCode.PARSE,
}

# Field names that name artifacts carry the artifact code on pattern
# failures, so hash formats report E_ARTIFACT instead of E_SCHEMA.
_ARTIFACT_FIELD_NAMES = frozenset(
    {
        "artifact",
        "scriptArtifact",
        "requestPayloadHash",
        "queryHash",
    }
)

# Fields whose value selects a version. A wrong value reports E_VERSION.
_VERSION_FIELD_NAMES = frozenset({"language", "protocolVersion", "schemaVersion"})


# Pydantic inserts the matched branch tag, or the branch class name,
# into the error location of union members. These markers are not part
# of the document, so the mapping removes them. The marker only ever
# appears directly under a field that holds a tagged union, under a
# list element of `controls` or `operations`, under a map value of
# `projections` or `sources`, or at the root of a record or response.
# Map keys never sit in those positions in a real error location, so
# an entity that happens to be named like a tag keeps its path.
_UNION_MARKER_ROOT = frozenset(
    {
        "run",
        "registration",
        "permissionGrant",
        "artifact",
        "parameterState",
        "referenceState",
        "commandReceipt",
        "snapshot",
        "changeNotification",
        "pageRequest",
        "pageResponse",
        "OkResponse",
        "RejectedResponse",
    }
)
_UNION_MARKER_TAGS = frozenset(
    {
        "table",
        "image",
        "text",
        "import",
        "run",
        "number",
        "toggle",
        "select",
        "button",
        "plot",
        "document",
        "pane",
        "split",
        "parameter.changed",
        "control.activate",
        "action.invoke",
        "entity.create",
        "entity.replace",
        "entity.delete",
        "parameter.set",
        "state.set",
        "layout.replace",
        "registration",
        "permissionGrant",
        "artifact",
        "parameterState",
        "referenceState",
        "commandReceipt",
        "snapshot",
        "changeNotification",
        "pageRequest",
        "pageResponse",
        "Parameter",
        "Action",
        "TableProjection",
        "PlotProjection",
        "DocumentProjection",
        "Binding",
        "SemanticCollection",
        "SemanticState",
        "TableSchema",
        "ImageSchema",
        "TextSchema",
    }
)
_UNION_HOLDER_FIELDS = frozenset(
    {"value", "event", "effect", "origin", "root", "first", "second", "schema"}
)
_UNION_HOLDER_LISTS = frozenset({"controls", "operations"})
_UNION_HOLDER_MAPS = frozenset({"projections", "sources", "collections", "state"})

# Scalar unions, such as `JsonScalar`, mark the failing branch with the
# branch type name, or with the wrapping function of an annotated
# branch, as in `function-before[_finite_number(), float]`. No field or
# entity ID can carry those names or brackets, so the same stripping
# removes them under the fields that hold a scalar union.
_SCALAR_BRANCH_MARKER = re.compile(
    r"(?:bool|int|float|str|NoneType|function-(?:before|after|wrap)\[.+\])\Z"
)
_SCALAR_UNION_HOLDER_FIELDS = frozenset(
    {"default", "value", "minimum", "maximum"}
)
# A scalar union also sits under integer list indices, as the cells of
# `PageResponse.rows` do. The marker then follows the indices, not a
# named field.
_SCALAR_UNION_HOLDER_LISTS = frozenset({"rows"})
# A scalar union also sits under map keys, as the captured values of
# `RunManifest.parameters` do. A key is an arbitrary entity ID, so it
# cannot name the holder; the enclosing map field does.
_SCALAR_UNION_HOLDER_MAPS = frozenset({"parameters"})

# Error types that report one failing scalar-union branch. A type
# mismatch names the branch kind, and `value_error` carries the message
# of the branch's own validator, as `_finite_number` does. Other types,
# such as `extra_forbidden` under an entity that happens to be named
# `default`, never describe a scalar branch, so they never group.
_SCALAR_BRANCH_ERROR_TYPES = frozenset(
    {
        "bool_type",
        "int_type",
        "float_type",
        "string_type",
        "none_type",
        "value_error",
    }
)


def _is_scalar_branch_marker(location: tuple, index: int) -> bool:
    """Report whether `location[index]` marks a scalar-union branch.

    The marker follows its holder field directly, as `value` does,
    follows the integer indices of a list the holder field owns, as a
    `TableCell` in `rows` does, or follows a key of a map the holder
    field owns, as a captured value in `parameters` does. The walk
    back to the holder crosses integer indices only. A map key is an
    arbitrary entity ID, so the map case reads the holder name from
    the segment before the key. A discriminated-union tag in between,
    as under a projection named `default`, stops the walk and keeps
    the segment, so a field named like a marker keeps its path.
    """

    part = location[index]
    if not isinstance(part, str) or not _SCALAR_BRANCH_MARKER.match(part):
        return False
    holder = index - 1
    while holder >= 0 and isinstance(location[holder], int):
        holder -= 1
    if holder < 0:
        return False
    name = location[holder]
    if name in _SCALAR_UNION_HOLDER_FIELDS or name in _SCALAR_UNION_HOLDER_LISTS:
        return True
    # The holder segment is a map key when the segment before it names
    # a map whose values are a scalar union. A key that happens to
    # match a holder-field name already returned above.
    return (
        holder >= 1
        and isinstance(location[holder - 1], str)
        and location[holder - 1] in _SCALAR_UNION_HOLDER_MAPS
    )


def _strip_union_markers(location: tuple) -> tuple:
    """Remove inserted union branch markers from an error location."""

    kept: list = []
    for index, part in enumerate(location):
        if isinstance(part, str):
            if index == 0 and part in _UNION_MARKER_ROOT:
                continue
            if part in _UNION_MARKER_TAGS:
                previous = location[index - 1] if index else None
                if previous in _UNION_HOLDER_FIELDS:
                    continue
                if (
                    isinstance(previous, int)
                    and index >= 2
                    and location[index - 2] in _UNION_HOLDER_LISTS
                ):
                    continue
                if index >= 2 and location[index - 2] in _UNION_HOLDER_MAPS:
                    continue
            if _is_scalar_branch_marker(location, index):
                continue
        kept.append(part)
    return tuple(kept)


def diagnostic_from_pydantic_error(error: dict, default_code: str) -> Diagnostic:
    """Convert one `ValidationError.errors()` entry into a Diagnostic."""
    location = tuple(error.get("loc") or ())
    # Pydantic marks dictionary key failures with a trailing `[key]`
    # segment. The key itself is already in the location.
    if location and location[-1] == "[key]":
        location = location[:-1]
    location = _strip_union_markers(location)
    field_name = location[-1] if location and isinstance(location[-1], str) else ""
    message = _strip_message_prefix(str(error.get("msg", "validation failed")))
    error_type = error.get("type", "")

    code: str | None = None
    if error_type == "value_error":
        code, message = split_code(message)
    if code is None:
        code = _DEFAULT_CODE_BY_TYPE.get(error_type, default_code)
    if error_type == "string_pattern_mismatch" and field_name in _ARTIFACT_FIELD_NAMES:
        code = DiagnosticCode.ARTIFACT
    if (
        error_type in ("literal_type", "literal_error", "enum")
        and field_name in _VERSION_FIELD_NAMES
    ):
        code = DiagnosticCode.VERSION

    context = error.get("ctx") or {}
    hint = ""
    if error_type == "extra_forbidden":
        hint = "Remove the field. The model accepts no unknown fields."
    elif error_type == "missing":
        hint = "Add the required field."
    return Diagnostic(
        code=code,
        path=json_pointer(location),
        message=message,
        relatedPaths=(),
        hint=hint,
    )


def _scalar_branch_group_key(error: dict) -> "tuple | None":
    """Return the shared location of one scalar-union branch error.

    A scalar union, such as `JsonScalar` or a `TableCell`, marks the
    failing branch in the final location segment, and the errors of
    every branch describe the same rejected value. Errors whose final
    segment is such a marker, held by a scalar-union field, list
    position, or map key, share the location without the marker as
    their group key. Every other error returns `None` and never
    groups.
    """
    location = tuple(error.get("loc") or ())
    if len(location) < 2:
        return None
    if error.get("type") not in _SCALAR_BRANCH_ERROR_TYPES:
        return None
    if not _is_scalar_branch_marker(location, len(location) - 1):
        return None
    return location[:-1]


def _carries_explicit_code(error: dict) -> bool:
    """Report whether a branch validator named its own diagnostic."""
    if error.get("type") != "value_error":
        return False
    code, _ = split_code(_strip_message_prefix(str(error.get("msg", ""))))
    return code is not None


def diagnostics_from_pydantic_errors(
    errors: "list[dict] | tuple[dict, ...]",
    default_code: str,
) -> list[Diagnostic]:
    """Convert pydantic error entries into one diagnostic per field.

    Pydantic reports one error per branch when a scalar union such as
    `JsonScalar` rejects a value, so one bad field at
    `/parameters/p/default`, one bad cell at `/rows/0/1`, or one bad
    captured value at `/parameters/threshold` yields three entries:
    the decisive safe-range message plus one type mismatch per
    sibling branch. The branch errors of one location collapse into
    their decisive member, the branch whose validator names the actual
    problem. Every other error converts unchanged.
    """
    entries = list(errors)
    members: dict[tuple, list[int]] = {}
    for index, error in enumerate(entries):
        key = _scalar_branch_group_key(error)
        if key is not None:
            members.setdefault(key, []).append(index)
    dropped: set[int] = set()
    for indices in members.values():
        if len(indices) < 2:
            continue
        decisive = next(
            (index for index in indices if _carries_explicit_code(entries[index])),
            indices[0],
        )
        dropped.update(index for index in indices if index != decisive)
    return [
        diagnostic_from_pydantic_error(error, default_code)
        for index, error in enumerate(entries)
        if index not in dropped
    ]

