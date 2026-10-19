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
    diagnostics_from_pydantic_errors,
    sorted_diagnostics,
)
from melt.schema.document import (
    Binding,
    ButtonControl,
    Document,
    TableProjection,
)
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits
from melt.schema.semantic import (
    OBJECT_SELECT_EVENT,
    SELECTION_CLEAR_EVENT,
    SEMANTIC_LANGUAGE_ID,
    GraphProjection,
    GraphSchema,
    OutputSubscription,
    RecordProjection,
    SemanticDocument,
    SemanticReference,
    SemanticState,
    document_diagnostics,
    document_model_of,
    is_output_subscription,
)
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
    GraphNodeIdentity,
    IdentityPlan,
    InteractionPlan,
    InteractionPlanEntry,
    LayoutPlan,
    OutputDependency,
    OutputGroupPlan,
    OutputSubscriber,
    PLAN_VERSION,
    PaneIdentity,
    ParameterDependency,
    Placement,
    ReferenceStateDependency,
    RegistrationRef,
    RenderObserver,
    RenderPlan,
    RenderPlanEntry,
    RendererRef,
    SourceVersionReaders,
    StateObserver,
    StateParameterPlan,
    StatePlan,
    StateReferencePlan,
)

COMPILER_VERSION = "0.1.0"
COMPILER_IDENTIFIER = f"melt-compiler/{COMPILER_VERSION}"
"""The compiler version that plan keys cover (SPEC.md section 4.2)."""


# ---------------------------------------------------------------------------
# Renderer registry (SPEC.md sections 8 and 11.1; RFC 001 section 7.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RendererSpec:
    """One trusted built-in renderer of the host.

    `events` names the renderer events the contract declares and
    `observer_slots` the selector slots it supports; a v0.1 renderer
    declares neither, because v0.1 events come from controls only
    (RFC 001 section 7.2).
    """

    kind: str
    id: str
    contract_version: int
    source_kind: str
    events: tuple[str, ...] = ()
    observer_slots: tuple[str, ...] = ()


RENDERER_REGISTRY: dict[str, RendererSpec] = {
    spec.kind: spec
    for spec in (
        RendererSpec("table", "melt.table", 1, "table"),
        RendererSpec("plot", "melt.plot", 1, "image"),
        RendererSpec("document", "melt.document", 1, "text"),
    )
}
"""The versioned renderer registry that v0.1 ships in the host."""


SEMANTIC_RENDERER_SPECS: dict[str, RendererSpec] = {
    spec.kind: spec
    for spec in (
        RendererSpec(
            "graph",
            "melt.graph",
            1,
            "graph",
            events=(OBJECT_SELECT_EVENT, SELECTION_CLEAR_EVENT),
            observer_slots=("highlight",),
        ),
        RendererSpec("record", "melt.record", 1, "table", observer_slots=("record",)),
    )
}
"""The renderers the `melt/semantic-draft-1` language adds (RFC 001 6, 7).

They ride only the extension's plans and plan keys, so a `melt/v0.1`
plan keeps exactly the renderer set v0.1 shipped."""


def renderer_registry(document: "Document | Mapping[str, Any] | str") -> dict[str, RendererSpec]:
    """The trusted renderers one document's language may use.

    The document may be a model, a wire tree, or a language identifier
    itself; a semantic document in any of those forms resolves the
    graph and record renderers too.
    """

    language = document
    if isinstance(document, Document):
        language = document.language
    elif isinstance(document, Mapping):
        language = document.get("language")
    return registry_of_language(language)


def registry_of_language(language: "str | None") -> dict[str, RendererSpec]:
    """The trusted renderers one language identifier may use."""

    if language == SEMANTIC_LANGUAGE_ID:
        return {**RENDERER_REGISTRY, **SEMANTIC_RENDERER_SPECS}
    return dict(RENDERER_REGISTRY)


