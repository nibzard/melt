"""Canonical document models for `melt/v0.1` (SPEC.md sections 4 to 10).

The models reject unknown fields, non-finite numbers, unsafe integers,
and invalid entity identifiers at the field level. Checks that span
several fields or several collections run in a separate semantic pass,
`document_semantic_diagnostics`, so every failure keeps a precise JSON
Pointer path and a diagnostic code from SPEC.md section 11.3.

Use `parse_document` for strict parsing. It reads JSON through the
strict reader in `melt.schema.canonical`, validates the structure, and
runs the semantic pass.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Iterable, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from melt.schema.canonical import parse_json
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostics_from_pydantic_errors,
    json_pointer,
    sorted_diagnostics,
    split_code,
    with_code,
)
from melt.schema.limits import DEFAULT_LIMITS

LANGUAGE_ID = "melt/v0.1"


class MeltModel(BaseModel):
    """Base model: every object rejects unknown fields.

    `populate_by_name` allows construction with Python names for fields
    that carry a wire alias, such as `schema`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Wire-level scalar types (SPEC.md sections 3 and 4.2)
# ---------------------------------------------------------------------------

SAFE_INTEGER_MAX = 2**53 - 1
"""Integers on the wire stay within -(2^53 - 1) through 2^53 - 1."""


def _safe_integer(value: int) -> int:
    if value < -SAFE_INTEGER_MAX or value > SAFE_INTEGER_MAX:
        raise ValueError(
            with_code(
                DiagnosticCode.TYPE,
                f"integer {value} is outside the safe integer range"
                f" [-(2^53 - 1), 2^53 - 1]",
            )
        )
    return value


SafeInt = Annotated[int, Strict(), AfterValidator(_safe_integer)]
PositiveSafeInt = Annotated[SafeInt, Field(gt=0)]
NonNegativeSafeInt = Annotated[SafeInt, Field(ge=0)]


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "expected a JSON number, no coercion")
        )
    if isinstance(value, int) and not -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX:
        # Converting first would round 2^53 + 1 to 2^53 silently, so the
        # wire rule of SPEC.md section 4.2 is checked before the cast.
        raise ValueError(
            with_code(
                DiagnosticCode.TYPE,
                f"integer {value} is outside the safe integer range"
                f" [-(2^53 - 1), 2^53 - 1]",
            )
        )
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "number is not finite")
        )
    return result


FiniteNumber = Annotated[float, BeforeValidator(_finite_number)]
"""A finite JSON number. Integers outside the safe range are rejected
before the cast to `float` can round them (SPEC.md section 4.2)."""

JsonScalar = Union[StrictBool, FiniteNumber, StrictStr]
"""A JSON scalar value. The semantic pass checks it against its type."""

EntityId = Annotated[
    StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}\z")
]
"""Entity identifiers match `[a-z][a-z0-9_-]{0,63}` (SPEC.md section 3)."""

Title = Annotated[StrictStr, StringConstraints(min_length=1, max_length=256)]
Label = Annotated[StrictStr, StringConstraints(min_length=1, max_length=256)]
OpaqueId = Annotated[StrictStr, StringConstraints(min_length=1, max_length=128)]
NonEmptyStr = Annotated[StrictStr, StringConstraints(min_length=1)]

ArtifactHash = Annotated[
    StrictStr, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}\z")
]
"""Artifact hashes are lowercase SHA-256 hex with the `sha256:` prefix."""

VersionKey = Annotated[StrictStr, StringConstraints(pattern=r"^[1-9][0-9]*\z")]
"""Source version keys are canonical decimal strings of positive integers."""

_TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z\z"


def _utc_timestamp(value: str) -> str:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            datetime.strptime(value, fmt)
            return value
        except ValueError:
            continue
    raise ValueError(
        with_code(
            DiagnosticCode.SCHEMA,
            "timestamp must be a UTC RFC 3339 string with a Z suffix",
        )
    )


Timestamp = Annotated[StrictStr, StringConstraints(pattern=_TIMESTAMP_PATTERN), AfterValidator(_utc_timestamp)]

_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\z"


def _calendar_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            with_code(DiagnosticCode.TYPE, f"invalid date: {error}")
        ) from error
    return value


DateValue = Annotated[StrictStr, StringConstraints(pattern=_DATE_PATTERN), AfterValidator(_calendar_date)]
"""Table `date` values use `YYYY-MM-DD` (SPEC.md section 5.1)."""


def _rfc3339_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "timestamp requires a Z suffix")
        )
    return value


TimestampValue = Annotated[Timestamp, AfterValidator(_rfc3339_utc)]
"""Table `timestamp` values are UTC RFC 3339 strings (section 5.1)."""


# ---------------------------------------------------------------------------
# Sources and artifacts (SPEC.md section 5)
# ---------------------------------------------------------------------------


