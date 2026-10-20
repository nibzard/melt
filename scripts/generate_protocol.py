#!/usr/bin/env python3
"""Generate the JSON Schema export and the TypeScript and Swift models.

One repeatable command produces every generated file under
``protocol/schema`` and ``protocol/generated`` from the single
restricted Pydantic vocabulary in ``python/melt/schema`` (SPEC.md
sections 2.1 and 17):

    make protocol
    uv run --locked python3 scripts/generate_protocol.py

The generator never invents a schema. It introspects the authoritative
models and writes:

- ``protocol/schema/document.schema.json``: the JSON Schema of the
  ``melt/v0.1`` document.
- ``protocol/schema/semantic-document.schema.json``: the JSON Schema of
  the provisional ``melt/semantic-draft-1`` document (RFC 001).
- ``protocol/schema/command.schema.json``: the JSON Schema of the
  command envelope, its responses, and every method payload.
- ``protocol/schema/record.schema.json``: the JSON Schema of the
  versioned persisted records.
- ``protocol/generated/melt-document.ts``: the TypeScript models with a
  table-driven validator that normalizes a parsed document into its
  wire form and hashes it with RFC 8785 canonicalization.
- ``protocol/generated/MeltDocument.swift``: the Swift models with the
  same validator, canonicalizer, and a verification harness.

The wire form is the shape ``model_dump(by_alias=True)`` produces in
Python: every field of every model is present, and an absent optional
field carries its default (an explicit null, an empty collection, or a
scalar default such as the zero debounce of a pure-event binding).
Because of that rule the generated models verify the recorded canonical
hashes of ``fixtures/valid/document-hashes.json`` and
``fixtures/semantic/document-hashes.json`` together with the Python
implementation and ``scripts/verify_canonical_hashes.mjs`` (SPEC.md
sections 4.2 and 18.1).

``--check`` regenerates into a temporary directory and exits nonzero
when the committed files would change; ``scripts/check.sh`` runs it, so
continuous integration fails when regeneration changes committed output
(SPEC.md section 17). Generated files carry a header. Never edit them
by hand: change the vocabulary and regenerate.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import tempfile
from decimal import Decimal, getcontext
from pathlib import Path
from typing import ForwardRef, Literal, Union, get_args, get_origin

from pydantic import BaseModel, StringConstraints, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic.types import StrictBool, StrictFloat, StrictInt, StrictStr

from melt.schema import document as document_module
from melt.schema import records as records_module
from melt.schema import commands as commands_module
from melt.schema import semantic as semantic_module
from melt.schema.semantic import SEMANTIC_LANGUAGE_ID

REPO_ROOT = Path(__file__).resolve().parents[1]

GENERATED_HEADER_TS = """\
// Generated file - do not edit by hand.
// Regenerate with: make protocol (scripts/generate_protocol.py)
// Source of truth: python/melt/schema, the single restricted vocabulary
// of SPEC.md section 17. Do not author an independent schema here.
"""

GENERATED_HEADER_SWIFT = """\
// Generated file - do not edit by hand.
// Regenerate with: make protocol (scripts/generate_protocol.py)
// Source of truth: python/melt/schema, the single restricted vocabulary
// of SPEC.md section 17. Do not author an independent schema here.
"""

SAFE_INTEGER_MAX = 2**53 - 1


# ---------------------------------------------------------------------------
# Intermediate representation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TString:
    pattern: "str | None" = None
    min_length: "int | None" = None
    max_length: "int | None" = None


@dataclasses.dataclass(frozen=True)
class TNumber:
    pass


@dataclasses.dataclass(frozen=True)
class TInteger:
    gt: "int | None" = None
    ge: "int | None" = None
    lt: "int | None" = None
    le: "int | None" = None


@dataclasses.dataclass(frozen=True)
class TBoolean:
    pass


@dataclasses.dataclass(frozen=True)
class TNull:
    pass


@dataclasses.dataclass(frozen=True)
class TLiteral:
    values: tuple


@dataclasses.dataclass(frozen=True)
class TArray:
    item: object
    min_items: "int | None" = None


@dataclasses.dataclass(frozen=True)
class TMap:
    key: TString
    value: object


@dataclasses.dataclass(frozen=True)
class TModel:
    name: str


@dataclasses.dataclass(frozen=True)
class TUnion:
    members: tuple
    discriminator: "str | None" = None
    literal_map: "tuple[tuple[str, str], ...] | None" = None
    dispatch: "tuple[tuple, ...] | None" = None


# Dispatch rules mirror the key-dispatched unions of the vocabulary:
# their BeforeValidator closures pick a branch by the keys or by one
# field value, because those unions carry no discriminator of their own.
DISPATCH_RULES = {
    "SourceSchema": (
        ("any_keys", ("columns", "rowKey"), "TableSchema"),
        ("any_keys", ("width", "height"), "ImageSchema"),
        ("any_keys", ("encoding",), "TextSchema"),
    ),
    "AnyMeasuredSchema": (
        ("any_keys", ("nodes", "edges"), "GraphSchema"),
        ("any_keys", ("columns", "rowKey"), "TableSchema"),
        ("any_keys", ("width", "height"), "ImageSchema"),
        ("any_keys", ("encoding",), "TextSchema"),
    ),
    "SemanticSource": (
        ("field_equals", "kind", "graph", "GraphSource"),
        ("fallback", "Source"),
    ),
    "ProjectionSource": (
        ("any_keys", ("actionOutput",), "OutputSubscription"),
        ("fallback", "SourceReference"),
    ),
    "StateSetValue": (
        ("exact_keys", ("eventField",), "EventFieldValue"),
        ("exact_keys", ("literal",), "NullLiteralValue"),
    ),
    "AnyDocument": (
        (
            "field_equals",
            "language",
            SEMANTIC_LANGUAGE_ID,
            "SemanticDocument",
        ),
        ("fallback", "Document"),
    ),
}

UNION_TYPE_NAMES = {
    frozenset(
        {
            "NumberControl",
            "TextControl",
            "ToggleControl",
            "SelectControl",
            "ButtonControl",
        }
    ): "Control",
    frozenset({"TableProjection", "PlotProjection", "DocumentProjection"}): (
        "Projection"
    ),
    frozenset({"TableSchema", "ImageSchema", "TextSchema"}): "SourceSchema",
    frozenset({"GraphSchema", "TableSchema", "ImageSchema", "TextSchema"}): (
        "AnyMeasuredSchema"
    ),
    frozenset({"ImportOrigin", "RunOrigin"}): "SourceOrigin",
    frozenset({"ParameterChangedEvent", "ControlActivateEvent"}): (
        "BindingEvent"
    ),
    frozenset({"PaneNode", "SplitNode"}): "LayoutNode",
    frozenset(
        {
            "SemanticTableProjection",
            "SemanticPlotProjection",
            "SemanticDocumentProjection",
            "GraphProjection",
            "RecordProjection",
        }
    ): "SemanticProjection",
    frozenset(
        {
            "ParameterChangedEvent",
            "ControlActivateEvent",
            "ObjectSelectEvent",
            "SelectionClearEvent",
        }
    ): "SemanticBindingEvent",
    frozenset({"ActionInvokeEffect", "StateSetEffect"}): (
        "SemanticBindingEffect"
    ),
    frozenset({"SourceReference", "OutputSubscription"}): "ProjectionSource",
    frozenset({"EventFieldValue", "NullLiteralValue"}): "StateSetValue",
    frozenset({"Source", "GraphSource"}): "SemanticSource",
    frozenset({"Document", "SemanticDocument"}): "AnyDocument",
}

# Scalar unions the Swift models express as generated enums. The key is
# the frozenset of member kind names.
SCALAR_UNION_NAMES = {
    frozenset({"boolean", "number", "string"}): "JsonScalar",
    frozenset({"boolean", "number", "string", "null"}): "TableCell",
}

DOCUMENT_MODELS = (
    "SourceReference",
    "TableColumn",
    "TableSchema",
    "ImageSchema",
    "TextSchema",
    "ImportOrigin",
    "RunOrigin",
    "SourceVersion",
    "Source",
    "ParameterConstraints",
    "Parameter",
    "SelectOption",
    "NumberControl",
    "TextControl",
    "ToggleControl",
    "SelectControl",
    "ButtonControl",
    "ActionImplementation",
    "ActionParameterMapping",
    "Action",
    "TableConfig",
    "PlotConfig",
    "DocumentConfig",
    "TableProjection",
    "PlotProjection",
    "DocumentProjection",
    "ParameterChangedEvent",
    "ControlActivateEvent",
    "ActionInvokeEffect",
    "Binding",
    "PaneNode",
    "SplitNode",
    "Layout",
    "Document",
)

SEMANTIC_MODELS = (
    "SemanticCollection",
    "ReferenceType",
    "SemanticState",
    "SemanticReference",
    "SemanticAction",
    "ActionOutputId",
    "OutputSubscription",
    "GraphNode",
    "GraphEdge",
    "GraphSchema",
    "GraphSourceVersion",
    "GraphSource",
    "GraphConfig",
    "RecordConfig",
    "HighlightObserve",
    "RecordObserve",
    "GraphObservers",
    "RecordObservers",
    "SemanticTableProjection",
    "SemanticPlotProjection",
    "SemanticDocumentProjection",
    "GraphProjection",
    "RecordProjection",
    "ObjectSelectEvent",
    "SelectionClearEvent",
    "EventFieldValue",
    "NullLiteralValue",
    "StateSetEffect",
    "SemanticBinding",
    "SemanticDocument",
)

CODEGEN_MODELS = DOCUMENT_MODELS + SEMANTIC_MODELS

SWIFT_KEYWORDS = {
    "as", "break", "case", "catch", "class", "continue", "default",
    "defer", "do", "else", "enum", "extension", "fallthrough", "for",
    "func", "guard", "if", "import", "in", "init", "inout", "internal",
    "is", "let", "nil", "open", "operator", "private", "protocol",
    "public", "repeat", "rethrows", "return", "self", "static", "struct",
    "subscript", "switch", "throw", "throws", "try", "typealias", "var",
    "where", "while", "fileprivate", "package",
}


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    python_name: str
    wire_name: str
    type: object
    required: bool
    default: object  # None, bool, int, float, str, LIST, MAP


LIST = object()
MAP = object()


def _namespace() -> dict:
    return {**vars(document_module), **vars(semantic_module)}


NS = _namespace()


def _collect_model_classes() -> dict:
    classes: dict = {}
    for module in (document_module, semantic_module):
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                classes[value.__name__] = value
    return classes


MODEL_CLASSES = _collect_model_classes()


def _raw_annotations(cls: type) -> dict:
    """The unevaluated annotations of a model, base fields first.

    ``typing.get_type_hints`` strips the ``Annotated`` aliases of the
    vocabulary, so the generator evaluates the raw annotation strings
    itself and keeps every constraint, discriminator, and validator.
    """

    merged: dict = {}
    for klass in reversed(cls.__mro__):
        if klass in (BaseModel, object):
            continue
        own = vars(klass).get("__annotations__")
        if own:
            merged.update(own)
    return merged


def _is_annotated(annotation: object) -> bool:
    return hasattr(annotation, "__metadata__")


def _unwrap_metadata(annotation: object, collected: list) -> object:
    if _is_annotated(annotation):
        base, *extra = get_args(annotation)
        collected.extend(extra)
        return _unwrap_metadata(base, collected)
    return annotation


def _resolve_forward(base: object) -> object:
    """Evaluate a nested forward reference such as
    ``'SourceReference | OutputSubscription'``."""

    content = (
        base.__forward_arg__ if isinstance(base, ForwardRef) else base
    )
    if not isinstance(content, str):
        return base
    # A quoted expression inside an already lazy annotation keeps its
    # quotes; strip them before splitting the union apart.
    if (
        len(content) >= 2
        and content[0] == content[-1]
        and content[0] in ("'", '"')
    ):
        content = content[1:-1]
    parts = [part.strip() for part in content.split("|")]
    resolved = []
    for part in parts:
        value = eval(part, NS)  # noqa: S307
        # A quoted name inside an already lazy annotation resolves to
        # another string; keep evaluating until a type appears.
        while isinstance(value, str):
            value = eval(value, NS)  # noqa: S307
        if value is None:
            value = type(None)
        resolved.append(value)
    if len(resolved) == 1:
        return resolved[0]
    return Union[tuple(resolved)]


def _scalar_constraints(metadata: list) -> dict:
    """Collect string and integer constraints from Annotated metadata."""

    gathered: dict = {}
    for item in metadata:
        if isinstance(item, StringConstraints):
            for source, target in (
                ("pattern", "pattern"),
                ("min_length", "min_length"),
                ("max_length", "max_length"),
            ):
                value = getattr(item, source, None)
                if value is not None:
                    gathered[target] = value
        elif isinstance(item, FieldInfo):
            if item.discriminator is not None:
                gathered["discriminator"] = item.discriminator
            for source, target in (
                ("gt", "gt"),
                ("ge", "ge"),
                ("lt", "lt"),
                ("le", "le"),
                ("min_length", "min_length"),
                ("max_length", "max_length"),
            ):
                value = getattr(item, source, None)
                if value is not None:
                    gathered[target] = value
    return gathered


def resolve(annotation: object) -> object:
    """Resolve one annotation into the intermediate representation."""

    metadata: list = []
    base = _unwrap_metadata(annotation, metadata)
    if isinstance(base, (str, ForwardRef)):
        base = _resolve_forward(base)
        if _is_annotated(base):
            base = _unwrap_metadata(base, metadata)
    constraints = _scalar_constraints(metadata)

    origin = get_origin(base)

    if origin is Union:
        members = []
        for argument in get_args(base):
            argument = (
                _resolve_forward(argument)
                if isinstance(argument, (str, ForwardRef))
                else argument
            )
            if argument is type(None):
                members.append(TNull())
                continue
            resolved_member = resolve(argument)
            if (
                isinstance(resolved_member, TUnion)
                and resolved_member.discriminator is None
                and resolved_member.dispatch is None
            ):
                # Optional[Union[A, B]] flattens exactly as typing does,
                # so the key-dispatch rules still find their members.
                members.extend(resolved_member.members)
            else:
                members.append(resolved_member)
        union = TUnion(tuple(members))
        if "discriminator" in constraints:
            union = _attach_discriminator(union, constraints["discriminator"])
        return union

    if origin is Literal:
        return TLiteral(get_args(base))

    if origin is tuple:
        args = get_args(base)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise SystemExit(
                f"generate_protocol: only homogeneous tuples are supported,"
                f" found {base!r}"
            )
        return TArray(resolve(args[0]), constraints.get("min_length"))

    if origin is dict:
        key_type, value_type = get_args(base)
        key = resolve(key_type)
        if not isinstance(key, TString):
            raise SystemExit(
                f"generate_protocol: object keys must be strings, found"
                f" {key_type!r}"
            )
        return TMap(key, resolve(value_type))

    if base is type(None):
        return TNull()
    if base is StrictBool or base is bool:
        return TBoolean()
    if base is StrictInt or base is int:
        return TInteger(
            gt=constraints.get("gt"),
            ge=constraints.get("ge"),
            lt=constraints.get("lt"),
            le=constraints.get("le"),
        )
    if base is StrictFloat or base is float:
        return TNumber()
    if base is StrictStr or base is str:
        return TString(
            pattern=constraints.get("pattern"),
            min_length=constraints.get("min_length"),
            max_length=constraints.get("max_length"),
        )
    if isinstance(base, type) and issubclass(base, BaseModel):
        return TModel(base.__name__)
    raise SystemExit(
        f"generate_protocol: cannot resolve annotation {annotation!r}"
    )


def _attach_discriminator(union: TUnion, discriminator: str) -> TUnion:
    literal_map = []
    for member in union.members:
        if not isinstance(member, TModel):
            raise SystemExit(
                "generate_protocol: a discriminated union must hold models,"
                f" found {member!r}"
            )
        cls = MODEL_CLASSES[member.name]
        raw = _raw_annotations(cls)
        if discriminator not in raw:
            raise SystemExit(
                f"generate_protocol: {member.name} carries no"
                f" {discriminator!r} field"
            )
        resolved = resolve(raw[discriminator])
        if not isinstance(resolved, TLiteral) or len(resolved.values) != 1:
            raise SystemExit(
                f"generate_protocol: the {discriminator!r} field of"
                f" {member.name} is not a single literal"
            )
        literal_map.append((resolved.values[0], member.name))
    return dataclasses.replace(union, discriminator=discriminator,
                               literal_map=tuple(literal_map))


def _attach_dispatch(field_type: object) -> object:
    """Attach the key-dispatch rules of one named model union, if any."""

    if isinstance(field_type, TArray):
        return dataclasses.replace(
            field_type, item=_attach_dispatch(field_type.item)
        )
    if isinstance(field_type, TMap):
        return dataclasses.replace(
            field_type, value=_attach_dispatch(field_type.value)
        )
    if not isinstance(field_type, TUnion):
        return field_type
    union = field_type
    member_names = frozenset(
        member.name for member in union.members if isinstance(member, TModel)
    )
    name = UNION_TYPE_NAMES.get(member_names)
    if name is None:
        return union
    rules = DISPATCH_RULES.get(name)
    if rules is None:
        return union
    resolved_rules = []
    for rule in rules:
        kind = rule[0]
        if kind == "any_keys":
            resolved_rules.append(
                ("any_keys", tuple(rule[1]), TModel(rule[2]))
            )
        elif kind == "exact_keys":
            resolved_rules.append(
                ("exact_keys", tuple(rule[1]), TModel(rule[2]))
            )
        elif kind == "field_equals":
            resolved_rules.append(
                ("field_equals", rule[1], rule[2], TModel(rule[3]))
            )
        elif kind == "fallback":
            resolved_rules.append(("fallback", TModel(rule[1])))
        else:
            raise SystemExit(f"generate_protocol: unknown rule {rule!r}")
    return dataclasses.replace(union, dispatch=tuple(resolved_rules))


def _default_of(field: FieldInfo) -> "tuple[bool, object]":
    if field.is_required():
        return True, None
    if field.default_factory is not None:
        factory = field.default_factory
        if factory is dict:
            return False, MAP
        raise SystemExit(
            "generate_protocol: unsupported default factory"
            f" {factory!r}"
        )
    value = field.default
    if value is None or isinstance(value, (bool, int, float, str)):
        return False, value
    if isinstance(value, tuple):
        if value:
            raise SystemExit(
                "generate_protocol: only the empty tuple default is"
                " supported"
            )
        return False, LIST
    raise SystemExit(f"generate_protocol: unsupported default {value!r}")


def build_field_specs(cls: type) -> "tuple[FieldSpec, ...]":
    raw = _raw_annotations(cls)
    specs = []
    for python_name, field in cls.model_fields.items():
        annotation = raw.get(python_name)
        if annotation is None:
            raise SystemExit(
                f"generate_protocol: {cls.__name__} field {python_name!r}"
                " has no raw annotation"
            )
        field_type = _attach_dispatch(resolve(annotation))
        required, default = _default_of(field)
        wire_name = field.alias or python_name
        specs.append(
            FieldSpec(
                python_name=python_name,
                wire_name=wire_name,
                type=field_type,
                required=required,
                default=default,
            )
        )
    return tuple(specs)


def build_model_table() -> "dict[str, tuple[FieldSpec, ...]]":
    table = {}
    for name in CODEGEN_MODELS:
        cls = MODEL_CLASSES.get(name)
        if cls is None:
            raise SystemExit(f"generate_protocol: unknown model {name}")
        table[name] = build_field_specs(cls)
    for name, specs in table.items():
        for spec in specs:
            _check_type(spec.type, f"{name}.{spec.python_name}")
    return table


def _check_type(field_type: object, where: str) -> None:
    """Reject unions the generator cannot dispatch or name."""

    if isinstance(field_type, TArray):
        _check_type(field_type.item, where)
        return
    if isinstance(field_type, TMap):
        _check_type(field_type.value, where)
        return
    if not isinstance(field_type, TUnion):
        return
    for member in field_type.members:
        _check_type(member, where)
    model_members = frozenset(
        member.name
        for member in field_type.members
        if isinstance(member, TModel)
    )
    has_null = any(isinstance(m, TNull) for m in field_type.members)
    scalar_members = frozenset(
        _kind_name(member)
        for member in field_type.members
        if not isinstance(member, (TModel, TNull))
    )
    if model_members and not scalar_members and not has_null:
        if field_type.discriminator is None and field_type.dispatch is None:
            name = UNION_TYPE_NAMES.get(model_members)
            raise SystemExit(
                f"generate_protocol: the union at {where} holds models"
                f" {sorted(model_members)} but has no discriminator and no"
                f" dispatch rule (name lookup: {name!r})"
            )
        if field_type.dispatch is not None:
            return
        name = UNION_TYPE_NAMES.get(model_members)
        if name is None:
            raise SystemExit(
                f"generate_protocol: no type name for the union at {where}"
                f" with members {sorted(model_members)}"
            )
        if field_type.discriminator is not None and name in DISPATCH_RULES:
            raise SystemExit(
                f"generate_protocol: {name} has both a discriminator and"
                " dispatch rules"
            )


def _kind_name(member: object) -> str:
    if isinstance(member, TBoolean):
        return "boolean"
    if isinstance(member, TNumber):
        return "number"
    if isinstance(member, TString):
        return "string"
    if isinstance(member, TLiteral):
        return "literal"
    return "other"


# ---------------------------------------------------------------------------
# Shared emission helpers
# ---------------------------------------------------------------------------


def _json_default(value: object) -> object:
    if value is LIST:
        return []
    if value is MAP:
        return {}
    return value


def ts_default_literal(spec: FieldSpec) -> str:
    if spec.required:
        return "null"
    return json.dumps(_json_default(spec.default))


def swift_default_literal(spec: FieldSpec) -> str:
    if spec.required:
        return ".null"
    value = _json_default(spec.default)
    if value is None:
        return ".null"
    if value is True:
        return ".bool(true)"
    if value is False:
        return ".bool(false)"
    if isinstance(value, (int, float)):
        return f".number({json.dumps(value)})"
    if isinstance(value, str):
        return f".string({swift_string(value)})"
    if isinstance(value, list):
        return ".array([])"
    if isinstance(value, dict):
        return ".object([:])"
    raise SystemExit(f"generate_protocol: bad default {value!r}")


def swift_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# TypeScript emission
# ---------------------------------------------------------------------------

TS_ENGINE = """\
// ---------------------------------------------------------------------------
// Table-driven validation engine
//
// One generic engine validates a value tree against MELT_MODEL_FIELDS
// and normalizes it into the wire form: every field of every model is
// present, and an absent optional field carries its default (SPEC.md
// section 4.2). That wire form is what the Python implementation
// serializes through model_dump(by_alias=True), so both languages hash
// the same bytes. Python stays the authoritative validator: these
// models check the structural shape only (SPEC.md section 17).
// ---------------------------------------------------------------------------