def renderer_events(kind: str, document: "Document | Mapping[str, Any] | str | None" = None) -> tuple[str, ...]:
    """The event names one renderer kind's contract declares.

    Without a document, the answer covers every trusted renderer of
    both languages.
    """

    registry = (
        {**RENDERER_REGISTRY, **SEMANTIC_RENDERER_SPECS}
        if document is None
        else renderer_registry(document)
    )
    spec = registry.get(kind)
    return spec.events if spec is not None else ()


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterStateInput:
    """One current parameter value with its state revision (6.2)."""

    value: "bool | float | str | None"
    revision: int


@dataclass(frozen=True)
class ReferenceStateInput:
    """One current reference state value with its revision (RFC 001 4).

    `value` is a serialized semantic reference or null. The service
    supplies these inputs exactly as it supplies parameter state, so a
    committed selection and a compiled plan agree on one definition of
    validity.
    """

    value: "SemanticReference | None"
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
    candidate definitions. `reference_states` and
    `collection_incarnations` carry the same service state for the
    RFC 001 extension: the stored selection of each reference state
    entry and the incarnation the service issued for each collection.
    `output_incarnations` carries the stable output identity the
    service issued for each extension action (RFC 001 section 8.1).
    All stay empty for a `melt/v0.1` document.
    """

    document: "str | bytes | bytearray | dict | Document"
    source_catalog: SourceCatalog = field(default_factory=SourceCatalog)
    action_registry: ActionRegistry = field(default_factory=ActionRegistry)
    parameter_states: Mapping[str, ParameterStateInput] = field(default_factory=dict)
    reference_states: Mapping[str, Any] = field(default_factory=dict)
    collection_incarnations: Mapping[str, str] = field(default_factory=dict)
    output_incarnations: Mapping[str, str] = field(default_factory=dict)
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
    """Validate the document structure and its language version.

    The `language` field selects the model: a `melt/v0.1` document
    keeps its closed v0.1 shape and rejects the RFC 001 extension
    fields, and a `melt/semantic-draft-1` document adds them. Any other
    identifier fails on the v0.1 literal, which reports `E_VERSION` at
    `/language`.
    """

    if isinstance(value, Document):
        return value, []
    try:
        return document_model_of(value), []
    except ValidationError as error:
        return None, diagnostics_from_pydantic_errors(
            error.errors(), DiagnosticCode.SCHEMA
        )


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


def check_output_incarnations(
    document: Document, output_incarnations: "Mapping[str, str]"
) -> list[Diagnostic]:
    """Verify every subscription against the issued incarnations.

    The compiler verifies the stable output identity a subscription
    carries (RFC 001 section 8.1): an action the map knows must hold
    exactly the incarnation the subscription names, so deleting and
    recreating an action does not attach old subscribers to a new
    calculation. The service, whose map is complete by construction,
    additionally rejects a subscription to an action with no issued
    incarnation; a service-less compile cannot know one.
    """

    if not isinstance(document, SemanticDocument):
        return []
    diagnostics: list[Diagnostic] = []
    for projection_id, projection in sorted(document.projections.items()):
        source = getattr(projection, "source", None)
        if not is_output_subscription(source):
            continue
        identity = source.actionOutput
        current = output_incarnations.get(identity.action)
        if current is None or current == identity.incarnation:
            continue
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.CONFLICT,
                f"/projections/{projection_id}/source/actionOutput/incarnation",
                f"subscription carries incarnation"
                f" {identity.incarnation!r}, but action"
                f" {identity.action!r} currently holds {current!r}",
                hint="Migrate or remove the subscriber in the same"
                " transaction that recreates the action (RFC 001"
                " section 8.1).",
            )
        )
    return diagnostics


def stage_references(
    document: Document,
    catalog: SourceCatalog,
    registry: ActionRegistry,
    output_incarnations: "Mapping[str, str] | None" = None,
) -> list[Diagnostic]:
    """Resolve document, catalog, and registry references."""

    diagnostics: list[Diagnostic] = []
    diagnostics.extend(document_diagnostics(document))
    diagnostics.extend(_resolve_registrations(document, registry))
    diagnostics.extend(_resolve_artifacts(document, catalog))
    diagnostics.extend(
        check_output_incarnations(document, output_incarnations or {})
    )
    return diagnostics


# ---------------------------------------------------------------------------
# Stage 4: types, output ownership, renderer compatibility
# ---------------------------------------------------------------------------


def _constraints_fit(parameter, entry) -> "str | None":
    """Return a mismatch message, or None when the mapping fits.

    The workspace parameter must accept only values the registration
    accepts: constraints may narrow the registration, never widen it
    (SPEC.md section 7.2). A dimension the registration bounds must be
    bounded by the parameter too, because an open dimension would let a
    committed value outside the registration's accepted range reach an
    admitted run without any diagnostic.
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
    for dimension in ("minimum", "maximum", "maxLength"):
        bound = getattr(accepted, dimension)
        mapped = getattr(constraints, dimension)
        if bound is None:
            continue
        if mapped is None:
            return (
                f"constraints set no {dimension}; the registration"
                f" accepts values only within {dimension} {bound!r}"
            )
        widened = mapped < bound if dimension == "minimum" else mapped > bound
        if widened:
            verb = "falls below" if dimension == "minimum" else "exceeds"
            return (
                f"{dimension} {mapped!r} {verb} the registration"
                f" {dimension} {bound!r}"
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
    """Check every projection against the renderer registry.

    A `melt/v0.1` document checks against the v0.1 registry only; a
    `melt/semantic-draft-1` document also resolves the graph and record
    renderers (RFC 001 section 7.2).
    """

    diagnostics: list[Diagnostic] = []
    registry = renderer_registry(document)
    semantic = isinstance(document, SemanticDocument)
    for projection_id, projection in sorted(document.projections.items()):
        spec = registry.get(projection.kind)
        if spec is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.TYPE,
                    f"/projections/{projection_id}/kind",
                    f"no renderer in the registry handles kind"
                    f" {projection.kind!r}",
                    hint=(
                        "Use one of table, plot, document, graph, or"
                        " record."
                        if semantic
                        else "Use one of table, plot, or document."
                    ),
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


def reference_collection_error(
    definition: SemanticState, value: SemanticReference
) -> "str | None":
    """Return a message when a reference names the wrong collection.

    The workspace service reuses this check when it validates a
    candidate reference assignment, so a committed value and a compiled
    plan agree on one definition of validity (RFC 001 section 4: a
    reference must match the declared collection).
    """

    if value.collection != definition.type.collection:
        return (
            f"reference names collection {value.collection!r} but the"
            f" state declares {definition.type.collection!r}"
        )
    return None


def reference_incarnation_error(
    value: SemanticReference, incarnations: "Mapping[str, str]"
) -> "str | None":
    """Return a message when a reference carries a stale incarnation.

    A reference must match the collection's current incarnation at
    assignment time, so an old incarnation cannot select an object in a
    recreated collection (RFC 001 sections 3.2 and 4).
    """

    current = incarnations.get(value.collection)
    if current is None:
        return (
            f"collection {value.collection!r} holds no current incarnation;"
            " the service has not created it"
        )
    if value.incarnation != current:
        return (
            f"reference carries incarnation {value.incarnation!r} but"
            f" collection {value.collection!r} currently holds"
            f" {current!r}"
        )
    return None


def reference_state_value_error(
    definition: SemanticState,
    value: "SemanticReference | None",
    incarnations: "Mapping[str, str]",
) -> "str | None":
    """Return a message when a stored selection violates its definition.

    Null always satisfies a nullable reference state entry; the schema
    layer enforces that every reference state entry is nullable in this
    extension.
    """

    if value is None:
        return None
    message = reference_collection_error(definition, value)
    if message is not None:
        return message
    return reference_incarnation_error(value, incarnations)


def stage_state(
    document: Document,
    parameter_states: Mapping[str, ParameterStateInput],
    limits: ServiceLimits,
    reference_states: "Mapping[str, ReferenceStateInput] | None" = None,
    collection_incarnations: "Mapping[str, str] | None" = None,
) -> list[Diagnostic]:
    """Validate state continuity and native policy constraints."""

    incarnations = collection_incarnations or {}
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
    if isinstance(document, SemanticDocument):
        for state_id in sorted(reference_states or {}):
            stored = reference_states[state_id]
            definition = document.state.get(state_id)
            if definition is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STATE_MIGRATION,
                        f"/state/{state_id}",
                        "reference state exists for an entry the document"
                        " does not define",
                        hint="Remove the state with its entry, or recreate"
                        " the state definition.",
                    )
                )
                continue
            message = reference_state_value_error(
                definition, stored.value, incarnations
            )
            if message is not None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STATE_MIGRATION,
                        f"/state/{state_id}",
                        f"current selection violates the definition:"
                        f" {message}",
                        hint="Supply state.set with a valid reference or"
                        " null and the expected state revision in the"
                        " same transaction.",
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
    registry = renderer_registry(document)
    for projection_id, projection in sorted(document.projections.items()):
        spec = registry[projection.kind]
        collection: "str | None" = None
        observers: dict[str, RenderObserver] = {}
        if isinstance(projection, GraphProjection):
            collection = projection.collection
            observers["highlight"] = RenderObserver(
                state=projection.observe.highlight.state,
                collection=projection.collection,
            )
        elif isinstance(projection, RecordProjection):
            collection = projection.collection
            observers["record"] = RenderObserver(
                state=projection.observe.record.state,
                collection=projection.collection,
            )
        entries[projection_id] = RenderPlanEntry(
            projection=projection_id,
            renderer=_renderer_ref(spec),
            # A record projection reads through its collection's pinned
            # version, so it carries no source of its own (RFC 001 6).
            source=None
            if isinstance(projection, RecordProjection)
            else projection.source,
            config=projection.config,
            controls=projection.controls,
            collection=collection,
            observers=observers,
        )
    return RenderPlan(projections=entries)


