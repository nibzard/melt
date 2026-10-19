"""Command envelope and mutation operation models (SPEC.md section 12).

This module holds the framed request envelope, the typed operation
list, the per-method payloads, the response pair, and the four-byte
framed IPC helpers. The transport supplies caller identity; no field
in the envelope can impersonate a caller.

`workspace.apply` and `workspace.preview` carry `operations`. Every
other method carries `payload`. Revision preconditions travel in
`expected`, which maps touched record paths to revisions. Zero means
expected absence.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal, Union

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from melt.schema.canonical import parse_json, validate_json_tree
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostics_from_pydantic_errors,
    sorted_diagnostics,
    with_code,
)
from melt.schema.document import (
    Action,
    Binding,
    Document,
    DocumentProjection,
    EntityId,
    JsonScalar,
    Label,
    Layout,
    MeltModel,
    NonEmptyStr,
    NonNegativeSafeInt,
    OpaqueId,
    Parameter,
    ParameterConstraints,
    PlotProjection,
    PositiveSafeInt,
    SourceReference,
    TableProjection,
    _first_error_message,
)
from melt.schema.limits import DEFAULT_LIMITS
from melt.schema.semantic import (
    OBJECT_SELECT_EVENT,
    SELECTION_CLEAR_EVENT,
    SEMANTIC_LANGUAGE_ID,
    GraphProjection,
    RecordProjection,
    SemanticAction,
    SemanticBinding,
    SemanticCollection,
    SemanticDocument,
    SemanticDocumentProjection,
    SemanticPlotProjection,
    SemanticReference,
    SemanticState,
    SemanticTableProjection,
    document_diagnostics,
    document_model_of,
)

PROTOCOL_VERSION = 1

MethodName = Literal[
    "session.hello",
    "workspace.create",
    "workspace.inspect",
    "workspace.preview",
    "workspace.apply",
    "source.import",
    "action.register",
    "action.invoke",
    "control.activate",
    OBJECT_SELECT_EVENT,
    SELECTION_CLEAR_EVENT,
    "run.cancel",
    "run.inspect",
    "table.page",
    "workspace.subscribe",
    "workspace.migrate",
]

EntityCollection = Literal[
    "parameters", "actions", "projections", "bindings", "collections", "state"
]
"""The entity collections a mutation operation may address.

