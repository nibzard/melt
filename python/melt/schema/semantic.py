"""Semantic collections, events, and observers (RFC 001 sections 3-6).

The provisional language identifier `melt/semantic-draft-1` extends the
`melt/v0.1` document:

- `collections`: typed identity indexes over table source versions. A
  collection does not copy records; it pins one immutable source version
  and names the column whose values identify the logical objects
  (section 3).
- `state`: nullable reference state with workspace scope. A state entry
  holds a reference to one collection, and its stored record holds a
  serialized semantic reference or null (section 4).
- Pure renderer events (`object.select`, `selection.clear`) bound to
  typed `state.set` effects with no expressions (section 5).
- Graph and record projections that observe shared state through fixed
  `highlight` and `record` selector slots, plus the immutable graph
  source whose identity map binds visual nodes to semantic references
  (sections 6 and 7.1).
- Actions that declare output names, kinds, and a publication group
  without projection targets, and projection sources that subscribe to
  those outputs by their stable incarnation (section 8).

The extension is not a stable interchange version (RFC 001 section 1).
A `melt/v0.1` document rejects both fields: the v0.1 model accepts no
unknown fields, and the semantic checks below never run for it.

The service issues an opaque incarnation when a collection entity is
created, so a document never carries one and a client cannot choose it
(RFC 001 section 3.2). This module therefore models only what a
document may say.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictStr,
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
    sorted_diagnostics,
    with_code,
)
from melt.schema.document import (
    LANGUAGE_ID,
    ActionImplementation,
    ActionInvokeEffect,
    ActionParameterMapping,
    Control,
    ControlActivateEvent,
    Document,
    DocumentConfig,
    DocumentProjection,
    EntityId,
    ImageSchema,
    Label,
    MeltDocumentError,
    MeltModel,
    NonEmptyStr,
    NonNegativeSafeInt,
    ParameterChangedEvent,
    PlotConfig,
    PlotProjection,
    PositiveSafeInt,
    Source,
    SourceReference,
    SourceVersion,
    TableConfig,
    TableProjection,
    TableSchema,
    TextSchema,
    Title,
    VersionKey,
    _first_error_message,
    document_semantic_diagnostics,
)
from melt.schema.limits import DEFAULT_LIMITS

SEMANTIC_LANGUAGE_ID = "melt/semantic-draft-1"
"""The provisional identifier of the RFC 001 extension (section 1).

It marks isolated experimental fixtures, not a stable interchange
version. An accepted extension requires a new language identifier and
an explicit migration path."""

SUPPORTED_LANGUAGE_IDS = (LANGUAGE_ID, SEMANTIC_LANGUAGE_ID)
"""The language identifiers this schema package parses."""


class SemanticCollection(MeltModel):
    """One typed identity index over a table source (RFC 001 section 3.1).

    `source` pins one immutable source version; updating it is a
    revision-checked mutation. `key` names the column whose values are
    the object keys: unique, non-null strings, compared case-sensitively
    and never trimmed or normalized.
    """

    source: SourceReference
    key: EntityId


class ReferenceType(MeltModel):
    """The type of one reference state entry (RFC 001 section 4).

    Only `ref` exists in this extension. Numeric and string parameters
    keep their v0.1 shape and are not migrated into this state system.
    """

    kind: Literal["ref"]
    collection: EntityId


class SemanticState(MeltModel):
    """One nullable reference state entry with workspace scope.

    The default is null and only null: a reference value carries the
    service-issued incarnation of its collection, so no document can
    name one before the service creates the collection. Defaults apply
    only on creation; changing one never resets a selection.
    """

    type: ReferenceType
    nullable: StrictBool
    default: None
    scope: Literal["workspace"]


class SemanticReference(MeltModel):
    """One serialized semantic reference (RFC 001 section 3.2).

    References are workspace-scoped and carry no source version:
    resolution uses the collection's current pinned version, and run
    inputs capture their versions separately.
    """

    collection: EntityId
    incarnation: NonEmptyStr
    key: StrictStr


# ---------------------------------------------------------------------------
# Pane-independent outputs (RFC 001 section 8)
# ---------------------------------------------------------------------------


class SemanticAction(MeltModel):
    """One action of `melt/semantic-draft-1` (RFC 001 section 8.1).

    The extension action declares output names, kinds, and a
    publication group. It carries no projection targets: publication is
    independent of panes, and projections subscribe to an output by its
    stable incarnation instead. An action with no subscribers can still
    run and publish, and closing, hiding, or removing a projection
    neither cancels it nor invalidates its outputs.
    """

    implementation: ActionImplementation
    parameters: dict[EntityId, ActionParameterMapping]
    inputs: dict[EntityId, SourceReference]
    outputs: dict[EntityId, Literal["table", "image", "text"]]
    outputGroup: EntityId


class ActionOutputId(MeltModel):
    """The stable identity one subscription follows (RFC 001 8.1).

    The incarnation is service-issued when the action entity is
    created; inspection returns it and the compiler verifies it, so
    deleting and recreating an action does not attach old subscribers
    to a new calculation.
    """

    action: EntityId
    output: EntityId
    incarnation: NonEmptyStr


class OutputSubscription(MeltModel):
    """One projection source that follows an action output (RFC 001 8.1).

    ``follow: "current"`` resolves the output's current pointer at
    every commit sequence, so the subscriber advances with each
    successful publication. ``follow: "pinned"`` freezes one version of
    the output's stable logical source: the subscriber does not
    advance, and its view reports the version status it holds.
    """

    actionOutput: ActionOutputId
    follow: Literal["current", "pinned"]
    version: PositiveSafeInt | None = None

    @model_validator(mode="after")
    def _version_matches_follow(self) -> "OutputSubscription":
        if self.follow == "pinned" and self.version is None:
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    "a pinned subscription names the version of the"
                    " output's source it freezes",
                )
            )
        if self.follow == "current" and self.version is not None:
            raise ValueError(
                with_code(
                    DiagnosticCode.SCHEMA,
                    "a following subscription carries no version; it"
                    " resolves the output's current pointer",
                )
            )
        return self


def is_output_subscription(value: object) -> bool:
    """Report whether one projection source subscribes to an output."""

    return isinstance(value, OutputSubscription)


def is_subscription_wire(value: object) -> bool:
    """Report whether one wire-tree projection source is a subscription."""

    return isinstance(value, dict) and "actionOutput" in value


# ---------------------------------------------------------------------------
# Graph sources and their identity map (RFC 001 section 7.1)
# ---------------------------------------------------------------------------


GRAPH_MEDIA_TYPE = "application/vnd.melt.graph+json"
"""The media type of one immutable graph artifact."""

SEMANTIC_SOURCE_KIND_MEDIA_TYPES: dict[str, frozenset[str]] = {
    "graph": frozenset({GRAPH_MEDIA_TYPE}),
}
"""The media types each extension source kind accepts.