class SourceReference(MeltModel):
    """A source reference is exactly a source ID and a version."""

    sourceId: EntityId
    version: PositiveSafeInt


class TableColumn(MeltModel):
    id: EntityId
    type: Literal[
        "string", "bool", "int64", "float64", "decimal", "date", "timestamp"
    ]
    nullable: StrictBool
    label: Label | None = None
    precision: PositiveSafeInt | None = None
    scale: NonNegativeSafeInt | None = None

    @model_validator(mode="after")
    def _decimal_fields(self) -> "TableColumn":
        if self.type == "decimal":
            if self.precision is None or self.scale is None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        "a decimal column requires precision and scale",
                    )
                )
            if self.precision > 38:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA, "decimal precision must be 1 to 38"
                    )
                )
            if self.scale > self.precision:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA, "decimal scale must not exceed precision"
                    )
                )
        elif self.precision is not None or self.scale is not None:
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    "only decimal columns carry precision and scale",
                )
            )
        return self


class TableSchema(MeltModel):
    columns: tuple[TableColumn, ...] = Field(min_length=1)
    rowKey: EntityId | None = None

    @model_validator(mode="after")
    def _unique_columns(self) -> "TableSchema":
        seen: set[str] = set()
        for column in self.columns:
            if column.id in seen:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"duplicate column id {column.id!r}",
                    )
                )
            seen.add(column.id)
        if self.rowKey is not None and self.rowKey not in seen:
            raise ValueError(
                with_code(
                    DiagnosticCode.REFERENCE,
                    f"rowKey {self.rowKey!r} is not a column in this schema",
                )
            )
        return self


class ImageSchema(MeltModel):
    width: PositiveSafeInt
    height: PositiveSafeInt

    @model_validator(mode="after")
    def _png_limits(self) -> "ImageSchema":
        limits = DEFAULT_LIMITS
        if self.width > limits.png_max_axis or self.height > limits.png_max_axis:
            raise ValueError(
                with_code(
                    DiagnosticCode.LIMIT,
                    "PNG axis length exceeds the allowed bound of"
                    f" {limits.png_max_axis}",
                )
            )
        if self.width * self.height > limits.png_max_total_pixels:
            raise ValueError(
                with_code(
                    DiagnosticCode.LIMIT,
                    "PNG total pixels exceed the allowed bound of"
                    f" {limits.png_max_total_pixels}",
                )
            )
        return self


class TextSchema(MeltModel):
    encoding: Literal["utf-8"]


def _first_error_message(error: ValidationError) -> tuple[str, str]:
    """Return the code marker and text of the first nested error."""

    item = error.errors()[0]
    message = str(item.get("msg", "validation failed"))
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            message = message[len(prefix) :]
            break
    code, text = split_code(message)
    location = json_pointer(item.get("loc") or ())
    return (code or DiagnosticCode.SCHEMA), f"{location}: {text}"


def _dispatch_source_schema(value: object):
    """Validate a source schema by its keys.

    Source schemas carry no discriminator field of their own; the
    source kind selects the shape. Dispatching on keys keeps the error
    paths free of union branch noise.
    """

    if not isinstance(value, dict):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "schema must be an object")
        )
    keys = set(value)
    branch: type[BaseModel]
    if keys & {"columns", "rowKey"}:
        branch = TableSchema
    elif keys & {"width", "height"}:
        branch = ImageSchema
    elif "encoding" in keys:
        branch = TextSchema
    else:
        raise ValueError(
            with_code(
                DiagnosticCode.SCHEMA,
                "schema matches no table, image, or text shape",
            )
        )
    try:
        return branch.model_validate(value)
    except ValidationError as error:
        code, message = _first_error_message(error)
        raise ValueError(with_code(code, message)) from error


SourceSchema = Annotated[
    Union[TableSchema, ImageSchema, TextSchema],
    BeforeValidator(_dispatch_source_schema),
]


class ImportOrigin(MeltModel):
    kind: Literal["import"]
    label: Label


class RunOrigin(MeltModel):
    kind: Literal["run"]
    runId: OpaqueId
    output: EntityId


SourceOrigin = Annotated[
    Union[ImportOrigin, RunOrigin], Field(discriminator="kind")
]


class SourceVersion(MeltModel):
    artifact: ArtifactHash
    mediaType: StrictStr
    byteLength: NonNegativeSafeInt
    # The wire name is `schema`. The Python attribute carries a trailing
    # underscore so it does not shadow `BaseModel.schema`.
    schema_: SourceSchema = Field(alias="schema")
    origin: SourceOrigin

    @property
    def schema(self) -> SourceSchema:
        """The source schema, under its wire name."""
        return self.schema_


class Source(MeltModel):
    kind: Literal["table", "image", "text"]
    currentVersion: PositiveSafeInt
    versions: dict[VersionKey, SourceVersion] = Field(min_length=1)