def _build_state_plan(
    document: Document, collection_incarnations: "Mapping[str, str]"
) -> StatePlan:
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
    references: dict[str, StateReferencePlan] = {}
    if isinstance(document, SemanticDocument):
        for state_id, entry in sorted(document.state.items()):
            collection = document.collections[entry.type.collection]
            references[state_id] = StateReferencePlan(
                state=state_id,
                collection=entry.type.collection,
                key=collection.key,
                source=collection.source,
                incarnation=collection_incarnations.get(entry.type.collection),
                nullable=entry.nullable,
                scope="workspace",
            )
    return StatePlan(parameters=entries, references=references)


def _event_address(binding: Binding) -> str:
    event = binding.event
    kind = event.kind
    if kind == "parameter.changed":
        return f"parameter.changed/{event.parameter}"
    if kind in (OBJECT_SELECT_EVENT, SELECTION_CLEAR_EVENT):
        return f"{kind}/{event.projection}"
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
    document: Document,
    registry: ActionRegistry,
    output_incarnations: "Mapping[str, str] | None" = None,
) -> ActionPlan:
    entries: dict[str, ActionPlanEntry] = {}
    groups: dict[str, OutputGroupPlan] = {}
    semantic = isinstance(document, SemanticDocument)
    incarnations = output_incarnations or {}
    for action_id, action in sorted(document.actions.items()):
        entry: RegistrationEntry = registry.entry(action.implementation.registrationId)
        targets = dict(sorted(getattr(action, "targets", {}).items()))
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
            targets=targets,
            # The stable output identity of the extension action: the
            # incarnation its subscribers follow. A `melt/v0.1` action
            # owns targets instead (RFC 001 sections 8.1 and 9).
            incarnation=incarnations.get(action_id) if semantic else None,
        )
        groups[action.outputGroup] = OutputGroupPlan(
            action=action_id,
            targets=targets,
        )
    return ActionPlan(actions=entries, outputGroups=groups)