The graph kind belongs to `melt/semantic-draft-1` only; a `melt/v0.1`
document rejects the kind at its own source model."""


class GraphNode(MeltModel):
    """One node of the minimal graph contract (RFC 001 section 7.1).

    `nodeId` identifies the visual element; the collection and key name
    the semantic object it represents. Node IDs and object identities
    stay separate: several nodes may carry the same reference.
    """

    nodeId: EntityId
    label: Label
    collection: EntityId
    key: StrictStr


class GraphEdge(MeltModel):
    """One edge between two existing nodes, by their node IDs."""

    edgeId: EntityId
    # The wire name is `from`. The Python attribute carries a trailing
    # underscore so it reads as the keyword it is.
    from_: EntityId = Field(alias="from")
    to: EntityId


class GraphSchema(MeltModel):
    """The typed graph and its validated identity map (RFC 001 7.1).

    The identity map is measured from the artifact bytes at import and
    stored with the source version, so compilation never derives object
    identity from labels or element order.
    """

    nodes: tuple[GraphNode, ...] = Field(min_length=1)
    edges: tuple[GraphEdge, ...] = ()

    @model_validator(mode="after")
    def _validate_map(self) -> "GraphSchema":
        """Node and edge ids are stable and unique; edges name nodes
        the graph actually carries."""

        seen: dict[str, int] = {}
        for index, node in enumerate(self.nodes):
            first = seen.get(node.nodeId)
            if first is not None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"node id {node.nodeId!r} repeats at positions"
                        f" {first} and {index}; node ids are stable and"
                        " unique",
                    )
                )
            seen[node.nodeId] = index
        node_ids = frozenset(seen)
        edge_ids: dict[str, int] = {}
        for index, edge in enumerate(self.edges):
            first = edge_ids.get(edge.edgeId)
            if first is not None:
                raise ValueError(
                    with_code(
                        DiagnosticCode.SCHEMA,
                        f"edge id {edge.edgeId!r} repeats at positions"
                        f" {first} and {index}; edge ids are stable and"
                        " unique",
                    )
                )
            edge_ids[edge.edgeId] = index
            for endpoint in (edge.from_, edge.to):
                if endpoint not in node_ids:
                    raise ValueError(
                        with_code(
                            DiagnosticCode.REFERENCE,
                            f"edge {edge.edgeId!r} names node"
                            f" {endpoint!r}, which the graph does not"
                            " carry",
                        )
                    )
        return self


class GraphSourceVersion(SourceVersion):
    """One version of a graph source: the measured identity map."""

    schema_: GraphSchema = Field(alias="schema")

    @property
    def schema(self) -> GraphSchema:
        """The identity map, under its wire name."""

        return self.schema_


class GraphSource(Source):
    """One logical graph source of immutable graph versions."""

    kind: Literal["graph"]
    versions: dict[VersionKey, GraphSourceVersion] = Field(min_length=1)


def _dispatch_semantic_source(value: object):
    """Validate one source entry under the kind it declares.

    The v0.1 kinds keep their exact model; `graph` is the extension's
    kind, whose versions carry the identity map as their schema.
    """

    if isinstance(value, Source):
        return value
    if isinstance(value, dict) and value.get("kind") == "graph":
        return GraphSource.model_validate(value)
    return Source.model_validate(value)


SemanticSource = Annotated[
    Union[Source, GraphSource], BeforeValidator(_dispatch_semantic_source)
]


def _dispatch_measured_schema(value: object):
    """Validate one measured schema by its keys, graph included.

    The v0.1 shapes dispatch exactly as the document layer does; the
    graph identity map joins them for the trusted catalog and the
    artifact records, which carry every language's measurements.
    """

    if isinstance(value, (TableSchema, ImageSchema, TextSchema, GraphSchema)):
        return value
    if not isinstance(value, dict):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "schema must be an object")
        )
    keys = set(value)
    if keys & {"nodes", "edges"}:
        return GraphSchema.model_validate(value)
    if keys & {"columns", "rowKey"}:
        return TableSchema.model_validate(value)
    if keys & {"width", "height"}:
        return ImageSchema.model_validate(value)
    if "encoding" in keys:
        return TextSchema.model_validate(value)
    raise ValueError(
        with_code(
            DiagnosticCode.SCHEMA,
            "schema matches no table, image, text, or graph shape",
        )
    )


AnyMeasuredSchema = Annotated[
    Union[TableSchema, ImageSchema, TextSchema, GraphSchema],
    BeforeValidator(_dispatch_measured_schema),
]
"""One measured schema of any supported language: a v0.1 table, image,
or text schema, or the graph identity map of the extension."""


# ---------------------------------------------------------------------------
# Projection observers (RFC 001 section 6)
# ---------------------------------------------------------------------------


class GraphConfig(MeltModel):
    """The renderer configuration of one graph projection."""

    nodeLabels: Literal["show", "hide"]


class RecordConfig(MeltModel):
    """The renderer configuration of one record projection.

    `fields` is null for every column of the collection's pinned
    version, or an ordered list of distinct column identifiers.
    """

    fields: tuple[EntityId, ...] | None

    @field_validator("fields")
    @classmethod
    def _distinct_fields(cls, value):
        if value is None or len(set(value)) == len(value):
            return value
        raise ValueError(
            with_code(DiagnosticCode.SCHEMA, "record fields must be distinct")
        )


class HighlightObserve(MeltModel):
    """The `highlight` selector: which state the graph highlights by."""

    state: EntityId


class RecordObserve(MeltModel):
    """The `record` selector: which state resolves the shown record."""

    state: EntityId


class GraphObservers(MeltModel):
    """The observer slots of one graph projection."""

    highlight: HighlightObserve


class RecordObservers(MeltModel):
    """The observer slots of one record projection."""

    record: RecordObserve


class _SemanticProjectionBase(MeltModel):
    title: Title
    collection: EntityId
    controls: tuple[Control, ...]


def _dispatch_projection_source(value: object):
    """Validate one projection source by its keys.

    An `actionOutput` key selects the subscription shape; anything
    else is the existing source reference form. Dispatching on keys
    keeps error paths free of union branch noise, exactly as the other
    key-dispatched unions of this package do.
    """

    if value is None or isinstance(value, (SourceReference, OutputSubscription)):
        return value
    if not isinstance(value, dict):
        raise ValueError(
            with_code(DiagnosticCode.TYPE, "source must be an object")
        )
    branch = (
        OutputSubscription
        if "actionOutput" in value
        else SourceReference
    )
    try:
        return branch.model_validate(value)
    except ValidationError as error:
        code, message = _first_error_message(error)
        raise ValueError(with_code(code, message)) from error


ProjectionSource = Annotated[
    "SourceReference | OutputSubscription",
    BeforeValidator(_dispatch_projection_source),
]


class _SubscribingProjectionBase(MeltModel):
    """The shared fields of the three v0.1 projection kinds under the
    extension language.

    `source` widens to carry one output subscription (RFC 001 8.1): a
    projection may follow an action output by its stable incarnation,
    pin a source version in the existing reference form, or stay null
    and display an empty result state.
    """

    title: Title
    source: "ProjectionSource | None"
    controls: tuple[Control, ...]


class SemanticTableProjection(_SubscribingProjectionBase):
    kind: Literal["table"]
    config: TableConfig


class SemanticPlotProjection(_SubscribingProjectionBase):
    kind: Literal["plot"]
    config: PlotConfig


class SemanticDocumentProjection(_SubscribingProjectionBase):
    kind: Literal["document"]
    config: DocumentConfig


class GraphProjection(_SemanticProjectionBase):
    """One projection over an immutable graph source (RFC 001 6, 7.1).

    The graph declares the one collection its nodes reference, so
    selection has one static reference type. Nodes outside that
    collection are rejected by validation.
    """

    kind: Literal["graph"]
    source: SourceReference
    config: GraphConfig
    observe: GraphObservers


class RecordProjection(_SemanticProjectionBase):
    """One projection that resolves a reference in one collection.

    The record selector reads state and sources; it writes neither. A
    null selection displays an empty state, a missing key displays the
    missing key, and an unreadable source displays a recoverable error
    (RFC 001 section 6).
    """

    kind: Literal["record"]
    config: RecordConfig
    observe: RecordObservers


SemanticProjection = Annotated[
    Union[
        SemanticTableProjection,
        SemanticPlotProjection,
        SemanticDocumentProjection,
        GraphProjection,
        RecordProjection,
    ],
    Field(discriminator="kind"),
]
"""The projections of `melt/semantic-draft-1`: the three v0.1 kinds with
their exact v0.1 shapes widened by output subscriptions (RFC 001 8.1),
plus the graph and record kinds of RFC 001."""


# ---------------------------------------------------------------------------
# Pure events and typed assignments (RFC 001 section 5)
# ---------------------------------------------------------------------------


OBJECT_SELECT_EVENT = "object.select"
SELECTION_CLEAR_EVENT = "selection.clear"
"""The initial renderer event names (RFC 001 section 5.1)."""

OBJECT_REF_FIELD = "objectRef"
"""The one declared payload field of `object.select`."""


class ObjectSelectEvent(MeltModel):
    """One selection gesture: carries one semantic reference."""

    kind: Literal[OBJECT_SELECT_EVENT]
    projection: EntityId


class SelectionClearEvent(MeltModel):
    """One clearing gesture: carries no object data."""

    kind: Literal[SELECTION_CLEAR_EVENT]
    projection: EntityId


SemanticBindingEvent = Annotated[
    Union[
        ParameterChangedEvent,
        ControlActivateEvent,
        ObjectSelectEvent,
        SelectionClearEvent,
    ],
    Field(discriminator="kind"),
]
"""The events a `melt/semantic-draft-1` binding may observe: the two
v0.1 event kinds with their exact shapes, plus the pure renderer events
of RFC 001 section 5.1."""


class EventFieldValue(MeltModel):
    """One value selector: a declared field of the event payload."""

    eventField: Literal[OBJECT_REF_FIELD]


class NullLiteralValue(MeltModel):
    """One value selector: the typed null literal."""

    literal: None


def _dispatch_state_set_value(value: object):
    """Validate one assignment value selector by its single key.

    The compiler permits only declared event fields and typed literals
    (RFC 001 section 5.2): no expressions, function calls, string
    interpolation, or executable callbacks. Dispatching on the single
    key keeps that boundary explicit in the diagnostic.
    """

    if isinstance(value, (EventFieldValue, NullLiteralValue)):
        return value
    if not isinstance(value, dict):
        raise ValueError(
            with_code(
                DiagnosticCode.TYPE, "value selector must be an object"
            )
        )
    keys = set(value)
    if keys == {"eventField"}:
        return EventFieldValue.model_validate(value)
    if keys == {"literal"}:
        return NullLiteralValue.model_validate(value)
    raise ValueError(
        with_code(
            DiagnosticCode.SCHEMA,
            "value must name one declared event field or one typed"
            " literal; expressions are not part of the language",
        )
    )


StateSetValue = Annotated[
    Union[EventFieldValue, NullLiteralValue],
    BeforeValidator(_dispatch_state_set_value),
]


class StateSetEffect(MeltModel):
    """One typed assignment: this binding writes one state record.

    The value selector names a declared event field or a typed literal.
    There are no expressions, function calls, string interpolations, or
    executable callbacks (RFC 001 section 5.2).
    """

    kind: Literal["state.set"]
    target: EntityId
    value: StateSetValue


SemanticBindingEffect = Annotated[
    Union[ActionInvokeEffect, StateSetEffect], Field(discriminator="kind")
]
"""The effects of the extension: the v0.1 action invocation, plus the
pure state assignment."""


class SemanticBinding(MeltModel):
    """One binding of `melt/semantic-draft-1`.

    A pure renderer event commits immediately: its debounce is fixed at
    zero, because the service checks the event and commits the state
    change and receipt together (RFC 001 section 5.3).
    """

    event: SemanticBindingEvent
    effect: SemanticBindingEffect
    debounceMs: NonNegativeSafeInt = Field(default=0, le=5000)


class SemanticDocument(Document):
    """The `melt/semantic-draft-1` document.

    Every v0.1 field keeps its v0.1 meaning and checks. The two added
    maps are required, like every other top-level collection, and may
    be empty. The graph and record projections and the pure-event
    bindings extend their v0.1 counterparts without changing them, and
    the actions of this language declare outputs without projection
    targets (RFC 001 section 8.1).
    """

    language: Literal[SEMANTIC_LANGUAGE_ID]
    sources: dict[EntityId, SemanticSource]
    actions: dict[EntityId, SemanticAction]
    projections: dict[EntityId, SemanticProjection]
    bindings: dict[EntityId, SemanticBinding]
    collections: dict[EntityId, SemanticCollection]
    state: dict[EntityId, SemanticState]


# ---------------------------------------------------------------------------
# Semantic pass (RFC 001 sections 3 and 4)
# ---------------------------------------------------------------------------


def _diagnostic(
    code: str,
    path: str,
    message: str,
    related_paths: "tuple[str, ...]" = (),
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        path=path,
        message=message,
        relatedPaths=related_paths,
        hint=hint,
    )


def check_semantic_collections(document: SemanticDocument) -> list[Diagnostic]:
    """Check every collection against its pinned table version.

    The key column must exist in the pinned version's schema and must
    be a non-nullable string column, because keys are unique non-null
    strings (RFC 001 section 3.1). The service additionally verifies
    the stored bytes when it admits the collection binding.
    """

    diagnostics: list[Diagnostic] = []
    limits = DEFAULT_LIMITS
    if len(document.collections) > limits.max_collections:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.LIMIT,
                "/collections",
                f"collections count {len(document.collections)} exceeds the"
                f" allowed bound of {limits.max_collections}",
            )
        )
    for collection_id, collection in sorted(document.collections.items()):
        path = f"/collections/{collection_id}"
        source = document.sources.get(collection.source.sourceId)
        if source is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/source/sourceId",
                    f"collection references unknown source"
                    f" {collection.source.sourceId!r}",
                    hint="Import the source, or reference an imported one.",
                )
            )
            continue
        if source.kind != "table":
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/source",
                    f"a semantic collection indexes a table source, but"
                    f" source {collection.source.sourceId!r} has kind"
                    f" {source.kind!r}",
                )
            )
            continue
        version = source.versions.get(str(collection.source.version))
        if version is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/source/version",
                    f"source {collection.source.sourceId!r} holds no version"
                    f" {collection.source.version}",
                    hint="Pin a version the source actually carries.",
                )
            )
            continue
        if not isinstance(version.schema, TableSchema):
            continue  # the v0.1 source checks already report the drift
        column = next(
            (
                item
                for item in version.schema.columns
                if item.id == collection.key
            ),
            None,
        )
        if column is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/key",
                    f"key column {collection.key!r} is not in the schema of"
                    f" source {collection.source.sourceId!r} version"
                    f" {collection.source.version}",
                    hint="Key the collection on a column the pinned version"
                    " carries.",
                )
            )
        else:
            if column.type != "string":
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/key",
                        f"key column {collection.key!r} has type"
                        f" {column.type!r}; keys are strings in this"
                        " extension",
                    )
                )
            if column.nullable:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/key",
                        f"key column {collection.key!r} is nullable; keys"
                        " must be unique and non-null",
                    )
                )
    return diagnostics


def check_reference_state(document: SemanticDocument) -> list[Diagnostic]:
    """Check every reference state entry against its collection."""

    diagnostics: list[Diagnostic] = []
    limits = DEFAULT_LIMITS
    if len(document.state) > limits.max_reference_states:
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.LIMIT,
                "/state",
                f"reference state count {len(document.state)} exceeds the"
                f" allowed bound of {limits.max_reference_states}",
            )
        )
    for state_id, entry in sorted(document.state.items()):
        path = f"/state/{state_id}"
        if entry.type.collection not in document.collections:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/type/collection",
                    f"state references unknown collection"
                    f" {entry.type.collection!r}",
                    hint="Declare the collection, or reference a declared"
                    " one.",
                )
            )
        if not entry.nullable:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/nullable",
                    "reference state is nullable in this extension; a"
                    " non-null default cannot name a collection the"
                    " service has not issued an incarnation for",
                )
            )
    return diagnostics


# ---------------------------------------------------------------------------
# Graph sources and projections (RFC 001 sections 6 and 7.1)
# ---------------------------------------------------------------------------


def check_graph_sources(document: SemanticDocument) -> list[Diagnostic]:
    """Check every graph source against its own versions.

    The v0.1 source pass skips the extension's kind; this check covers
    it: `currentVersion` must name an included version and every version
    carries the graph media type. The identity map itself is validated
    by the schema model the import measured.
    """

    diagnostics: list[Diagnostic] = []
    for source_id, source in sorted(document.sources.items()):
        if not isinstance(source, GraphSource):
            continue
        path = f"/sources/{source_id}"
        if str(source.currentVersion) not in source.versions:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/currentVersion",
                    "currentVersion does not refer to an included version",
                )
            )
        for version_key, version in sorted(source.versions.items()):
            if version.mediaType != GRAPH_MEDIA_TYPE:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.TYPE,
                        f"{path}/versions/{version_key}/mediaType",
                        f"source kind 'graph' does not accept media type"
                        f" {version.mediaType!r}",
                    )
                )
    return diagnostics


def _collection_table_schema(
    document: SemanticDocument, collection_id: str
) -> "TableSchema | None":
    """The pinned table schema of one collection, or None."""

    collection = document.collections.get(collection_id)
    if collection is None:
        return None
    source = document.sources.get(collection.source.sourceId)
    if source is None:
        return None
    version = source.versions.get(str(collection.source.version))
    if version is None or not isinstance(version.schema, TableSchema):
        return None
    return version.schema


def check_semantic_projections(document: SemanticDocument) -> list[Diagnostic]:
    """Check every graph and record projection (RFC 001 section 6).

    Both kinds declare the one collection they read, and their observer
    slot names a state entry of that same collection: the record
    selector resolves a reference in the projection's declared
    collection, and a graph highlights nodes of one static reference
    type. A graph additionally pins a graph source whose identity map
    references no other collection, because nodes outside the declared
    collection would leave selection without a type (RFC 001 7.1).
    """

    diagnostics: list[Diagnostic] = []
    for projection_id, projection in sorted(document.projections.items()):
        path = f"/projections/{projection_id}"
        if not isinstance(projection, (GraphProjection, RecordProjection)):
            continue
        collection = document.collections.get(projection.collection)
        if collection is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/collection",
                    f"projection references unknown collection"
                    f" {projection.collection!r}",
                    hint="Declare the collection, or reference a declared"
                    " one.",
                )
            )
        if isinstance(projection, GraphProjection):
            source = document.sources.get(projection.source.sourceId)
            if source is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REFERENCE,
                        f"{path}/source",
                        f"projection references unknown source"
                        f" {projection.source.sourceId!r}",
                    )
                )
            else:
                version = source.versions.get(str(projection.source.version))
                if version is None:
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.REFERENCE,
                            f"{path}/source/version",
                            f"source version {projection.source.version}"
                            " does not exist",
                        )
                    )
                elif not isinstance(source, GraphSource):
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.TYPE,
                            f"{path}/source",
                            "a graph projection requires a graph source,"
                            f" found kind {source.kind!r}",
                        )
                    )
                else:
                    for node in version.schema.nodes:
                        if node.collection != projection.collection:
                            diagnostics.append(
                                _diagnostic(
                                    DiagnosticCode.TYPE,
                                    f"{path}/collection",
                                    f"graph node {node.nodeId!r} references"
                                    f" collection {node.collection!r}, but"
                                    " the graph declares"
                                    f" {projection.collection!r}; a"
                                    " selectable graph carries one"
                                    " collection",
                                    related_paths=[
                                        f"/sources/{projection.source.sourceId}"
                                    ],
                                )
                            )
        slot, observer = (
            ("highlight", projection.observe.highlight)
            if isinstance(projection, GraphProjection)
            else ("record", projection.observe.record)
        )
        entry = document.state.get(observer.state)
        if entry is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/observe/{slot}/state",
                    f"observer references unknown state"
                    f" {observer.state!r}",
                    hint="Declare the reference state, or observe a"
                    " declared one.",
                )
            )
        elif collection is not None and (
            entry.type.collection != projection.collection
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/observe/{slot}/state",
                    f"observed state {observer.state!r} holds references"
                    f" of collection {entry.type.collection!r}, but the"
                    " projection reads"
                    f" {projection.collection!r}",
                )
            )
        if isinstance(projection, RecordProjection):
            schema = (
                _collection_table_schema(document, projection.collection)
                if collection is not None
                else None
            )
            if projection.config.fields is not None and schema is not None:
                column_ids = {column.id for column in schema.columns}
                for index, field_id in enumerate(projection.config.fields):
                    if field_id not in column_ids:
                        diagnostics.append(
                            _diagnostic(
                                DiagnosticCode.REFERENCE,
                                f"{path}/config/fields/{index}",
                                f"configured field {field_id!r} is not in"
                                " the schema of the collection's pinned"
                                " version",
                            )
                        )
    return diagnostics


# ---------------------------------------------------------------------------
# Pure events and typed assignments (RFC 001 section 5)
# ---------------------------------------------------------------------------


def check_semantic_bindings(document: SemanticDocument) -> list[Diagnostic]:
    """Check every pure-event binding (RFC 001 sections 5.1 and 5.2).

    A renderer event binds only the projection whose renderer declares
    it, and only to a `state.set` effect: pure interactions cannot
    execute project code (RFC 001 section 2), and selection never
    implicitly invokes a capability. The assignment selector names a
    declared event field or the typed null literal, the target state
    holds references of the collection the projection selects, and each
    event address carries at most one binding.
    """

    diagnostics: list[Diagnostic] = []
    addresses: dict[tuple[str, str], str] = {}
    for binding_id, binding in sorted(document.bindings.items()):
        event = binding.event
        effect = binding.effect
        if isinstance(
            event, (ParameterChangedEvent, ControlActivateEvent)
        ) and isinstance(effect, ActionInvokeEffect):
            # The v0.1 shape keeps its v0.1 checks.
            continue
        path = f"/bindings/{binding_id}"
        if not isinstance(event, (ObjectSelectEvent, SelectionClearEvent)):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/effect",
                    "a state.set effect binds only to the object.select"
                    " and selection.clear events of RFC 001 section 5.1",
                )
            )
            continue
        if not isinstance(effect, StateSetEffect):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/effect",
                    f"a pure {event.kind!r} event binds only a state.set"
                    " effect; selection never invokes a capability"
                    " (RFC 001 section 5.2)",
                )
            )
            continue
        address = (event.kind, event.projection)
        first = addresses.get(address)
        if first is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"{path}/event",
                    f"event address {event.kind}/{event.projection} already"
                    f" has writer {first!r}; each event address carries at"
                    " most one binding",
                    related_paths=[f"/bindings/{first}/event"],
                )
            )
        else:
            addresses[address] = binding_id
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
            continue
        if not isinstance(projection, GraphProjection):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/event/projection",
                    f"the renderer of projection {event.projection!r}"
                    f" declares no {event.kind!r} event",
                )
            )
            continue
        if binding.debounceMs != 0:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"{path}/debounceMs",
                    "a pure event binding commits immediately; its"
                    " debounce is fixed at zero",
                )
            )
        entry = document.state.get(effect.target)
        if entry is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/effect/target",
                    f"binding writes unknown state {effect.target!r}",
                    hint="Declare the reference state, or target a"
                    " declared one.",
                )
            )
            continue
        if entry.type.collection != projection.collection:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/effect/target",
                    f"reference-type mismatch: state {effect.target!r}"
                    f" holds references of collection"
                    f" {entry.type.collection!r}, but the event selects"
                    f" {projection.collection!r}",
                )
            )
        if isinstance(event, SelectionClearEvent) and not isinstance(
            effect.value, NullLiteralValue
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"{path}/effect/value",
                    "selection.clear carries no object data; only the"
                    " typed null literal is assignable",
                )
            )
    return diagnostics


# ---------------------------------------------------------------------------
# Output subscriptions (RFC 001 section 8)
# ---------------------------------------------------------------------------


_OUTPUT_KIND_PROJECTION_KIND = {
    "table": "table",
    "image": "plot",
    "text": "document",
}


def check_output_subscriptions(document: SemanticDocument) -> list[Diagnostic]:
    """Check every output subscription (RFC 001 section 8.1).

    A subscription names one declared output of one existing action,
    and the subscribing projection must be compatible with the output
    kind. The service-issued incarnation is verified against service
    state by the compiler and the workspace service; a document alone
    cannot know it. A pane's configured columns are no part of this
    check: a projection that cannot render a valid result shows a
    projection error and keeps its prior display (RFC 001 8.2).
    """

    diagnostics: list[Diagnostic] = []
    for projection_id, projection in sorted(document.projections.items()):
        source = getattr(projection, "source", None)
        if not isinstance(source, OutputSubscription):
            continue
        path = f"/projections/{projection_id}/source"
        identity = source.actionOutput
        action = document.actions.get(identity.action)
        if action is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/actionOutput/action",
                    f"subscription references unknown action"
                    f" {identity.action!r}",
                    hint="The subscription must migrate or leave with the"
                    " action in the same transaction (RFC 001 8.1).",
                )
            )
            continue
        output_kind = action.outputs.get(identity.output)
        if output_kind is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/actionOutput/output",
                    f"action {identity.action!r} declares no output"
                    f" {identity.output!r}",
                    hint="Changing output names or kinds requires"
                    " revision-checked subscriber migration or removal"
                    " (RFC 001 8.1).",
                )
            )
            continue
        expected = _OUTPUT_KIND_PROJECTION_KIND.get(output_kind)
        if expected is not None and projection.kind != expected:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    path,
                    f"output {identity.output!r} of action"
                    f" {identity.action!r} has kind {output_kind!r},"
                    f" which cannot feed a {projection.kind!r}"
                    " projection",
                )
            )
    return diagnostics


def semantic_document_diagnostics(document: SemanticDocument) -> list[Diagnostic]:
    """Run every check of the v0.1 semantic pass plus the extension's."""

    diagnostics: list[Diagnostic] = list(document_semantic_diagnostics(document))
    diagnostics.extend(check_semantic_collections(document))
    diagnostics.extend(check_reference_state(document))
    diagnostics.extend(check_graph_sources(document))
    diagnostics.extend(check_semantic_projections(document))
    diagnostics.extend(check_semantic_bindings(document))
    diagnostics.extend(check_output_subscriptions(document))
    return sorted_diagnostics(diagnostics)


