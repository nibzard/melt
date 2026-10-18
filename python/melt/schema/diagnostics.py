"""Diagnostic codes and the shared diagnostic shape (SPEC.md section 11.3).

Every layer reports failures with the same shape: a code, a JSON Pointer
path, a message, optional related paths, and a hint. The compiler, the
schema models, and the service records all emit this shape. Messages
never contain secrets or full source contents.
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
        "layout.replace",
        "registration",
        "permissionGrant",
        "artifact",
        "parameterState",
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
        "TableSchema",
        "ImageSchema",
        "TextSchema",
    }
)
_UNION_HOLDER_FIELDS = frozenset(
    {"value", "event", "effect", "origin", "root", "first", "second", "schema"}
)
_UNION_HOLDER_LISTS = frozenset({"controls", "operations"})
_UNION_HOLDER_MAPS = frozenset({"projections", "sources"})


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
    message = str(error.get("msg", "validation failed"))
    # Pydantic prefixes custom ValueError messages with a fixed label.
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            message = message[len(prefix) :]
            break
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