def _build_dependency_plan(
    document: Document,
    output_incarnations: "Mapping[str, str] | None" = None,
) -> DependencyPlan:
    sources: dict[str, dict[str, list[str]]] = {}
    parameters: dict[str, list[str]] = {}
    incarnations = output_incarnations or {}

    def _read(source_id: str, version: int, reader: str) -> None:
        versions = sources.setdefault(source_id, {})
        versions.setdefault(str(version), []).append(reader)

    # The output-to-subscriber index of RFC 001 section 9: one entry per
    # stable output identity, naming every projection subscribed to it.
    subscribers: dict[str, list[OutputSubscriber]] = {}
    for projection_id, projection in sorted(document.projections.items()):
        source = getattr(projection, "source", None)
        if isinstance(source, OutputSubscription):
            identity = source.actionOutput
            subscribers.setdefault(f"{identity.action}/{identity.output}", []).append(
                OutputSubscriber(
                    projection=projection_id,
                    follow=source.follow,
                    version=source.version,
                )
            )
        elif source is not None:
            _read(
                source.sourceId,
                source.version,
                f"projections/{projection_id}",
            )
        for control in projection.controls:
            if not isinstance(control, ButtonControl):
                parameters.setdefault(control.parameter, []).append(
                    f"projections/{projection_id}"
                )
    if isinstance(document, SemanticDocument):
        for collection_id, collection in sorted(document.collections.items()):
            _read(
                collection.source.sourceId,
                collection.source.version,
                f"collections/{collection_id}",
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
            targets=tuple(sorted(getattr(action, "targets", {}).values())),
        )
    for binding_id, binding in sorted(document.bindings.items()):
        event = binding.event
        if event.kind == "parameter.changed":
            parameters.setdefault(event.parameter, []).append(
                f"bindings/{binding_id}"
            )
    # The state-to-observer index of RFC 001 section 9: one entry per
    # observed state, naming every projection whose selector reads it.
    observed: dict[str, list[StateObserver]] = {}
    if isinstance(document, SemanticDocument):
        for projection_id, projection in sorted(document.projections.items()):
            if isinstance(projection, GraphProjection):
                slot, observer = "highlight", projection.observe.highlight
            elif isinstance(projection, RecordProjection):
                slot, observer = "record", projection.observe.record
            else:
                continue
            observed.setdefault(observer.state, []).append(
                StateObserver(
                    projection=projection_id,
                    slot=slot,
                    state=observer.state,
                    collection=projection.collection,
                )
            )
    output_dependencies: dict[str, OutputDependency] = {}
    if isinstance(document, SemanticDocument):
        # The output-to-subscriber index belongs to the extension
        # language alone; a `melt/v0.1` plan keeps exactly its v0.1
        # indexes (RFC 001 section 9).
        for action_id, action in sorted(document.actions.items()):
            for output_name, kind in sorted(action.outputs.items()):
                key = f"{action_id}/{output_name}"
                output_dependencies[key] = OutputDependency(
                    action=action_id,
                    output=output_name,
                    kind=kind,
                    incarnation=incarnations.get(action_id),
                    subscribers=tuple(
                        sorted(
                            subscribers.get(key, ()),
                            key=lambda item: item.projection,
                        )
                    ),
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
        referenceStates={
            state_id: ReferenceStateDependency(
                observers=tuple(
                    sorted(observers_, key=lambda item: (item.projection, item.slot))
                )
            )
            for state_id, observers_ in sorted(observed.items())
        },
        outputs=output_dependencies,
    )