def document_diagnostics(document: Document) -> list[Diagnostic]:
    """Run the semantic pass of the document's own language."""

    if isinstance(document, SemanticDocument):
        return semantic_document_diagnostics(document)
    return document_semantic_diagnostics(document)


# ---------------------------------------------------------------------------
# Language dispatch
# ---------------------------------------------------------------------------


def document_model_of(value: object) -> Document:
    """Validate one document tree under the language it declares.

    The `language` field selects the model: `melt/v0.1` keeps its exact
    v0.1 shape and rejects the extension fields, and
    `melt/semantic-draft-1` adds the two extension maps. Any other
    identifier fails on the v0.1 literal, which reports `E_VERSION` at
    `/language`.
    """

    if isinstance(value, Document):
        return value
    language = value.get("language") if isinstance(value, dict) else None
    if language == SEMANTIC_LANGUAGE_ID:
        return SemanticDocument.model_validate(value)
    return Document.model_validate(value)


def parse_semantic_document(
    data: "str | bytes | bytearray | dict",
) -> SemanticDocument:
    """Parse and validate one `melt/semantic-draft-1` document."""

    document = _validated_tree(data)
    if not isinstance(document, SemanticDocument):
        raise MeltDocumentError(
            (
                _diagnostic(
                    DiagnosticCode.VERSION,
                    "/language",
                    f"document declares {document.language!r}, not"
                    f" {SEMANTIC_LANGUAGE_ID!r}",
                ),
            )
        )
    diagnostics = semantic_document_diagnostics(document)
    if diagnostics:
        raise MeltDocumentError(diagnostics)
    return document