export type FieldSpec = {
  name: string;
  type: TypeExpr;
  required: boolean;
  default: unknown;
};

export type DispatchRule =
  | { kind: "anyKeys"; keys: string[]; member: TypeExpr }
  | { kind: "exactKeys"; keys: string[]; member: TypeExpr }
  | { kind: "fieldEquals"; field: string; value: string; member: TypeExpr }
  | { kind: "fallback"; member: TypeExpr };

export type TypeExpr =
  | { kind: "string"; pattern?: string; minLength?: number; maxLength?: number }
  | { kind: "number" }
  | { kind: "integer"; gt?: number; ge?: number; lt?: number; le?: number }
  | { kind: "boolean" }
  | { kind: "null" }
  | { kind: "literal"; values: (string | null)[] }
  | { kind: "array"; item: TypeExpr; minItems?: number }
  | { kind: "map"; keyPattern?: string; value: TypeExpr }
  | { kind: "model"; name: string }
  | {
      kind: "union";
      members: TypeExpr[];
      discriminator?: string;
      discriminatorMap?: Record<string, string>;
      dispatch?: DispatchRule[];
    };

export class MeltModelParseError extends Error {
  readonly path: string;

  constructor(path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "MeltModelParseError";
    this.path = path;
  }
}

function fail(path: string, message: string): never {
  throw new MeltModelParseError(path, message);
}