SOURCE_KIND_MEDIA_TYPES: dict[str, frozenset[str]] = {
    "table": frozenset({"application/vnd.apache.parquet"}),
    "image": frozenset({"image/png"}),
    "text": frozenset({"text/plain", "text/markdown"}),
}
"""The media types that each source kind accepts (SPEC.md section 5.1)."""


# ---------------------------------------------------------------------------
# Parameters and controls (SPEC.md section 6)
# ---------------------------------------------------------------------------


class ParameterConstraints(MeltModel):
    """Constraints for every parameter type in one shape.

    The semantic pass checks that only the fields of the declared type
    are present, and that the default satisfies them.
    """

    minimum: FiniteNumber | None = None
    maximum: FiniteNumber | None = None
    maxLength: PositiveSafeInt | None = None
    enum: tuple[StrictStr, ...] | None = None


class Parameter(MeltModel):
    type: Literal["string", "number", "integer", "boolean"]
    default: JsonScalar | None
    nullable: StrictBool
    constraints: ParameterConstraints


class SelectOption(MeltModel):
    value: StrictStr
    label: Label


class _ControlBase(MeltModel):
    id: EntityId
    label: Label


class NumberControl(_ControlBase):
    kind: Literal["number"]
    parameter: EntityId


class TextControl(_ControlBase):
    kind: Literal["text"]
    parameter: EntityId


class ToggleControl(_ControlBase):
    kind: Literal["toggle"]
    parameter: EntityId


class SelectControl(_ControlBase):
    kind: Literal["select"]
    parameter: EntityId
    options: tuple[SelectOption, ...] = Field(min_length=1)


class ButtonControl(_ControlBase):
    kind: Literal["button"]


Control = Annotated[
    Union[NumberControl, TextControl, ToggleControl, SelectControl, ButtonControl],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Actions (SPEC.md section 7)
# ---------------------------------------------------------------------------


class ActionImplementation(MeltModel):
    registrationId: OpaqueId


class ActionParameterMapping(MeltModel):
    parameter: EntityId


class Action(MeltModel):
    implementation: ActionImplementation
    parameters: dict[EntityId, ActionParameterMapping]
    inputs: dict[EntityId, SourceReference]
    outputs: dict[EntityId, Literal["table", "image", "text"]]
    outputGroup: EntityId
    targets: dict[EntityId, EntityId]


# ---------------------------------------------------------------------------
# Projections (SPEC.md section 8)
# ---------------------------------------------------------------------------


class TableConfig(MeltModel):
    columns: tuple[EntityId, ...] | None
    selection: Literal["none", "single"]

    @field_validator("columns")
    @classmethod
    def _distinct_columns(cls, value):
        if value is None:
            return value
        if len(set(value)) != len(value):
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA, "table columns must be distinct"
                )
            )
        return value


class PlotConfig(MeltModel):
    fit: Literal["contain"]


class DocumentConfig(MeltModel):
    format: Literal["plain", "markdown"]


class _ProjectionBase(MeltModel):
    title: Title
    source: SourceReference | None
    controls: tuple[Control, ...]


class TableProjection(_ProjectionBase):
    kind: Literal["table"]
    config: TableConfig


class PlotProjection(_ProjectionBase):
    kind: Literal["plot"]
    config: PlotConfig


class DocumentProjection(_ProjectionBase):
    kind: Literal["document"]
    config: DocumentConfig