def _source_sort_key(reference) -> tuple:
    return reference.sourceId, reference.version


def _build_identity_plan(document: Document) -> IdentityPlan:
    panes: dict[str, PaneIdentity] = {}
    controls: dict[str, ControlIdentity] = {}
    nodes: dict[str, GraphNodeIdentity] = {}
    for projection_id, projection in sorted(document.projections.items()):
        owner: "str | None" = None
        for action in document.actions.values():
            if projection_id in getattr(action, "targets", {}).values():
                owner = action.outputGroup
                break
        if owner is None and isinstance(
            getattr(projection, "source", None), OutputSubscription
        ):
            # A subscriber sits in the output group of the action it
            # follows (RFC 001 section 8.2).
            identity = projection.source.actionOutput
            action = document.actions.get(identity.action)
            if action is not None:
                owner = action.outputGroup
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
        if isinstance(projection, GraphProjection):
            # The graph's identity map, keyed by graph and node ID, so
            # hit testing and highlighting resolve through the compiled
            # plan instead of visible labels or element order (RFC 001
            # sections 7.1 and 7.3).
            source = document.sources[projection.source.sourceId]
            version = source.versions[str(projection.source.version)]
            if isinstance(version.schema, GraphSchema):
                for node in version.schema.nodes:
                    nodes[f"{projection_id}/{node.nodeId}"] = GraphNodeIdentity(
                        graph=projection_id,
                        node=node.nodeId,
                        collection=node.collection,
                        key=node.key,
                    )
    return IdentityPlan(panes=panes, controls=controls, nodes=nodes)


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