export function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const PATTERN_CACHE = new Map<string, RegExp>();

function patternOf(source: string): RegExp {
  let pattern = PATTERN_CACHE.get(source);
  if (pattern === undefined) {
    // The vocabulary patterns end with the Python-only \\z anchor. In
    // JavaScript the unadorned $ anchor is the same end-of-input test.
    pattern = new RegExp(source.replace(/\\\\z/g, "$"));
    PATTERN_CACHE.set(source, pattern);
  }
  return pattern;
}

const SAFE_INTEGER_MAX = 9007199254740991;

export function checkValue(
  type: TypeExpr,
  value: unknown,
  path: string,
): unknown {
  switch (type.kind) {
    case "string": {
      if (typeof value !== "string") {
        return fail(path, "expected a string");
      }
      if (type.pattern !== undefined && !patternOf(type.pattern).test(value)) {
        return fail(path, `string does not match ${type.pattern}`);
      }
      if (type.minLength !== undefined && value.length < type.minLength) {
        return fail(path, `string is shorter than ${type.minLength}`);
      }
      if (type.maxLength !== undefined && value.length > type.maxLength) {
        return fail(path, `string is longer than ${type.maxLength}`);
      }
      return value;
    }
    case "number": {
      if (typeof value !== "number" || !Number.isFinite(value)) {
        return fail(path, "expected a finite number");
      }
      return value;
    }
    case "integer": {
      if (
        typeof value !== "number" ||
        !Number.isInteger(value) ||
        Math.abs(value) > SAFE_INTEGER_MAX
      ) {
        return fail(path, "expected a safe integer");
      }
      if (type.gt !== undefined && !(value > type.gt)) {
        return fail(path, `integer must be greater than ${type.gt}`);
      }
      if (type.ge !== undefined && !(value >= type.ge)) {
        return fail(path, `integer must be at least ${type.ge}`);
      }
      if (type.lt !== undefined && !(value < type.lt)) {
        return fail(path, `integer must be less than ${type.lt}`);
      }
      if (type.le !== undefined && !(value <= type.le)) {
        return fail(path, `integer must be at most ${type.le}`);
      }
      return value;
    }
    case "boolean": {
      if (typeof value !== "boolean") {
        return fail(path, "expected a boolean");
      }
      return value;
    }
    case "null": {
      if (value !== null) {
        return fail(path, "expected null");
      }
      return value;
    }
    case "literal": {
      if (!type.values.includes(value as string | null)) {
        return fail(
          path,
          `expected one of ${type.values.map((item) => JSON.stringify(item)).join(", ")}`,
        );
      }
      return value;
    }
    case "array": {
      if (!Array.isArray(value)) {
        return fail(path, "expected an array");
      }
      if (type.minItems !== undefined && value.length < type.minItems) {
        return fail(path, `array holds fewer than ${type.minItems} items`);
      }
      return value.map((item, index) =>
        checkValue(type.item, item, `${path}[${index}]`),
      );
    }
    case "map": {
      if (!isJsonObject(value)) {
        return fail(path, "expected an object");
      }
      const result: Record<string, unknown> = {};
      for (const key of Object.keys(value)) {
        if (type.keyPattern !== undefined && !patternOf(type.keyPattern).test(key)) {
          return fail(
            `${path}/${key}`,
            "object key does not match the identifier pattern",
          );
        }
        result[key] = checkValue(type.value, value[key], `${path}/${key}`);
      }
      return result;
    }
    case "model":
      return normalizeModel(type.name, value, path);
    case "union": {
      if (type.discriminator !== undefined) {
        if (!isJsonObject(value)) {
          return fail(path, "expected an object");
        }
        const tag = value[type.discriminator];
        const member =
          typeof tag === "string" ? type.discriminatorMap?.[tag] : undefined;
        if (member === undefined) {
          return fail(`${path}/${type.discriminator}`, "unknown union member");
        }
        return normalizeModel(member, value, path);
      }
      if (type.dispatch !== undefined) {
        if (!isJsonObject(value)) {
          return fail(path, "expected an object");
        }
        const keys = Object.keys(value);
        for (const rule of type.dispatch) {
          if (rule.kind === "anyKeys" && rule.keys.some((key) => keys.includes(key))) {
            return checkValue(rule.member, value, path);
          }
          if (
            rule.kind === "exactKeys" &&
            keys.length === rule.keys.length &&
            rule.keys.every((key) => keys.includes(key))
          ) {
            return checkValue(rule.member, value, path);
          }
          if (rule.kind === "fieldEquals" && value[rule.field] === rule.value) {
            return checkValue(rule.member, value, path);
          }
          if (rule.kind === "fallback") {
            return checkValue(rule.member, value, path);
          }
        }
        return fail(path, "value matches no union member");
      }
      let firstError: unknown = null;
      for (const member of type.members) {
        try {
          return checkValue(member, value, path);
        } catch (error) {
          firstError ??= error;
        }
      }
      throw firstError ?? fail(path, "value matches no union member");
    }
  }
}

function cloneDefault(value: unknown): unknown {
  if (Array.isArray(value)) {
    return [];
  }
  if (isJsonObject(value)) {
    return {};
  }
  return value;
}

export function normalizeModel(
  name: string,
  value: unknown,
  path: string,
): Record<string, unknown> {
  const fields = MELT_MODEL_FIELDS[name];
  if (fields === undefined) {
    return fail(path, `unknown model ${name}`);
  }
  if (!isJsonObject(value)) {
    return fail(path, `${name} must be an object`);
  }
  const known = new Set(fields.map((field) => field.name));
  for (const key of Object.keys(value)) {
    if (!known.has(key)) {
      return fail(
        `${path}/${key}`,
        `${name} rejects unknown field ${JSON.stringify(key)}`,
      );
    }
  }
  const result: Record<string, unknown> = {};
  for (const field of fields) {
    if (field.name in value) {
      result[field.name] = checkValue(
        field.type,
        value[field.name],
        `${path}/${field.name}`,
      );
    } else if (field.required) {
      return fail(
        `${path}/${field.name}`,
        `${name} requires field ${JSON.stringify(field.name)}`,
      );
    } else {
      result[field.name] = cloneDefault(field.default);
    }
  }
  return result;
}
"""

TS_CANONICAL = """\
// ---------------------------------------------------------------------------
// RFC 8785 canonicalization and canonical hashing (SPEC.md section 4.2)
// ---------------------------------------------------------------------------

import { createHash } from "node:crypto";

function assertScalarString(value: string, where: string): void {
  // Melt strings hold valid Unicode scalar values: every surrogate must
  // be a complete pair (SPEC.md section 4.2).
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const low = value.charCodeAt(index + 1);
      if (!(low >= 0xdc00 && low <= 0xdfff)) {
        throw new Error(`${where}: lone high surrogate at index ${index}`);
      }
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      throw new Error(`${where}: lone low surrogate at index ${index}`);
    }
  }
}

export function canonicalize(value: unknown, where = ""): string {
  if (value === null) {
    return "null";
  }
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "string":
      assertScalarString(value, where);
      // JSON.stringify escapes exactly the characters RFC 8785 requires,
      // with lowercase hexadecimal escapes.
      return JSON.stringify(value);
    case "number": {
      if (!Number.isFinite(value)) {
        throw new Error(`${where}: non-finite number`);
      }
      // String(n) is the ECMAScript number serialization RFC 8785
      // prescribes: shortest round-trip, -0 becomes "0".
      return String(value);
    }
    case "object": {
      if (Array.isArray(value)) {
        const parts = value.map((item, index) =>
          canonicalize(item, `${where}[${index}]`),
        );
        return `[${parts.join(",")}]`;
      }
      // Array#sort without a comparator compares UTF-16 code units, the
      // key order RFC 8785 section 3.2.3 defines.
      const keys = Object.keys(value).sort();
      const parts = keys.map((key) => {
        assertScalarString(key, `${where}: key`);
        const record = value as Record<string, unknown>;
        return `${JSON.stringify(key)}:${canonicalize(record[key], `${where}/${key}`)}`;
      });
      return `{${parts.join(",")}}`;
    }
    default:
      throw new Error(`${where}: unsupported value of type ${typeof value}`);
  }
}