Projection = Annotated[
    Union[TableProjection, PlotProjection, DocumentProjection],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Events and bindings (SPEC.md section 9)
# ---------------------------------------------------------------------------


class ParameterChangedEvent(MeltModel):
    kind: Literal["parameter.changed"]
    parameter: EntityId


class ControlActivateEvent(MeltModel):
    kind: Literal["control.activate"]
    projection: EntityId
    control: EntityId


BindingEvent = Annotated[
    Union[ParameterChangedEvent, ControlActivateEvent],
    Field(discriminator="kind"),
]


class ActionInvokeEffect(MeltModel):
    kind: Literal["action.invoke"]
    action: EntityId


BindingEffect = ActionInvokeEffect
"""Only `action.invoke` is supported as an effect in v0.1."""


class Binding(MeltModel):
    event: BindingEvent
    effect: BindingEffect
    debounceMs: NonNegativeSafeInt = Field(le=5000)


# ---------------------------------------------------------------------------
# Layout (SPEC.md section 10)
# ---------------------------------------------------------------------------


def _split_ratio(value: float) -> float:
    if not 0.1 <= value <= 0.9:
        raise ValueError(
            with_code(
                DiagnosticCode.LAYOUT, "split ratio must be 0.1 through 0.9"
            )
        )
    return value


SplitRatio = Annotated[FiniteNumber, AfterValidator(_split_ratio)]


class PaneNode(MeltModel):
    kind: Literal["pane"]
    projection: EntityId


class SplitNode(MeltModel):
    kind: Literal["split"]
    axis: Literal["horizontal", "vertical"]
    ratio: SplitRatio
    first: "LayoutNode"
    second: "LayoutNode"


LayoutNode = Annotated[Union[PaneNode, SplitNode], Field(discriminator="kind")]


class Layout(MeltModel):
    root: LayoutNode | None
    hidden: tuple[EntityId, ...]


SplitNode.model_rebuild()


# ---------------------------------------------------------------------------
# Document (SPEC.md section 4)
# ---------------------------------------------------------------------------


class Document(MeltModel):
    """The canonical document (SPEC.md section 4.1).

    Every field is required. Collections may be empty. The document
    holds definitions and current published source references only.
    """

    language: Literal[LANGUAGE_ID]
    workspaceId: EntityId
    title: Title
    sources: dict[EntityId, Source]
    parameters: dict[EntityId, Parameter]
    actions: dict[EntityId, Action]
    projections: dict[EntityId, Projection]
    bindings: dict[EntityId, Binding]
    layout: Layout


# ---------------------------------------------------------------------------
# Semantic pass
# ---------------------------------------------------------------------------


def _diagnostic(
    code: str,
    path: str,
    message: str,
    related_paths: "Iterable[str]" = (),
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        path=path,
        message=message,
        relatedPaths=tuple(related_paths),
        hint=hint,
    )


def check_limits(document: Document) -> list[Diagnostic]:
    """Enforce the collection limits of SPEC.md section 15.2."""
    limits = DEFAULT_LIMITS
    checks = (
        ("parameters", limits.max_parameters),
        ("actions", limits.max_actions),
        ("projections", limits.max_projections),
        ("bindings", limits.max_bindings),
    )
    diagnostics: list[Diagnostic] = []
    for collection, bound in checks:
        count = len(getattr(document, collection))
        if count > bound:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LIMIT,
                    f"/{collection}",
                    f"{collection} count {count} exceeds the allowed"
                    f" bound of {bound}",
                )
            )
    return diagnostics


def check_parameter(parameter_id: str, parameter: Parameter) -> list[Diagnostic]:
    """Check one parameter definition against its own type and constraints."""
    path = f"/parameters/{parameter_id}"
    constraints = parameter.constraints
    diagnostics: list[Diagnostic] = []

    allowed = {
        "number": {"minimum", "maximum"},
        "integer": {"minimum", "maximum"},
        "string": {"maxLength", "enum"},
        "boolean": set(),
    }[parameter.type]
    present = {
        name
        for name in ("minimum", "maximum", "maxLength", "enum")
        if getattr(constraints, name) is not None
    }
    unexpected = sorted(present - allowed)
    if unexpected:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.SCHEMA,
                f"{path}/constraints",
                f"constraints for type {parameter.type!r} cannot carry"
                f" {', '.join(unexpected)}",
            )
        )

    if parameter.type in ("number", "integer"):
        minimum, maximum = constraints.minimum, constraints.maximum
        if minimum is not None and parameter.type == "integer":
            if not float(minimum).is_integer():
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/constraints/minimum",
                        "integer constraints must hold integers",
                    )
                )
        if maximum is not None and parameter.type == "integer":
            if not float(maximum).is_integer():
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/constraints/maximum",
                        "integer constraints must hold integers",
                    )
                )
        if minimum is not None and maximum is not None and minimum > maximum:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"{path}/constraints",
                    "minimum must not exceed maximum",
                )
            )
    if parameter.type == "string":
        if constraints.enum is not None:
            if len(constraints.enum) == 0:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.SCHEMA,
                        f"{path}/constraints/enum",
                        "enum must hold at least one value",
                    )
                )
            elif len(set(constraints.enum)) != len(constraints.enum):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.SCHEMA,
                        f"{path}/constraints/enum",
                        "enum values must be unique",
                    )
                )

    default = parameter.default
    if default is None:
        if not parameter.nullable:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/default",
                    "null default requires nullable to be true",
                )
            )
        return diagnostics

    type_matches = {
        "boolean": lambda value: isinstance(value, bool),
        "number": lambda value: isinstance(value, float),
        "integer": lambda value: isinstance(value, float) and value.is_integer(),
        "string": lambda value: isinstance(value, str),
    }[parameter.type]
    if not type_matches(default):
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/default",
                f"default {default!r} does not satisfy type"
                f" {parameter.type!r}",
            )
        )
        return diagnostics

    if parameter.type in ("number", "integer"):
        if constraints.minimum is not None and default < constraints.minimum:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/default",
                    f"default {default!r} is below the minimum"
                    f" {constraints.minimum!r}",
                )
            )
        if constraints.maximum is not None and default > constraints.maximum:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/default",
                    f"default {default!r} is above the maximum"
                    f" {constraints.maximum!r}",
                )
            )
    if parameter.type == "string":
        if (
            constraints.maxLength is not None
            and len(default) > constraints.maxLength
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/default",
                    f"default is longer than maxLength"
                    f" {constraints.maxLength}",
                )
            )
        if constraints.enum is not None and default not in constraints.enum:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/default",
                    "default is not one of the enum values",
                )
            )
    return diagnostics