def _plan_key(document: Document, registry: ActionRegistry, digest: str) -> str:
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
        "documentHash": digest,
        "renderers": {
            kind: {"id": spec.id, "contractVersion": spec.contract_version}
            for kind, spec in sorted(renderer_registry(document).items())
        },
        "registrations": registrations,
    }
    return canonical_hash(material)


def build_plan(
    document: Document,
    registry: ActionRegistry,
    collection_incarnations: "Mapping[str, str] | None" = None,
    output_incarnations: "Mapping[str, str] | None" = None,
) -> CompiledPlan:
    """Build every plan from a fully validated document (stage 6)."""

    digest = _document_hash(document)
    return CompiledPlan(
        planVersion=PLAN_VERSION,
        language=document.language,
        compilerVersion=COMPILER_IDENTIFIER,
        workspaceId=document.workspaceId,
        documentHash=digest,
        planKey=_plan_key(document, registry, digest),
        renderers={
            kind: _renderer_ref(spec)
            for kind, spec in sorted(renderer_registry(document).items())
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
        state=_build_state_plan(document, collection_incarnations or {}),
        interaction=_build_interaction_plan(document),
        action=_build_action_plan(document, registry, output_incarnations),
        dependency=_build_dependency_plan(document, output_incarnations),
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


def _normalize_reference_states(
    reference_states: Mapping[str, Any]
) -> dict[str, ReferenceStateInput]:
    states: dict[str, ReferenceStateInput] = {}
    for state_id, state in reference_states.items():
        if isinstance(state, ReferenceStateInput):
            states[state_id] = state
            continue
        if isinstance(state, Mapping) and "value" in state and "revision" in state:
            value = state["value"]
            if value is not None and not isinstance(value, SemanticReference):
                value = SemanticReference.model_validate(value)
            states[state_id] = ReferenceStateInput(
                value=value, revision=state["revision"]
            )
            continue
        raise MeltCompileError(
            (
                _diagnostic(
                    DiagnosticCode.SCHEMA,
                    f"/refstate/{state_id}",
                    "reference state input needs a value and a revision",
                ),
            )
        )
    return states


def compile_document(request: CompileRequest) -> CompileResult:
    """Run the seven stages and return diagnostics or a plan."""

    states = _normalize_states(request.parameter_states)
    reference_states = _normalize_reference_states(request.reference_states)
    diagnostics: list[Diagnostic] = []

    value, stage_diagnostics = stage_decode(request.document, request.max_input_bytes)
    diagnostics.extend(stage_diagnostics)
    document = None
    if value is not None:
        document, stage_diagnostics = stage_structure(value)
        diagnostics.extend(stage_diagnostics)
    if document is not None:
        diagnostics.extend(
            stage_references(
                document,
                request.source_catalog,
                request.action_registry,
                request.output_incarnations,
            )
        )
        diagnostics.extend(stage_types(document, request.action_registry))
        diagnostics.extend(
            stage_state(
                document,
                states,
                request.limits,
                reference_states=reference_states,
                collection_incarnations=request.collection_incarnations,
            )
        )
    if diagnostics or document is None:
        return CompileResult(None, tuple(sorted_diagnostics(diagnostics)))
    return CompileResult(
        build_plan(
            document,
            request.action_registry,
            request.collection_incarnations,
            request.output_incarnations,
        ),
        (),
    )


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
    "ReferenceStateInput",
    "RendererSpec",
    "SEMANTIC_RENDERER_SPECS",
    "build_plan",
    "check_output_incarnations",
    "compile_document",
    "compile_json",
    "parameter_value_error",
    "reference_collection_error",
    "reference_incarnation_error",
    "reference_state_value_error",
    "renderer_events",
    "renderer_registry",
    "stage_decode",
    "stage_references",
    "stage_state",
    "stage_structure",
    "stage_types",
]