export function canonicalHash(value: unknown, where = ""): string {
  const text = canonicalize(value, where);
  return `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`;
}
"""


def ts_type_expr(field_type: object) -> str:
    """One TypeExpr literal as compact TypeScript."""

    def expression(item: object) -> str:
        if isinstance(item, TString):
            payload = {"kind": "string"}
            if item.pattern is not None:
                payload["pattern"] = item.pattern
            if item.min_length is not None:
                payload["minLength"] = item.min_length
            if item.max_length is not None:
                payload["maxLength"] = item.max_length
            return json.dumps(payload)
        if isinstance(item, TNumber):
            return '{"kind":"number"}'
        if isinstance(item, TInteger):
            payload = {"kind": "integer"}
            for key, value in (
                ("gt", item.gt),
                ("ge", item.ge),
                ("lt", item.lt),
                ("le", item.le),
            ):
                if value is not None:
                    payload[key] = value
            return json.dumps(payload)
        if isinstance(item, TBoolean):
            return '{"kind":"boolean"}'
        if isinstance(item, TNull):
            return '{"kind":"null"}'
        if isinstance(item, TLiteral):
            values = [
                None if value is None else value for value in item.values
            ]
            return json.dumps({"kind": "literal", "values": values})
        if isinstance(item, TArray):
            payload = {"kind": "array", "item": json.loads(expression(item.item))}
            if item.min_items is not None:
                payload["minItems"] = item.min_items
            return json.dumps(payload)
        if isinstance(item, TMap):
            payload = {"kind": "map", "value": json.loads(expression(item.value))}
            if item.key.pattern is not None:
                payload["keyPattern"] = item.key.pattern
            return json.dumps(payload)
        if isinstance(item, TModel):
            return json.dumps({"kind": "model", "name": item.name})
        if isinstance(item, TUnion):
            members = [json.loads(expression(member)) for member in item.members]
            payload: dict = {"kind": "union", "members": members}
            if item.discriminator is not None:
                payload["discriminator"] = item.discriminator
                payload["discriminatorMap"] = dict(item.literal_map or ())
            if item.dispatch is not None:
                rules = []
                for rule in item.dispatch:
                    if rule[0] == "any_keys":
                        rules.append(
                            {
                                "kind": "anyKeys",
                                "keys": list(rule[1]),
                                "member": json.loads(expression(rule[2])),
                            }
                        )
                    elif rule[0] == "exact_keys":
                        rules.append(
                            {
                                "kind": "exactKeys",
                                "keys": list(rule[1]),
                                "member": json.loads(expression(rule[2])),
                            }
                        )
                    elif rule[0] == "field_equals":
                        rules.append(
                            {
                                "kind": "fieldEquals",
                                "field": rule[1],
                                "value": rule[2],
                                "member": json.loads(expression(rule[3])),
                            }
                        )
                    else:
                        rules.append(
                            {
                                "kind": "fallback",
                                "member": json.loads(expression(rule[1])),
                            }
                        )
                payload["dispatch"] = rules
            return json.dumps(payload)
        raise SystemExit(f"generate_protocol: cannot emit {item!r}")

    return expression(field_type)


def ts_interface_type(field_type: object) -> str:
    if isinstance(field_type, TString):
        return "string"
    if isinstance(field_type, TNumber):
        return "number"
    if isinstance(field_type, TInteger):
        return "number"
    if isinstance(field_type, TBoolean):
        return "boolean"
    if isinstance(field_type, TNull):
        return "null"
    if isinstance(field_type, TLiteral):
        parts = []
        for value in field_type.values:
            parts.append("null" if value is None else json.dumps(value))
        return " | ".join(parts)
    if isinstance(field_type, TArray):
        return f"{ts_interface_type(field_type.item)}[]"
    if isinstance(field_type, TMap):
        return f"Record<string, {ts_interface_type(field_type.value)}>"
    if isinstance(field_type, TModel):
        return field_type.name
    if isinstance(field_type, TUnion):
        members = list(field_type.members)
        nulls = [m for m in members if isinstance(m, TNull)]
        rest = [m for m in members if not isinstance(m, TNull)]
        parts: list[str]
        if rest and all(isinstance(m, TModel) for m in rest):
            name = UNION_TYPE_NAMES.get(frozenset(m.name for m in rest))
            parts = [name] if name is not None else [
                ts_interface_type(member) for member in rest
            ]
        else:
            parts = [ts_interface_type(member) for member in rest]
        if nulls:
            parts.append("null")
        return " | ".join(parts)
    raise SystemExit(f"generate_protocol: cannot type {field_type!r}")


@dataclasses.dataclass(frozen=True)
class NamedUnion:
    name: str
    members: tuple
    discriminator: "str | None"


def _named_unions_of(field_type: object):
    """Yield every named model union reachable inside a field type.

    The walk recurses through arrays, maps, and unions, so the unions
    used as `tuple[Control, ...]` or `ProjectionSource | None` are
    found as reliably as the bare ones.
    """

    if isinstance(field_type, TArray):
        yield from _named_unions_of(field_type.item)
        return
    if isinstance(field_type, TMap):
        yield from _named_unions_of(field_type.value)
        return
    if not isinstance(field_type, TUnion):
        return
    for member in field_type.members:
        yield from _named_unions_of(member)
    members = [m for m in field_type.members if isinstance(m, TModel)]
    nulls = any(isinstance(m, TNull) for m in field_type.members)
    if not members or len(members) + int(nulls) != len(field_type.members):
        return
    name = UNION_TYPE_NAMES.get(frozenset(m.name for m in members))
    if name is None:
        return
    yield NamedUnion(name, tuple(members), field_type.discriminator)


def ts_union_type_alias(union_name: str, members: tuple) -> str:
    """The named TypeScript alias of one model union."""

    members_type = " | ".join(
        ts_interface_type(member) for member in members
    )
    return f"export type {union_name} = {members_type};"


def emit_typescript(table: "dict[str, tuple[FieldSpec, ...]]") -> str:
    lines: list[str] = []
    lines.append(GENERATED_HEADER_TS)
    lines.append("")
    lines.append("// Table-driven validator and wire-form normalizer.")
    lines.append(TS_ENGINE)
    lines.append("")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("// The vocabulary table")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("")
    lines.append("export const MELT_MODEL_FIELDS: Record<string, FieldSpec[]> = {")
    for name in CODEGEN_MODELS:
        lines.append(f"  {json.dumps(name)}: [")
        for spec in table[name]:
            lines.append(
                "    {"
                f"name: {json.dumps(spec.wire_name)}, "
                f"type: {ts_type_expr(spec.type)}, "
                f"required: {'true' if spec.required else 'false'}, "
                f"default: {ts_default_literal(spec)}"
                "},"
            )
        lines.append("  ],")
    lines.append("};")
    lines.append("")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("// TypeScript models")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("")
    emitted_aliases: set[str] = set()
    for name in CODEGEN_MODELS:
        lines.append(f"export interface {name} {{")
        for spec in table[name]:
            optional = "" if spec.required else "?"
            lines.append(
                f"  {json.dumps(spec.wire_name).replace('"', '')}"
                f"{optional}: {ts_interface_type(spec.type)};"
            )
        lines.append("}")
        lines.append("")
        for spec in table[name]:
            for union in _named_unions_of(spec.type):
                if union.name in emitted_aliases:
                    continue
                emitted_aliases.add(union.name)
                lines.append(ts_union_type_alias(union.name, union.members))
                lines.append("")
    lines.append(f'export const SEMANTIC_LANGUAGE_ID = "{SEMANTIC_LANGUAGE_ID}";')
    lines.append("")
    lines.append(TS_CANONICAL)
    lines.append(
        """\
// ---------------------------------------------------------------------------
// Document entry points
// ---------------------------------------------------------------------------

/**
 * Parse and normalize one melt/v0.1 document into its wire form.
 *
 * Throws MeltModelParseError when the shape does not match the
 * vocabulary: unknown fields, missing required fields, or a value of
 * the wrong type. Python stays the authoritative validator; these
 * models check the structural shape (SPEC.md section 17).
 */
export function parseDocument(value: unknown): Document {
  return normalizeModel("Document", value, "") as unknown as Document;
}

/** Parse and normalize one melt/semantic-draft-1 document (RFC 001). */
export function parseSemanticDocument(value: unknown): SemanticDocument {
  return normalizeModel(
    "SemanticDocument",
    value,
    "",
  ) as unknown as SemanticDocument;
}

/** Parse a document under the language its `language` field declares. */
export function parseDocumentForLanguage(
  value: unknown,
): Document | SemanticDocument {
  if (
    isJsonObject(value) &&
    value["language"] === SEMANTIC_LANGUAGE_ID
  ) {
    return parseSemanticDocument(value);
  }
  return parseDocument(value);
}

/**
 * The canonical RFC 8785 hash of one document given as JSON text.
 *
 * The text is parsed, validated, and normalized into the wire form
 * (every field present, absent optional fields explicit nulls or their
 * defaults), then canonicalized and hashed. This is the value recorded
 * in fixtures/valid/document-hashes.json and
 * fixtures/semantic/document-hashes.json (SPEC.md section 18.1).
 *
 * JSON.parse keeps the last of duplicate object keys; the Python reader
 * rejects duplicates. Only the Python side owns that strictness.
 */
export function canonicalDocumentHash(jsonText: string): string {
  return canonicalHash(parseDocumentForLanguage(JSON.parse(jsonText)));
}
"""
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Swift emission
# ---------------------------------------------------------------------------


def _sha256_constants() -> "tuple[list[int], list[int]]":
    """The SHA-256 round constants, derived exactly with integers."""

    getcontext().prec = 80

    def primes(count: int) -> list[int]:
        found: list[int] = []
        candidate = 2
        while len(found) < count:
            if all(
                candidate % prime
                for prime in found
                if prime * prime <= candidate
            ):
                found.append(candidate)
            candidate += 1
        return found

    roots = primes(64)
    k = [
        int((Decimal(prime) ** (Decimal(1) / Decimal(3))) % 1 * (1 << 32))
        for prime in roots
    ]
    h = [int(Decimal(prime).sqrt() % 1 * (1 << 32)) for prime in roots[:8]]

    def reference(constants: list[int], initial: list[int], message: bytes) -> str:
        padded = message + b"\x80"
        padded += b"\x00" * ((56 - len(padded) % 64) % 64)
        padded += (len(message) * 8).to_bytes(8, "big")
        digest = list(initial)
        for start in range(0, len(padded), 64):
            block = padded[start : start + 64]
            w = [
                int.from_bytes(block[i : i + 4], "big")
                for i in range(0, 64, 4)
            ]
            for index in range(16, 64):
                s0 = (
                    _rotr(w[index - 15], 7)
                    ^ _rotr(w[index - 15], 18)
                    ^ (w[index - 15] >> 3)
                )
                s1 = (
                    _rotr(w[index - 2], 17)
                    ^ _rotr(w[index - 2], 19)
                    ^ (w[index - 2] >> 10)
                )
                w.append(
                    (w[index - 16] + s0 + w[index - 7] + s1) & 0xFFFFFFFF
                )
            hh = list(digest)
            for index in range(64):
                s1 = _rotr(hh[4], 6) ^ _rotr(hh[4], 11) ^ _rotr(hh[4], 25)
                ch = (hh[4] & hh[5]) ^ (~hh[4] & hh[6])
                temp1 = (
                    hh[7] + s1 + ch + constants[index] + w[index]
                ) & 0xFFFFFFFF
                s0 = _rotr(hh[0], 2) ^ _rotr(hh[0], 13) ^ _rotr(hh[0], 22)
                maj = (hh[0] & hh[1]) ^ (hh[0] & hh[2]) ^ (hh[1] & hh[2])
                temp2 = (s0 + maj) & 0xFFFFFFFF
                hh = [
                    (temp1 + temp2) & 0xFFFFFFFF,
                    hh[0],
                    hh[1],
                    hh[2],
                    (hh[3] + temp1) & 0xFFFFFFFF,
                    hh[4],
                    hh[5],
                    hh[6],
                ]
            digest = [
                (left + right) & 0xFFFFFFFF
                for left, right in zip(digest, hh)
            ]
        return b"".join(
            value.to_bytes(4, "big") for value in digest
        ).hex()

    for sample in (b"", b"abc", b"The quick brown fox jumps over the lazy dog"):
        if reference(k, h, sample) != hashlib.sha256(sample).hexdigest():
            raise SystemExit(
                "generate_protocol: derived SHA-256 constants are wrong"
            )
    return k, h


def _rotr(value: int, amount: int) -> int:
    return ((value >> amount) | (value << (32 - amount))) & 0xFFFFFFFF


SWIFT_ENGINE = """\
// ---------------------------------------------------------------------------
// JSON value tree
// ---------------------------------------------------------------------------

/// One parsed JSON value. The verification harness reads fixtures into
/// this tree, validates and normalizes it against the model table, and
/// canonicalizes the result.
public enum JsonValue: Sendable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JsonValue])
    case object([String: JsonValue])
}