def check_source(source_id: str, source: Source) -> list[Diagnostic]:
    """Check one source against its own versions.

    A source kind outside the v0.1 table answers nothing here: the
    extension language checks its own kinds in
    :mod:`melt.schema.semantic`, and a `melt/v0.1` document never
    carries one.
    """
    path = f"/sources/{source_id}"
    diagnostics: list[Diagnostic] = []
    if source.kind not in SOURCE_KIND_MEDIA_TYPES:
        return diagnostics
    if str(source.currentVersion) not in source.versions:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.REFERENCE,
                f"{path}/currentVersion",
                "currentVersion does not refer to an included version",
            )
        )
    allowed_media = SOURCE_KIND_MEDIA_TYPES[source.kind]
    schema_type = {
        "table": TableSchema,
        "image": ImageSchema,
        "text": TextSchema,
    }[source.kind]
    for version_key, version in source.versions.items():
        version_path = f"{path}/versions/{version_key}"
        if version.mediaType not in allowed_media:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{version_path}/mediaType",
                    f"source kind {source.kind!r} does not accept media type"
                    f" {version.mediaType!r}",
                )
            )
        if not isinstance(version.schema, schema_type):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{version_path}/schema",
                    f"source kind {source.kind!r} requires a"
                    f" {source.kind} schema",
                )
            )
    return diagnostics


def check_controls(
    projection_id: str,
    projection: "_ProjectionBase",
    parameters: dict,
) -> list[Diagnostic]:
    """Check control identifiers and parameter compatibility."""
    path = f"/projections/{projection_id}/controls"
    diagnostics: list[Diagnostic] = []
    if len(projection.controls) > DEFAULT_LIMITS.max_controls_per_projection:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.LIMIT,
                path,
                "controls per projection exceed the allowed bound of"
                f" {DEFAULT_LIMITS.max_controls_per_projection}",
            )
        )
    seen: set[str] = set()
    for index, control in enumerate(projection.controls):
        control_path = f"{path}/{index}"
        if control.id in seen:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"{control_path}/id",
                    f"duplicate control id {control.id!r} in this projection",
                )
            )
        seen.add(control.id)
        if isinstance(control, ButtonControl):
            continue
        parameter = parameters.get(control.parameter)
        if parameter is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{control_path}/parameter",
                    f"control references unknown parameter"
                    f" {control.parameter!r}",
                )
            )
            continue
        compatible = {
            NumberControl: parameter.type in ("number", "integer"),
            TextControl: parameter.type == "string",
            ToggleControl: parameter.type == "boolean"
            and not parameter.nullable,
            SelectControl: parameter.type == "string"
            and parameter.constraints.enum is not None
            and len(parameter.constraints.enum) > 0,
        }[type(control)]
        if not compatible:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{control_path}/kind",
                    f"{control.kind} control is not compatible with"
                    f" parameter {control.parameter!r}",
                )
            )
        if isinstance(control, SelectControl):
            values = [option.value for option in control.options]
            if len(set(values)) != len(values):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{control_path}/options",
                        "select option values must be unique",
                    )
                )
            if parameter.constraints.enum is not None and set(values) != set(
                parameter.constraints.enum
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{control_path}/options",
                        "select option values must exactly match the"
                        " parameter enum",
                    )
                )
    return diagnostics


_OUTPUT_KIND_PROJECTION_KIND = {
    "table": "table",
    "image": "plot",
    "text": "document",
}


def check_action(action_id: str, action: Action, document: Document) -> list[Diagnostic]:
    """Check one action's mappings against the document collections.

    An action of the extension language carries no ``targets``: its
    outputs subscribe projections instead of targeting them, and
    :func:`melt.schema.semantic.check_output_subscriptions` covers those
    (RFC 001 section 8.1). A `melt/v0.1` action keeps every v0.1 check.
    """
    path = f"/actions/{action_id}"
    diagnostics: list[Diagnostic] = []
    for name, mapping in action.parameters.items():
        if mapping.parameter not in document.parameters:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/parameters/{name}/parameter",
                    f"action maps {name!r} to unknown parameter"
                    f" {mapping.parameter!r}",
                )
            )
    for name, reference in action.inputs.items():
        source = document.sources.get(reference.sourceId)
        if source is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/inputs/{name}",
                    f"action input {name!r} references unknown source"
                    f" {reference.sourceId!r}",
                )
            )
        elif str(reference.version) not in source.versions:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/inputs/{name}/version",
                    f"source version {reference.version} does not exist",
                )
            )
    targets = getattr(action, "targets", None)
    if targets is None:
        return diagnostics
    for output_name in action.outputs:
        if output_name not in action.targets:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.OUTPUT_OWNER,
                    f"{path}/targets/{output_name}",
                    f"declared output {output_name!r} has no target",
                )
            )
    seen_targets: set[str] = set()
    for target_name, projection_id in action.targets.items():
        target_path = f"{path}/targets/{target_name}"
        if target_name not in action.outputs:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.OUTPUT_OWNER,
                    target_path,
                    f"target {target_name!r} has no declared output",
                )
            )
        if projection_id in seen_targets:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.OUTPUT_OWNER,
                    target_path,
                    f"action targets projection {projection_id!r} twice",
                )
            )
        seen_targets.add(projection_id)
        projection = document.projections.get(projection_id)
        if projection is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    target_path,
                    f"target references unknown projection"
                    f" {projection_id!r}",
                )
            )
            continue
        output_kind = action.outputs.get(target_name)
        if (
            output_kind is not None
            and _OUTPUT_KIND_PROJECTION_KIND[output_kind] != projection.kind
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    target_path,
                    f"output kind {output_kind!r} cannot target a"
                    f" {projection.kind!r} projection",
                )
            )
    return diagnostics


