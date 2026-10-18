"""Compiler stages, checks, and plan construction (SPEC.md section 11).

The compiler runs seven stages:

1. Decode and reject malformed or oversized input.
2. Validate structural schemas and language version.
3. Resolve source, parameter, action, control, and layout references.
4. Check types, output ownership, and renderer compatibility.
5. Validate state continuity and native policy constraints.
6. Build dependency indexes and compiled plans.
7. Return the plan and deterministic diagnostics.

It returns either sorted diagnostics or an immutable plan, never both.
Compilation performs no project execution, network access, package
installation, or permission grant. It reads trusted catalog metadata
only (SPEC.md section 11.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from melt.schema.canonical import (
    canonical_hash,
    document_hash,
    model_to_wire,
    parse_json,
)
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostic_from_pydantic_error,
    sorted_diagnostics,
)
from melt.schema.document import (
    Binding,
    ButtonControl,
    Document,
    TableProjection,
    document_semantic_diagnostics,
)
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits
from runtime.compiler.catalog import (
    ActionRegistry,
    RegistrationEntry,
    SourceCatalog,
)
from runtime.compiler.plans import (
    ActionDependency,
    ActionPlan,
    ActionPlanEntry,
    CompiledPlan,
    ControlIdentity,
    DependencyPlan,
    IdentityPlan,
    InteractionPlan,
    InteractionPlanEntry,
    LayoutPlan,
    OutputGroupPlan,
    PLAN_VERSION,
    PaneIdentity,
    ParameterDependency,
    Placement,
    RegistrationRef,
    RenderPlan,
    RenderPlanEntry,
    RendererRef,
    SourceVersionReaders,
    StateParameterPlan,
    StatePlan,
)

COMPILER_VERSION = "0.1.0"
COMPILER_IDENTIFIER = f"melt-compiler/{COMPILER_VERSION}"
"""The compiler version that plan keys cover (SPEC.md section 4.2)."""


# ---------------------------------------------------------------------------
# Renderer registry (SPEC.md sections 8 and 11.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RendererSpec:
    """One trusted built-in renderer of the v0.1 host."""

    kind: str
    id: str
    contract_version: int
    source_kind: str


RENDERER_REGISTRY: dict[str, RendererSpec] = {
    spec.kind: spec
    for spec in (
        RendererSpec("table", "melt.table", 1, "table"),
        RendererSpec("plot", "melt.plot", 1, "image"),
        RendererSpec("document", "melt.document", 1, "text"),
    )
}
"""The versioned renderer registry that v0.1 ships in the host."""


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterStateInput:
    """One current parameter value with its state revision (6.2)."""

    value: "bool | float | str | None"
    revision: int


class MeltCompileError(MeltSchemaError):
    """Raised for malformed compiler-side inputs.

    A malformed catalog or state input is an operational failure: the
    caller supplied bad service data, not an invalid document.
    """


@dataclass
class CompileRequest:
    """The inputs of one compilation (SPEC.md section 11.1).

    Permission grants are not compiler inputs. `parameter_states`
    carries the current values the service wants validated against the
    candidate definitions.
    """

    document: "str | bytes | bytearray | dict | Document"
    source_catalog: SourceCatalog = field(default_factory=SourceCatalog)
    action_registry: ActionRegistry = field(default_factory=ActionRegistry)
    parameter_states: Mapping[str, ParameterStateInput] = field(default_factory=dict)
    limits: ServiceLimits = DEFAULT_LIMITS
    max_input_bytes: int = DEFAULT_LIMITS.control_frame_bytes


@dataclass(frozen=True)
class CompileResult:
    """Either sorted diagnostics or an immutable compiled plan."""

    plan: CompiledPlan | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return self.plan is not None


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


# ---------------------------------------------------------------------------
# Stage 1: decode
# ---------------------------------------------------------------------------


def stage_decode(
    data: "str | bytes | bytearray | dict | Document", max_input_bytes: int
) -> tuple["Any | None", list[Diagnostic]]:
    """Decode input and reject malformed or oversized text."""

    if isinstance(data, Document):
        return data, []
    if isinstance(data, (bytes, bytearray)):
        size = len(data)
    elif isinstance(data, str):
        size = len(data.encode("utf-8"))
    else:
        size = 0
    if size > max_input_bytes:
        return None, [
            _diagnostic(
                DiagnosticCode.LIMIT,
                "",
                f"compiler input of {size} bytes exceeds the allowed"
                f" bound of {max_input_bytes} bytes",
                hint="Split the document, or raise the compiler input policy.",
            )
        ]
    if isinstance(data, dict):
        return data, []
    try:
        return parse_json(data), []
    except MeltSchemaError as error:
        return None, list(error.diagnostics)


# ---------------------------------------------------------------------------
# Stage 2: structural schemas and language version
# ---------------------------------------------------------------------------


def stage_structure(value: Any) -> tuple[Document | None, list[Diagnostic]]:
    """Validate the document structure and its language version."""

    if isinstance(value, Document):
        return value, []
    try:
        return Document.model_validate(value), []
    except ValidationError as error:
        return None, [
            diagnostic_from_pydantic_error(item, DiagnosticCode.SCHEMA)
            for item in error.errors()
        ]


# ---------------------------------------------------------------------------
# Stage 3: reference resolution
# ---------------------------------------------------------------------------


def _resolve_registrations(
    document: Document, registry: ActionRegistry
) -> list[Diagnostic]:
    """Resolve every action implementation against the registry."""

    diagnostics: list[Diagnostic] = []
    for action_id, action in sorted(document.actions.items()):
        path = f"/actions/{action_id}"
        entry = registry.entry(action.implementation.registrationId)
        if entry is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/implementation/registrationId",
                    f"unknown registration"
                    f" {action.implementation.registrationId!r}",
                    hint="Register the script through the service, or use an"
                    " existing registration id.",
                )
            )
            continue
        contract = entry.contract
        mapped_parameters = set(action.parameters)
        declared_parameters = set(contract.parameters)
        for name in sorted(mapped_parameters - declared_parameters):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/parameters/{name}",
                    f"registration {entry.registration_id!r} declares no"
                    f" parameter {name!r}",
                    related_paths=[f"{path}/parameters"],
                    hint="Align the action mapping with the registration"
                    " contract.",
                )
            )
        for name in sorted(declared_parameters - mapped_parameters):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/parameters",
                    f"action does not map registration parameter {name!r}",
                    hint="Map every parameter the registration declares.",
                )
            )
        mapped_inputs = set(action.inputs)
        declared_inputs = set(contract.inputs)
        for name in sorted(mapped_inputs - declared_inputs):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/inputs/{name}",
                    f"registration {entry.registration_id!r} declares no"
                    f" input {name!r}",
                    related_paths=[f"{path}/inputs"],
                    hint="Align the action mapping with the registration"
                    " contract.",
                )
            )
        for name in sorted(declared_inputs - mapped_inputs):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/inputs",
                    f"action does not map registration input {name!r}",
                    hint="Map every input the registration declares.",
                )
            )
        mapped_outputs = set(action.outputs)
        declared_outputs = set(contract.outputs)
        for name in sorted(mapped_outputs - declared_outputs):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/outputs/{name}",
                    f"registration {entry.registration_id!r} declares no"
                    f" output {name!r}",
                    related_paths=[f"{path}/outputs"],
                    hint="Align the action mapping with the registration"
                    " contract.",
                )
            )
        for name in sorted(declared_outputs - mapped_outputs):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REGISTRATION,
                    f"{path}/outputs",
                    f"action does not declare registration output {name!r}",
                    hint="Declare every output the registration produces.",
                )
            )
    return diagnostics


def _resolve_artifacts(
    document: Document, catalog: SourceCatalog
) -> list[Diagnostic]:
    """Resolve every source version against the trusted catalog."""

    diagnostics: list[Diagnostic] = []
    for source_id, source in sorted(document.sources.items()):
        for version_key, version in sorted(source.versions.items()):
            path = f"/sources/{source_id}/versions/{version_key}"
            entry = catalog.entry(version.artifact)
            if entry is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        f"{path}/artifact",
                        "artifact is not in the trusted catalog",
                        hint="Import the artifact through the service so the"
                        " catalog records it.",
                    )
                )
                continue
            if source.kind != entry.kind:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        f"{path}/schema",
                        f"source declares kind {source.kind!r} but the"
                        f" catalog measures {entry.kind!r}",
                        hint="Correct the declared kind or reimport the"
                        " artifact.",
                    )
                )
            if version.byteLength != entry.byteLength:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        f"{path}/byteLength",
                        f"declared byte length {version.byteLength} differs"
                        f" from the catalog length {entry.byteLength}",
                        hint="Reimport the source so the declared length"
                        " matches the artifact bytes.",
                    )
                )
            if version.mediaType != entry.mediaType:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        f"{path}/mediaType",
                        f"declared media type {version.mediaType!r} differs"
                        f" from the catalog type {entry.mediaType!r}",
                        hint="Reimport the source so the declared media type"
                        " matches the artifact bytes.",
                    )
                )
            if model_to_wire(version.schema) != model_to_wire(entry.schema):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.ARTIFACT,
                        f"{path}/schema",
                        "declared schema differs from the schema the catalog"
                        " measured from the artifact bytes",
                        hint="Reimport the source, or correct the declared"
                        " schema to match the artifact.",
                    )
                )
    return diagnostics


def stage_references(
    document: Document, catalog: SourceCatalog, registry: ActionRegistry
) -> list[Diagnostic]:
    """Resolve document, catalog, and registry references."""

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(document_semantic_diagnostics(document))
    diagnostics.extend(_resolve_registrations(document, registry))
    diagnostics.extend(_resolve_artifacts(document, catalog))
    return diagnostics


# ---------------------------------------------------------------------------
# Stage 4: types, output ownership, renderer compatibility
# ---------------------------------------------------------------------------


def _constraints_fit(parameter, entry) -> "str | None":
    """Return a mismatch message, or None when the mapping fits.

    The workspace parameter must accept only values the registration
    accepts: constraints may narrow the registration, never widen it
    (SPEC.md section 7.2).
    """

    if parameter.type != entry.type:
        return (
            f"parameter type {parameter.type!r} does not match the"
            f" registration type {entry.type!r}"
        )
    if parameter.nullable and not entry.nullable:
        return "a nullable parameter cannot feed a non-nullable slot"
    constraints = parameter.constraints
    accepted = entry.constraints
    if accepted is None:
        return None
    if constraints.minimum is not None and accepted.minimum is not None:
        if constraints.minimum < accepted.minimum:
            return (
                f"minimum {constraints.minimum!r} falls below the"
                f" registration minimum {accepted.minimum!r}"
            )
    if constraints.maximum is not None and accepted.maximum is not None:
        if constraints.maximum > accepted.maximum:
            return (
                f"maximum {constraints.maximum!r} exceeds the registration"
                f" maximum {accepted.maximum!r}"
            )
    if accepted.enum is not None:
        if constraints.enum is None:
            return "an unconstrained parameter cannot feed an enum slot"
        outside = sorted(set(constraints.enum) - set(accepted.enum))
        if outside:
            return (
                "enum accepts values outside the registration enum:"
                f" {', '.join(repr(item) for item in outside)}"
            )
    if (
        constraints.maxLength is not None
        and accepted.maxLength is not None
        and constraints.maxLength > accepted.maxLength
    ):
        return (
            f"maxLength {constraints.maxLength} exceeds the registration"
            f" maxLength {accepted.maxLength}"
        )
    return None


def _check_registration_types(
    document: Document, registry: ActionRegistry
) -> list[Diagnostic]:
    """Check parameter, input, and output kinds against contracts."""

    diagnostics: list[Diagnostic] = []
    for action_id, action in sorted(document.actions.items()):
        path = f"/actions/{action_id}"
        entry = registry.entry(action.implementation.registrationId)
        if entry is None:
            continue
        contract = entry.contract
        for name, mapping in sorted(action.parameters.items()):
            slot = contract.parameters.get(name)
            if slot is None:
                continue
            parameter = document.parameters.get(mapping.parameter)
            if parameter is None:
                continue
            mismatch = _constraints_fit(parameter, slot)
            if mismatch is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REGISTRATION,
                        f"{path}/parameters/{name}",
                        f"mapping of parameter {mapping.parameter!r} does"
                        f" not fit the registration: {mismatch}",
                        related_paths=[f"/parameters/{mapping.parameter}"],
                        hint="Narrow the workspace parameter, or register a"
                        " contract that accepts its values.",
                    )
                )
        for name, reference in sorted(action.inputs.items()):
            expected = contract.inputs.get(name)
            if expected is None:
                continue
            source = document.sources.get(reference.sourceId)
            if source is not None and source.kind != expected:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REGISTRATION,
                        f"{path}/inputs/{name}",
                        f"input {name!r} carries source kind {source.kind!r}"
                        f" but the registration accepts {expected!r}",
                        hint="Map the input to a source of the declared"
                        " kind.",
                    )
                )
        for name, kind in sorted(action.outputs.items()):
            expected = contract.outputs.get(name)
            if expected is not None and kind != expected:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.REGISTRATION,
                        f"{path}/outputs/{name}",
                        f"output {name!r} declares kind {kind!r} but the"
                        f" registration produces {expected!r}",
                        hint="Match the declared output kind to the"
                        " registration contract.",
                    )
                )
    return diagnostics


def _check_renderer_compatibility(document: Document) -> list[Diagnostic]:
    """Check every projection against the renderer registry."""

    diagnostics: list[Diagnostic] = []
    for projection_id, projection in sorted(document.projections.items()):
        spec = RENDERER_REGISTRY.get(projection.kind)
        if spec is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"/projections/{projection_id}/kind",
                    f"no renderer in the registry handles kind"
                    f" {projection.kind!r}",
                    hint="Use one of table, plot, or document.",
                )
            )
    return diagnostics


def stage_types(
    document: Document, registry: ActionRegistry
) -> list[Diagnostic]:
    """Check types, output ownership, and renderer compatibility.

    Output ownership itself is part of the document semantic pass;
    this stage adds the registration and renderer checks.
    """

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_check_registration_types(document, registry))
    diagnostics.extend(_check_renderer_compatibility(document))
    return diagnostics


# ---------------------------------------------------------------------------
# Stage 5: state continuity and native policy
# ---------------------------------------------------------------------------


def parameter_value_error(parameter, value: Any) -> "str | None":
    """Return a message when a value violates a parameter definition.

    The workspace service reuses this check when it validates candidate
    parameter state, so a committed value and a compiled plan agree on
    one definition of validity (SPEC.md sections 6.1 and 12.3).
    """

    if value is None:
        if not parameter.nullable:
            return "null value requires nullable to be true"
        return None
    type_matches = {
        "boolean": lambda item: isinstance(item, bool),
        "number": lambda item: isinstance(item, float),
        "integer": lambda item: isinstance(item, float) and item.is_integer(),
        "string": lambda item: isinstance(item, str),
    }[parameter.type]
    if not type_matches(value):
        return f"value {value!r} does not satisfy type {parameter.type!r}"
    constraints = parameter.constraints
    if parameter.type in ("number", "integer"):
        if constraints.minimum is not None and value < constraints.minimum:
            return f"value {value!r} is below the minimum {constraints.minimum!r}"
        if constraints.maximum is not None and value > constraints.maximum:
            return f"value {value!r} is above the maximum {constraints.maximum!r}"
    if parameter.type == "string":
        if (
            constraints.maxLength is not None
            and len(value) > constraints.maxLength
        ):
            return f"value is longer than maxLength {constraints.maxLength}"
        if constraints.enum is not None and value not in constraints.enum:
            return "value is not one of the enum values"
    return None


def stage_state(
    document: Document,
    parameter_states: Mapping[str, ParameterStateInput],
    limits: ServiceLimits,
) -> list[Diagnostic]:
    """Validate state continuity and native policy constraints."""

    diagnostics: list[Diagnostic] = []
    for parameter_id in sorted(parameter_states):
        state = parameter_states[parameter_id]
        parameter = document.parameters.get(parameter_id)
        if parameter is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STATE_MIGRATION,
                    f"/parameters/{parameter_id}",
                    "state exists for a parameter the document does not"
                    " define",
                    hint="Remove the state with the parameter, or recreate"
                    " the parameter definition.",
                )
            )
            continue
        message = parameter_value_error(parameter, state.value)
        if message is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STATE_MIGRATION,
                    f"/parameters/{parameter_id}",
                    f"current state violates the definition: {message}",
                    hint="Supply parameter.set with a valid replacement and"
                    " the expected state revision in the same transaction.",
                )
            )
    for projection_id, projection in sorted(document.projections.items()):
        if isinstance(projection, TableProjection) and (
            projection.config.columns is not None
            and len(projection.config.columns) > limits.table_page_columns
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.LIMIT,
                    f"/projections/{projection_id}/config/columns",
                    "configured table columns exceed the allowed bound of"
                    f" {limits.table_page_columns}",
                    hint="Select fewer columns, or page them explicitly.",
                )
            )
    return diagnostics


# ---------------------------------------------------------------------------
# Stage 6: dependency indexes and plans
# ---------------------------------------------------------------------------


def _renderer_ref(spec: RendererSpec) -> RendererRef:
    return RendererRef(id=spec.id, contractVersion=spec.contract_version)


def _build_render_plan(document: Document) -> RenderPlan:
    entries: dict[str, RenderPlanEntry] = {}
    for projection_id, projection in sorted(document.projections.items()):
        spec = RENDERER_REGISTRY[projection.kind]
        entries[projection_id] = RenderPlanEntry(
            projection=projection_id,
            renderer=_renderer_ref(spec),
            source=projection.source,
            config=projection.config,
            controls=projection.controls,
        )
    return RenderPlan(projections=entries)


def _build_state_plan(document: Document) -> StatePlan:
    entries: dict[str, StateParameterPlan] = {}
    for parameter_id, parameter in sorted(document.parameters.items()):
        entries[parameter_id] = StateParameterPlan(
            parameter=parameter_id,
            type=parameter.type,
            default=parameter.default,
            nullable=parameter.nullable,
            constraints=parameter.constraints,
            scope="workspace",
        )
    return StatePlan(parameters=entries)


def _event_address(binding: Binding) -> str:
    event = binding.event
    kind = event.kind
    if kind == "parameter.changed":
        return f"parameter.changed/{event.parameter}"
    return f"control.activate/{event.projection}/{event.control}"


def _build_interaction_plan(document: Document) -> InteractionPlan:
    entries: dict[str, InteractionPlanEntry] = {}
    addresses: dict[str, list[str]] = {}
    for binding_id, binding in sorted(document.bindings.items()):
        entries[binding_id] = InteractionPlanEntry(
            binding=binding_id,
            event=binding.event,
            effect=binding.effect,
            debounceMs=binding.debounceMs,
        )
        addresses.setdefault(_event_address(binding), []).append(binding_id)
    return InteractionPlan(
        bindings=entries,
        eventAddresses={
            address: tuple(sorted(binding_ids))
            for address, binding_ids in sorted(addresses.items())
        },
    )


def _build_action_plan(
    document: Document, registry: ActionRegistry
) -> ActionPlan:
    entries: dict[str, ActionPlanEntry] = {}
    groups: dict[str, OutputGroupPlan] = {}
    for action_id, action in sorted(document.actions.items()):
        entry: RegistrationEntry = registry.entry(action.implementation.registrationId)
        entries[action_id] = ActionPlanEntry(
            action=action_id,
            registration=RegistrationRef(
                registrationId=entry.registration_id,
                revision=entry.revision,
                contract=entry.contract,
                requiredPermission=entry.required_permission,
            ),
            parameters={
                name: mapping.parameter
                for name, mapping in sorted(action.parameters.items())
            },
            inputs=dict(sorted(action.inputs.items())),
            outputs=dict(sorted(action.outputs.items())),
            outputGroup=action.outputGroup,
            targets=dict(sorted(action.targets.items())),
        )
        groups[action.outputGroup] = OutputGroupPlan(
            action=action_id,
            targets=dict(sorted(action.targets.items())),
        )
    return ActionPlan(actions=entries, outputGroups=groups)


def _build_dependency_plan(document: Document) -> DependencyPlan:
    sources: dict[str, dict[str, list[str]]] = {}
    parameters: dict[str, list[str]] = {}

    def _read(source_id: str, version: int, reader: str) -> None:
        versions = sources.setdefault(source_id, {})
        versions.setdefault(str(version), []).append(reader)

    for projection_id, projection in sorted(document.projections.items()):
        if projection.source is not None:
            _read(
                projection.source.sourceId,
                projection.source.version,
                f"projections/{projection_id}",
            )
        for control in projection.controls:
            if not isinstance(control, ButtonControl):
                parameters.setdefault(control.parameter, []).append(
                    f"projections/{projection_id}"
                )
    actions: dict[str, ActionDependency] = {}
    for action_id, action in sorted(document.actions.items()):
        for reference in action.inputs.values():
            _read(
                reference.sourceId,
                reference.version,
                f"actions/{action_id}",
            )
        for mapping in action.parameters.values():
            parameters.setdefault(mapping.parameter, []).append(
                f"actions/{action_id}"
            )
        actions[action_id] = ActionDependency(
            registrationId=action.implementation.registrationId,
            parameters=tuple(
                sorted(mapping.parameter for mapping in action.parameters.values())
            ),
            inputs=tuple(sorted(action.inputs.values(), key=_source_sort_key)),
            targets=tuple(sorted(action.targets.values())),
        )
    for binding_id, binding in sorted(document.bindings.items()):
        event = binding.event
        if event.kind == "parameter.changed":
            parameters.setdefault(event.parameter, []).append(
                f"bindings/{binding_id}"
            )
    return DependencyPlan(
        sources={
            source_id: {
                version: SourceVersionReaders(readers=tuple(sorted(readers)))
                for version, readers in sorted(versions.items())
            }
            for source_id, versions in sorted(sources.items())
        },
        parameters={
            parameter_id: ParameterDependency(readers=tuple(sorted(readers)))
            for parameter_id, readers in sorted(parameters.items())
        },
        actions=actions,
    )


def _source_sort_key(reference) -> tuple:
    return reference.sourceId, reference.version


def _build_identity_plan(document: Document) -> IdentityPlan:
    panes: dict[str, PaneIdentity] = {}
    controls: dict[str, ControlIdentity] = {}
    for projection_id, projection in sorted(document.projections.items()):
        owner: "str | None" = None
        for action in document.actions.values():
            if projection_id in action.targets.values():
                owner = action.outputGroup
                break
        panes[projection_id] = PaneIdentity(
            projection=projection_id,
            pane=projection_id,
            kind=projection.kind,
            outputGroup=owner,
        )
        for control in sorted(projection.controls, key=lambda item: item.id):
            if isinstance(control, ButtonControl):
                continue
            controls[f"{projection_id}/{control.id}"] = ControlIdentity(
                projection=projection_id,
                control=control.id,
                parameter=control.parameter,
            )
    return IdentityPlan(panes=panes, controls=controls)


def _walk_placements(node, path: str, placements: dict) -> None:
    if node.kind == "pane":
        placements[node.projection] = Placement(placement="tree", path=path)
        return
    _walk_placements(node.first, f"{path}/first", placements)
    _walk_placements(node.second, f"{path}/second", placements)


def _build_layout_plan(document: Document) -> LayoutPlan:
    placements: dict[str, Placement] = {}
    if document.layout.root is not None:
        _walk_placements(document.layout.root, "/layout/root", placements)
    for index, projection_id in enumerate(document.layout.hidden):
        placements[projection_id] = Placement(
            placement="hidden", path=f"/layout/hidden/{index}"
        )
    return LayoutPlan(
        root=document.layout.root,
        hidden=tuple(document.layout.hidden),
        placements=dict(sorted(placements.items())),
    )


def _document_hash(document: Document) -> str:
    return document_hash(document)


def _plan_key(document: Document, registry: ActionRegistry) -> str:
    registrations = {
        action.implementation.registrationId: registry.entry(
            action.implementation.registrationId
        ).revision
        for action in sorted(
            document.actions.values(),
            key=lambda item: item.implementation.registrationId,
        )
    }
    material = {
        "compilerVersion": COMPILER_IDENTIFIER,
        "documentHash": _document_hash(document),
        "renderers": {
            kind: {"id": spec.id, "contractVersion": spec.contract_version}
            for kind, spec in sorted(RENDERER_REGISTRY.items())
        },
        "registrations": registrations,
    }
    return canonical_hash(material)


def build_plan(document: Document, registry: ActionRegistry) -> CompiledPlan:
    """Build every plan from a fully validated document (stage 6)."""

    return CompiledPlan(
        planVersion=PLAN_VERSION,
        language=document.language,
        compilerVersion=COMPILER_IDENTIFIER,
        workspaceId=document.workspaceId,
        documentHash=_document_hash(document),
        planKey=_plan_key(document, registry),
        renderers={
            kind: _renderer_ref(spec)
            for kind, spec in sorted(RENDERER_REGISTRY.items())
        },
        registrations={
            action.implementation.registrationId: registry.entry(
                action.implementation.registrationId
            ).revision
            for action in sorted(
                document.actions.values(),
                key=lambda item: item.implementation.registrationId,
            )
        },
        render=_build_render_plan(document),
        state=_build_state_plan(document),
        interaction=_build_interaction_plan(document),
        action=_build_action_plan(document, registry),
        dependency=_build_dependency_plan(document),
        identity=_build_identity_plan(document),
        layout=_build_layout_plan(document),
    )


# ---------------------------------------------------------------------------
# Stage 7: the compile entry point
# ---------------------------------------------------------------------------


def _normalize_states(
    parameter_states: Mapping[str, Any]
) -> dict[str, ParameterStateInput]:
    states: dict[str, ParameterStateInput] = {}
    for parameter_id, state in parameter_states.items():
        if isinstance(state, ParameterStateInput):
            states[parameter_id] = state
            continue
        if isinstance(state, Mapping) and "value" in state and "revision" in state:
            states[parameter_id] = ParameterStateInput(
                value=state["value"], revision=state["revision"]
            )
            continue
        raise MeltCompileError(
            (
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"/state/{parameter_id}",
                    "parameter state input needs a value and a revision",
                ),
            )
        )
    return states


def compile_document(request: CompileRequest) -> CompileResult:
    """Run the seven stages and return diagnostics or a plan."""

    states = _normalize_states(request.parameter_states)
    diagnostics: list[Diagnostic] = []

    value, stage_diagnostics = stage_decode(request.document, request.max_input_bytes)
    diagnostics.extend(stage_diagnostics)
    document = None
    if value is not None:
        document, stage_diagnostics = stage_structure(value)
        diagnostics.extend(stage_diagnostics)
    if document is not None:
        diagnostics.extend(
            stage_references(document, request.source_catalog, request.action_registry)
        )
        diagnostics.extend(stage_types(document, request.action_registry))
        diagnostics.extend(stage_state(document, states, request.limits))
    if diagnostics or document is None:
        return CompileResult(None, tuple(sorted_diagnostics(diagnostics)))
    return CompileResult(build_plan(document, request.action_registry), ())


def compile_json(
    data: "str | bytes | bytearray | dict | Document",
    catalog: "SourceCatalog | None" = None,
    registry: "ActionRegistry | None" = None,
    parameter_states: "Mapping[str, Any] | None" = None,
) -> CompileResult:
    """Compile one document against optional catalog and registry."""

    request = CompileRequest(
        document=data,
        source_catalog=catalog or SourceCatalog(),
        action_registry=registry or ActionRegistry(),
        parameter_states=_normalize_states(parameter_states or {}),
    )
    return compile_document(request)


__all__ = [
    "COMPILER_IDENTIFIER",
    "COMPILER_VERSION",
    "CompileRequest",
    "CompileResult",
    "MeltCompileError",
    "ParameterStateInput",
    "RENDERER_REGISTRY",
    "RendererSpec",
    "build_plan",
    "compile_document",
    "compile_json",
    "parameter_value_error",
    "stage_decode",
    "stage_references",
    "stage_state",
    "stage_structure",
    "stage_types",
]