/// The unit type of a wire field that is always null.
public struct MeltNull: Sendable, Equatable {
    public init() {}
}

/// One JSON scalar of the mixed scalar union the vocabulary carries
/// (a parameter default value, for example).
public enum JsonScalar: Sendable, Equatable {
    case bool(Bool)
    case number(Double)
    case string(String)
}

public struct MeltModelError: Error, CustomStringConvertible {
    public let path: String
    public let message: String
    public var description: String { "\\(path): \\(message)" }
    init(_ path: String, _ message: String) {
        self.path = path
        self.message = message
    }
}

// ---------------------------------------------------------------------------
// Table-driven validation engine
//
// One generic engine validates a JsonValue tree against
// meltModelTable and normalizes it into the wire form: every field of
// every model is present, and an absent optional field carries its
// default (SPEC.md section 4.2). That wire form is what the Python
// implementation serializes through model_dump(by_alias=True), so both
// languages hash the same bytes. Python stays the authoritative
// validator: these models check the structural shape only (SPEC.md
// section 17). Swift decoding alone is not semantic validation.
// ---------------------------------------------------------------------------

public indirect enum TypeExpr: Sendable {
    case string(pattern: String?, minLength: Int?, maxLength: Int?)
    case number
    case integer(gt: Int?, ge: Int?, lt: Int?, le: Int?)
    case boolean
    case nullValue
    case literal([String])
    case array(item: TypeExpr, minItems: Int?)
    case map(keyPattern: String?, value: TypeExpr)
    case model(name: String)
    case union(
        members: [TypeExpr],
        discriminator: String?,
        discriminatorMap: [String: String]?,
        dispatch: [DispatchRule]?
    )
}

public indirect enum DispatchRule: Sendable {
    case anyKeys([String], TypeExpr)
    case exactKeys([String], TypeExpr)
    case fieldEquals(String, String, TypeExpr)
    case fallback(TypeExpr)
}

public struct FieldSpec: Sendable {
    public let name: String
    public let type: TypeExpr
    public let required: Bool
    public let defaultValue: JsonValue
}

let SAFE_INTEGER_MAX: Double = 9_007_199_254_740_991

private var patternCache: [String: NSRegularExpression] = [:]

private func cachedPattern(_ source: String) -> NSRegularExpression {
    if let cached = patternCache[source] {
        return cached
    }
    do {
        // The vocabulary patterns end with the Python-only \\z anchor.
        // ICU shares the same meaning through the unadorned $ anchor.
        let regex = try NSRegularExpression(
            pattern: source.replacingOccurrences(of: "\\\\z", with: "$")
        )
        patternCache[source] = regex
        return regex
    } catch {
        fatalError("the generated vocabulary patterns are valid ICU expressions: \\(error)")
    }
}

private func matchesPattern(_ source: String, _ text: String) -> Bool {
    let regex = cachedPattern(source)
    let range = NSRange(text.startIndex..., in: text)
    return regex.firstMatch(in: text, range: range) != nil
}

public func checkValue(
    _ type: TypeExpr,
    _ value: JsonValue,
    _ path: String
) throws -> JsonValue {
    switch type {
    case .string(let pattern, let minLength, let maxLength):
        guard case .string(let text) = value else {
            throw MeltModelError(path, "expected a string")
        }
        if let pattern, !matchesPattern(pattern, text) {
            throw MeltModelError(path, "string does not match \\(pattern)")
        }
        if let minLength, text.count < minLength {
            throw MeltModelError(path, "string is shorter than \\(minLength)")
        }
        if let maxLength, text.count > maxLength {
            throw MeltModelError(path, "string is longer than \\(maxLength)")
        }
        return value
    case .number:
        guard case .number = value else {
            throw MeltModelError(path, "expected a number")
        }
        return value
    case .integer(let gt, let ge, let lt, let le):
        guard case .number(let raw) = value else {
            throw MeltModelError(path, "expected a number")
        }
        if raw.rounded() != raw || abs(raw) > SAFE_INTEGER_MAX {
            throw MeltModelError(path, "expected a safe integer")
        }
        if let gt, !(raw > Double(gt)) {
            throw MeltModelError(path, "integer must be greater than \\(gt)")
        }
        if let ge, !(raw >= Double(ge)) {
            throw MeltModelError(path, "integer must be at least \\(ge)")
        }
        if let lt, !(raw < Double(lt)) {
            throw MeltModelError(path, "integer must be less than \\(lt)")
        }
        if let le, !(raw <= Double(le)) {
            throw MeltModelError(path, "integer must be at most \\(le)")
        }
        return value
    case .boolean:
        guard case .bool = value else {
            throw MeltModelError(path, "expected a boolean")
        }
        return value
    case .nullValue:
        guard case .null = value else {
            throw MeltModelError(path, "expected null")
        }
        return value
    case .literal(let allowed):
        guard case .string(let text) = value, allowed.contains(text) else {
            throw MeltModelError(path, "expected one of \\(allowed.joined(separator: ", "))")
        }
        return value
    case .array(let item, let minItems):
        guard case .array(let items) = value else {
            throw MeltModelError(path, "expected an array")
        }
        if let minItems, items.count < minItems {
            throw MeltModelError(path, "array holds fewer than \\(minItems) items")
        }
        var normalized: [JsonValue] = []
        normalized.reserveCapacity(items.count)
        for (index, element) in items.enumerated() {
            normalized.append(try checkValue(item, element, "\\(path)[\\(index)]"))
        }
        return .array(normalized)
    case .map(let keyPattern, let valueType):
        guard case .object(let fields) = value else {
            throw MeltModelError(path, "expected an object")
        }
        var normalized: [String: JsonValue] = [:]
        for (key, item) in fields {
            if let keyPattern, !matchesPattern(keyPattern, key) {
                throw MeltModelError(
                    "\\(path)/\\(key)",
                    "object key does not match the identifier pattern"
                )
            }
            normalized[key] = try checkValue(valueType, item, "\\(path)/\\(key)")
        }
        return .object(normalized)
    case .model(let name):
        return try normalizeModel(name, value, path)
    case .union(let members, let discriminator, let discriminatorMap, let dispatch):
        if let discriminator {
            guard case .object(let fields) = value else {
                throw MeltModelError(path, "expected an object")
            }
            let rawTag = fields[discriminator]
            if case .some(.string(let tag)) = rawTag,
               let member = discriminatorMap?[tag] {
                return try normalizeModel(member, value, path)
            }
            throw MeltModelError("\\(path)/\\(discriminator)", "unknown union member")
        }
        if let dispatch {
            guard case .object(let fields) = value else {
                throw MeltModelError(path, "expected an object")
            }
            let keys = Array(fields.keys)
            for rule in dispatch {
                switch rule {
                case .anyKeys(let wanted, let member):
                    if !wanted.filter(keys.contains).isEmpty {
                        return try checkValue(member, value, path)
                    }
                case .exactKeys(let wanted, let member):
                    if Set(keys) == Set(wanted) {
                        return try checkValue(member, value, path)
                    }
                case .fieldEquals(let field, let wanted, let member):
                    if case .some(.string(wanted)) = fields[field] {
                        return try checkValue(member, value, path)
                    }
                case .fallback(let member):
                    return try checkValue(member, value, path)
                }
            }
            throw MeltModelError(path, "value matches no union member")
        }
        var firstError: Error? = nil
        for member in members {
            do {
                return try checkValue(member, value, path)
            } catch {
                if firstError == nil {
                    firstError = error
                }
            }
        }
        throw firstError ?? MeltModelError(path, "value matches no union member")
    }
}

public func normalizeModel(
    _ name: String,
    _ value: JsonValue,
    _ path: String
) throws -> JsonValue {
    guard let fields = meltModelTable[name] else {
        throw MeltModelError(path, "unknown model \\(name)")
    }
    guard case .object(let object) = value else {
        throw MeltModelError(path, "\\(name) must be an object")
    }
    let known = Set(fields.map { $0.name })
    for key in object.keys where !known.contains(key) {
        throw MeltModelError(
            "\\(path)/\\(key)",
            "\\(name) rejects unknown field \\"\\(key)\\""
        )
    }
    var result: [String: JsonValue] = [:]
    for field in fields {
        if let present = object[field.name] {
            result[field.name] = try checkValue(
                field.type,
                present,
                "\\(path)/\\(field.name)"
            )
        } else if field.required {
            throw MeltModelError(
                "\\(path)/\\(field.name)",
                "\\(name) requires field \\"\\(field.name)\\""
            )
        } else {
            result[field.name] = field.defaultValue
        }
    }
    return .object(result)
}
"""

SWIFT_CANONICAL = """\
// ---------------------------------------------------------------------------
// RFC 8785 canonicalization and canonical hashing (SPEC.md section 4.2)
// ---------------------------------------------------------------------------

func canonicalString(_ value: String) -> String {
    var out = "\\""
    for scalar in value.unicodeScalars {
        switch scalar.value {
        case 0x22: out += "\\\\\\""
        case 0x5C: out += "\\\\\\\\"
        case 0x08: out += "\\\\b"
        case 0x09: out += "\\\\t"
        case 0x0A: out += "\\\\n"
        case 0x0C: out += "\\\\f"
        case 0x0D: out += "\\\\r"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\\\u%04x", scalar.value)
            } else {
                out.append(Character(scalar))
            }
        }
    }
    out += "\\""
    return out
}

/// Serialize one number with ECMAScript `Number::toString` rules, the
/// number serialization RFC 8785 section 3.2.2.3 requires.
func es6Number(_ value: Double) -> String {
    if value == 0 {
        return "0"
    }
    let sign = value < 0 ? "-" : ""
    let repr = String(abs(value))
    var mantissa = repr
    var exponent = 0
    if let eIndex = repr.firstIndex(where: { $0 == "e" || $0 == "E" }) {
        mantissa = String(repr[repr.startIndex..<eIndex])
        exponent = Int(repr[repr.index(after: eIndex)...]) ?? 0
    }
    var integerPart = mantissa
    var fractionPart = ""
    if let dot = mantissa.firstIndex(of: ".") {
        integerPart = String(mantissa[mantissa.startIndex..<dot])
        fractionPart = String(mantissa[mantissa.index(after: dot)...])
    }
    if fractionPart == "0" {
        fractionPart = ""
    }
    let rawDigits = integerPart + fractionPart
    let digits = rawDigits.drop(while: { $0 == "0" })
    let digitText = String(digits)
    if digitText.isEmpty {
        return "0"
    }
    let leading = rawDigits.count - digitText.count
    let point = integerPart.count + exponent - leading
    let count = digitText.count
    if count <= point && point <= 21 {
        return sign + digitText + String(repeating: "0", count: point - count)
    }
    if 0 < point && point <= 21 {
        let head = String(digitText.prefix(point))
        let tail = String(digitText.dropFirst(point))
        return sign + head + "." + tail
    }
    if -6 < point && point <= 0 {
        return sign + "0." + String(repeating: "0", count: -point) + digitText
    }
    let exponentValue = point - 1
    let exponentText = exponentValue >= 0
        ? "+\\(exponentValue)"
        : String(exponentValue)
    if count == 1 {
        return sign + digitText + "e" + exponentText
    }
    return sign + String(digitText.prefix(1)) + "." + String(digitText.dropFirst()) + "e" + exponentText
}