def check_output_groups(document: Document) -> list[Diagnostic]:
    """One action owns each output group; each projection sits in at most
    one output group (SPEC.md section 7.1)."""
    diagnostics: list[Diagnostic] = []
    group_owner: dict[str, str] = {}
    projection_owner: dict[str, str] = {}
    for action_id, action in document.actions.items():
        first = group_owner.get(action.outputGroup)
        if first is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.OUTPUT_OWNER,
                    f"/actions/{action_id}/outputGroup",
                    f"output group {action.outputGroup!r} is already owned"
                    f" by action {first!r}",
                    related_paths=[f"/actions/{first}/outputGroup"],
                )
            )
        else:
            group_owner[action.outputGroup] = action_id
        for target_name, projection_id in getattr(action, "targets", {}).items():
            first_projection = projection_owner.get(projection_id)
            if first_projection is not None and first_projection != action_id:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.OUTPUT_OWNER,
                        f"/actions/{action_id}/targets/{target_name}",
                        f"projection {projection_id!r} already belongs to"
                        f" the output group of action {first_projection!r}",
                        related_paths=[
                            f"/projections/{projection_id}"
                        ],
                    )
                )
            else:
                projection_owner[projection_id] = action_id
    return diagnostics


def check_projection(
    projection_id: str,
    projection: "_ProjectionBase",
    document: Document,
    targeted: "set[str] | None",
) -> list[Diagnostic]:
    """Check one projection against its source and renderer contract.

    A projection kind outside the v0.1 three answers nothing here: the
    extension language checks its own kinds in
    :mod:`melt.schema.semantic`, and a `melt/v0.1` document never
    carries one. ``targeted`` is None under the extension language,
    whose actions target nothing: a null source stays an empty result
    state there, and an output subscription answers its own checks
    (RFC 001 section 8).
    """
    path = f"/projections/{projection_id}"
    diagnostics: list[Diagnostic] = []
    if projection.kind not in ("table", "plot", "document"):
        return diagnostics
    if projection.source is None:
        if targeted is not None and projection_id not in targeted:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/source",
                    "a null source is allowed only when an action targets"
                    " the projection",
                )
            )
        return diagnostics
    if not isinstance(projection.source, SourceReference):
        # An output subscription of the extension language: a pane's
        # configured columns are no part of an action's output validity,
        # and a projection that cannot render a valid result shows a
        # projection error instead (RFC 001 section 8.2).
        return diagnostics
    reference = projection.source
    source = document.sources.get(reference.sourceId)
    if source is None:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.REFERENCE,
                f"{path}/source",
                f"projection references unknown source"
                f" {reference.sourceId!r}",
            )
        )
        return diagnostics
    version = source.versions.get(str(reference.version))
    if version is None:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.REFERENCE,
                f"{path}/source/version",
                f"source version {reference.version} does not exist",
            )
        )
        return diagnostics
    expected_kind = {
        "table": "table",
        "plot": "image",
        "document": "text",
    }[projection.kind]
    if source.kind != expected_kind:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.TYPE,
                f"{path}/source",
                f"{projection.kind!r} projection requires a"
                f" {expected_kind!r} source, found {source.kind!r}",
            )
        )
        return diagnostics
    if isinstance(projection, TableProjection) and isinstance(
        version.schema, TableSchema
    ):
        column_ids = {column.id for column in version.schema.columns}
        if projection.config.columns is not None:
            for index, column_id in enumerate(projection.config.columns):
                if column_id not in column_ids:
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.REFERENCE,
                            f"{path}/config/columns/{index}",
                            f"configured column {column_id!r} is not in the"
                            " source schema",
                        )
                    )
        if projection.config.selection == "single":
            if version.schema.rowKey is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/config/selection",
                        "single selection requires a source with a valid"
                        " rowKey",
                    )
                )
    return diagnostics


