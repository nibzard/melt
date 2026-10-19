// Generated file - do not edit by hand.
// Regenerate with: make protocol (scripts/generate_protocol.py)
// Source of truth: python/melt/schema, the single restricted vocabulary
// of SPEC.md section 17. Do not author an independent schema here.


#if canImport(Glibc)
import Glibc
#else
import Darwin
#endif
import Foundation

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
    public var description: String { "\(path): \(message)" }
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
        // The vocabulary patterns end with the Python-only \z anchor.
        // ICU shares the same meaning through the unadorned $ anchor.
        let regex = try NSRegularExpression(
            pattern: source.replacingOccurrences(of: "\\z", with: "$")
        )
        patternCache[source] = regex
        return regex
    } catch {
        fatalError("the generated vocabulary patterns are valid ICU expressions: \(error)")
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
            throw MeltModelError(path, "string does not match \(pattern)")
        }
        if let minLength, text.count < minLength {
            throw MeltModelError(path, "string is shorter than \(minLength)")
        }
        if let maxLength, text.count > maxLength {
            throw MeltModelError(path, "string is longer than \(maxLength)")
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
            throw MeltModelError(path, "integer must be greater than \(gt)")
        }
        if let ge, !(raw >= Double(ge)) {
            throw MeltModelError(path, "integer must be at least \(ge)")
        }
        if let lt, !(raw < Double(lt)) {
            throw MeltModelError(path, "integer must be less than \(lt)")
        }
        if let le, !(raw <= Double(le)) {
            throw MeltModelError(path, "integer must be at most \(le)")
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
            throw MeltModelError(path, "expected one of \(allowed.joined(separator: ", "))")
        }
        return value
    case .array(let item, let minItems):
        guard case .array(let items) = value else {
            throw MeltModelError(path, "expected an array")
        }
        if let minItems, items.count < minItems {
            throw MeltModelError(path, "array holds fewer than \(minItems) items")
        }
        var normalized: [JsonValue] = []
        normalized.reserveCapacity(items.count)
        for (index, element) in items.enumerated() {
            normalized.append(try checkValue(item, element, "\(path)[\(index)]"))
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
                    "\(path)/\(key)",
                    "object key does not match the identifier pattern"
                )
            }
            normalized[key] = try checkValue(valueType, item, "\(path)/\(key)")
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
            throw MeltModelError("\(path)/\(discriminator)", "unknown union member")
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
        throw MeltModelError(path, "unknown model \(name)")
    }
    guard case .object(let object) = value else {
        throw MeltModelError(path, "\(name) must be an object")
    }
    let known = Set(fields.map { $0.name })
    for key in object.keys where !known.contains(key) {
        throw MeltModelError(
            "\(path)/\(key)",
            "\(name) rejects unknown field \"\(key)\""
        )
    }
    var result: [String: JsonValue] = [:]
    for field in fields {
        if let present = object[field.name] {
            result[field.name] = try checkValue(
                field.type,
                present,
                "\(path)/\(field.name)"
            )
        } else if field.required {
            throw MeltModelError(
                "\(path)/\(field.name)",
                "\(name) requires field \"\(field.name)\""
            )
        } else {
            result[field.name] = field.defaultValue
        }
    }
    return .object(result)
}


// ---------------------------------------------------------------------------
// The vocabulary table
// ---------------------------------------------------------------------------