def parse_document_for_language(
    data: "str | bytes | bytearray | dict",
) -> Document:
    """Parse and validate one document under either supported language.

    Raises `MeltDocumentError` with sorted diagnostics on any failure.
    """

    document = _validated_tree(data)
    diagnostics = document_diagnostics(document)
    if diagnostics:
        raise MeltDocumentError(diagnostics)
    return document


def _validated_tree(data: "str | bytes | bytearray | dict") -> Document:
    """Decode and structurally validate one document tree."""

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltDocumentError(error.diagnostics) from error
    else:
        value = data
    try:
        return document_model_of(value)
    except ValidationError as error:
        raise MeltDocumentError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error


def is_semantic_language(language: object) -> bool:
    """Report whether one language identifier is the extension's."""

    return language == SEMANTIC_LANGUAGE_ID


__all__ = [
    "AnyMeasuredSchema",
    "GRAPH_MEDIA_TYPE",
    "OBJECT_REF_FIELD",
    "OBJECT_SELECT_EVENT",
    "SELECTION_CLEAR_EVENT",
    "SEMANTIC_LANGUAGE_ID",
    "SEMANTIC_SOURCE_KIND_MEDIA_TYPES",
    "SUPPORTED_LANGUAGE_IDS",
    "ActionOutputId",
    "EventFieldValue",
    "GraphConfig",
    "GraphEdge",
    "GraphNode",
    "GraphObservers",
    "GraphProjection",
    "GraphSchema",
    "GraphSource",
    "GraphSourceVersion",
    "HighlightObserve",
    "NullLiteralValue",
    "ObjectSelectEvent",
    "OutputSubscription",
    "RecordConfig",
    "RecordObserve",
    "RecordObservers",
    "RecordProjection",
    "ReferenceType",
    "SelectionClearEvent",
    "SemanticAction",
    "SemanticBinding",
    "SemanticBindingEffect",
    "SemanticBindingEvent",
    "SemanticCollection",
    "SemanticDocument",
    "SemanticDocumentProjection",
    "SemanticPlotProjection",
    "SemanticProjection",
    "SemanticReference",
    "SemanticSource",
    "SemanticState",
    "SemanticTableProjection",
    "StateSetEffect",
    "StateSetValue",
    "check_graph_sources",
    "check_output_subscriptions",
    "check_reference_state",
    "check_semantic_bindings",
    "check_semantic_collections",
    "check_semantic_projections",
    "document_diagnostics",
    "document_model_of",
    "is_output_subscription",
    "is_semantic_language",
    "is_subscription_wire",
    "parse_document_for_language",
    "parse_semantic_document",
    "semantic_document_diagnostics",
]