def check_binding(binding_id: str, binding: Binding, document: Document) -> list[Diagnostic]:
    """Check one binding's references and debounce policy.

    A binding whose event or effect is not a v0.1 shape answers nothing
    here: the extension language checks its pure-event bindings in
    :mod:`melt.schema.semantic`, and a `melt/v0.1` document never
    carries one.
    """
    path = f"/bindings/{binding_id}"
    diagnostics: list[Diagnostic] = []
    event = binding.event
    if not isinstance(event, (ParameterChangedEvent, ControlActivateEvent)) or (
        not isinstance(binding.effect, ActionInvokeEffect)
    ):
        return diagnostics
    if isinstance(event, ParameterChangedEvent):
        if event.parameter not in document.parameters:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/event/parameter",
                    f"binding references unknown parameter"
                    f" {event.parameter!r}",
                )
            )
    else:
        projection = document.projections.get(event.projection)
        if projection is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/event/projection",
                    f"binding references unknown projection"
                    f" {event.projection!r}",
                )
            )
        else:
            control = next(
                (item for item in projection.controls if item.id == event.control),
                None,
            )
            if control is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REFERENCE,
                        f"{path}/event/control",
                        f"projection {event.projection!r} has no control"
                        f" {event.control!r}",
                    )
                )
            elif not isinstance(control, ButtonControl):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/event/control",
                        "only button controls emit control.activate",
                    )
                )
        if binding.debounceMs != 0:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"{path}/debounceMs",
                    "button bindings require a debounce of zero",
                )
            )
    if binding.effect.action not in document.actions:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.REFERENCE,
                f"{path}/effect/action",
                f"binding references unknown action"
                f" {binding.effect.action!r}",
            )
        )
    return diagnostics


def check_binding_uniqueness(document: Document) -> list[Diagnostic]:
    """A button has at most one binding; a parameter connects to an action
    at most once (SPEC.md section 9.2).

    Bindings outside the v0.1 shapes carry their own uniqueness rule in
    :mod:`melt.schema.semantic` (one writer per event address, RFC 001
    section 5.2).
    """
    diagnostics: list[Diagnostic] = []
    button_addresses: dict[tuple[str, str], str] = {}
    parameter_actions: dict[tuple[str, str], str] = {}
    for binding_id, binding in document.bindings.items():
        event = binding.event
        if not isinstance(event, (ParameterChangedEvent, ControlActivateEvent)) or (
            not isinstance(binding.effect, ActionInvokeEffect)
        ):
            continue
        if isinstance(event, ControlActivateEvent):
            address = (event.projection, event.control)
            first = button_addresses.get(address)
            if first is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.SCHEMA,
                        f"/bindings/{binding_id}/event",
                        f"control {event.control!r} in projection"
                        f" {event.projection!r} already has binding"
                        f" {first!r}",
                        related_paths=[f"/bindings/{first}/event"],
                    )
                )
            else:
                button_addresses[address] = binding_id
        else:
            pair = (event.parameter, binding.effect.action)
            first = parameter_actions.get(pair)
            if first is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.SCHEMA,
                        f"/bindings/{binding_id}/event",
                        f"parameter {event.parameter!r} already connects to"
                        f" action {binding.effect.action!r} through binding"
                        f" {first!r}",
                        related_paths=[f"/bindings/{first}/event"],
                    )
                )
            else:
                parameter_actions[pair] = binding_id
    return diagnostics


def _walk_layout(
    node,
    depth: int,
    path: str,
    projections: dict,
    placements: dict,
    diagnostics: list[Diagnostic],
) -> None:
    too_deep = depth > DEFAULT_LIMITS.max_layout_depth
    if too_deep:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.LAYOUT,
                path,
                f"layout depth exceeds the allowed bound of"
                f" {DEFAULT_LIMITS.max_layout_depth}",
            )
        )
    if isinstance(node, PaneNode):
        if node.projection not in projections:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LAYOUT,
                    f"{path}/projection",
                    f"layout references unknown projection"
                    f" {node.projection!r}",
                )
            )
            return
        first = placements.get(node.projection)
        if first is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LAYOUT,
                    f"{path}/projection",
                    f"projection {node.projection!r} is placed twice",
                    related_paths=[first],
                )
            )
        else:
            placements[node.projection] = f"{path}/projection"
        return
    if too_deep:
        return
    _walk_layout(
        node.first, depth + 1, f"{path}/first", projections, placements, diagnostics
    )
    _walk_layout(
        node.second, depth + 1, f"{path}/second", projections, placements, diagnostics
    )