/// RFC 8785 canonical JSON text of one value tree. Object keys sort by
/// UTF-16 code unit; array order is preserved.
public func canonicalize(_ value: JsonValue) -> String {
    switch value {
    case .null:
        return "null"
    case .bool(let flag):
        return flag ? "true" : "false"
    case .string(let text):
        return canonicalString(text)
    case .number(let number):
        return es6Number(number)
    case .array(let items):
        let parts = items.map { canonicalize($0) }
        return "[" + parts.joined(separator: ",") + "]"
    case .object(let fields):
        let keys = fields.keys.sorted {
            $0.utf16.lexicographicallyPrecedes($1.utf16)
        }
        let parts = keys.map { canonicalString($0) + ":" + canonicalize(fields[$0]!) }
        return "{" + parts.joined(separator: ",") + "}"
    }
}

// ---------------------------------------------------------------------------
// SHA-256 (FIPS 180-4), dependency-free
// ---------------------------------------------------------------------------

private func rotr(_ value: UInt32, _ amount: UInt32) -> UInt32 {
    return (value >> amount) | (value << (32 - amount))
}

func sha256Hex(_ data: Data) -> String {
    var message = [UInt8](data)
    let bitLength = UInt64(message.count) * 8
    message.append(0x80)
    while message.count % 64 != 56 {
        message.append(0)
    }
    for index in 0..<8 {
        let shift = UInt64(56 - 8 * index)
        message.append(UInt8((bitLength >> shift) & 0xff))
    }
    var digest = MELT_SHA256_H
    for chunkStart in stride(from: 0, to: message.count, by: 64) {
        var w = [UInt32](repeating: 0, count: 64)
        for index in 0..<16 {
            let offset = chunkStart + index * 4
            w[index] =
                (UInt32(message[offset]) << 24)
                | (UInt32(message[offset + 1]) << 16)
                | (UInt32(message[offset + 2]) << 8)
                | UInt32(message[offset + 3])
        }
        for index in 16..<64 {
            let s0 =
                rotr(w[index - 15], 7) ^ rotr(w[index - 15], 18)
                ^ (w[index - 15] >> 3)
            let s1 =
                rotr(w[index - 2], 17) ^ rotr(w[index - 2], 19)
                ^ (w[index - 2] >> 10)
            w[index] = w[index - 16] &+ s0 &+ w[index - 7] &+ s1
        }
        var a = digest[0]
        var b = digest[1]
        var c = digest[2]
        var d = digest[3]
        var e = digest[4]
        var f = digest[5]
        var g = digest[6]
        var h = digest[7]
        for index in 0..<64 {
            let s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
            let ch = (e & f) ^ (~e & g)
            let temp1 = h &+ s1 &+ ch &+ MELT_SHA256_K[index] &+ w[index]
            let s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
            let maj = (a & b) ^ (a & c) ^ (b & c)
            let temp2 = s0 &+ maj
            h = g
            g = f
            f = e
            e = d &+ temp1
            d = c
            c = b
            b = a
            a = temp1 &+ temp2
        }
        digest[0] = digest[0] &+ a
        digest[1] = digest[1] &+ b
        digest[2] = digest[2] &+ c
        digest[3] = digest[3] &+ d
        digest[4] = digest[4] &+ e
        digest[5] = digest[5] &+ f
        digest[6] = digest[6] &+ g
        digest[7] = digest[7] &+ h
    }
    var hex = ""
    for word in digest {
        hex += String(format: "%08x", word)
    }
    return hex
}

public func canonicalHash(_ value: JsonValue) -> String {
    return "sha256:" + sha256Hex(Data(canonicalize(value).utf8))
}
"""

SWIFT_JSON_PARSER = """\
// ---------------------------------------------------------------------------
// A strict JSON reader for the verification harness
//
// It accepts the JSON grammar and rejects non-finite numbers, lone
// surrogates, and trailing data. It keeps every number as a Double,
// exactly as ECMAScript does, and every string as valid Unicode scalar
// values. The Python reader owns the additional strictness (duplicate
// object keys, leading zeros); the harness verifies committed fixtures
// that already passed it.
// ---------------------------------------------------------------------------

struct JsonTextParser {
    private let scalars: [Unicode.Scalar]
    private var index = 0

    init(_ text: String) {
        scalars = Array(text.unicodeScalars)
    }

    static func parse(_ text: String) throws -> JsonValue {
        var parser = JsonTextParser(text)
        let value = try parser.parseValue()
        parser.skipWhitespace()
        if parser.index != parser.scalars.count {
            throw MeltModelError("", "trailing data after the JSON value")
        }
        return value
    }

    private var current: Unicode.Scalar? {
        index < scalars.count ? scalars[index] : nil
    }

    private mutating func skipWhitespace() {
        while let scalar = current,
              scalar == " " || scalar == "\\t" || scalar == "\\n" || scalar == "\\r" {
            index += 1
        }
    }