let meltModelTable: [String: [FieldSpec]] = [
    "SourceReference": [
        FieldSpec(name: "sourceId", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "version", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
    ],
    "TableColumn": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "type", type: .literal(["string", "bool", "int64", "float64", "decimal", "date", "timestamp"]), required: true, defaultValue: .null),
        FieldSpec(name: "nullable", type: .boolean, required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .union(members: [.string(pattern:nil, minLength:1, maxLength:256), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
        FieldSpec(name: "precision", type: .union(members: [.integer(gt: nil, ge: nil, lt: nil, le: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
        FieldSpec(name: "scale", type: .union(members: [.integer(gt: nil, ge: nil, lt: nil, le: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
    ],
    "TableSchema": [
        FieldSpec(name: "columns", type: .array(item: .model(name: "TableColumn"), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "rowKey", type: .union(members: [.string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
    ],
    "ImageSchema": [
        FieldSpec(name: "width", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
        FieldSpec(name: "height", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
    ],
    "TextSchema": [
        FieldSpec(name: "encoding", type: .literal(["utf-8"]), required: true, defaultValue: .null),
    ],
    "ImportOrigin": [
        FieldSpec(name: "kind", type: .literal(["import"]), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
    ],
    "RunOrigin": [
        FieldSpec(name: "kind", type: .literal(["run"]), required: true, defaultValue: .null),
        FieldSpec(name: "runId", type: .string(pattern:nil, minLength:1, maxLength:128), required: true, defaultValue: .null),
        FieldSpec(name: "output", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SourceVersion": [
        FieldSpec(name: "artifact", type: .string(pattern:"^sha256:[0-9a-f]{64}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "mediaType", type: .string(pattern:nil, minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "byteLength", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
        FieldSpec(name: "schema", type: .union(members: [.model(name: "TableSchema"), .model(name: "ImageSchema"), .model(name: "TextSchema")], discriminator: nil, discriminatorMap: nil, dispatch: [.anyKeys(["columns", "rowKey"], .model(name: "TableSchema")), .anyKeys(["width", "height"], .model(name: "ImageSchema")), .anyKeys(["encoding"], .model(name: "TextSchema"))]), required: true, defaultValue: .null),
        FieldSpec(name: "origin", type: .union(members: [.model(name: "ImportOrigin"), .model(name: "RunOrigin")], discriminator: "kind", discriminatorMap: ["import": "ImportOrigin", "run": "RunOrigin"], dispatch: nil), required: true, defaultValue: .null),
    ],
    "Source": [
        FieldSpec(name: "kind", type: .literal(["table", "image", "text"]), required: true, defaultValue: .null),
        FieldSpec(name: "currentVersion", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
        FieldSpec(name: "versions", type: .map(keyPattern: "^[1-9][0-9]*\\z", value: .model(name: "SourceVersion")), required: true, defaultValue: .null),
    ],
    "ParameterConstraints": [
        FieldSpec(name: "minimum", type: .union(members: [.number, .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
        FieldSpec(name: "maximum", type: .union(members: [.number, .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
        FieldSpec(name: "maxLength", type: .union(members: [.integer(gt: nil, ge: nil, lt: nil, le: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
        FieldSpec(name: "enum", type: .union(members: [.array(item: .string(pattern:nil, minLength:nil, maxLength:nil), minItems: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
    ],
    "Parameter": [
        FieldSpec(name: "type", type: .literal(["string", "number", "integer", "boolean"]), required: true, defaultValue: .null),
        FieldSpec(name: "default", type: .union(members: [.boolean, .number, .string(pattern:nil, minLength:nil, maxLength:nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "nullable", type: .boolean, required: true, defaultValue: .null),
        FieldSpec(name: "constraints", type: .model(name: "ParameterConstraints"), required: true, defaultValue: .null),
    ],
    "SelectOption": [
        FieldSpec(name: "value", type: .string(pattern:nil, minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
    ],
    "NumberControl": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["number"]), required: true, defaultValue: .null),
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "TextControl": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["text"]), required: true, defaultValue: .null),
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "ToggleControl": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["toggle"]), required: true, defaultValue: .null),
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SelectControl": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["select"]), required: true, defaultValue: .null),
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "options", type: .array(item: .model(name: "SelectOption"), minItems: nil), required: true, defaultValue: .null),
    ],
    "ButtonControl": [
        FieldSpec(name: "id", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["button"]), required: true, defaultValue: .null),
    ],
    "ActionImplementation": [
        FieldSpec(name: "registrationId", type: .string(pattern:nil, minLength:1, maxLength:128), required: true, defaultValue: .null),
    ],
    "ActionParameterMapping": [
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "Action": [
        FieldSpec(name: "implementation", type: .model(name: "ActionImplementation"), required: true, defaultValue: .null),
        FieldSpec(name: "parameters", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "ActionParameterMapping")), required: true, defaultValue: .null),
        FieldSpec(name: "inputs", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SourceReference")), required: true, defaultValue: .null),
        FieldSpec(name: "outputs", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .literal(["table", "image", "text"])), required: true, defaultValue: .null),
        FieldSpec(name: "outputGroup", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "targets", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil)), required: true, defaultValue: .null),
    ],
    "TableConfig": [
        FieldSpec(name: "columns", type: .union(members: [.array(item: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), minItems: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "selection", type: .literal(["none", "single"]), required: true, defaultValue: .null),
    ],
    "PlotConfig": [
        FieldSpec(name: "fit", type: .literal(["contain"]), required: true, defaultValue: .null),
    ],
    "DocumentConfig": [
        FieldSpec(name: "format", type: .literal(["plain", "markdown"]), required: true, defaultValue: .null),
    ],
    "TableProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["table"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "TableConfig"), required: true, defaultValue: .null),
    ],
    "PlotProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["plot"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "PlotConfig"), required: true, defaultValue: .null),
    ],
    "DocumentProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["document"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "DocumentConfig"), required: true, defaultValue: .null),
    ],
    "ParameterChangedEvent": [
        FieldSpec(name: "kind", type: .literal(["parameter.changed"]), required: true, defaultValue: .null),
        FieldSpec(name: "parameter", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "ControlActivateEvent": [
        FieldSpec(name: "kind", type: .literal(["control.activate"]), required: true, defaultValue: .null),
        FieldSpec(name: "projection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "control", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "ActionInvokeEffect": [
        FieldSpec(name: "kind", type: .literal(["action.invoke"]), required: true, defaultValue: .null),
        FieldSpec(name: "action", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "Binding": [
        FieldSpec(name: "event", type: .union(members: [.model(name: "ParameterChangedEvent"), .model(name: "ControlActivateEvent")], discriminator: "kind", discriminatorMap: ["parameter.changed": "ParameterChangedEvent", "control.activate": "ControlActivateEvent"], dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "effect", type: .model(name: "ActionInvokeEffect"), required: true, defaultValue: .null),
        FieldSpec(name: "debounceMs", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
    ],
    "PaneNode": [
        FieldSpec(name: "kind", type: .literal(["pane"]), required: true, defaultValue: .null),
        FieldSpec(name: "projection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SplitNode": [
        FieldSpec(name: "kind", type: .literal(["split"]), required: true, defaultValue: .null),
        FieldSpec(name: "axis", type: .literal(["horizontal", "vertical"]), required: true, defaultValue: .null),
        FieldSpec(name: "ratio", type: .number, required: true, defaultValue: .null),
        FieldSpec(name: "first", type: .union(members: [.model(name: "PaneNode"), .model(name: "SplitNode")], discriminator: "kind", discriminatorMap: ["pane": "PaneNode", "split": "SplitNode"], dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "second", type: .union(members: [.model(name: "PaneNode"), .model(name: "SplitNode")], discriminator: "kind", discriminatorMap: ["pane": "PaneNode", "split": "SplitNode"], dispatch: nil), required: true, defaultValue: .null),
    ],
    "Layout": [
        FieldSpec(name: "root", type: .union(members: [.union(members: [.model(name: "PaneNode"), .model(name: "SplitNode")], discriminator: "kind", discriminatorMap: ["pane": "PaneNode", "split": "SplitNode"], dispatch: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "hidden", type: .array(item: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), minItems: nil), required: true, defaultValue: .null),
    ],
    "Document": [
        FieldSpec(name: "language", type: .literal(["melt/v0.1"]), required: true, defaultValue: .null),
        FieldSpec(name: "workspaceId", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "sources", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "Source")), required: true, defaultValue: .null),
        FieldSpec(name: "parameters", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "Parameter")), required: true, defaultValue: .null),
        FieldSpec(name: "actions", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "Action")), required: true, defaultValue: .null),
        FieldSpec(name: "projections", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .union(members: [.model(name: "TableProjection"), .model(name: "PlotProjection"), .model(name: "DocumentProjection")], discriminator: "kind", discriminatorMap: ["table": "TableProjection", "plot": "PlotProjection", "document": "DocumentProjection"], dispatch: nil)), required: true, defaultValue: .null),
        FieldSpec(name: "bindings", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "Binding")), required: true, defaultValue: .null),
        FieldSpec(name: "layout", type: .model(name: "Layout"), required: true, defaultValue: .null),
    ],
    "SemanticCollection": [
        FieldSpec(name: "source", type: .model(name: "SourceReference"), required: true, defaultValue: .null),
        FieldSpec(name: "key", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "ReferenceType": [
        FieldSpec(name: "kind", type: .literal(["ref"]), required: true, defaultValue: .null),
        FieldSpec(name: "collection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SemanticState": [
        FieldSpec(name: "type", type: .model(name: "ReferenceType"), required: true, defaultValue: .null),
        FieldSpec(name: "nullable", type: .boolean, required: true, defaultValue: .null),
        FieldSpec(name: "default", type: .nullValue, required: true, defaultValue: .null),
        FieldSpec(name: "scope", type: .literal(["workspace"]), required: true, defaultValue: .null),
    ],
    "SemanticReference": [
        FieldSpec(name: "collection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "incarnation", type: .string(pattern:nil, minLength:1, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "key", type: .string(pattern:nil, minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SemanticAction": [
        FieldSpec(name: "implementation", type: .model(name: "ActionImplementation"), required: true, defaultValue: .null),
        FieldSpec(name: "parameters", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "ActionParameterMapping")), required: true, defaultValue: .null),
        FieldSpec(name: "inputs", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SourceReference")), required: true, defaultValue: .null),
        FieldSpec(name: "outputs", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .literal(["table", "image", "text"])), required: true, defaultValue: .null),
        FieldSpec(name: "outputGroup", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "ActionOutputId": [
        FieldSpec(name: "action", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "output", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "incarnation", type: .string(pattern:nil, minLength:1, maxLength:nil), required: true, defaultValue: .null),
    ],
    "OutputSubscription": [
        FieldSpec(name: "actionOutput", type: .model(name: "ActionOutputId"), required: true, defaultValue: .null),
        FieldSpec(name: "follow", type: .literal(["current", "pinned"]), required: true, defaultValue: .null),
        FieldSpec(name: "version", type: .union(members: [.integer(gt: nil, ge: nil, lt: nil, le: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: false, defaultValue: .null),
    ],
    "GraphNode": [
        FieldSpec(name: "nodeId", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "label", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "collection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "key", type: .string(pattern:nil, minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "GraphEdge": [
        FieldSpec(name: "edgeId", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "from", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "to", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "GraphSchema": [
        FieldSpec(name: "nodes", type: .array(item: .model(name: "GraphNode"), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "edges", type: .array(item: .model(name: "GraphEdge"), minItems: nil), required: false, defaultValue: .array([])),
    ],
    "GraphSourceVersion": [
        FieldSpec(name: "artifact", type: .string(pattern:"^sha256:[0-9a-f]{64}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "mediaType", type: .string(pattern:nil, minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "byteLength", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
        FieldSpec(name: "schema", type: .model(name: "GraphSchema"), required: true, defaultValue: .null),
        FieldSpec(name: "origin", type: .union(members: [.model(name: "ImportOrigin"), .model(name: "RunOrigin")], discriminator: "kind", discriminatorMap: ["import": "ImportOrigin", "run": "RunOrigin"], dispatch: nil), required: true, defaultValue: .null),
    ],
    "GraphSource": [
        FieldSpec(name: "kind", type: .literal(["graph"]), required: true, defaultValue: .null),
        FieldSpec(name: "currentVersion", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: true, defaultValue: .null),
        FieldSpec(name: "versions", type: .map(keyPattern: "^[1-9][0-9]*\\z", value: .model(name: "GraphSourceVersion")), required: true, defaultValue: .null),
    ],
    "GraphConfig": [
        FieldSpec(name: "nodeLabels", type: .literal(["show", "hide"]), required: true, defaultValue: .null),
    ],
    "RecordConfig": [
        FieldSpec(name: "fields", type: .union(members: [.array(item: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), minItems: nil), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: nil), required: true, defaultValue: .null),
    ],
    "HighlightObserve": [
        FieldSpec(name: "state", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "RecordObserve": [
        FieldSpec(name: "state", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "GraphObservers": [
        FieldSpec(name: "highlight", type: .model(name: "HighlightObserve"), required: true, defaultValue: .null),
    ],
    "RecordObservers": [
        FieldSpec(name: "record", type: .model(name: "RecordObserve"), required: true, defaultValue: .null),
    ],
    "SemanticTableProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .model(name: "OutputSubscription"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: [.anyKeys(["actionOutput"], .model(name: "OutputSubscription")), .fallback(.model(name: "SourceReference"))]), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["table"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "TableConfig"), required: true, defaultValue: .null),
    ],
    "SemanticPlotProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .model(name: "OutputSubscription"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: [.anyKeys(["actionOutput"], .model(name: "OutputSubscription")), .fallback(.model(name: "SourceReference"))]), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["plot"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "PlotConfig"), required: true, defaultValue: .null),
    ],
    "SemanticDocumentProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .union(members: [.model(name: "SourceReference"), .model(name: "OutputSubscription"), .nullValue], discriminator: nil, discriminatorMap: nil, dispatch: [.anyKeys(["actionOutput"], .model(name: "OutputSubscription")), .fallback(.model(name: "SourceReference"))]), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["document"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "DocumentConfig"), required: true, defaultValue: .null),
    ],
    "GraphProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "collection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["graph"]), required: true, defaultValue: .null),
        FieldSpec(name: "source", type: .model(name: "SourceReference"), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "GraphConfig"), required: true, defaultValue: .null),
        FieldSpec(name: "observe", type: .model(name: "GraphObservers"), required: true, defaultValue: .null),
    ],
    "RecordProjection": [
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "collection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "controls", type: .array(item: .union(members: [.model(name: "NumberControl"), .model(name: "TextControl"), .model(name: "ToggleControl"), .model(name: "SelectControl"), .model(name: "ButtonControl")], discriminator: "kind", discriminatorMap: ["number": "NumberControl", "text": "TextControl", "toggle": "ToggleControl", "select": "SelectControl", "button": "ButtonControl"], dispatch: nil), minItems: nil), required: true, defaultValue: .null),
        FieldSpec(name: "kind", type: .literal(["record"]), required: true, defaultValue: .null),
        FieldSpec(name: "config", type: .model(name: "RecordConfig"), required: true, defaultValue: .null),
        FieldSpec(name: "observe", type: .model(name: "RecordObservers"), required: true, defaultValue: .null),
    ],
    "ObjectSelectEvent": [
        FieldSpec(name: "kind", type: .literal(["object.select"]), required: true, defaultValue: .null),
        FieldSpec(name: "projection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "SelectionClearEvent": [
        FieldSpec(name: "kind", type: .literal(["selection.clear"]), required: true, defaultValue: .null),
        FieldSpec(name: "projection", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
    ],
    "EventFieldValue": [
        FieldSpec(name: "eventField", type: .literal(["objectRef"]), required: true, defaultValue: .null),
    ],
    "NullLiteralValue": [
        FieldSpec(name: "literal", type: .nullValue, required: true, defaultValue: .null),
    ],
    "StateSetEffect": [
        FieldSpec(name: "kind", type: .literal(["state.set"]), required: true, defaultValue: .null),
        FieldSpec(name: "target", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "value", type: .union(members: [.model(name: "EventFieldValue"), .model(name: "NullLiteralValue")], discriminator: nil, discriminatorMap: nil, dispatch: [.exactKeys(["eventField"], .model(name: "EventFieldValue")), .exactKeys(["literal"], .model(name: "NullLiteralValue"))]), required: true, defaultValue: .null),
    ],
    "SemanticBinding": [
        FieldSpec(name: "event", type: .union(members: [.model(name: "ParameterChangedEvent"), .model(name: "ControlActivateEvent"), .model(name: "ObjectSelectEvent"), .model(name: "SelectionClearEvent")], discriminator: "kind", discriminatorMap: ["parameter.changed": "ParameterChangedEvent", "control.activate": "ControlActivateEvent", "object.select": "ObjectSelectEvent", "selection.clear": "SelectionClearEvent"], dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "effect", type: .union(members: [.model(name: "ActionInvokeEffect"), .model(name: "StateSetEffect")], discriminator: "kind", discriminatorMap: ["action.invoke": "ActionInvokeEffect", "state.set": "StateSetEffect"], dispatch: nil), required: true, defaultValue: .null),
        FieldSpec(name: "debounceMs", type: .integer(gt: nil, ge: nil, lt: nil, le: nil), required: false, defaultValue: .number(0)),
    ],
    "SemanticDocument": [
        FieldSpec(name: "language", type: .literal(["melt/semantic-draft-1"]), required: true, defaultValue: .null),
        FieldSpec(name: "workspaceId", type: .string(pattern:"^[a-z][a-z0-9_-]{0,63}\\z", minLength:nil, maxLength:nil), required: true, defaultValue: .null),
        FieldSpec(name: "title", type: .string(pattern:nil, minLength:1, maxLength:256), required: true, defaultValue: .null),
        FieldSpec(name: "sources", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .union(members: [.model(name: "Source"), .model(name: "GraphSource")], discriminator: nil, discriminatorMap: nil, dispatch: [.fieldEquals("kind", "graph", .model(name: "GraphSource")), .fallback(.model(name: "Source"))])), required: true, defaultValue: .null),
        FieldSpec(name: "parameters", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "Parameter")), required: true, defaultValue: .null),
        FieldSpec(name: "actions", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SemanticAction")), required: true, defaultValue: .null),
        FieldSpec(name: "projections", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .union(members: [.model(name: "SemanticTableProjection"), .model(name: "SemanticPlotProjection"), .model(name: "SemanticDocumentProjection"), .model(name: "GraphProjection"), .model(name: "RecordProjection")], discriminator: "kind", discriminatorMap: ["table": "SemanticTableProjection", "plot": "SemanticPlotProjection", "document": "SemanticDocumentProjection", "graph": "GraphProjection", "record": "RecordProjection"], dispatch: nil)), required: true, defaultValue: .null),
        FieldSpec(name: "bindings", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SemanticBinding")), required: true, defaultValue: .null),
        FieldSpec(name: "layout", type: .model(name: "Layout"), required: true, defaultValue: .null),
        FieldSpec(name: "collections", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SemanticCollection")), required: true, defaultValue: .null),
        FieldSpec(name: "state", type: .map(keyPattern: "^[a-z][a-z0-9_-]{0,63}\\z", value: .model(name: "SemanticState")), required: true, defaultValue: .null),
    ],
]

// ---------------------------------------------------------------------------
// Swift models
// ---------------------------------------------------------------------------

public struct SourceReference: Sendable, Equatable {
    public var sourceId: String
    public var version: Int

    public init(
        sourceId: String,
        version: Int
    ) {
        self.sourceId = sourceId
        self.version = version
    }
}

public struct TableColumn: Sendable, Equatable {
    public var id: String
    public var type: String
    public var nullable: Bool
    public var label: String?
    public var precision: Int?
    public var scale: Int?

    public init(
        id: String,
        type: String,
        nullable: Bool,
        label: String? = nil,
        precision: Int? = nil,
        scale: Int? = nil
    ) {
        self.id = id
        self.type = type
        self.nullable = nullable
        self.label = label
        self.precision = precision
        self.scale = scale
    }
}

public struct TableSchema: Sendable, Equatable {
    public var columns: [TableColumn]
    public var rowKey: String?

    public init(
        columns: [TableColumn],
        rowKey: String? = nil
    ) {
        self.columns = columns
        self.rowKey = rowKey
    }
}

public struct ImageSchema: Sendable, Equatable {
    public var width: Int
    public var height: Int

    public init(
        width: Int,
        height: Int
    ) {
        self.width = width
        self.height = height
    }
}

public struct TextSchema: Sendable, Equatable {
    public var encoding: String

    public init(
        encoding: String
    ) {
        self.encoding = encoding
    }
}

public struct ImportOrigin: Sendable, Equatable {
    public var kind: String
    public var label: String

    public init(
        kind: String,
        label: String
    ) {
        self.kind = kind
        self.label = label
    }
}

public struct RunOrigin: Sendable, Equatable {
    public var kind: String
    public var runId: String
    public var output: String

    public init(
        kind: String,
        runId: String,
        output: String
    ) {
        self.kind = kind
        self.runId = runId
        self.output = output
    }
}

public struct SourceVersion: Sendable, Equatable {
    public var artifact: String
    public var mediaType: String
    public var byteLength: Int
    public var schema: SourceSchema
    public var origin: SourceOrigin

    public init(
        artifact: String,
        mediaType: String,
        byteLength: Int,
        schema: SourceSchema,
        origin: SourceOrigin
    ) {
        self.artifact = artifact
        self.mediaType = mediaType
        self.byteLength = byteLength
        self.schema = schema
        self.origin = origin
    }
}

public struct Source: Sendable, Equatable {
    public var kind: String
    public var currentVersion: Int
    public var versions: [String: SourceVersion]

    public init(
        kind: String,
        currentVersion: Int,
        versions: [String: SourceVersion]
    ) {
        self.kind = kind
        self.currentVersion = currentVersion
        self.versions = versions
    }
}

public struct ParameterConstraints: Sendable, Equatable {
    public var minimum: Double?
    public var maximum: Double?
    public var maxLength: Int?
    public var `enum`: [String]?

    public init(
        minimum: Double? = nil,
        maximum: Double? = nil,
        maxLength: Int? = nil,
        `enum`: [String]? = nil
    ) {
        self.minimum = minimum
        self.maximum = maximum
        self.maxLength = maxLength
        self.`enum` = `enum`
    }
}

public struct Parameter: Sendable, Equatable {
    public var type: String
    public var `default`: JsonScalar?
    public var nullable: Bool
    public var constraints: ParameterConstraints

    public init(
        type: String,
        `default`: JsonScalar?,
        nullable: Bool,
        constraints: ParameterConstraints
    ) {
        self.type = type
        self.`default` = `default`
        self.nullable = nullable
        self.constraints = constraints
    }
}

public struct SelectOption: Sendable, Equatable {
    public var value: String
    public var label: String

    public init(
        value: String,
        label: String
    ) {
        self.value = value
        self.label = label
    }
}

public struct NumberControl: Sendable, Equatable {
    public var id: String
    public var label: String
    public var kind: String
    public var parameter: String

    public init(
        id: String,
        label: String,
        kind: String,
        parameter: String
    ) {
        self.id = id
        self.label = label
        self.kind = kind
        self.parameter = parameter
    }
}

public struct TextControl: Sendable, Equatable {
    public var id: String
    public var label: String
    public var kind: String
    public var parameter: String

    public init(
        id: String,
        label: String,
        kind: String,
        parameter: String
    ) {
        self.id = id
        self.label = label
        self.kind = kind
        self.parameter = parameter
    }
}

public struct ToggleControl: Sendable, Equatable {
    public var id: String
    public var label: String
    public var kind: String
    public var parameter: String

    public init(
        id: String,
        label: String,
        kind: String,
        parameter: String
    ) {
        self.id = id
        self.label = label
        self.kind = kind
        self.parameter = parameter
    }
}

public struct SelectControl: Sendable, Equatable {
    public var id: String
    public var label: String
    public var kind: String
    public var parameter: String
    public var options: [SelectOption]

    public init(
        id: String,
        label: String,
        kind: String,
        parameter: String,
        options: [SelectOption]
    ) {
        self.id = id
        self.label = label
        self.kind = kind
        self.parameter = parameter
        self.options = options
    }
}

public struct ButtonControl: Sendable, Equatable {
    public var id: String
    public var label: String
    public var kind: String

    public init(
        id: String,
        label: String,
        kind: String
    ) {
        self.id = id
        self.label = label
        self.kind = kind
    }
}

public struct ActionImplementation: Sendable, Equatable {
    public var registrationId: String

    public init(
        registrationId: String
    ) {
        self.registrationId = registrationId
    }
}

public struct ActionParameterMapping: Sendable, Equatable {
    public var parameter: String

    public init(
        parameter: String
    ) {
        self.parameter = parameter
    }
}

public struct Action: Sendable, Equatable {
    public var implementation: ActionImplementation
    public var parameters: [String: ActionParameterMapping]
    public var inputs: [String: SourceReference]
    public var outputs: [String: String]
    public var outputGroup: String
    public var targets: [String: String]

    public init(
        implementation: ActionImplementation,
        parameters: [String: ActionParameterMapping],
        inputs: [String: SourceReference],
        outputs: [String: String],
        outputGroup: String,
        targets: [String: String]
    ) {
        self.implementation = implementation
        self.parameters = parameters
        self.inputs = inputs
        self.outputs = outputs
        self.outputGroup = outputGroup
        self.targets = targets
    }
}

public struct TableConfig: Sendable, Equatable {
    public var columns: [String]?
    public var selection: String

    public init(
        columns: [String]?,
        selection: String
    ) {
        self.columns = columns
        self.selection = selection
    }
}

public struct PlotConfig: Sendable, Equatable {
    public var fit: String

    public init(
        fit: String
    ) {
        self.fit = fit
    }
}

public struct DocumentConfig: Sendable, Equatable {
    public var format: String

    public init(
        format: String
    ) {
        self.format = format
    }
}

public struct TableProjection: Sendable, Equatable {
    public var title: String
    public var source: SourceReference?
    public var controls: [Control]
    public var kind: String
    public var config: TableConfig

    public init(
        title: String,
        source: SourceReference?,
        controls: [Control],
        kind: String,
        config: TableConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct PlotProjection: Sendable, Equatable {
    public var title: String
    public var source: SourceReference?
    public var controls: [Control]
    public var kind: String
    public var config: PlotConfig

    public init(
        title: String,
        source: SourceReference?,
        controls: [Control],
        kind: String,
        config: PlotConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct DocumentProjection: Sendable, Equatable {
    public var title: String
    public var source: SourceReference?
    public var controls: [Control]
    public var kind: String
    public var config: DocumentConfig

    public init(
        title: String,
        source: SourceReference?,
        controls: [Control],
        kind: String,
        config: DocumentConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct ParameterChangedEvent: Sendable, Equatable {
    public var kind: String
    public var parameter: String

    public init(
        kind: String,
        parameter: String
    ) {
        self.kind = kind
        self.parameter = parameter
    }
}

public struct ControlActivateEvent: Sendable, Equatable {
    public var kind: String
    public var projection: String
    public var control: String

    public init(
        kind: String,
        projection: String,
        control: String
    ) {
        self.kind = kind
        self.projection = projection
        self.control = control
    }
}

public struct ActionInvokeEffect: Sendable, Equatable {
    public var kind: String
    public var action: String

    public init(
        kind: String,
        action: String
    ) {
        self.kind = kind
        self.action = action
    }
}

public struct Binding: Sendable, Equatable {
    public var event: BindingEvent
    public var effect: ActionInvokeEffect
    public var debounceMs: Int

    public init(
        event: BindingEvent,
        effect: ActionInvokeEffect,
        debounceMs: Int
    ) {
        self.event = event
        self.effect = effect
        self.debounceMs = debounceMs
    }
}

public struct PaneNode: Sendable, Equatable {
    public var kind: String
    public var projection: String

    public init(
        kind: String,
        projection: String
    ) {
        self.kind = kind
        self.projection = projection
    }
}

public struct SplitNode: Sendable, Equatable {
    public var kind: String
    public var axis: String
    public var ratio: Double
    public var first: LayoutNode
    public var second: LayoutNode

    public init(
        kind: String,
        axis: String,
        ratio: Double,
        first: LayoutNode,
        second: LayoutNode
    ) {
        self.kind = kind
        self.axis = axis
        self.ratio = ratio
        self.first = first
        self.second = second
    }
}

public struct Layout: Sendable, Equatable {
    public var root: LayoutNode?
    public var hidden: [String]

    public init(
        root: LayoutNode?,
        hidden: [String]
    ) {
        self.root = root
        self.hidden = hidden
    }
}

public struct Document: Sendable, Equatable {
    public var language: String
    public var workspaceId: String
    public var title: String
    public var sources: [String: Source]
    public var parameters: [String: Parameter]
    public var actions: [String: Action]
    public var projections: [String: Projection]
    public var bindings: [String: Binding]
    public var layout: Layout

    public init(
        language: String,
        workspaceId: String,
        title: String,
        sources: [String: Source],
        parameters: [String: Parameter],
        actions: [String: Action],
        projections: [String: Projection],
        bindings: [String: Binding],
        layout: Layout
    ) {
        self.language = language
        self.workspaceId = workspaceId
        self.title = title
        self.sources = sources
        self.parameters = parameters
        self.actions = actions
        self.projections = projections
        self.bindings = bindings
        self.layout = layout
    }
}

public struct SemanticCollection: Sendable, Equatable {
    public var source: SourceReference
    public var key: String

    public init(
        source: SourceReference,
        key: String
    ) {
        self.source = source
        self.key = key
    }
}

public struct ReferenceType: Sendable, Equatable {
    public var kind: String
    public var collection: String

    public init(
        kind: String,
        collection: String
    ) {
        self.kind = kind
        self.collection = collection
    }
}

public struct SemanticState: Sendable, Equatable {
    public var type: ReferenceType
    public var nullable: Bool
    public var `default`: MeltNull
    public var scope: String

    public init(
        type: ReferenceType,
        nullable: Bool,
        `default`: MeltNull,
        scope: String
    ) {
        self.type = type
        self.nullable = nullable
        self.`default` = `default`
        self.scope = scope
    }
}

public struct SemanticReference: Sendable, Equatable {
    public var collection: String
    public var incarnation: String
    public var key: String

    public init(
        collection: String,
        incarnation: String,
        key: String
    ) {
        self.collection = collection
        self.incarnation = incarnation
        self.key = key
    }
}

public struct SemanticAction: Sendable, Equatable {
    public var implementation: ActionImplementation
    public var parameters: [String: ActionParameterMapping]
    public var inputs: [String: SourceReference]
    public var outputs: [String: String]
    public var outputGroup: String

    public init(
        implementation: ActionImplementation,
        parameters: [String: ActionParameterMapping],
        inputs: [String: SourceReference],
        outputs: [String: String],
        outputGroup: String
    ) {
        self.implementation = implementation
        self.parameters = parameters
        self.inputs = inputs
        self.outputs = outputs
        self.outputGroup = outputGroup
    }
}

public struct ActionOutputId: Sendable, Equatable {
    public var action: String
    public var output: String
    public var incarnation: String

    public init(
        action: String,
        output: String,
        incarnation: String
    ) {
        self.action = action
        self.output = output
        self.incarnation = incarnation
    }
}

public struct OutputSubscription: Sendable, Equatable {
    public var actionOutput: ActionOutputId
    public var follow: String
    public var version: Int?

    public init(
        actionOutput: ActionOutputId,
        follow: String,
        version: Int? = nil
    ) {
        self.actionOutput = actionOutput
        self.follow = follow
        self.version = version
    }
}

public struct GraphNode: Sendable, Equatable {
    public var nodeId: String
    public var label: String
    public var collection: String
    public var key: String

    public init(
        nodeId: String,
        label: String,
        collection: String,
        key: String
    ) {
        self.nodeId = nodeId
        self.label = label
        self.collection = collection
        self.key = key
    }
}

public struct GraphEdge: Sendable, Equatable {
    public var edgeId: String
    public var from: String
    public var to: String

    public init(
        edgeId: String,
        from: String,
        to: String
    ) {
        self.edgeId = edgeId
        self.from = from
        self.to = to
    }
}

public struct GraphSchema: Sendable, Equatable {
    public var nodes: [GraphNode]
    public var edges: [GraphEdge]

    public init(
        nodes: [GraphNode],
        edges: [GraphEdge] = []
    ) {
        self.nodes = nodes
        self.edges = edges
    }
}

public struct GraphSourceVersion: Sendable, Equatable {
    public var artifact: String
    public var mediaType: String
    public var byteLength: Int
    public var schema: GraphSchema
    public var origin: SourceOrigin

    public init(
        artifact: String,
        mediaType: String,
        byteLength: Int,
        schema: GraphSchema,
        origin: SourceOrigin
    ) {
        self.artifact = artifact
        self.mediaType = mediaType
        self.byteLength = byteLength
        self.schema = schema
        self.origin = origin
    }
}

public struct GraphSource: Sendable, Equatable {
    public var kind: String
    public var currentVersion: Int
    public var versions: [String: GraphSourceVersion]

    public init(
        kind: String,
        currentVersion: Int,
        versions: [String: GraphSourceVersion]
    ) {
        self.kind = kind
        self.currentVersion = currentVersion
        self.versions = versions
    }
}

public struct GraphConfig: Sendable, Equatable {
    public var nodeLabels: String

    public init(
        nodeLabels: String
    ) {
        self.nodeLabels = nodeLabels
    }
}

public struct RecordConfig: Sendable, Equatable {
    public var fields: [String]?

    public init(
        fields: [String]?
    ) {
        self.fields = fields
    }
}

public struct HighlightObserve: Sendable, Equatable {
    public var state: String

    public init(
        state: String
    ) {
        self.state = state
    }
}

public struct RecordObserve: Sendable, Equatable {
    public var state: String

    public init(
        state: String
    ) {
        self.state = state
    }
}

public struct GraphObservers: Sendable, Equatable {
    public var highlight: HighlightObserve

    public init(
        highlight: HighlightObserve
    ) {
        self.highlight = highlight
    }
}

public struct RecordObservers: Sendable, Equatable {
    public var record: RecordObserve

    public init(
        record: RecordObserve
    ) {
        self.record = record
    }
}

public struct SemanticTableProjection: Sendable, Equatable {
    public var title: String
    public var source: ProjectionSource?
    public var controls: [Control]
    public var kind: String
    public var config: TableConfig

    public init(
        title: String,
        source: ProjectionSource?,
        controls: [Control],
        kind: String,
        config: TableConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct SemanticPlotProjection: Sendable, Equatable {
    public var title: String
    public var source: ProjectionSource?
    public var controls: [Control]
    public var kind: String
    public var config: PlotConfig

    public init(
        title: String,
        source: ProjectionSource?,
        controls: [Control],
        kind: String,
        config: PlotConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct SemanticDocumentProjection: Sendable, Equatable {
    public var title: String
    public var source: ProjectionSource?
    public var controls: [Control]
    public var kind: String
    public var config: DocumentConfig

    public init(
        title: String,
        source: ProjectionSource?,
        controls: [Control],
        kind: String,
        config: DocumentConfig
    ) {
        self.title = title
        self.source = source
        self.controls = controls
        self.kind = kind
        self.config = config
    }
}

public struct GraphProjection: Sendable, Equatable {
    public var title: String
    public var collection: String
    public var controls: [Control]
    public var kind: String
    public var source: SourceReference
    public var config: GraphConfig
    public var observe: GraphObservers

    public init(
        title: String,
        collection: String,
        controls: [Control],
        kind: String,
        source: SourceReference,
        config: GraphConfig,
        observe: GraphObservers
    ) {
        self.title = title
        self.collection = collection
        self.controls = controls
        self.kind = kind
        self.source = source
        self.config = config
        self.observe = observe
    }
}

public struct RecordProjection: Sendable, Equatable {
    public var title: String
    public var collection: String
    public var controls: [Control]
    public var kind: String
    public var config: RecordConfig
    public var observe: RecordObservers

    public init(
        title: String,
        collection: String,
        controls: [Control],
        kind: String,
        config: RecordConfig,
        observe: RecordObservers
    ) {
        self.title = title
        self.collection = collection
        self.controls = controls
        self.kind = kind
        self.config = config
        self.observe = observe
    }
}

public struct ObjectSelectEvent: Sendable, Equatable {
    public var kind: String
    public var projection: String

    public init(
        kind: String,
        projection: String
    ) {
        self.kind = kind
        self.projection = projection
    }
}

public struct SelectionClearEvent: Sendable, Equatable {
    public var kind: String
    public var projection: String

    public init(
        kind: String,
        projection: String
    ) {
        self.kind = kind
        self.projection = projection
    }
}

public struct EventFieldValue: Sendable, Equatable {
    public var eventField: String

    public init(
        eventField: String
    ) {
        self.eventField = eventField
    }
}

public struct NullLiteralValue: Sendable, Equatable {
    public var literal: MeltNull

    public init(
        literal: MeltNull
    ) {
        self.literal = literal
    }
}

public struct StateSetEffect: Sendable, Equatable {
    public var kind: String
    public var target: String
    public var value: StateSetValue

    public init(
        kind: String,
        target: String,
        value: StateSetValue
    ) {
        self.kind = kind
        self.target = target
        self.value = value
    }
}

public struct SemanticBinding: Sendable, Equatable {
    public var event: SemanticBindingEvent
    public var effect: SemanticBindingEffect
    public var debounceMs: Int

    public init(
        event: SemanticBindingEvent,
        effect: SemanticBindingEffect,
        debounceMs: Int = 0
    ) {
        self.event = event
        self.effect = effect
        self.debounceMs = debounceMs
    }
}

public struct SemanticDocument: Sendable, Equatable {
    public var language: String
    public var workspaceId: String
    public var title: String
    public var sources: [String: SemanticSource]
    public var parameters: [String: Parameter]
    public var actions: [String: SemanticAction]
    public var projections: [String: SemanticProjection]
    public var bindings: [String: SemanticBinding]
    public var layout: Layout
    public var collections: [String: SemanticCollection]
    public var state: [String: SemanticState]

    public init(
        language: String,
        workspaceId: String,
        title: String,
        sources: [String: SemanticSource],
        parameters: [String: Parameter],
        actions: [String: SemanticAction],
        projections: [String: SemanticProjection],
        bindings: [String: SemanticBinding],
        layout: Layout,
        collections: [String: SemanticCollection],
        state: [String: SemanticState]
    ) {
        self.language = language
        self.workspaceId = workspaceId
        self.title = title
        self.sources = sources
        self.parameters = parameters
        self.actions = actions
        self.projections = projections
        self.bindings = bindings
        self.layout = layout
        self.collections = collections
        self.state = state
    }
}

public enum SourceSchema: Sendable, Equatable {
    case tableSchema(TableSchema)
    case imageSchema(ImageSchema)
    case textSchema(TextSchema)
}

public enum SourceOrigin: Sendable, Equatable {
    case `import`(ImportOrigin)
    case run(RunOrigin)
}

public enum Control: Sendable, Equatable {
    case number(NumberControl)
    case text(TextControl)
    case toggle(ToggleControl)
    case select(SelectControl)
    case button(ButtonControl)
}

public enum BindingEvent: Sendable, Equatable {
    case parameterChanged(ParameterChangedEvent)
    case controlActivate(ControlActivateEvent)
}

public indirect enum LayoutNode: Sendable, Equatable {
    case pane(PaneNode)
    case split(SplitNode)
}

public enum Projection: Sendable, Equatable {
    case table(TableProjection)
    case plot(PlotProjection)
    case document(DocumentProjection)
}

public enum ProjectionSource: Sendable, Equatable {
    case sourceReference(SourceReference)
    case outputSubscription(OutputSubscription)
}

public enum StateSetValue: Sendable, Equatable {
    case eventFieldValue(EventFieldValue)
    case nullLiteralValue(NullLiteralValue)
}

public enum SemanticBindingEvent: Sendable, Equatable {
    case parameterChanged(ParameterChangedEvent)
    case controlActivate(ControlActivateEvent)
    case objectSelect(ObjectSelectEvent)
    case selectionClear(SelectionClearEvent)
}

public enum SemanticBindingEffect: Sendable, Equatable {
    case actionInvoke(ActionInvokeEffect)
    case stateSet(StateSetEffect)
}

public enum SemanticSource: Sendable, Equatable {
    case source(Source)
    case graphSource(GraphSource)
}

public enum SemanticProjection: Sendable, Equatable {
    case table(SemanticTableProjection)
    case plot(SemanticPlotProjection)
    case document(SemanticDocumentProjection)
    case graph(GraphProjection)
    case record(RecordProjection)
}

public let MELT_SEMANTIC_LANGUAGE_ID = "melt/semantic-draft-1"

// ---------------------------------------------------------------------------
// RFC 8785 canonicalization and canonical hashing (SPEC.md section 4.2)
// ---------------------------------------------------------------------------

func canonicalString(_ value: String) -> String {
    var out = "\""
    for scalar in value.unicodeScalars {
        switch scalar.value {
        case 0x22: out += "\\\""
        case 0x5C: out += "\\\\"
        case 0x08: out += "\\b"
        case 0x09: out += "\\t"
        case 0x0A: out += "\\n"
        case 0x0C: out += "\\f"
        case 0x0D: out += "\\r"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.append(Character(scalar))
            }
        }
    }
    out += "\""
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
        ? "+\(exponentValue)"
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

let MELT_SHA256_K: [UInt32] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
]
let MELT_SHA256_H: [UInt32] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]

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
              scalar == " " || scalar == "\t" || scalar == "\n" || scalar == "\r" {
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
        if scalar == "\"" {
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
        throw MeltModelError("", "unexpected character \(scalar) at index \(index)")
    }

    private mutating func expect(_ word: String) throws {
        for character in word.unicodeScalars {
            guard current == character else {
                throw MeltModelError("", "invalid literal at index \(index)")
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
            guard current == "\"" else {
                throw MeltModelError("", "expected an object key at index \(index)")
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
            if scalar == "\"" {
                index += 1
                return String(String.UnicodeScalarView(scalarsOut))
            }
            if scalar == "\\" {
                index += 1
                try parseEscape(into: &scalarsOut)
                continue
            }
            if scalar.value < 0x20 {
                throw MeltModelError(
                    "",
                    "raw control character in a string at index \(index)"
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
        case "\"":
            scalars.append("\"")
        case "\\":
            scalars.append("\\")
        case "/":
            scalars.append("/")
        case "b":
            scalars.append("\u{08}")
        case "f":
            scalars.append("\u{0C}")
        case "n":
            scalars.append("\n")
        case "r":
            scalars.append("\r")
        case "t":
            scalars.append("\t")
        case "u":
            let first = try parseHexQuad()
            if first >= 0xD800 && first <= 0xDBFF {
                guard current == "\\",
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
            throw MeltModelError("", "invalid escape sequence \\(escape)")
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
                throw MeltModelError("", "invalid \\u escape sequence")
            }
            guard let digit = hexDigit(scalar) else {
                throw MeltModelError("", "invalid \\u escape sequence")
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
            throw MeltModelError("", "number \(text) is not finite")
        }
        return .number(value)
    }
}


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
                        + "\n  swift:    " + hash
                        + "\n  recorded: " + recordedHash
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
                        + "\n  swift:    " + canonical
                        + "\n  expected: " + expected
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
        Data("melt-swift-verify: \(error)\n".utf8)
    )
    exit(1)
}
if !failures.isEmpty {
    let message = failures.joined(separator: "\n") + "\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}
print("melt-swift-verify: the generated Swift models agree with every recorded hash")