def check_layout(document: Document) -> list[Diagnostic]:
    """Check placement, depth, and completeness of the layout tree."""
    diagnostics: list[Diagnostic] = []
    placements: dict[str, str] = {}
    if document.layout.root is not None:
        _walk_layout(
            document.layout.root,
            1,
            "/layout/root",
            document.projections,
            placements,
            diagnostics,
        )
    for index, projection_id in enumerate(document.layout.hidden):
        path = f"/layout/hidden/{index}"
        if projection_id not in document.projections:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LAYOUT,
                    path,
                    f"hidden list references unknown projection"
                    f" {projection_id!r}",
                )
            )
            continue
        first = placements.get(projection_id)
        if first is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LAYOUT,
                    path,
                    f"projection {projection_id!r} is placed twice",
                    related_paths=[first],
                )
            )
        else:
            placements[projection_id] = path
    for projection_id in document.projections:
        if projection_id not in placements:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LAYOUT,
                    f"/projections/{projection_id}",
                    "projection appears neither in the layout tree nor in"
                    " hidden",
                )
            )
    return diagnostics


def document_semantic_diagnostics(document: Document) -> list[Diagnostic]:
    """Run every document-level check and return sorted diagnostics."""
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(check_limits(document))
    for source_id, source in document.sources.items():
        diagnostics.extend(check_source(source_id, source))
    for parameter_id, parameter in document.parameters.items():
        diagnostics.extend(check_parameter(parameter_id, parameter))
    # The null-source rule of SPEC.md section 8.1 belongs to the v0.1
    # target contract. The extension language targets nothing, so its
    # documents pass None and keep a null source an empty result state
    # (RFC 001 section 8).
    targeted: "set[str] | None" = None
    if document.language == LANGUAGE_ID:
        targeted = set()
        for action in document.actions.values():
            targeted.update(getattr(action, "targets", {}).values())
    for action_id, action in document.actions.items():
        diagnostics.extend(check_action(action_id, action, document))
    diagnostics.extend(check_output_groups(document))
    for projection_id, projection in document.projections.items():
        diagnostics.extend(
            check_controls(projection_id, projection, document.parameters)
        )
        diagnostics.extend(
            check_projection(projection_id, projection, document, targeted)
        )
    for binding_id, binding in document.bindings.items():
        diagnostics.extend(check_binding(binding_id, binding, document))
    diagnostics.extend(check_binding_uniqueness(document))
    diagnostics.extend(check_layout(document))
    return sorted_diagnostics(diagnostics)


class MeltDocumentError(MeltSchemaError):
    """Raised when a document fails schema or semantic validation."""


def parse_document(data: "str | bytes | bytearray | dict") -> Document:
    """Parse and validate a Melt document.

    The input may be JSON text, JSON bytes, or an already parsed value
    tree. The function raises `MeltDocumentError` with sorted
    diagnostics on any failure.
    """

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltDocumentError(error.diagnostics) from error
    else:
        value = data
    try:
        document = Document.model_validate(value)
    except ValidationError as error:
        raise MeltDocumentError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error
    diagnostics = document_semantic_diagnostics(document)
    if diagnostics:
        raise MeltDocumentError(diagnostics)
    return document


def validate_document(data: "str | bytes | bytearray | dict") -> list[Diagnostic]:
    """Validate a document and return its diagnostics without raising."""
    try:
        parse_document(data)
    except MeltDocumentError as error:
        return list(error.diagnostics)
    return []


__all__ = [
    "ArtifactHash",
    "Action",
    "ActionImplementation",
    "ActionInvokeEffect",
    "ActionParameterMapping",
    "Binding",
    "BindingEffect",
    "BindingEvent",
    "ButtonControl",
    "Control",
    "ControlActivateEvent",
    "DateValue",
    "Document",
    "DocumentConfig",
    "DocumentProjection",
    "EntityId",
    "FiniteNumber",
    "ImageSchema",
    "ImportOrigin",
    "JsonScalar",
    "Label",
    "LANGUAGE_ID",
    "Layout",
    "LayoutNode",
    "MeltDocumentError",
    "MeltModel",
    "NonEmptyStr",
    "NumberControl",
    "OpaqueId",
    "PaneNode",
    "Parameter",
    "ParameterChangedEvent",
    "ParameterConstraints",
    "PlotConfig",
    "PlotProjection",
    "Projection",
    "RunOrigin",
    "SAFE_INTEGER_MAX",
    "SafeInt",
    "SelectControl",
    "SelectOption",
    "Source",
    "SourceOrigin",
    "SourceReference",
    "SourceVersion",
    "SplitNode",
    "SplitRatio",
    "TableColumn",
    "TableConfig",
    "TableProjection",
    "TableSchema",
    "TextControl",
    "TextSchema",
    "Timestamp",
    "TimestampValue",
    "Title",
    "ToggleControl",
    "VersionKey",
    "check_action",
    "check_binding",
    "check_controls",
    "check_limits",
    "check_output_groups",
    "check_parameter",
    "check_projection",
    "check_source",
    "document_semantic_diagnostics",
    "parse_document",
    "validate_document",
]