    private mutating func parseValue() throws -> JsonValue {
        skipWhitespace()
        guard let scalar = current else {
            throw MeltModelError("", "unexpected end of input")
        }
        if scalar == "{" {
            return try parseObject()
        }
        if scalar == "[" {
            return try parseArray()
        }
        if scalar == "\\"" {
            return .string(try parseString())
        }
        if scalar == "t" {
            try expect("true")
            return .bool(true)
        }
        if scalar == "f" {
            try expect("false")
            return .bool(false)
        }
        if scalar == "n" {
            try expect("null")
            return JsonValue.null
        }
        if scalar == "-" || ("0"..."9").contains(scalar) {
            return try parseNumber()
        }
        throw MeltModelError("", "unexpected character \\(scalar) at index \\(index)")
    }

    private mutating func expect(_ word: String) throws {
        for character in word.unicodeScalars {
            guard current == character else {
                throw MeltModelError("", "invalid literal at index \\(index)")
            }
            index += 1
        }
    }

    private mutating func parseObject() throws -> JsonValue {
        index += 1
        skipWhitespace()
        var fields: [String: JsonValue] = [:]
        if current == "}" {
            index += 1
            return .object(fields)
        }
        while true {
            skipWhitespace()
            guard current == "\\"" else {
                throw MeltModelError("", "expected an object key at index \\(index)")
            }
            let key = try parseString()
            skipWhitespace()
            guard current == ":" else {
                throw MeltModelError("", "expected ':' after an object key")
            }
            index += 1
            let value = try parseValue()
            fields[key] = value
            skipWhitespace()
            if current == "," {
                index += 1
                continue
            }
            if current == "}" {
                index += 1
                return .object(fields)
            }
            throw MeltModelError("", "expected ',' or '}' in an object")
        }
    }

    private mutating func parseArray() throws -> JsonValue {
        index += 1
        skipWhitespace()
        var items: [JsonValue] = []
        if current == "]" {
            index += 1
            return .array(items)
        }
        while true {
            let value = try parseValue()
            items.append(value)
            skipWhitespace()
            if current == "," {
                index += 1
                continue
            }
            if current == "]" {
                index += 1
                return .array(items)
            }
            throw MeltModelError("", "expected ',' or ']' in an array")
        }
    }

    private mutating func parseString() throws -> String {
        index += 1
        var scalarsOut = [Unicode.Scalar]()
        while let scalar = current {
            if scalar == "\\"" {
                index += 1
                return String(String.UnicodeScalarView(scalarsOut))
            }
            if scalar == "\\\\" {
                index += 1
                try parseEscape(into: &scalarsOut)
                continue
            }
            if scalar.value < 0x20 {
                throw MeltModelError(
                    "",
                    "raw control character in a string at index \\(index)"
                )
            }
            scalarsOut.append(scalar)
            index += 1
        }
        throw MeltModelError("", "unterminated string")
    }

    private mutating func parseEscape(into scalars: inout [Unicode.Scalar]) throws {
        guard let escape = current else {
            throw MeltModelError("", "unterminated escape sequence")
        }
        index += 1
        switch escape {
        case "\\"":
            scalars.append("\\"")
        case "\\\\":
            scalars.append("\\\\")
        case "/":
            scalars.append("/")
        case "b":
            scalars.append("\\u{08}")
        case "f":
            scalars.append("\\u{0C}")
        case "n":
            scalars.append("\\n")
        case "r":
            scalars.append("\\r")
        case "t":
            scalars.append("\\t")
        case "u":
            let first = try parseHexQuad()
            if first >= 0xD800 && first <= 0xDBFF {
                guard current == "\\\\",
                      index + 1 < self.scalars.count,
                      self.scalars[index + 1] == "u" else {
                    throw MeltModelError(
                        "",
                        "high surrogate is not followed by a low surrogate"
                    )
                }
                index += 2
                let second = try parseHexQuad()
                guard second >= 0xDC00 && second <= 0xDFFF else {
                    throw MeltModelError(
                        "",
                        "high surrogate is not followed by a low surrogate"
                    )
                }
                let combined =
                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                guard let scalar = Unicode.Scalar(combined) else {
                    throw MeltModelError("", "invalid Unicode scalar value")
                }
                scalars.append(scalar)
            } else if first >= 0xDC00 && first <= 0xDFFF {
                throw MeltModelError("", "lone low surrogate in a string")
            } else {
                guard let scalar = Unicode.Scalar(first) else {
                    throw MeltModelError("", "invalid Unicode scalar value")
                }
                scalars.append(scalar)
            }
        default:
            throw MeltModelError("", "invalid escape sequence \\\\(escape)")
        }
    }

    private func hexDigit(_ scalar: Unicode.Scalar) -> Int? {
        switch scalar.value {
        case 0x30...0x39:
            return Int(scalar.value - 0x30)
        case 0x41...0x46:
            return Int(scalar.value - 0x41 + 10)
        case 0x61...0x66:
            return Int(scalar.value - 0x61 + 10)
        default:
            return nil
        }
    }

    private mutating func parseHexQuad() throws -> Int {
        var value = 0
        for _ in 0..<4 {
            guard let scalar = current else {
                throw MeltModelError("", "invalid \\\\u escape sequence")
            }
            guard let digit = hexDigit(scalar) else {
                throw MeltModelError("", "invalid \\\\u escape sequence")
            }
            value = value * 16 + digit
            index += 1
        }
        return value
    }

    private mutating func parseNumber() throws -> JsonValue {
        let start = index
        if current == "-" {
            index += 1
        }
        while let scalar = current, ("0"..."9").contains(scalar) {
            index += 1
        }
        if current == "." {
            index += 1
            while let scalar = current, ("0"..."9").contains(scalar) {
                index += 1
            }
        }
        if current == "e" || current == "E" {
            index += 1
            if current == "+" || current == "-" {
                index += 1
            }
            while let scalar = current, ("0"..."9").contains(scalar) {
                index += 1
            }
        }
        let text = String(String.UnicodeScalarView(scalars[start..<index]))
        guard let value = Double(text), value.isFinite else {
            throw MeltModelError("", "number \\(text) is not finite")
        }
        return .number(value)
    }
}
"""


def swift_type_expr(field_type: object) -> str:
    def expression(item: object) -> str:
        if isinstance(item, TString):
            parts = [
                "pattern:"
                + (
                    swift_string(item.pattern)
                    if item.pattern is not None
                    else "nil"
                ),
                "minLength:"
                + (
                    str(item.min_length)
                    if item.min_length is not None
                    else "nil"
                ),
                "maxLength:"
                + (
                    str(item.max_length)
                    if item.max_length is not None
                    else "nil"
                ),
            ]
            return ".string(" + ", ".join(parts) + ")"
        if isinstance(item, TNumber):
            return ".number"
        if isinstance(item, TInteger):
            parts = [
                f"gt: {item.gt if item.gt is not None else 'nil'}",
                f"ge: {item.ge if item.ge is not None else 'nil'}",
                f"lt: {item.lt if item.lt is not None else 'nil'}",
                f"le: {item.le if item.le is not None else 'nil'}",
            ]
            return ".integer(" + ", ".join(parts) + ")"
        if isinstance(item, TBoolean):
            return ".boolean"
        if isinstance(item, TNull):
            return ".nullValue"
        if isinstance(item, TLiteral):
            values = ", ".join(
                swift_string(value) for value in item.values
            )
            return f".literal([{values}])"
        if isinstance(item, TArray):
            min_items = (
                f", minItems: {item.min_items}"
                if item.min_items is not None
                else ", minItems: nil"
            )
            return f".array(item: {expression(item.item)}{min_items})"
        if isinstance(item, TMap):
            key = (
                swift_string(item.key.pattern)
                if item.key.pattern is not None
                else "nil"
            )
            return f".map(keyPattern: {key}, value: {expression(item.value)})"
        if isinstance(item, TModel):
            return f'.model(name: {swift_string(item.name)})'
        if isinstance(item, TUnion):
            members = ", ".join(
                expression(member) for member in item.members
            )
            discriminator = (
                swift_string(item.discriminator)
                if item.discriminator is not None
                else "nil"
            )
            literal_map = "nil"
            if item.literal_map:
                entries = ", ".join(
                    f"{swift_string(key)}: {swift_string(value)}"
                    for key, value in item.literal_map
                )
                literal_map = f"[{entries}]"
            dispatch = "nil"
            if item.dispatch is not None:
                rules = []
                for rule in item.dispatch:
                    if rule[0] == "any_keys":
                        keys = ", ".join(swift_string(k) for k in rule[1])
                        rules.append(
                            f".anyKeys([{keys}], {expression(rule[2])})"
                        )
                    elif rule[0] == "exact_keys":
                        keys = ", ".join(swift_string(k) for k in rule[1])
                        rules.append(
                            f".exactKeys([{keys}], {expression(rule[2])})"
                        )
                    elif rule[0] == "field_equals":
                        rules.append(
                            f".fieldEquals("
                            f"{swift_string(rule[1])}, "
                            f"{swift_string(rule[2])}, "
                            f"{expression(rule[3])})"
                        )
                    else:
                        rules.append(f".fallback({expression(rule[1])})")
                dispatch = "[" + ", ".join(rules) + "]"
            return (
                f".union(members: [{members}], "
                f"discriminator: {discriminator}, "
                f"discriminatorMap: {literal_map}, "
                f"dispatch: {dispatch})"
            )
        raise SystemExit(f"generate_protocol: cannot emit {item!r}")

    return expression(field_type)


def _swift_property(name: str) -> str:
    return f"`{name}`" if name in SWIFT_KEYWORDS else name


def _swift_case_name(literal: str) -> str:
    parts = literal.split(".")
    name = parts[0] + "".join(
        part[:1].upper() + part[1:] for part in parts[1:]
    )
    return f"`{name}`" if name in SWIFT_KEYWORDS else name


def swift_field_type(field_type: object) -> str:
    if isinstance(field_type, TString):
        return "String"
    if isinstance(field_type, TNumber):
        return "Double"
    if isinstance(field_type, TInteger):
        return "Int"
    if isinstance(field_type, TBoolean):
        return "Bool"
    if isinstance(field_type, TNull):
        return "MeltNull"
    if isinstance(field_type, TLiteral):
        return "String"
    if isinstance(field_type, TArray):
        return f"[{swift_field_type(field_type.item)}]"
    if isinstance(field_type, TMap):
        return f"[String: {swift_field_type(field_type.value)}]"
    if isinstance(field_type, TModel):
        return field_type.name
    if isinstance(field_type, TUnion):
        members = list(field_type.members)
        nulls = [m for m in members if isinstance(m, TNull)]
        rest = [m for m in members if not isinstance(m, TNull)]
        suffix = "?" if nulls else ""
        if len(rest) == 1:
            return swift_field_type(rest[0]) + suffix
        if rest and all(isinstance(m, (TBoolean, TNumber, TString)) for m in rest):
            kinds = frozenset(_kind_name(m) for m in rest)
            name = SCALAR_UNION_NAMES.get(kinds)
            if name is None:
                raise SystemExit(
                    f"generate_protocol: no Swift name for scalar union {kinds}"
                )
            return name + suffix
        if rest and all(isinstance(m, TModel) for m in rest):
            names = frozenset(m.name for m in rest)
            name = UNION_TYPE_NAMES.get(names)
            if name is None:
                raise SystemExit(
                    f"generate_protocol: no Swift name for union {sorted(names)}"
                )
            return name + suffix
        raise SystemExit(
            "generate_protocol: mixed model and scalar unions are not"
            " supported in Swift"
        )
    raise SystemExit(f"generate_protocol: cannot type {field_type!r}")


def _swift_default_expression(spec: FieldSpec) -> str:
    if spec.required:
        return ""
    target = swift_field_type(spec.type)
    if target.endswith("?"):
        return " = nil"
    if target == "MeltNull":
        return " = MeltNull()"
    default = _json_default(spec.default)
    if default is None:
        return " = nil"
    if isinstance(default, bool):
        return f" = {str(default).lower()}"
    if isinstance(default, (int, float)):
        return f" = {default}"
    if isinstance(default, str):
        return f" = {swift_string(default)}"
    if isinstance(default, list):
        return " = []"
    if isinstance(default, dict):
        return " = [:]"
    raise SystemExit(f"generate_protocol: bad default {default!r}")


def _union_members_for_enums(table) -> list:
    """Every named model union the Swift models express as an enum.

    Returns (union name, member models, discriminator, recursive)
    tuples. The discriminator is None for the key-dispatched unions,
    whose cases take the member type name instead of a literal. A
    recursive union (the layout tree) needs the `indirect` marker.
    """

    def references(
        field_type: object, target: str, target_members: frozenset,
        visited: "set[str]",
    ) -> bool:
        """Whether a type transitively contains the target union."""

        if isinstance(field_type, TArray):
            return references(
                field_type.item, target, target_members, visited
            )
        if isinstance(field_type, TMap):
            return references(
                field_type.value, target, target_members, visited
            )
        if isinstance(field_type, TModel):
            if field_type.name == target:
                return True
            if field_type.name in visited:
                return False
            visited.add(field_type.name)
            return any(
                references(spec.type, target, target_members, visited)
                for spec in table.get(field_type.name, ())
            )
        if isinstance(field_type, TUnion):
            members = frozenset(
                member.name
                for member in field_type.members
                if isinstance(member, TModel)
            )
            if members == target_members:
                return True
            return any(
                references(member, target, target_members, visited)
                for member in field_type.members
            )
        return False

    seen: dict[str, NamedUnion] = {}
    for name in CODEGEN_MODELS:
        for spec in table[name]:
            for union in _named_unions_of(spec.type):
                seen.setdefault(union.name, union)
    unions = []
    for union_name, union in seen.items():
        member_names = frozenset(member.name for member in union.members)
        recursive = any(
            references(member, union_name, member_names, set())
            for member in union.members
        )
        unions.append(
            (union_name, union.members, union.discriminator, recursive)
        )
    return unions


def emit_swift(table) -> str:
    lines: list[str] = []
    lines.append(GENERATED_HEADER_SWIFT)
    lines.append("")
    lines.append("#if canImport(Glibc)")
    lines.append("import Glibc")
    lines.append("#else")
    lines.append("import Darwin")
    lines.append("#endif")
    lines.append("import Foundation")
    lines.append("")
    lines.append(SWIFT_ENGINE)
    lines.append("")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("// The vocabulary table")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("")
    lines.append("let meltModelTable: [String: [FieldSpec]] = [")
    for name in CODEGEN_MODELS:
        lines.append(f"    {swift_string(name)}: [")
        for spec in table[name]:
            lines.append(
                "        FieldSpec("
                f"name: {swift_string(spec.wire_name)}, "
                f"type: {swift_type_expr(spec.type)}, "
                f"required: {'true' if spec.required else 'false'}, "
                f"defaultValue: {swift_default_literal(spec)}"
                "),"
            )
        lines.append("    ],")
    lines.append("]")
    lines.append("")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("// Swift models")
    lines.append("// ---------------------------------------------------------------------------")
    lines.append("")
    for name in CODEGEN_MODELS:
        lines.append(f"public struct {name}: Sendable, Equatable {{")
        init_parameters = []
        assignments = []
        for spec in table[name]:
            target = swift_field_type(spec.type)
            property = _swift_property(spec.wire_name)
            lines.append(f"    public var {property}: {target}")
            init_parameters.append(
                f"{_swift_property(spec.wire_name)}: {target}"
                + _swift_default_expression(spec)
            )
            assignments.append(f"        self.{property} = {property}")
        lines.append("")
        lines.append("    public init(")
        for position, parameter in enumerate(init_parameters):
            separator = "," if position + 1 < len(init_parameters) else ""
            lines.append(f"        {parameter}{separator}")
        lines.append("    ) {")
        lines.extend(assignments)
        lines.append("    }")
        lines.append("}")
        lines.append("")
    for union_name, members, discriminator, recursive in _union_members_for_enums(table):
        marker = "indirect " if recursive else ""
        lines.append(
            f"public {marker}enum {union_name}: Sendable, Equatable {{"
        )
        for member in members:
            case_name = _union_case_name(table, member, discriminator)
            lines.append(f"    case {case_name}({member.name})")
        lines.append("}")
        lines.append("")
    lines.append(f'public let MELT_SEMANTIC_LANGUAGE_ID = "{SEMANTIC_LANGUAGE_ID}"')
    lines.append("")
    lines.append(SWIFT_CANONICAL)
    lines.append("")
    lines.append(SWIFT_JSON_PARSER)
    lines.append(
        """
// ---------------------------------------------------------------------------
// Document entry points
// ---------------------------------------------------------------------------

public func parseDocumentWire(_ value: JsonValue) throws -> JsonValue {
    return try normalizeModel("Document", value, "")
}

public func parseSemanticDocumentWire(_ value: JsonValue) throws -> JsonValue {
    return try normalizeModel("SemanticDocument", value, "")
}

/// Validate and normalize a document under the language it declares.
public func parseDocumentForLanguageWire(_ value: JsonValue) throws -> JsonValue {
    if case .object(let fields) = value,
       case .some(.string(MELT_SEMANTIC_LANGUAGE_ID)) = fields["language"] {
        return try parseSemanticDocumentWire(value)
    }
    return try parseDocumentWire(value)
}

// ---------------------------------------------------------------------------
// Verification harness
//
// Run from the repository root:
//
//     swift protocol/generated/MeltDocument.swift "$(pwd)"
//
// It replays the RFC 8785 reference vectors and verifies every recorded
// hash of fixtures/valid/document-hashes.json and
// fixtures/semantic/document-hashes.json through the generated models
// (SPEC.md sections 4.2 and 18.1). The Linux gate never requires a
// Swift toolchain (SPEC.md section 2.2); scripts/check.sh runs this
// harness only where one exists.
// ---------------------------------------------------------------------------