The first four are the v0.1 collections of SPEC.md section 12.2. The
last two belong to `melt/semantic-draft-1` (RFC 001 sections 3 and 4);
the workspace service rejects them for a `melt/v0.1` workspace, whose
document defines neither map."""

RequestId = Annotated[StrictStr, Field(min_length=1, max_length=128)]
"""Request IDs are opaque strings of 1 to 128 characters."""


# ---------------------------------------------------------------------------
# Table page query pieces (SPEC.md sections 8.2 and 12.7)
# ---------------------------------------------------------------------------


class SortKey(MeltModel):
    column: EntityId
    direction: Literal["asc", "desc"]


def _value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    return "number"


class Filter(MeltModel):
    """One table filter.

    `isNull` omits `value`. `contains` accepts only strings. Order
    comparisons accept numbers, dates, timestamps, and strings. The
    reader checks operators against column types (SPEC.md section
    12.7).
    """

    column: EntityId
    operator: Literal["eq", "lt", "lte", "gt", "gte", "contains", "isNull"]
    value: JsonScalar | None = None

    @model_validator(mode="after")
    def _check_value(self) -> "Filter":
        operator, value = self.operator, self.value
        if operator == "isNull":
            if value is not None:
                raise ValueError(
                    with_code(DiagnosticCode.SCHEMA, "isNull omits value")
                )
            return self
        if value is None:
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    f"operator {operator!r} requires a value",
                )
            )
        kind = _value_kind(value)
        if operator == "contains":
            if kind != "string":
                raise ValueError(
                    with_code(DiagnosticCode.TYPE, "contains accepts only strings")
                )
            return self
        if operator != "eq" and kind == "boolean":
            raise ValueError(
                with_code(
                    DiagnosticCode.TYPE,
                    f"operator {operator!r} does not accept booleans",
                )
            )
        return self


def _page_limit(value: int) -> int:
    bound = DEFAULT_LIMITS.table_page_rows
    if not 1 <= value <= bound:
        raise ValueError(
            with_code(
                DiagnosticCode.LIMIT,
                f"table page rows exceed the allowed bound of {bound}",
            )
        )
    return value


PageLimit = Annotated[PositiveSafeInt, AfterValidator(_page_limit)]


# ---------------------------------------------------------------------------
# Registration contract for `action.register` (SPEC.md sections 7.2, 12.6)
# ---------------------------------------------------------------------------


class ParameterContractEntry(MeltModel):
    """One parameter slot in a registration contract."""

    type: Literal["string", "number", "integer", "boolean"]
    nullable: bool
    constraints: ParameterConstraints | None = None


class RegistrationContract(MeltModel):
    """The contract an `action.register` payload declares.

    A contract holds parameters, inputs, and outputs. It cannot
    declare a permission grant (SPEC.md section 12.6).
    """

    parameters: dict[EntityId, ParameterContractEntry]
    inputs: dict[EntityId, Literal["table", "image", "text"]]
    outputs: dict[EntityId, Literal["table", "image", "text"]]


# ---------------------------------------------------------------------------
# Method payloads (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


class SessionHelloPayload(MeltModel):
    protocolVersions: tuple[NonNegativeSafeInt, ...] = Field(min_length=1)
    languageVersions: tuple[NonEmptyStr, ...] = Field(min_length=1)
    clientVersion: NonEmptyStr


def _dispatch_document(value: object):
    """Validate a workspace document under the language it declares.

    `melt/v0.1` keeps its closed v0.1 shape; `melt/semantic-draft-1`
    adds the RFC 001 extension maps. Any other identifier reports
    `E_VERSION` at `/language` through the v0.1 literal.
    """

    return document_model_of(value)


AnyDocument = Annotated[
    Union[Document, SemanticDocument], BeforeValidator(_dispatch_document)
]
"""A workspace document of either supported language."""


class WorkspaceCreatePayload(MeltModel):
    document: AnyDocument


class WorkspaceInspectPayload(MeltModel):
    collection: EntityCollection | None = None
    ids: tuple[EntityId, ...] | None = None


class SourceImportPayload(MeltModel):
    sourceId: EntityId
    artifactToken: OpaqueId
    originLabel: Label


class ActionRegisterPayload(MeltModel):
    scriptToken: OpaqueId
    environmentId: OpaqueId
    contract: RegistrationContract


class ActionInvokePayload(MeltModel):
    actionId: EntityId


class ControlActivatePayload(MeltModel):
    projectionId: EntityId
    controlId: EntityId


class ObjectSelectPayload(MeltModel):
    """One `object.select` renderer event (RFC 001 section 5.1).

    The payload carries the one semantic reference the gesture selected.
    The projection, its definition revision, the renderer contract, and
    the displayed source and collection revisions ride the envelope:
    its `expected` map pins them (sections 5.1 and 5.3).
    """

    projectionId: EntityId
    objectRef: SemanticReference


class SelectionClearPayload(MeltModel):
    """One `selection.clear` renderer event (RFC 001 section 5.1).

    Clear carries no object data; the bound assignment writes the typed
    null literal.
    """

    projectionId: EntityId


class RunCancelPayload(MeltModel):
    runId: OpaqueId


class RunInspectPayload(MeltModel):
    runId: OpaqueId


class TablePagePayload(MeltModel):
    source: SourceReference
    sort: tuple[SortKey, ...] = ()
    filters: tuple[Filter, ...] = ()
    offset: NonNegativeSafeInt
    limit: PageLimit

    @field_validator("sort")
    @classmethod
    def _distinct_sort_keys(cls, value):
        columns = [key.column for key in value]
        if len(set(columns)) != len(columns):
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    "sort keys must not repeat a column",
                )
            )
        return value


class WorkspaceSubscribePayload(MeltModel):
    afterSequence: NonNegativeSafeInt
    epoch: PositiveSafeInt


class WorkspaceMigratePayload(MeltModel):
    """One explicit language migration request (RFC 001 section 8.3).

    The migration starts from `melt/v0.1` and commits
    `melt/semantic-draft-1`: every v0.1 target becomes a following
    subscription, and source versions, projection IDs, parameter
    values, and layout stay unchanged. ``dryRun`` previews the same
    migration without writing; its report explains that pane removal no
    longer invalidates output publication.
    """

    targetLanguage: Literal[SEMANTIC_LANGUAGE_ID]
    dryRun: StrictBool = False


PAYLOAD_MODELS: dict[str, type[MeltModel] | None] = {
    "session.hello": SessionHelloPayload,
    "workspace.create": WorkspaceCreatePayload,
    "workspace.inspect": WorkspaceInspectPayload,
    "workspace.preview": None,
    "workspace.apply": None,
    "source.import": SourceImportPayload,
    "action.register": ActionRegisterPayload,
    "action.invoke": ActionInvokePayload,
    "control.activate": ControlActivatePayload,
    OBJECT_SELECT_EVENT: ObjectSelectPayload,
    SELECTION_CLEAR_EVENT: SelectionClearPayload,
    "run.cancel": RunCancelPayload,
    "run.inspect": RunInspectPayload,
    "table.page": TablePagePayload,
    "workspace.subscribe": WorkspaceSubscribePayload,
    "workspace.migrate": WorkspaceMigratePayload,
}
"""The payload model of each method. `None` marks the operation methods."""


# ---------------------------------------------------------------------------
# Mutation operations (SPEC.md section 12.2)
# ---------------------------------------------------------------------------

def _dispatch_entity_value(value: object):
    """Validate an entity value by its keys.

    Entity values carry no discriminator of their own; the `collection`
    sibling selects the shape. Dispatching on keys keeps error paths
    free of union branch noise. `command_semantic_diagnostics` still
    checks that the value matches the declared collection.
    """

    if not isinstance(value, dict):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "entity value must be an object")
        )
    keys = set(value)
    branch: type | None
    if "implementation" in keys:
        # The extension action carries no targets (RFC 001 8.1); the
        # final candidate validation under the document's own language
        # rejects the shape that language does not define.
        branch = SemanticAction if "targets" not in keys else Action
    elif "event" in keys:
        event = value.get("event")
        event_kind = event.get("kind") if isinstance(event, dict) else None
        effect = value.get("effect")
        effect_kind = effect.get("kind") if isinstance(effect, dict) else None
        if event_kind in (OBJECT_SELECT_EVENT, SELECTION_CLEAR_EVENT) or (
            effect_kind == "state.set"
        ):
            # The pure-event bindings of RFC 001 section 5.2 accept the
            # extension events and effects; the v0.1 Binding does not.
            branch = SemanticBinding
        else:
            branch = Binding
    elif "kind" in keys:
        source = value.get("source")
        subscribing = isinstance(source, dict) and "actionOutput" in source
        branch = {
            "table": SemanticTableProjection if subscribing else TableProjection,
            "plot": SemanticPlotProjection if subscribing else PlotProjection,
            "document": (
                SemanticDocumentProjection if subscribing else DocumentProjection
            ),
            "graph": GraphProjection,
            "record": RecordProjection,
        }.get(value.get("kind"))
        if branch is None:
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    f"unknown projection kind {value.get('kind')!r}",
                )
            )
    elif "source" in keys and "key" in keys:
        branch = SemanticCollection
    elif "scope" in keys:
        branch = SemanticState
    elif "default" in keys or "nullable" in keys or "constraints" in keys:
        branch = Parameter
    else:
        raise ValueError(
            with_code(
                DiagnosticCode.SCHEMA,
                "value matches no parameter, action, projection,"
                " binding, collection, or reference state shape",
            )
        )
    try:
        return branch.model_validate(value)
    except ValidationError as error:
        code, message = _first_error_message(error)
        raise ValueError(with_code(code, message)) from error


EntityValue = Annotated[
    Union[
        Parameter,
        Action,
        SemanticAction,
        TableProjection,
        PlotProjection,
        DocumentProjection,
        SemanticTableProjection,
        SemanticPlotProjection,
        SemanticDocumentProjection,
        GraphProjection,
        RecordProjection,
        Binding,
        SemanticBinding,
        SemanticCollection,
        SemanticState,
    ],
    BeforeValidator(_dispatch_entity_value),
]

ENTITY_MODELS: dict[str, tuple[type, ...]] = {
    "parameters": (Parameter,),
    "actions": (Action, SemanticAction),
    "projections": (
        TableProjection,
        PlotProjection,
        DocumentProjection,
        SemanticTableProjection,
        SemanticPlotProjection,
        SemanticDocumentProjection,
        GraphProjection,
        RecordProjection,
    ),
    "bindings": (Binding, SemanticBinding),
    "collections": (SemanticCollection,),
    "state": (SemanticState,),
}
"""The entity model of each mutable collection."""


class EntityCreate(MeltModel):
    op: Literal["entity.create"]
    collection: EntityCollection
    id: EntityId
    value: EntityValue


class EntityReplace(MeltModel):
    op: Literal["entity.replace"]
    collection: EntityCollection
    id: EntityId
    value: EntityValue


class EntityDelete(MeltModel):
    op: Literal["entity.delete"]
    collection: EntityCollection
    id: EntityId


class ParameterSet(MeltModel):
    op: Literal["parameter.set"]
    id: EntityId
    value: JsonScalar | None


class StateSet(MeltModel):
    """One reference state assignment (RFC 001 sections 4 and 5.2).

    The value is a serialized semantic reference or null. A reference
    must match the declared collection and its current incarnation at
    assignment time, so a stale incarnation cannot select an object in
    a recreated collection. The workspace service rejects this
    operation for a `melt/v0.1` workspace, whose language defines no
    reference state.
    """

    op: Literal["state.set"]
    id: EntityId
    value: SemanticReference | None


class LayoutReplace(MeltModel):
    op: Literal["layout.replace"]
    value: Layout


Operation = Annotated[
    Union[
        EntityCreate,
        EntityReplace,
        EntityDelete,
        ParameterSet,
        StateSet,
        LayoutReplace,
    ],
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# Command envelope (SPEC.md section 12.1)
# ---------------------------------------------------------------------------


def _operations_limit(value):
    if value is None:
        return value
    bound = DEFAULT_LIMITS.operations_per_mutation
    if len(value) > bound:
        raise ValueError(
            with_code(
                DiagnosticCode.LIMIT,
                f"operations per mutation exceed the allowed bound of {bound}",
            )
        )
    return value


OperationsField = Annotated[
    tuple[Operation, ...] | None, AfterValidator(_operations_limit)
]


class CommandEnvelope(MeltModel):
    protocolVersion: Literal[PROTOCOL_VERSION]
    workspaceId: EntityId
    requestId: RequestId
    method: MethodName
    expected: dict[NonEmptyStr, NonNegativeSafeInt] | None = None
    operations: OperationsField = None
    payload: dict[StrictStr, Any] | None = None

    @model_validator(mode="after")
    def _method_shape(self) -> "CommandEnvelope":
        if self.method in ("workspace.apply", "workspace.preview"):
            if self.payload is not None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"{self.method} uses operations, not payload",
                    )
                )
            if self.operations is None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"{self.method} requires operations",
                    )
                )
        else:
            if self.operations is not None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"{self.method} uses payload, not operations",
                    )
                )
            if self.payload is None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"{self.method} requires a payload",
                    )
                )
        return self


# ---------------------------------------------------------------------------
# Responses (SPEC.md section 12.6)
# ---------------------------------------------------------------------------


def _json_tree_result(value: dict):
    try:
        validate_json_tree(value, "/result")
    except MeltSchemaError as error:
        first = error.diagnostics[0]
        raise ValueError(with_code(first.code, first.message)) from error
    return value


class OkResponse(MeltModel):
    requestId: RequestId
    ok: Literal[True]
    commitSequence: NonNegativeSafeInt
    result: Annotated[dict[StrictStr, Any], AfterValidator(_json_tree_result)] = (
        Field(default_factory=dict)
    )


class RejectedResponse(MeltModel):
    requestId: RequestId
    ok: Literal[False]
    errors: tuple[Diagnostic, ...] = Field(min_length=1)


CommandResponse = Union[OkResponse, RejectedResponse]


def _dispatch_response(value: object):
    """Validate a response through its `ok` flag.

    Responses carry no discriminator field. The boolean `ok` value
    selects the branch, so error paths stay free of union noise.
    """

    if not isinstance(value, dict):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "response must be an object")
        )
    flag = value.get("ok")
    if flag is True:
        branch: type = OkResponse
    elif flag is False:
        branch = RejectedResponse
    else:
        raise ValueError(
            with_code(DiagnosticCode.SCHEMA, "response requires an ok flag")
        )
    try:
        return branch.model_validate(value)
    except ValidationError as error:
        code, message = _first_error_message(error)
        raise ValueError(with_code(code, message)) from error


TypedCommandResponse = Annotated[
    CommandResponse, BeforeValidator(_dispatch_response)
]

_RESPONSE_ADAPTER: TypeAdapter | None = None


def _response_adapter() -> TypeAdapter:
    global _RESPONSE_ADAPTER
    if _RESPONSE_ADAPTER is None:
        _RESPONSE_ADAPTER = TypeAdapter(TypedCommandResponse)
    return _RESPONSE_ADAPTER


# ---------------------------------------------------------------------------
# Framed IPC (SPEC.md section 12.1)
# ---------------------------------------------------------------------------

FRAME_HEADER_LENGTH = 4
_MAX_FRAME_LENGTH = 0xFFFFFFFF


class MeltCommandError(MeltSchemaError):
    """Raised when a command fails schema or semantic validation."""


def _frame_limit_error(declared: int, maximum: int) -> MeltCommandError:
    return MeltCommandError(
        (
            Diagnostic(
                code=DiagnosticCode.LIMIT,
                path="",
                message=f"declared frame length {declared} exceeds the"
                f" allowed bound of {maximum} bytes",
            ),
        )
    )


def encode_frame(payload: bytes, *, limit: int | None = None) -> bytes:
    """Prefix one payload with its four-byte big-endian length."""
    maximum = DEFAULT_LIMITS.control_frame_bytes if limit is None else limit
    if len(payload) > _MAX_FRAME_LENGTH or len(payload) > maximum:
        raise _frame_limit_error(len(payload), maximum)
    return len(payload).to_bytes(FRAME_HEADER_LENGTH, "big") + payload


def decode_frame(
    data: bytes, *, limit: int | None = None, offset: int = 0
) -> tuple[bytes, int]:
    """Decode one frame from a buffer.

    The length prefix is checked against the limit before any payload
    is read. Returns the payload and the offset after the frame.
    """

    maximum = DEFAULT_LIMITS.control_frame_bytes if limit is None else limit
    end = offset + FRAME_HEADER_LENGTH
    if len(data) < end:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message="truncated frame header",
                ),
            )
        )
    declared = int.from_bytes(data[offset:end], "big")
    if declared > maximum:
        raise _frame_limit_error(declared, maximum)
    frame_end = end + declared
    if len(data) < frame_end:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message=f"truncated frame: expected {declared} payload bytes",
                ),
            )
        )
    return data[end:frame_end], frame_end


def read_frame(read: Callable[[int], bytes], *, limit: int | None = None) -> bytes:
    """Read one frame from an exact-read callable."""

    maximum = DEFAULT_LIMITS.control_frame_bytes if limit is None else limit
    header = read(FRAME_HEADER_LENGTH)
    if len(header) != FRAME_HEADER_LENGTH:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message="truncated frame header",
                ),
            )
        )
    declared = int.from_bytes(header, "big")
    if declared > maximum:
        raise _frame_limit_error(declared, maximum)
    payload = read(declared)
    if len(payload) != declared:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.PARSE,
                    path="",
                    message=f"truncated frame: expected {declared} payload bytes",
                ),
            )
        )
    return payload


# ---------------------------------------------------------------------------
# Parsing entry points
# ---------------------------------------------------------------------------


def _prefixed(diagnostics: "list[Diagnostic] | tuple[Diagnostic, ...]", prefix: str):
    return [
        Diagnostic(
            code=item.code,
            path=f"{prefix}{item.path}",
            message=item.message,
            relatedPaths=tuple(item.relatedPaths),
            hint=item.hint,
        )
        for item in diagnostics
    ]


def parse_payload(method: str, payload: Any) -> MeltModel:
    """Validate one method payload and return its typed model.

    Raises `MeltCommandError` with paths under `/payload`.
    """

    model = PAYLOAD_MODELS.get(method)
    if model is None:
        raise MeltCommandError(
            (
                Diagnostic(
                    code=DiagnosticCode.SCHEMA,
                    path="/payload",
                    message=f"method {method!r} does not carry a payload",
                ),
            )
        )
    try:
        parsed = model.model_validate(payload)
    except ValidationError as error:
        raise MeltCommandError(
            _prefixed(
                diagnostics_from_pydantic_errors(
                    error.errors(), DiagnosticCode.SCHEMA
                ),
                "/payload",
            )
        ) from error
    if isinstance(parsed, WorkspaceCreatePayload):
        diagnostics = document_diagnostics(parsed.document)
        if diagnostics:
            raise MeltCommandError(_prefixed(diagnostics, "/payload/document"))
    return parsed


def command_semantic_diagnostics(envelope: CommandEnvelope) -> list[Diagnostic]:
    """Check operations and payload shapes beyond the field level."""

    diagnostics: list[Diagnostic] = []
    for index, operation in enumerate(envelope.operations or ()):
        path = f"/operations/{index}"
        if isinstance(operation, (EntityCreate, EntityReplace)):
            expected = ENTITY_MODELS[operation.collection]
            if not isinstance(operation.value, expected):
                diagnostics.append(
                    Diagnostic(
                        code=DiagnosticCode.TYPE,
                        path=f"{path}/value",
                        message=f"value does not match collection"
                        f" {operation.collection!r}",
                    )
                )
    if envelope.payload is not None:
        try:
            parse_payload(envelope.method, envelope.payload)
        except MeltCommandError as error:
            diagnostics.extend(error.diagnostics)
    return sorted_diagnostics(diagnostics)


def parse_command(data: "str | bytes | bytearray | dict") -> CommandEnvelope:
    """Parse and validate one command envelope.

    The input may be JSON text, JSON bytes, or an already parsed value
    tree. The function raises `MeltCommandError` with sorted
    diagnostics on any failure.
    """

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltCommandError(error.diagnostics) from error
    else:
        value = data
    try:
        envelope = CommandEnvelope.model_validate(value)
    except ValidationError as error:
        raise MeltCommandError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error
    diagnostics = command_semantic_diagnostics(envelope)
    if diagnostics:
        raise MeltCommandError(diagnostics)
    return envelope


def parse_response(data: "str | bytes | bytearray | dict") -> CommandResponse:
    """Parse one command response."""

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltCommandError(error.diagnostics) from error
    else:
        value = data
    try:
        return _response_adapter().validate_python(value)
    except ValidationError as error:
        raise MeltCommandError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error


__all__ = [
    "ActionInvokePayload",
    "ActionRegisterPayload",
    "AnyDocument",
    "CommandEnvelope",
    "CommandResponse",
    "ControlActivatePayload",
    "ENTITY_MODELS",
    "EntityCreate",
    "EntityDelete",
    "EntityReplace",
    "EntityValue",
    "Filter",
    "FRAME_HEADER_LENGTH",
    "LayoutReplace",
    "MeltCommandError",
    "MethodName",
    "ObjectSelectPayload",
    "OkResponse",
    "Operation",
    "PageLimit",
    "ParameterContractEntry",
    "ParameterSet",
    "PAYLOAD_MODELS",
    "PROTOCOL_VERSION",
    "RegistrationContract",
    "RequestId",
    "RejectedResponse",
    "RunCancelPayload",
    "RunInspectPayload",
    "SelectionClearPayload",
    "SessionHelloPayload",
    "SortKey",
    "SourceImportPayload",
    "StateSet",
    "TablePagePayload",
    "WorkspaceCreatePayload",
    "WorkspaceInspectPayload",
    "WorkspaceMigratePayload",
    "WorkspaceSubscribePayload",
    "command_semantic_diagnostics",
    "decode_frame",
    "encode_frame",
    "parse_command",
    "parse_payload",
    "parse_response",
    "read_frame",
]
