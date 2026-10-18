"""Persisted record schemas (SPEC.md section 17).

Every record carries `recordType` and `schemaVersion`. The version is
part of the stored data, so a later schema change can detect and
negotiate older records instead of silently misreading them
(SPEC.md section 14). Version 1 is the only version in `melt/v0.1`.

Records in this module: `RunRecord`, `RegistrationRecord`,
`PermissionGrantRecord`, `ArtifactRecord`, `ParameterState`,
`CommandReceipt`, `Snapshot`, `ChangeNotification`, `PageRequest`, and
`PageResponse`.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from melt.schema.canonical import parse_json
from melt.schema.commands import (
    CommandResponse,
    Filter,
    MethodName,
    PageLimit,
    ParameterContractEntry,
    SortKey,
)
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostic_from_pydantic_error,
    with_code,
)
from melt.schema.document import (
    ArtifactHash,
    Diagnostic as DiagnosticModel,
    Document,
    EntityId,
    FiniteNumber,
    JsonScalar,
    MeltModel,
    NonEmptyStr,
    NonNegativeSafeInt,
    OpaqueId,
    PositiveSafeInt,
    SourceReference,
    SourceSchema,
    Timestamp,
)
from melt.schema.limits import DEFAULT_LIMITS

RECORD_SCHEMA_VERSION = 1
"""The record schema version of `melt/v0.1`."""


def _record_schema_version(value: int) -> int:
    if value != RECORD_SCHEMA_VERSION:
        raise ValueError(
            with_code(
                DiagnosticCode.VERSION,
                f"unsupported record schema version {value};"
                f" this service stores version {RECORD_SCHEMA_VERSION}",
            )
        )
    return value


RecordVersion = Annotated[PositiveSafeInt, AfterValidator(_record_schema_version)]


class RecordBase(MeltModel):
    """Base record: carries the stored schema version."""

    schemaVersion: RecordVersion


RunState = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted", "unknown"
]
RunDisposition = Literal["none", "current", "superseded", "rejected"]


class CapturedParameter(MeltModel):
    """One parameter captured at admission (SPEC.md section 7.3)."""

    value: JsonScalar | None
    revision: PositiveSafeInt


class RunRecord(RecordBase):
    """One admitted or completed run, with its invocation snapshot."""

    recordType: Literal["run"]
    runId: OpaqueId
    workspaceId: EntityId
    actionId: EntityId
    actionRevision: PositiveSafeInt
    outputGroup: EntityId
    outputGroupRevision: PositiveSafeInt
    generation: PositiveSafeInt
    registrationId: OpaqueId
    registrationRevision: PositiveSafeInt
    scriptArtifact: ArtifactHash
    environmentFingerprint: NonEmptyStr
    inputs: dict[EntityId, SourceReference]
    parameters: dict[EntityId, CapturedParameter]
    permissionDecision: OpaqueId | None = None
    state: RunState
    disposition: RunDisposition
    outputs: dict[EntityId, SourceReference] = Field(default_factory=dict)
    createdAt: Timestamp
    updatedAt: Timestamp


class ScriptArtifact(MeltModel):
    """A captured script artifact reference."""

    artifact: ArtifactHash
    byteLength: NonNegativeSafeInt
    mediaType: NonEmptyStr


class RegistrationRecord(RecordBase):
    """A service-owned trusted registration (SPEC.md section 7.2).

    Registration never executes the script. The replay classification
    is fixed to `nonReplaySafe` for arbitrary Python.
    """

    recordType: Literal["registration"]
    registrationId: OpaqueId
    revision: PositiveSafeInt
    script: ScriptArtifact
    projectRoot: NonEmptyStr
    workingDirectoryPolicy: NonEmptyStr
    environmentFingerprint: NonEmptyStr
    parameters: dict[EntityId, ParameterContractEntry]
    inputs: dict[EntityId, Literal["table", "image", "text"]]
    outputs: dict[EntityId, Literal["table", "image", "text"]]
    requiredPermission: NonEmptyStr
    replayClassification: Literal["nonReplaySafe"]
    createdAt: Timestamp


GrantKind = Literal["specificRun", "projectExecution"]


class PermissionGrantRecord(RecordBase):
    """One execution grant held outside every exported document (15.1).

    A document never carries a grant: grants live in the service record
    stream, minted only through the authenticated host permission
    endpoint, never through a command envelope. A `specificRun` grant
    covers exactly one captured admission intent and pins the definition
    revisions it approved, so a changed action, registration, or output
    group stops it from covering and a new admission is required. A
    `projectExecution` grant covers every admission of one trusted
    registration under the explicit project trust policy the host
    recorded. Revocation is durable and covers nothing afterwards.
    """

    recordType: Literal["permissionGrant"]
    grantId: OpaqueId
    workspaceId: EntityId
    kind: GrantKind
    requiredPermission: NonEmptyStr
    registrationId: OpaqueId
    actionId: EntityId | None = None
    actionRevision: PositiveSafeInt | None = None
    registrationRevision: PositiveSafeInt | None = None
    outputGroup: EntityId | None = None
    outputGroupRevision: PositiveSafeInt | None = None
    intentId: OpaqueId | None = None
    generation: PositiveSafeInt | None = None
    grantedBy: NonEmptyStr
    grantedAt: Timestamp
    revokedAt: Timestamp | None = None
    revokedBy: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _kind_scope(self) -> "PermissionGrantRecord":
        if self.kind == "specificRun":
            missing = [
                name
                for name in (
                    "actionId",
                    "actionRevision",
                    "registrationRevision",
                    "outputGroup",
                    "intentId",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        "a specificRun grant pins the intent it covers;"
                        " it requires " + ", ".join(missing),
                    )
                )
        else:
            pinned = [
                name
                for name in (
                    "actionId",
                    "actionRevision",
                    "registrationRevision",
                    "outputGroup",
                    "outputGroupRevision",
                    "intentId",
                    "generation",
                )
                if getattr(self, name) is not None
            ]
            if pinned:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        "a projectExecution grant covers the whole"
                        " registration and pins no single intent; it"
                        " carries no " + ", ".join(pinned),
                    )
                )
        return self


class ArtifactRecord(RecordBase):
    """One immutable artifact as the service verified it (section 5.1).

    The record carries every verified fact of the registration: content
    hash, byte length, kind, media type, and the schema measured from
    the bytes. The service rebuilds its trusted source catalog from
    these records, so a reopened workspace keeps every imported
    artifact without rereading or re-executing anything.
    """

    recordType: Literal["artifact"]
    artifact: ArtifactHash
    kind: Literal["table", "image", "text"]
    mediaType: NonEmptyStr
    byteLength: NonNegativeSafeInt
    schema_: SourceSchema = Field(alias="schema")
    createdAt: Timestamp

    @property
    def schema(self) -> SourceSchema:
        """The measured schema, under its wire name."""

        return self.schema_


class ParameterState(RecordBase):
    """The stored value and revision of one parameter (section 6.2).

    Revisions begin at 1. A successful value change increments the
    revision.
    """

    recordType: Literal["parameterState"]
    parameterId: EntityId
    value: JsonScalar | None
    revision: PositiveSafeInt


class CommandReceipt(RecordBase):
    """The stored response of one processed request (section 12.5).

    A retry with the same ID and payload returns the stored response.
    Reuse with different content fails with `E_REQUEST_REUSE`.
    """

    recordType: Literal["commandReceipt"]
    requestId: OpaqueId
    method: MethodName
    requestPayloadHash: ArtifactHash
    response: CommandResponse
    commitSequence: NonNegativeSafeInt
    receivedAt: Timestamp


class Snapshot(RecordBase):
    """A full workspace snapshot (section 12.4).

    The snapshot separates the document from runtime records: entity
    revisions, parameter state, and active runs. The service commits one
    snapshot with every successful mutation, so reopening a workspace
    restores the last committed definitions and state without executing
    project code (SPEC.md section 14).

    `incarnations` counts how often each parameter identifier was
    created. Deleting and recreating a parameter starts a new identity
    incarnation, so old events cannot target the new one (section 12.3).
    """

    recordType: Literal["snapshot"]
    workspaceId: EntityId
    language: Literal["melt/v0.1"]
    epoch: PositiveSafeInt
    commitSequence: NonNegativeSafeInt
    document: Document
    entityRevisions: dict[NonEmptyStr, PositiveSafeInt]
    parameterStates: dict[EntityId, ParameterState] = Field(
        default_factory=dict
    )
    incarnations: dict[EntityId, PositiveSafeInt] = Field(
        default_factory=dict
    )
    activeRuns: tuple[RunRecord, ...] = ()
    diagnostics: tuple[DiagnosticModel, ...] = ()


class ChangeNotification(RecordBase):
    """One ordered change notification (section 12.5)."""

    recordType: Literal["changeNotification"]
    workspaceId: EntityId
    epoch: PositiveSafeInt
    commitSequence: PositiveSafeInt
    changedRecords: tuple[NonEmptyStr, ...] = Field(min_length=1)


class PageRequest(RecordBase):
    """One table page request (section 12.7)."""

    recordType: Literal["pageRequest"]
    source: SourceReference
    sort: tuple[SortKey, ...] = ()
    filters: tuple[Filter, ...] = ()
    offset: NonNegativeSafeInt
    limit: PageLimit


TableCell = Union[StrictBool, FiniteNumber, StrictStr, None]
"""A table cell value. `int64` and `decimal` values travel as strings."""


class PageResponse(RecordBase):
    """One table page response (section 12.7).

    `nextOffset` is null exactly at the end of the table. Rows are
    arrays in the returned column order.
    """

    recordType: Literal["pageResponse"]
    source: SourceReference
    queryHash: ArtifactHash
    columns: tuple[EntityId, ...]
    rows: tuple[tuple[TableCell, ...], ...]
    offset: NonNegativeSafeInt
    hasMore: StrictBool
    nextOffset: NonNegativeSafeInt | None

    @field_validator("columns")
    @classmethod
    def _column_limit(cls, value):
        bound = DEFAULT_LIMITS.table_page_columns
        if len(value) > bound:
            raise ValueError(
                with_code(
                    DiagnosticCode.LIMIT,
                    f"page columns exceed the allowed bound of {bound};"
                    " select columns explicitly",
                )
            )
        return value

    @field_validator("rows")
    @classmethod
    def _row_shape(cls, value, info):
        bound = DEFAULT_LIMITS.table_page_rows
        if len(value) > bound:
            raise ValueError(
                with_code(
                    DiagnosticCode.LIMIT,
                    f"page rows exceed the allowed bound of {bound}",
                )
            )
        columns = info.data.get("columns")
        if columns is None:
            return value
        for index, row in enumerate(value):
            if len(row) != len(columns):
                raise ValueError(
                    with_code(
                        DiagnosticCode.TYPE,
                        f"row {index} does not match the returned column"
                        " count",
                    )
                )
        return value

    @field_validator("nextOffset")
    @classmethod
    def _next_offset_pairs_with_has_more(cls, value, info):
        has_more = info.data.get("hasMore")
        if has_more is None:
            return value
        if has_more != (value is not None):
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    "nextOffset is null exactly when hasMore is false",
                )
            )
        return value


MeltRecord = Union[
    RunRecord,
    RegistrationRecord,
    PermissionGrantRecord,
    ArtifactRecord,
    ParameterState,
    CommandReceipt,
    Snapshot,
    ChangeNotification,
    PageRequest,
    PageResponse,
]

RECORD_TYPES: dict[str, int] = {
    "run": RECORD_SCHEMA_VERSION,
    "registration": RECORD_SCHEMA_VERSION,
    "permissionGrant": RECORD_SCHEMA_VERSION,
    "artifact": RECORD_SCHEMA_VERSION,
    "parameterState": RECORD_SCHEMA_VERSION,
    "commandReceipt": RECORD_SCHEMA_VERSION,
    "snapshot": RECORD_SCHEMA_VERSION,
    "changeNotification": RECORD_SCHEMA_VERSION,
    "pageRequest": RECORD_SCHEMA_VERSION,
    "pageResponse": RECORD_SCHEMA_VERSION,
}
"""The current stored schema version of each record type."""

_RECORD_ADAPTER: TypeAdapter | None = None


def _record_adapter() -> TypeAdapter:
    global _RECORD_ADAPTER
    if _RECORD_ADAPTER is None:
        _RECORD_ADAPTER = TypeAdapter(
            Annotated[MeltRecord, Field(discriminator="recordType")]
        )
    return _RECORD_ADAPTER


class MeltRecordError(MeltSchemaError):
    """Raised when a persisted record fails validation."""


def parse_record(data: "str | bytes | bytearray | dict") -> MeltRecord:
    """Parse and validate one persisted record.

    Unknown record types and unsupported schema versions report
    `E_VERSION`, so recovery can negotiate stored versions instead of
    misreading them.
    """

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltRecordError(error.diagnostics) from error
    else:
        value = data
    record_type = value.get("recordType") if isinstance(value, dict) else None
    if record_type not in RECORD_TYPES:
        raise MeltRecordError(
            (
                Diagnostic(
                    code=DiagnosticCode.VERSION,
                    path="/recordType",
                    message=f"unknown record type {record_type!r}",
                ),
            )
        )
    stored_version = value.get("schemaVersion") if isinstance(value, dict) else None
    if stored_version != RECORD_SCHEMA_VERSION:
        raise MeltRecordError(
            (
                Diagnostic(
                    code=DiagnosticCode.VERSION,
                    path="/schemaVersion",
                    message=f"unsupported record schema version"
                    f" {stored_version!r}; this service stores version"
                    f" {RECORD_SCHEMA_VERSION}",
                ),
            )
        )
    try:
        return _record_adapter().validate_python(value)
    except ValidationError as error:
        raise MeltRecordError(
            [
                diagnostic_from_pydantic_error(item, DiagnosticCode.SCHEMA)
                for item in error.errors()
            ]
        ) from error


def record_to_wire(record: MeltRecord) -> dict:
    """Convert one record into its wire-shaped value tree."""
    return record.model_dump(mode="json", by_alias=True)


__all__ = [
    "ArtifactRecord",
    "CapturedParameter",
    "ChangeNotification",
    "CommandReceipt",
    "GrantKind",
    "MeltRecord",
    "MeltRecordError",
    "PageRequest",
    "PageResponse",
    "ParameterState",
    "PermissionGrantRecord",
    "RECORD_SCHEMA_VERSION",
    "RECORD_TYPES",
    "RegistrationRecord",
    "RunDisposition",
    "RunRecord",
    "RunState",
    "ScriptArtifact",
    "Snapshot",
    "TableCell",
    "parse_record",
    "record_to_wire",
]