func verifyHashIndex(
    root: String,
    directory: String,
    indexName: String
) throws -> (agreed: Int, total: Int, failures: [String]) {
    let indexPath = root + "/" + directory + "/" + indexName
    let indexText = try String(contentsOfFile: indexPath, encoding: .utf8)
    let parsed = try JsonTextParser.parse(indexText)
    guard case .object(let index) = parsed,
          case .some(.object(let documents)) = index["documents"] else {
        throw MeltModelError(indexPath, "hash index is not an object with a documents map")
    }
    var agreed = 0
    var failures: [String] = []
    for (name, recorded) in documents.sorted(by: { $0.key < $1.key }) {
        guard case .string(let recordedHash) = recorded else {
            failures.append(directory + "/" + name + ": recorded hash is not a string")
            continue
        }
        let fixturePath = root + "/" + directory + "/" + name
        let text: String
        do {
            text = try String(contentsOfFile: fixturePath, encoding: .utf8)
        } catch {
            failures.append(directory + "/" + name + ": cannot read the fixture")
            continue
        }
        do {
            let value = try JsonTextParser.parse(text)
            let wire = try parseDocumentForLanguageWire(value)
            let hash = canonicalHash(wire)
            if hash == recordedHash {
                agreed += 1
            } else {
                failures.append(
                    directory + "/" + name
                        + ": canonical hash disagrees with the recorded value"
                        + "\\n  swift:    " + hash
                        + "\\n  recorded: " + recordedHash
                )
            }
        } catch let error as MeltModelError {
            failures.append(directory + "/" + name + ": " + error.description)
        } catch {
            failures.append(
                directory + "/" + name + ": " + String(describing: error)
            )
        }
    }
    return (agreed, documents.count, failures)
}

func verifyReferenceVectors(root: String) -> (agreed: Int, failures: [String]) {
    let names = ["arrays", "french", "structures", "unicode", "values", "weird"]
    var agreed = 0
    var failures: [String] = []
    for name in names {
        let inputPath = root + "/tests/contract/jcs/input-" + name + ".json"
        let expectedPath = root + "/tests/contract/jcs/expected-" + name + ".json"
        guard
            let inputText = try? String(contentsOfFile: inputPath, encoding: .utf8),
            let expectedText = try? String(contentsOfFile: expectedPath, encoding: .utf8)
        else {
            failures.append("reference vector " + name + ": missing fixture files")
            continue
        }
        do {
            let value = try JsonTextParser.parse(inputText)
            let canonical = canonicalize(value)
            let expected = expectedText.trimmingCharacters(in: .whitespacesAndNewlines)
            if canonical == expected {
                agreed += 1
            } else {
                failures.append(
                    "reference vector " + name + " disagrees"
                        + "\\n  swift:    " + canonical
                        + "\\n  expected: " + expected
                )
            }
        } catch let error as MeltModelError {
            failures.append("reference vector " + name + ": " + error.description)
        } catch {
            failures.append("reference vector " + name + ": " + String(describing: error))
        }
    }
    return (agreed, failures)
}

let arguments = CommandLine.arguments
let root = arguments.count > 1 ? arguments[1] : "."
var failures: [String] = []
do {
    let vectors = verifyReferenceVectors(root: root)
    failures.append(contentsOf: vectors.failures)
    let vectorMessage = "melt-swift-verify: "
        + String(vectors.agreed) + " of 6 RFC 8785 reference vectors agree"
    if vectors.failures.isEmpty {
        print(vectorMessage)
    }
    for (directory, indexName) in [
        ("fixtures/valid", "document-hashes.json"),
        ("fixtures/semantic", "document-hashes.json"),
    ] {
        let result = try verifyHashIndex(
            root: root,
            directory: directory,
            indexName: indexName
        )
        failures.append(contentsOf: result.failures)
        print(
            "melt-swift-verify: " + String(result.agreed) + " of "
                + String(result.total)
                + " document hashes agree under " + directory
        )
    }
} catch {
    FileHandle.standardError.write(
        Data("melt-swift-verify: \\(error)\\n".utf8)
    )
    exit(1)
}
if !failures.isEmpty {
    let message = failures.joined(separator: "\\n") + "\\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}
print("melt-swift-verify: the generated Swift models agree with every recorded hash")
"""
    )
    return "\n".join(lines)


def _union_case_name(
    table, member: TModel, discriminator: "str | None"
) -> str:
    """The Swift case name of one union member.

    A discriminated union names its cases after the discriminator
    literal; a key-dispatched union names them after the member type.
    """

    if discriminator is not None:
        cls = MODEL_CLASSES[member.name]
        raw = _raw_annotations(cls)
        if discriminator in raw:
            resolved = resolve(raw[discriminator])
            if isinstance(resolved, TLiteral) and len(resolved.values) == 1:
                return _swift_case_name(resolved.values[0])
    lowered = member.name[:1].lower() + member.name[1:]
    return _swift_case_name(lowered)


# ---------------------------------------------------------------------------
# JSON Schema export
# ---------------------------------------------------------------------------


def export_schema_group(models: list, primary: str, title: str) -> dict:
    """One self-contained schema library: $defs for every model plus a
    $ref to the primary root."""

    defs: dict = {}
    for model in models:
        schema = TypeAdapter(model).json_schema(
            by_alias=True, ref_template="#/$defs/{model}"
        )
        nested = schema.pop("$defs", {})
        for name, definition in nested.items():
            defs[name] = definition
        defs[model.__name__] = schema
    if primary not in defs:
        raise SystemExit(f"generate_protocol: {primary} is not in the group")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://melt.dev/protocol/{primary.lower()}-schema.json",
        "title": title,
        "x-generated": {
            "command": "make protocol (scripts/generate_protocol.py)",
            "source": "python/melt/schema, the single restricted vocabulary"
            " of SPEC.md section 17",
            "note": "Generated file: do not edit by hand. Regeneration must"
            " reproduce the committed bytes; scripts/check.sh enforces it.",
        },
        "$ref": f"#/$defs/{primary}",
        "$defs": defs,
    }


def command_schema_group() -> dict:
    envelope_models = [
        commands_module.CommandEnvelope,
        commands_module.OkResponse,
        commands_module.RejectedResponse,
    ]
    payload_models = [
        model
        for model in commands_module.PAYLOAD_MODELS.values()
        if model is not None
    ]
    operation_models = [
        commands_module.EntityCreate,
        commands_module.EntityReplace,
        commands_module.EntityDelete,
        commands_module.ParameterSet,
        commands_module.StateSet,
        commands_module.LayoutReplace,
    ]
    group = export_schema_group(
        envelope_models + payload_models + operation_models,
        "CommandEnvelope",
        "The Melt command envelope, its responses, and every method payload",
    )
    return group


def record_schema_group() -> dict:
    record_models = [
        records_module.HostViewRecord,
        records_module.RunRecord,
        records_module.RegistrationRecord,
        records_module.PermissionGrantRecord,
        records_module.ArtifactRecord,
        records_module.OutputGroupRecord,
        records_module.PendingIntentRecord,
        records_module.PublicationRecord,
        records_module.ParameterState,
        records_module.ReferenceState,
        records_module.CommandReceipt,
        records_module.Snapshot,
        records_module.ChangeNotification,
        records_module.PageRequest,
        records_module.PageResponse,
    ]
    group = export_schema_group(
        record_models,
        "RunRecord",
        "The versioned persisted records of the Melt service",
    )
    group["$defs"]["MeltRecord"] = {
        "oneOf": [
            {"$ref": f"#/$defs/{model.__name__}"} for model in record_models
        ]
    }
    group["$ref"] = "#/$defs/MeltRecord"
    group["$id"] = "https://melt.dev/protocol/meltrecord-schema.json"
    return group


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

SCHEMA_FILES = {
    "protocol/schema/document.schema.json": lambda: export_schema_group(
        [document_module.Document],
        "Document",
        "The melt/v0.1 canonical document",
    ),
    "protocol/schema/semantic-document.schema.json": lambda: (
        export_schema_group(
            [semantic_module.SemanticDocument],
            "SemanticDocument",
            "The provisional melt/semantic-draft-1 document of RFC 001",
        )
    ),
    "protocol/schema/command.schema.json": command_schema_group,
    "protocol/schema/record.schema.json": record_schema_group,
}


def render_outputs() -> "dict[str, str]":
    outputs: dict[str, str] = {}
    for relative, builder in SCHEMA_FILES.items():
        schema = builder()
        outputs[relative] = (
            json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )
    table = build_model_table()
    outputs["protocol/generated/melt-document.ts"] = emit_typescript(table)
    k_constants, h_constants = _sha256_constants()
    k_line = "let MELT_SHA256_K: [UInt32] = [\n"
    for index in range(0, len(k_constants), 6):
        chunk = k_constants[index : index + 6]
        k_line += (
            "    "
            + ", ".join(f"0x{value:08x}" for value in chunk)
            + ("," if index + 6 < len(k_constants) else "")
            + "\n"
        )
    k_line += "]"
    h_line = (
        "let MELT_SHA256_H: [UInt32] = ["
        + ", ".join(f"0x{value:08x}" for value in h_constants)
        + "]"
    )
    swift_text = emit_swift(table)
    anchor = "private func rotr(_ value: UInt32, _ amount: UInt32) -> UInt32 {"
    if anchor not in swift_text:
        raise SystemExit("generate_protocol: rotr anchor missing")
    swift_text = swift_text.replace(
        anchor,
        k_line
        + "\n"
        + h_line
        + "\n\n"
        + anchor,
        1,
    )
    outputs["protocol/generated/MeltDocument.swift"] = swift_text
    return outputs


def write_outputs(outputs: "dict[str, str]", target_root: Path) -> None:
    for relative, text in outputs.items():
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")


def check_outputs(outputs: "dict[str, str]") -> int:
    """Regenerate into a temporary directory and compare with the tree."""

    drift: list[str] = []
    with tempfile.TemporaryDirectory(prefix="melt-protocol-") as scratch:
        scratch_root = Path(scratch)
        write_outputs(outputs, scratch_root)
        committed = set()
        for directory in ("protocol/schema", "protocol/generated"):
            for path in (REPO_ROOT / directory).glob("*"):
                if path.suffix in (".json", ".ts", ".swift"):
                    committed.add(
                        path.relative_to(REPO_ROOT).as_posix()
                    )
        for relative in sorted(set(outputs) | committed):
            expected = outputs.get(relative)
            target = REPO_ROOT / relative
            if expected is None:
                drift.append(
                    f"{relative}: committed generated file has no generator"
                    " output; remove it or teach the generator about it"
                )
                continue
            if not target.exists():
                drift.append(f"{relative}: generated file is missing")
                continue
            actual = target.read_text(encoding="utf-8")
            if actual != expected:
                drift.append(
                    f"{relative}: regeneration changes the committed output;"
                    " run `make protocol` and commit the result"
                )
    if drift:
        for entry in drift:
            print(f"generate_protocol: {entry}", file=sys.stderr)
        print(
            "generate_protocol: regeneration drift detected (SPEC.md"
            " section 17: continuous integration must fail when"
            " regeneration changes committed output)",
            file=sys.stderr,
        )
        return 1
    print(
        "generate_protocol: the committed JSON Schema export and the"
        " TypeScript and Swift models are current"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temporary directory and fail on drift",
    )
    arguments = parser.parse_args()
    outputs = render_outputs()
    if arguments.check:
        return check_outputs(outputs)
    write_outputs(outputs, REPO_ROOT)
    for relative in sorted(outputs):
        print(f"generate_protocol: wrote {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
