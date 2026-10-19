"""Compiled plan models (SPEC.md section 11.1).

A plan is derived data: the host renders it, the service derives it
again from the document, and nothing persists native pointers or
compiler objects. Every model is frozen and rejects unknown fields, so
a plan that crosses a process boundary keeps its shape.

Every map is built with sorted keys, so equivalent canonical inputs
serialize to equal bytes (SPEC.md section 11.2).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BeforeValidator, Field, StrictBool, StrictStr

from melt.schema.commands import RegistrationContract
from melt.schema.document import (
    ActionInvokeEffect,
    ArtifactHash,
    Control,
    DocumentConfig,
    EntityId,
    LayoutNode,
    MeltModel,
    NonNegativeSafeInt,
    OpaqueId,
    ParameterConstraints,
    PlotConfig,
    PositiveSafeInt,
    SourceReference,
    TableConfig,
)
from melt.schema.semantic import (
    GraphConfig,
    OutputSubscription,
    RecordConfig,
    SemanticBindingEvent,
    StateSetEffect,
)

PLAN_VERSION = 1
"""The plan schema version of `melt/v0.1`."""


class PlanModel(MeltModel):
    """Base model of every plan object: frozen and closed."""

    model_config = dict(MeltModel.model_config, frozen=True)


def _dispatch_projection_config(value: object):
    """Validate a render configuration by its kind keys.

    Render entries carry the renderer configuration of their
    projection. The keys select the table, plot, document, graph, or
    record shape, which keeps union noise out of error paths.
    """

    if isinstance(
        value, (TableConfig, PlotConfig, DocumentConfig, GraphConfig, RecordConfig)
    ):
        return value
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    keys = set(value)
    branch: type | None
    if keys & {"columns", "selection"}:
        branch = TableConfig
    elif "fit" in keys:
        branch = PlotConfig
    elif "format" in keys:
        branch = DocumentConfig
    elif "nodeLabels" in keys:
        branch = GraphConfig
    elif "fields" in keys:
        branch = RecordConfig
    else:
        raise ValueError(
            "config matches no table, plot, document, graph, or record"
            " shape"
        )
    return branch.model_validate(value)


RenderConfig = Annotated[
    Union[
        TableConfig,
        PlotConfig,
        DocumentConfig,
        GraphConfig,
        RecordConfig,
    ],
    BeforeValidator(_dispatch_projection_config),
]


# ---------------------------------------------------------------------------
# Render plan
# ---------------------------------------------------------------------------


class RendererRef(PlanModel):
    """The renderer that handles one projection, with its version."""

    id: StrictStr
    contractVersion: PositiveSafeInt


class RenderObserver(PlanModel):
    """One observer slot of a projection: which state it reads.

    The slot name is fixed by the renderer contract; the state entry
    and the collection it resolves in come from the document (RFC 001
    section 6).
    """

    state: EntityId
    collection: EntityId


class RenderPlanEntry(PlanModel):
    """One projection lowered to its renderer contract.

    `collection` and `observers` carry the semantic selectors of
    `melt/semantic-draft-1`; both stay empty for a `melt/v0.1`
    projection. Under the extension language, `source` may carry one
    output subscription, which the service resolves against the
    output's current pointer at each commit sequence (RFC 001 8.1).
    """

    projection: EntityId
    renderer: RendererRef
    source: "SourceReference | OutputSubscription | None"
    config: RenderConfig
    controls: tuple[Control, ...]
    collection: EntityId | None = None
    observers: dict[StrictStr, RenderObserver] = Field(default_factory=dict)


class RenderPlan(PlanModel):
    """Renderer kind and version, source reference, configuration, and
    controls of every projection."""

    projections: dict[EntityId, RenderPlanEntry]


# ---------------------------------------------------------------------------
# State plan
# ---------------------------------------------------------------------------


class StateParameterPlan(PlanModel):
    """One parameter with its default and validation rules."""

    parameter: EntityId
    type: Literal["string", "number", "integer", "boolean"]
    default: "bool | float | str | None"
    nullable: StrictBool
    constraints: ParameterConstraints
    scope: Literal["workspace"]


class StateReferencePlan(PlanModel):
    """One reference state entry with its resolution rules
    (RFC 001 sections 4 and 9).

    `incarnation` is the opaque incarnation the service issued for the
    collection. It is present when the compiling caller knew it — the
    service always does — and null in a service-less compile, which
    cannot name an identity the service alone mints.
    """

    state: EntityId
    collection: EntityId
    key: EntityId
    source: SourceReference
    incarnation: StrictStr | None = None
    nullable: StrictBool
    scope: Literal["workspace"]


class StatePlan(PlanModel):
    """Parameter definitions, defaults, and validation rules.

    `references` carries the reference state entries of
    `melt/semantic-draft-1`; it stays empty for a `melt/v0.1` document.
    """

    parameters: dict[EntityId, StateParameterPlan]
    references: dict[EntityId, StateReferencePlan] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Interaction plan
# ---------------------------------------------------------------------------


class InteractionPlanEntry(PlanModel):
    """One binding with its debounce policy.

    The event and effect keep their v0.1 shapes for a `melt/v0.1`
    document; a `melt/semantic-draft-1` binding may instead observe a
    pure renderer event and assign state (RFC 001 section 5).
    """

    binding: EntityId
    event: SemanticBindingEvent
    effect: "ActionInvokeEffect | StateSetEffect"
    debounceMs: NonNegativeSafeInt


class InteractionPlan(PlanModel):
    """Event addresses, bindings, and debounce policy."""

    bindings: dict[EntityId, InteractionPlanEntry]
    eventAddresses: dict[StrictStr, tuple[EntityId, ...]]


# ---------------------------------------------------------------------------
# Action plan
# ---------------------------------------------------------------------------


class RegistrationRef(PlanModel):
    """The resolved registration of one action."""

    registrationId: OpaqueId
    revision: PositiveSafeInt
    contract: RegistrationContract
    requiredPermission: StrictStr


class ActionPlanEntry(PlanModel):
    """One action with its resolved registration and targets.

    `targets` carries the projection targets of a `melt/v0.1` action.
    An action of `melt/semantic-draft-1` owns no targets: `incarnation`
    carries the stable output identity the service issued instead, and
    projections subscribe through it (RFC 001 sections 8.1 and 9).
    """

    action: EntityId
    registration: RegistrationRef
    parameters: dict[EntityId, EntityId]
    inputs: dict[EntityId, SourceReference]
    outputs: dict[EntityId, StrictStr]
    outputGroup: EntityId
    targets: dict[EntityId, EntityId] = Field(default_factory=dict)
    incarnation: StrictStr | None = None


class OutputGroupPlan(PlanModel):
    """One output group with its owning action and target projections.

    `targets` stays empty for the extension language, whose groups own
    outputs rather than panes (RFC 001 section 8.2)."""

    action: EntityId
    targets: dict[EntityId, EntityId] = Field(default_factory=dict)


class ActionPlan(PlanModel):
    """Resolved registrations, mappings, targets, and permissions."""

    actions: dict[EntityId, ActionPlanEntry]
    outputGroups: dict[EntityId, OutputGroupPlan]


# ---------------------------------------------------------------------------
# Dependency plan
# ---------------------------------------------------------------------------


class SourceVersionReaders(PlanModel):
    """The entities that read one immutable source version."""

    readers: tuple[StrictStr, ...]


class ParameterDependency(PlanModel):
    """The entities that read one parameter."""

    readers: tuple[StrictStr, ...]


class ActionDependency(PlanModel):
    """The references one action depends on for invalidation."""

    registrationId: OpaqueId
    parameters: tuple[EntityId, ...]
    inputs: tuple[SourceReference, ...]
    targets: tuple[EntityId, ...] = ()


class OutputSubscriber(PlanModel):
    """One projection that subscribes to one action output.

    A following subscriber advances with each successful publication of
    the group; a pinned subscriber freezes one version of the output's
    stable logical source and reports its version status (RFC 001
    section 8.1).
    """

    projection: EntityId
    follow: Literal["current", "pinned"]
    version: PositiveSafeInt | None = None


class OutputDependency(PlanModel):
    """One stable output identity and every projection subscribed to it.

    The output-to-subscriber index of RFC 001 section 9: an action with
    no subscribers can still run and publish, and removing a subscriber
    changes no publication eligibility.
    """

    action: EntityId
    output: EntityId
    kind: StrictStr
    incarnation: StrictStr | None = None
    subscribers: tuple[OutputSubscriber, ...] = ()


class StateObserver(PlanModel):
    """One projection that observes one reference state entry.

    The dependency index runs state to observer: when the entry
    changes, exactly these projections need a new render (RFC 001
    sections 6 and 9). Adding an observer changes this index; it
    increments no state revision and touches no binding.
    """

    projection: EntityId
    slot: StrictStr
    state: EntityId
    collection: EntityId


class ReferenceStateDependency(PlanModel):
    """Every observer of one reference state entry."""

    observers: tuple[StateObserver, ...]


class DependencyPlan(PlanModel):
    """Reference indexes for invalidation and incremental compilation.

    `referenceStates` carries the state-to-observer index and `outputs`
    the output-to-subscriber index of RFC 001 section 9; both stay
    empty for a `melt/v0.1` document.
    """

    sources: dict[EntityId, dict[StrictStr, SourceVersionReaders]]
    parameters: dict[EntityId, ParameterDependency]
    actions: dict[EntityId, ActionDependency]
    referenceStates: dict[EntityId, ReferenceStateDependency] = Field(
        default_factory=dict
    )
    outputs: dict[StrictStr, OutputDependency] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Identity plan
# ---------------------------------------------------------------------------


class PaneIdentity(PlanModel):
    """One pane identity: projection IDs also identify panes."""

    projection: EntityId
    pane: EntityId
    kind: StrictStr
    outputGroup: EntityId | None = None


class ControlIdentity(PlanModel):
    """One control-to-parameter mapping."""

    projection: EntityId
    control: EntityId
    parameter: EntityId


class GraphNodeIdentity(PlanModel):
    """One graph node: its visual element and its semantic object.

    Node IDs identify visual graph elements; semantic references
    identify domain objects. The two stay separate, and several nodes
    may represent the same object (RFC 001 section 7.1).
    """

    graph: EntityId
    node: EntityId
    collection: EntityId
    key: StrictStr


class IdentityPlan(PlanModel):
    """Projection-to-pane and control-to-parameter mappings.

    `nodes` carries the graph identity map of `melt/semantic-draft-1`;
    it stays empty for a `melt/v0.1` document.
    """

    panes: dict[EntityId, PaneIdentity]
    controls: dict[StrictStr, ControlIdentity]
    nodes: dict[StrictStr, GraphNodeIdentity] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Layout plan
# ---------------------------------------------------------------------------


class Placement(PlanModel):
    """Where one projection sits in the validated layout."""

    placement: Literal["tree", "hidden"]
    path: StrictStr


class LayoutPlan(PlanModel):
    """The validated tree and the hidden projection IDs."""

    root: LayoutNode | None
    hidden: tuple[EntityId, ...]
    placements: dict[EntityId, Placement]


# ---------------------------------------------------------------------------
# Compiled plan
# ---------------------------------------------------------------------------


class CompiledPlan(PlanModel):
    """The immutable result of one successful compilation.

    `planKey` covers the document hash, the compiler version, the
    renderer contract versions, and the revisions of every registration
    the plan resolved (SPEC.md section 4.2). Equivalent canonical
    inputs with equal registry versions produce equal keys and equal
    plans.
    """

    planVersion: Literal[PLAN_VERSION]
    language: Literal["melt/v0.1", "melt/semantic-draft-1"]
    compilerVersion: StrictStr
    workspaceId: EntityId
    documentHash: ArtifactHash
    planKey: ArtifactHash
    renderers: dict[StrictStr, RendererRef]
    registrations: dict[OpaqueId, PositiveSafeInt]
    render: RenderPlan
    state: StatePlan
    interaction: InteractionPlan
    action: ActionPlan
    dependency: DependencyPlan
    identity: IdentityPlan
    layout: LayoutPlan


__all__ = [
    "ActionDependency",
    "ActionPlan",
    "ActionPlanEntry",
    "CompiledPlan",
    "ControlIdentity",
    "DependencyPlan",
    "GraphNodeIdentity",
    "IdentityPlan",
    "InteractionPlan",
    "InteractionPlanEntry",
    "LayoutPlan",
    "OutputDependency",
    "OutputGroupPlan",
    "OutputSubscriber",
    "PLAN_VERSION",
    "PaneIdentity",
    "ParameterDependency",
    "Placement",
    "ReferenceStateDependency",
    "RegistrationRef",
    "RenderConfig",
    "RenderObserver",
    "RenderPlan",
    "RenderPlanEntry",
    "RendererRef",
    "SourceVersionReaders",
    "StateObserver",
    "StateParameterPlan",
    "StatePlan",
    "StateReferencePlan",
]
