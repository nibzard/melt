// Generated file - do not edit by hand.
// Regenerate with: make protocol (scripts/generate_protocol.py)
// Source of truth: python/melt/schema, the single restricted vocabulary
// of SPEC.md section 17. Do not author an independent schema here.


// Table-driven validator and wire-form normalizer.
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
    // The vocabulary patterns end with the Python-only \z anchor. In
    // JavaScript the unadorned $ anchor is the same end-of-input test.
    pattern = new RegExp(source.replace(/\\z/g, "$"));
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


// ---------------------------------------------------------------------------
// The vocabulary table
// ---------------------------------------------------------------------------

export const MELT_MODEL_FIELDS: Record<string, FieldSpec[]> = {
  "SourceReference": [
    {name: "sourceId", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "version", type: {"kind": "integer"}, required: true, default: null},
  ],
  "TableColumn": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "type", type: {"kind": "literal", "values": ["string", "bool", "int64", "float64", "decimal", "date", "timestamp"]}, required: true, default: null},
    {name: "nullable", type: {"kind":"boolean"}, required: true, default: null},
    {name: "label", type: {"kind": "union", "members": [{"kind": "string", "minLength": 1, "maxLength": 256}, {"kind": "null"}]}, required: false, default: null},
    {name: "precision", type: {"kind": "union", "members": [{"kind": "integer"}, {"kind": "null"}]}, required: false, default: null},
    {name: "scale", type: {"kind": "union", "members": [{"kind": "integer"}, {"kind": "null"}]}, required: false, default: null},
  ],
  "TableSchema": [
    {name: "columns", type: {"kind": "array", "item": {"kind": "model", "name": "TableColumn"}}, required: true, default: null},
    {name: "rowKey", type: {"kind": "union", "members": [{"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, {"kind": "null"}]}, required: false, default: null},
  ],
  "ImageSchema": [
    {name: "width", type: {"kind": "integer"}, required: true, default: null},
    {name: "height", type: {"kind": "integer"}, required: true, default: null},
  ],
  "TextSchema": [
    {name: "encoding", type: {"kind": "literal", "values": ["utf-8"]}, required: true, default: null},
  ],
  "ImportOrigin": [
    {name: "kind", type: {"kind": "literal", "values": ["import"]}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
  ],
  "RunOrigin": [
    {name: "kind", type: {"kind": "literal", "values": ["run"]}, required: true, default: null},
    {name: "runId", type: {"kind": "string", "minLength": 1, "maxLength": 128}, required: true, default: null},
    {name: "output", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "SourceVersion": [
    {name: "artifact", type: {"kind": "string", "pattern": "^sha256:[0-9a-f]{64}\\z"}, required: true, default: null},
    {name: "mediaType", type: {"kind": "string"}, required: true, default: null},
    {name: "byteLength", type: {"kind": "integer"}, required: true, default: null},
    {name: "schema", type: {"kind": "union", "members": [{"kind": "model", "name": "TableSchema"}, {"kind": "model", "name": "ImageSchema"}, {"kind": "model", "name": "TextSchema"}], "dispatch": [{"kind": "anyKeys", "keys": ["columns", "rowKey"], "member": {"kind": "model", "name": "TableSchema"}}, {"kind": "anyKeys", "keys": ["width", "height"], "member": {"kind": "model", "name": "ImageSchema"}}, {"kind": "anyKeys", "keys": ["encoding"], "member": {"kind": "model", "name": "TextSchema"}}]}, required: true, default: null},
    {name: "origin", type: {"kind": "union", "members": [{"kind": "model", "name": "ImportOrigin"}, {"kind": "model", "name": "RunOrigin"}], "discriminator": "kind", "discriminatorMap": {"import": "ImportOrigin", "run": "RunOrigin"}}, required: true, default: null},
  ],
  "Source": [
    {name: "kind", type: {"kind": "literal", "values": ["table", "image", "text"]}, required: true, default: null},
    {name: "currentVersion", type: {"kind": "integer"}, required: true, default: null},
    {name: "versions", type: {"kind": "map", "value": {"kind": "model", "name": "SourceVersion"}, "keyPattern": "^[1-9][0-9]*\\z"}, required: true, default: null},
  ],
  "ParameterConstraints": [
    {name: "minimum", type: {"kind": "union", "members": [{"kind": "number"}, {"kind": "null"}]}, required: false, default: null},
    {name: "maximum", type: {"kind": "union", "members": [{"kind": "number"}, {"kind": "null"}]}, required: false, default: null},
    {name: "maxLength", type: {"kind": "union", "members": [{"kind": "integer"}, {"kind": "null"}]}, required: false, default: null},
    {name: "enum", type: {"kind": "union", "members": [{"kind": "array", "item": {"kind": "string"}}, {"kind": "null"}]}, required: false, default: null},
  ],
  "Parameter": [
    {name: "type", type: {"kind": "literal", "values": ["string", "number", "integer", "boolean"]}, required: true, default: null},
    {name: "default", type: {"kind": "union", "members": [{"kind": "boolean"}, {"kind": "number"}, {"kind": "string"}, {"kind": "null"}]}, required: true, default: null},
    {name: "nullable", type: {"kind":"boolean"}, required: true, default: null},
    {name: "constraints", type: {"kind": "model", "name": "ParameterConstraints"}, required: true, default: null},
  ],
  "SelectOption": [
    {name: "value", type: {"kind": "string"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
  ],
  "NumberControl": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["number"]}, required: true, default: null},
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "TextControl": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["text"]}, required: true, default: null},
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "ToggleControl": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["toggle"]}, required: true, default: null},
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "SelectControl": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["select"]}, required: true, default: null},
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "options", type: {"kind": "array", "item": {"kind": "model", "name": "SelectOption"}}, required: true, default: null},
  ],
  "ButtonControl": [
    {name: "id", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["button"]}, required: true, default: null},
  ],
  "ActionImplementation": [
    {name: "registrationId", type: {"kind": "string", "minLength": 1, "maxLength": 128}, required: true, default: null},
  ],
  "ActionParameterMapping": [
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "Action": [
    {name: "implementation", type: {"kind": "model", "name": "ActionImplementation"}, required: true, default: null},
    {name: "parameters", type: {"kind": "map", "value": {"kind": "model", "name": "ActionParameterMapping"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "inputs", type: {"kind": "map", "value": {"kind": "model", "name": "SourceReference"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "outputs", type: {"kind": "map", "value": {"kind": "literal", "values": ["table", "image", "text"]}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "outputGroup", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "targets", type: {"kind": "map", "value": {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "TableConfig": [
    {name: "columns", type: {"kind": "union", "members": [{"kind": "array", "item": {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}}, {"kind": "null"}]}, required: true, default: null},
    {name: "selection", type: {"kind": "literal", "values": ["none", "single"]}, required: true, default: null},
  ],
  "PlotConfig": [
    {name: "fit", type: {"kind": "literal", "values": ["contain"]}, required: true, default: null},
  ],
  "DocumentConfig": [
    {name: "format", type: {"kind": "literal", "values": ["plain", "markdown"]}, required: true, default: null},
  ],
  "TableProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "null"}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["table"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "TableConfig"}, required: true, default: null},
  ],
  "PlotProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "null"}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["plot"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "PlotConfig"}, required: true, default: null},
  ],
  "DocumentProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "null"}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["document"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "DocumentConfig"}, required: true, default: null},
  ],
  "ParameterChangedEvent": [
    {name: "kind", type: {"kind": "literal", "values": ["parameter.changed"]}, required: true, default: null},
    {name: "parameter", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "ControlActivateEvent": [
    {name: "kind", type: {"kind": "literal", "values": ["control.activate"]}, required: true, default: null},
    {name: "projection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "control", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "ActionInvokeEffect": [
    {name: "kind", type: {"kind": "literal", "values": ["action.invoke"]}, required: true, default: null},
    {name: "action", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "Binding": [
    {name: "event", type: {"kind": "union", "members": [{"kind": "model", "name": "ParameterChangedEvent"}, {"kind": "model", "name": "ControlActivateEvent"}], "discriminator": "kind", "discriminatorMap": {"parameter.changed": "ParameterChangedEvent", "control.activate": "ControlActivateEvent"}}, required: true, default: null},
    {name: "effect", type: {"kind": "model", "name": "ActionInvokeEffect"}, required: true, default: null},
    {name: "debounceMs", type: {"kind": "integer"}, required: true, default: null},
  ],
  "PaneNode": [
    {name: "kind", type: {"kind": "literal", "values": ["pane"]}, required: true, default: null},
    {name: "projection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "SplitNode": [
    {name: "kind", type: {"kind": "literal", "values": ["split"]}, required: true, default: null},
    {name: "axis", type: {"kind": "literal", "values": ["horizontal", "vertical"]}, required: true, default: null},
    {name: "ratio", type: {"kind":"number"}, required: true, default: null},
    {name: "first", type: {"kind": "union", "members": [{"kind": "model", "name": "PaneNode"}, {"kind": "model", "name": "SplitNode"}], "discriminator": "kind", "discriminatorMap": {"pane": "PaneNode", "split": "SplitNode"}}, required: true, default: null},
    {name: "second", type: {"kind": "union", "members": [{"kind": "model", "name": "PaneNode"}, {"kind": "model", "name": "SplitNode"}], "discriminator": "kind", "discriminatorMap": {"pane": "PaneNode", "split": "SplitNode"}}, required: true, default: null},
  ],
  "Layout": [
    {name: "root", type: {"kind": "union", "members": [{"kind": "union", "members": [{"kind": "model", "name": "PaneNode"}, {"kind": "model", "name": "SplitNode"}], "discriminator": "kind", "discriminatorMap": {"pane": "PaneNode", "split": "SplitNode"}}, {"kind": "null"}]}, required: true, default: null},
    {name: "hidden", type: {"kind": "array", "item": {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}}, required: true, default: null},
  ],
  "Document": [
    {name: "language", type: {"kind": "literal", "values": ["melt/v0.1"]}, required: true, default: null},
    {name: "workspaceId", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "sources", type: {"kind": "map", "value": {"kind": "model", "name": "Source"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "parameters", type: {"kind": "map", "value": {"kind": "model", "name": "Parameter"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "actions", type: {"kind": "map", "value": {"kind": "model", "name": "Action"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "projections", type: {"kind": "map", "value": {"kind": "union", "members": [{"kind": "model", "name": "TableProjection"}, {"kind": "model", "name": "PlotProjection"}, {"kind": "model", "name": "DocumentProjection"}], "discriminator": "kind", "discriminatorMap": {"table": "TableProjection", "plot": "PlotProjection", "document": "DocumentProjection"}}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "bindings", type: {"kind": "map", "value": {"kind": "model", "name": "Binding"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "layout", type: {"kind": "model", "name": "Layout"}, required: true, default: null},
  ],
  "SemanticCollection": [
    {name: "source", type: {"kind": "model", "name": "SourceReference"}, required: true, default: null},
    {name: "key", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "ReferenceType": [
    {name: "kind", type: {"kind": "literal", "values": ["ref"]}, required: true, default: null},
    {name: "collection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "SemanticState": [
    {name: "type", type: {"kind": "model", "name": "ReferenceType"}, required: true, default: null},
    {name: "nullable", type: {"kind":"boolean"}, required: true, default: null},
    {name: "default", type: {"kind":"null"}, required: true, default: null},
    {name: "scope", type: {"kind": "literal", "values": ["workspace"]}, required: true, default: null},
  ],
  "SemanticReference": [
    {name: "collection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "incarnation", type: {"kind": "string", "minLength": 1}, required: true, default: null},
    {name: "key", type: {"kind": "string"}, required: true, default: null},
  ],
  "SemanticAction": [
    {name: "implementation", type: {"kind": "model", "name": "ActionImplementation"}, required: true, default: null},
    {name: "parameters", type: {"kind": "map", "value": {"kind": "model", "name": "ActionParameterMapping"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "inputs", type: {"kind": "map", "value": {"kind": "model", "name": "SourceReference"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "outputs", type: {"kind": "map", "value": {"kind": "literal", "values": ["table", "image", "text"]}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "outputGroup", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "ActionOutputId": [
    {name: "action", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "output", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "incarnation", type: {"kind": "string", "minLength": 1}, required: true, default: null},
  ],
  "OutputSubscription": [
    {name: "actionOutput", type: {"kind": "model", "name": "ActionOutputId"}, required: true, default: null},
    {name: "follow", type: {"kind": "literal", "values": ["current", "pinned"]}, required: true, default: null},
    {name: "version", type: {"kind": "union", "members": [{"kind": "integer"}, {"kind": "null"}]}, required: false, default: null},
  ],
  "GraphNode": [
    {name: "nodeId", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "label", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "collection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "key", type: {"kind": "string"}, required: true, default: null},
  ],
  "GraphEdge": [
    {name: "edgeId", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "from", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "to", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "GraphSchema": [
    {name: "nodes", type: {"kind": "array", "item": {"kind": "model", "name": "GraphNode"}}, required: true, default: null},
    {name: "edges", type: {"kind": "array", "item": {"kind": "model", "name": "GraphEdge"}}, required: false, default: []},
  ],
  "GraphSourceVersion": [
    {name: "artifact", type: {"kind": "string", "pattern": "^sha256:[0-9a-f]{64}\\z"}, required: true, default: null},
    {name: "mediaType", type: {"kind": "string"}, required: true, default: null},
    {name: "byteLength", type: {"kind": "integer"}, required: true, default: null},
    {name: "schema", type: {"kind": "model", "name": "GraphSchema"}, required: true, default: null},
    {name: "origin", type: {"kind": "union", "members": [{"kind": "model", "name": "ImportOrigin"}, {"kind": "model", "name": "RunOrigin"}], "discriminator": "kind", "discriminatorMap": {"import": "ImportOrigin", "run": "RunOrigin"}}, required: true, default: null},
  ],
  "GraphSource": [
    {name: "kind", type: {"kind": "literal", "values": ["graph"]}, required: true, default: null},
    {name: "currentVersion", type: {"kind": "integer"}, required: true, default: null},
    {name: "versions", type: {"kind": "map", "value": {"kind": "model", "name": "GraphSourceVersion"}, "keyPattern": "^[1-9][0-9]*\\z"}, required: true, default: null},
  ],
  "GraphConfig": [
    {name: "nodeLabels", type: {"kind": "literal", "values": ["show", "hide"]}, required: true, default: null},
  ],
  "RecordConfig": [
    {name: "fields", type: {"kind": "union", "members": [{"kind": "array", "item": {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}}, {"kind": "null"}]}, required: true, default: null},
  ],
  "HighlightObserve": [
    {name: "state", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "RecordObserve": [
    {name: "state", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "GraphObservers": [
    {name: "highlight", type: {"kind": "model", "name": "HighlightObserve"}, required: true, default: null},
  ],
  "RecordObservers": [
    {name: "record", type: {"kind": "model", "name": "RecordObserve"}, required: true, default: null},
  ],
  "SemanticTableProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "model", "name": "OutputSubscription"}, {"kind": "null"}], "dispatch": [{"kind": "anyKeys", "keys": ["actionOutput"], "member": {"kind": "model", "name": "OutputSubscription"}}, {"kind": "fallback", "member": {"kind": "model", "name": "SourceReference"}}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["table"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "TableConfig"}, required: true, default: null},
  ],
  "SemanticPlotProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "model", "name": "OutputSubscription"}, {"kind": "null"}], "dispatch": [{"kind": "anyKeys", "keys": ["actionOutput"], "member": {"kind": "model", "name": "OutputSubscription"}}, {"kind": "fallback", "member": {"kind": "model", "name": "SourceReference"}}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["plot"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "PlotConfig"}, required: true, default: null},
  ],
  "SemanticDocumentProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "source", type: {"kind": "union", "members": [{"kind": "model", "name": "SourceReference"}, {"kind": "model", "name": "OutputSubscription"}, {"kind": "null"}], "dispatch": [{"kind": "anyKeys", "keys": ["actionOutput"], "member": {"kind": "model", "name": "OutputSubscription"}}, {"kind": "fallback", "member": {"kind": "model", "name": "SourceReference"}}]}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["document"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "DocumentConfig"}, required: true, default: null},
  ],
  "GraphProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "collection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["graph"]}, required: true, default: null},
    {name: "source", type: {"kind": "model", "name": "SourceReference"}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "GraphConfig"}, required: true, default: null},
    {name: "observe", type: {"kind": "model", "name": "GraphObservers"}, required: true, default: null},
  ],
  "RecordProjection": [
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "collection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "controls", type: {"kind": "array", "item": {"kind": "union", "members": [{"kind": "model", "name": "NumberControl"}, {"kind": "model", "name": "TextControl"}, {"kind": "model", "name": "ToggleControl"}, {"kind": "model", "name": "SelectControl"}, {"kind": "model", "name": "ButtonControl"}], "discriminator": "kind", "discriminatorMap": {"number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"}}}, required: true, default: null},
    {name: "kind", type: {"kind": "literal", "values": ["record"]}, required: true, default: null},
    {name: "config", type: {"kind": "model", "name": "RecordConfig"}, required: true, default: null},
    {name: "observe", type: {"kind": "model", "name": "RecordObservers"}, required: true, default: null},
  ],
  "ObjectSelectEvent": [
    {name: "kind", type: {"kind": "literal", "values": ["object.select"]}, required: true, default: null},
    {name: "projection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "SelectionClearEvent": [
    {name: "kind", type: {"kind": "literal", "values": ["selection.clear"]}, required: true, default: null},
    {name: "projection", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
  "EventFieldValue": [
    {name: "eventField", type: {"kind": "literal", "values": ["objectRef"]}, required: true, default: null},
  ],
  "NullLiteralValue": [
    {name: "literal", type: {"kind":"null"}, required: true, default: null},
  ],
  "StateSetEffect": [
    {name: "kind", type: {"kind": "literal", "values": ["state.set"]}, required: true, default: null},
    {name: "target", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "value", type: {"kind": "union", "members": [{"kind": "model", "name": "EventFieldValue"}, {"kind": "model", "name": "NullLiteralValue"}], "dispatch": [{"kind": "exactKeys", "keys": ["eventField"], "member": {"kind": "model", "name": "EventFieldValue"}}, {"kind": "exactKeys", "keys": ["literal"], "member": {"kind": "model", "name": "NullLiteralValue"}}]}, required: true, default: null},
  ],
  "SemanticBinding": [
    {name: "event", type: {"kind": "union", "members": [{"kind": "model", "name": "ParameterChangedEvent"}, {"kind": "model", "name": "ControlActivateEvent"}, {"kind": "model", "name": "ObjectSelectEvent"}, {"kind": "model", "name": "SelectionClearEvent"}], "discriminator": "kind", "discriminatorMap": {"parameter.changed": "ParameterChangedEvent", "control.activate": "ControlActivateEvent", "object.select": "ObjectSelectEvent", "selection.clear": "SelectionClearEvent"}}, required: true, default: null},
    {name: "effect", type: {"kind": "union", "members": [{"kind": "model", "name": "ActionInvokeEffect"}, {"kind": "model", "name": "StateSetEffect"}], "discriminator": "kind", "discriminatorMap": {"action.invoke": "ActionInvokeEffect", "state.set": "StateSetEffect"}}, required: true, default: null},
    {name: "debounceMs", type: {"kind": "integer"}, required: false, default: 0},
  ],
  "SemanticDocument": [
    {name: "language", type: {"kind": "literal", "values": ["melt/semantic-draft-1"]}, required: true, default: null},
    {name: "workspaceId", type: {"kind": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "title", type: {"kind": "string", "minLength": 1, "maxLength": 256}, required: true, default: null},
    {name: "sources", type: {"kind": "map", "value": {"kind": "union", "members": [{"kind": "model", "name": "Source"}, {"kind": "model", "name": "GraphSource"}], "dispatch": [{"kind": "fieldEquals", "field": "kind", "value": "graph", "member": {"kind": "model", "name": "GraphSource"}}, {"kind": "fallback", "member": {"kind": "model", "name": "Source"}}]}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "parameters", type: {"kind": "map", "value": {"kind": "model", "name": "Parameter"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "actions", type: {"kind": "map", "value": {"kind": "model", "name": "SemanticAction"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "projections", type: {"kind": "map", "value": {"kind": "union", "members": [{"kind": "model", "name": "SemanticTableProjection"}, {"kind": "model", "name": "SemanticPlotProjection"}, {"kind": "model", "name": "SemanticDocumentProjection"}, {"kind": "model", "name": "GraphProjection"}, {"kind": "model", "name": "RecordProjection"}], "discriminator": "kind", "discriminatorMap": {"table": "SemanticTableProjection", "plot": "SemanticPlotProjection", "document": "SemanticDocumentProjection", "graph": "GraphProjection", "record": "RecordProjection"}}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "bindings", type: {"kind": "map", "value": {"kind": "model", "name": "SemanticBinding"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "layout", type: {"kind": "model", "name": "Layout"}, required: true, default: null},
    {name: "collections", type: {"kind": "map", "value": {"kind": "model", "name": "SemanticCollection"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
    {name: "state", type: {"kind": "map", "value": {"kind": "model", "name": "SemanticState"}, "keyPattern": "^[a-z][a-z0-9_-]{0,63}\\z"}, required: true, default: null},
  ],
};

// ---------------------------------------------------------------------------
// TypeScript models
// ---------------------------------------------------------------------------

export interface SourceReference {
  sourceId: string;
  version: number;
}

export interface TableColumn {
  id: string;
  type: "string" | "bool" | "int64" | "float64" | "decimal" | "date" | "timestamp";
  nullable: boolean;
  label?: string | null;
  precision?: number | null;
  scale?: number | null;
}

export interface TableSchema {
  columns: TableColumn[];
  rowKey?: string | null;
}

export interface ImageSchema {
  width: number;
  height: number;
}

export interface TextSchema {
  encoding: "utf-8";
}

export interface ImportOrigin {
  kind: "import";
  label: string;
}

export interface RunOrigin {
  kind: "run";
  runId: string;
  output: string;
}

export interface SourceVersion {
  artifact: string;
  mediaType: string;
  byteLength: number;
  schema: SourceSchema;
  origin: SourceOrigin;
}

export type SourceSchema = TableSchema | ImageSchema | TextSchema;

export type SourceOrigin = ImportOrigin | RunOrigin;

export interface Source {
  kind: "table" | "image" | "text";
  currentVersion: number;
  versions: Record<string, SourceVersion>;
}

export interface ParameterConstraints {
  minimum?: number | null;
  maximum?: number | null;
  maxLength?: number | null;
  enum?: string[] | null;
}

export interface Parameter {
  type: "string" | "number" | "integer" | "boolean";
  default: boolean | number | string | null;
  nullable: boolean;
  constraints: ParameterConstraints;
}

export interface SelectOption {
  value: string;
  label: string;
}

export interface NumberControl {
  id: string;
  label: string;
  kind: "number";
  parameter: string;
}

export interface TextControl {
  id: string;
  label: string;
  kind: "text";
  parameter: string;
}

export interface ToggleControl {
  id: string;
  label: string;
  kind: "toggle";
  parameter: string;
}

export interface SelectControl {
  id: string;
  label: string;
  kind: "select";
  parameter: string;
  options: SelectOption[];
}

export interface ButtonControl {
  id: string;
  label: string;
  kind: "button";
}

export interface ActionImplementation {
  registrationId: string;
}

export interface ActionParameterMapping {
  parameter: string;
}

export interface Action {
  implementation: ActionImplementation;
  parameters: Record<string, ActionParameterMapping>;
  inputs: Record<string, SourceReference>;
  outputs: Record<string, "table" | "image" | "text">;
  outputGroup: string;
  targets: Record<string, string>;
}

export interface TableConfig {
  columns: string[] | null;
  selection: "none" | "single";
}

export interface PlotConfig {
  fit: "contain";
}

export interface DocumentConfig {
  format: "plain" | "markdown";
}

export interface TableProjection {
  title: string;
  source: SourceReference | null;
  controls: Control[];
  kind: "table";
  config: TableConfig;
}

export type Control = NumberControl | TextControl | ToggleControl | SelectControl | ButtonControl;

export interface PlotProjection {
  title: string;
  source: SourceReference | null;
  controls: Control[];
  kind: "plot";
  config: PlotConfig;
}

export interface DocumentProjection {
  title: string;
  source: SourceReference | null;
  controls: Control[];
  kind: "document";
  config: DocumentConfig;
}

export interface ParameterChangedEvent {
  kind: "parameter.changed";
  parameter: string;
}

export interface ControlActivateEvent {
  kind: "control.activate";
  projection: string;
  control: string;
}

export interface ActionInvokeEffect {
  kind: "action.invoke";
  action: string;
}

export interface Binding {
  event: BindingEvent;
  effect: ActionInvokeEffect;
  debounceMs: number;
}

export type BindingEvent = ParameterChangedEvent | ControlActivateEvent;

export interface PaneNode {
  kind: "pane";
  projection: string;
}

export interface SplitNode {
  kind: "split";
  axis: "horizontal" | "vertical";
  ratio: number;
  first: LayoutNode;
  second: LayoutNode;
}

export type LayoutNode = PaneNode | SplitNode;

export interface Layout {
  root: LayoutNode | null;
  hidden: string[];
}

export interface Document {
  language: "melt/v0.1";
  workspaceId: string;
  title: string;
  sources: Record<string, Source>;
  parameters: Record<string, Parameter>;
  actions: Record<string, Action>;
  projections: Record<string, Projection>;
  bindings: Record<string, Binding>;
  layout: Layout;
}

export type Projection = TableProjection | PlotProjection | DocumentProjection;

export interface SemanticCollection {
  source: SourceReference;
  key: string;
}

export interface ReferenceType {
  kind: "ref";
  collection: string;
}

export interface SemanticState {
  type: ReferenceType;
  nullable: boolean;
  default: null;
  scope: "workspace";
}

export interface SemanticReference {
  collection: string;
  incarnation: string;
  key: string;
}

export interface SemanticAction {
  implementation: ActionImplementation;
  parameters: Record<string, ActionParameterMapping>;
  inputs: Record<string, SourceReference>;
  outputs: Record<string, "table" | "image" | "text">;
  outputGroup: string;
}

export interface ActionOutputId {
  action: string;
  output: string;
  incarnation: string;
}

export interface OutputSubscription {
  actionOutput: ActionOutputId;
  follow: "current" | "pinned";
  version?: number | null;
}

export interface GraphNode {
  nodeId: string;
  label: string;
  collection: string;
  key: string;
}

export interface GraphEdge {
  edgeId: string;
  from: string;
  to: string;
}

export interface GraphSchema {
  nodes: GraphNode[];
  edges?: GraphEdge[];
}

export interface GraphSourceVersion {
  artifact: string;
  mediaType: string;
  byteLength: number;
  schema: GraphSchema;
  origin: SourceOrigin;
}

export interface GraphSource {
  kind: "graph";
  currentVersion: number;
  versions: Record<string, GraphSourceVersion>;
}

export interface GraphConfig {
  nodeLabels: "show" | "hide";
}

export interface RecordConfig {
  fields: string[] | null;
}

export interface HighlightObserve {
  state: string;
}

export interface RecordObserve {
  state: string;
}

export interface GraphObservers {
  highlight: HighlightObserve;
}

export interface RecordObservers {
  record: RecordObserve;
}

export interface SemanticTableProjection {
  title: string;
  source: ProjectionSource | null;
  controls: Control[];
  kind: "table";
  config: TableConfig;
}

export type ProjectionSource = SourceReference | OutputSubscription;

export interface SemanticPlotProjection {
  title: string;
  source: ProjectionSource | null;
  controls: Control[];
  kind: "plot";
  config: PlotConfig;
}

export interface SemanticDocumentProjection {
  title: string;
  source: ProjectionSource | null;
  controls: Control[];
  kind: "document";
  config: DocumentConfig;
}

export interface GraphProjection {
  title: string;
  collection: string;
  controls: Control[];
  kind: "graph";
  source: SourceReference;
  config: GraphConfig;
  observe: GraphObservers;
}

export interface RecordProjection {
  title: string;
  collection: string;
  controls: Control[];
  kind: "record";
  config: RecordConfig;
  observe: RecordObservers;
}

export interface ObjectSelectEvent {
  kind: "object.select";
  projection: string;
}

export interface SelectionClearEvent {
  kind: "selection.clear";
  projection: string;
}

export interface EventFieldValue {
  eventField: "objectRef";
}

export interface NullLiteralValue {
  literal: null;
}

export interface StateSetEffect {
  kind: "state.set";
  target: string;
  value: StateSetValue;
}

export type StateSetValue = EventFieldValue | NullLiteralValue;

export interface SemanticBinding {
  event: SemanticBindingEvent;
  effect: SemanticBindingEffect;
  debounceMs?: number;
}

export type SemanticBindingEvent = ParameterChangedEvent | ControlActivateEvent | ObjectSelectEvent | SelectionClearEvent;

export type SemanticBindingEffect = ActionInvokeEffect | StateSetEffect;

export interface SemanticDocument {
  language: "melt/semantic-draft-1";
  workspaceId: string;
  title: string;
  sources: Record<string, SemanticSource>;
  parameters: Record<string, Parameter>;
  actions: Record<string, SemanticAction>;
  projections: Record<string, SemanticProjection>;
  bindings: Record<string, SemanticBinding>;
  layout: Layout;
  collections: Record<string, SemanticCollection>;
  state: Record<string, SemanticState>;
}

export type SemanticSource = Source | GraphSource;

export type SemanticProjection = SemanticTableProjection | SemanticPlotProjection | SemanticDocumentProjection | GraphProjection | RecordProjection;

export const SEMANTIC_LANGUAGE_ID = "melt/semantic-draft-1";

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
