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
    BindingEvent,
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

PLAN_VERSION = 1
"""The plan schema version of `melt/v0.1`."""


class PlanModel(MeltModel):
    """Base model of every plan object: frozen and closed."""

    model_config = dict(MeltModel.model_config, frozen=True)


def _dispatch_projection_config(value: object):
    """Validate a render configuration by its kind keys.

    Render entries carry the renderer configuration of their
    projection. The keys select the table, plot, or document shape,
    which keeps union noise out of error paths.
    """

    if isinstance(value, (TableConfig, PlotConfig, DocumentConfig)):
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
    else:
        raise ValueError("config matches no table, plot, or document shape")
    return branch.model_validate(value)


RenderConfig = Annotated[
    Union[TableConfig, PlotConfig, DocumentConfig],
    BeforeValidator(_dispatch_projection_config),
]


# ---------------------------------------------------------------------------
# Render plan
# ---------------------------------------------------------------------------


class RendererRef(PlanModel):
    """The renderer that handles one projection, with its version."""

    id: StrictStr
    contractVersion: PositiveSafeInt


class RenderPlanEntry(PlanModel):
    """One projection lowered to its renderer contract."""

    projection: EntityId
    renderer: RendererRef
    source: SourceReference | None
    config: RenderConfig
    controls: tuple[Control, ...]


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


class StatePlan(PlanModel):
    """Parameter definitions, defaults, and validation rules."""

    parameters: dict[EntityId, StateParameterPlan]


# ---------------------------------------------------------------------------
# Interaction plan
# ---------------------------------------------------------------------------


class InteractionPlanEntry(PlanModel):
    """One binding with its debounce policy."""

    binding: EntityId
    event: BindingEvent
    effect: ActionInvokeEffect
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
    """One action with its resolved registration and targets."""

    action: EntityId
    registration: RegistrationRef
    parameters: dict[EntityId, EntityId]
    inputs: dict[EntityId, SourceReference]
    outputs: dict[EntityId, StrictStr]
    outputGroup: EntityId
    targets: dict[EntityId, EntityId]


class OutputGroupPlan(PlanModel):
    """One output group with its owning action and target projections."""

    action: EntityId
    targets: dict[EntityId, EntityId]


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
    targets: tuple[EntityId, ...]


class DependencyPlan(PlanModel):
    """Reference indexes for invalidation and incremental compilation."""

    sources: dict[EntityId, dict[StrictStr, SourceVersionReaders]]
    parameters: dict[EntityId, ParameterDependency]
    actions: dict[EntityId, ActionDependency]


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


class IdentityPlan(PlanModel):
    """Projection-to-pane and control-to-parameter mappings."""

    panes: dict[EntityId, PaneIdentity]
    controls: dict[StrictStr, ControlIdentity]


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
    language: Literal["melt/v0.1"]
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
    "InteractionPlan",
    "InteractionPlanEntry",
    "LayoutPlan",
    "OutputGroupPlan",
    "PLAN_VERSION",
    "PaneIdentity",
    "ParameterDependency",
    "Placement",
    "RegistrationRef",
    "RenderConfig",
    "RenderPlan",
    "RenderPlanEntry",
    "RendererRef",
    "SourceVersionReaders",
    "StateParameterPlan",
    "StatePlan",
]
