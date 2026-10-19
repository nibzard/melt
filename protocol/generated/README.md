# Generated language models

This directory holds the TypeScript and Swift models generated from the
single restricted schema vocabulary in `python/melt/schema` (SPEC.md
section 17). Every file here is generated. Do not edit these files by
hand and never author an independent schema: change the vocabulary and
regenerate. Swift decoding alone is not semantic validation; the
authoritative semantic implementation stays in the service.

## The generation command

```bash
make protocol
# or: uv run --locked python3 scripts/generate_protocol.py
```

The generator introspects the Pydantic models — field names and wire
aliases, required fields, defaults, literals, patterns, discriminators,
and the key-dispatched unions — and emits one model table per language
plus a generic engine that validates and normalizes a parsed document.
`scripts/check.sh` regenerates with `--check` and fails when the
committed files would change (SPEC.md section 17).

## Every generated file

| File | Contents |
|---|---|
| `melt-document.ts` | The TypeScript models of both languages: one interface per model, the type aliases of the unions, the `MELT_MODEL_FIELDS` table, the table-driven validator, and the RFC 8785 canonicalizer with `canonicalDocumentHash(jsonText)`. Runs on Node without a build step (Node strips the erasable types). |
| `MeltDocument.swift` | The Swift models: one struct per model, the enums of the unions, the same table and engine over a `JsonValue` tree, the canonicalizer, a dependency-free SHA-256, a strict JSON reader, and the verification harness. |

## The wire-form rule

The recorded canonical hashes cover the document in its wire form: the
shape `model_dump(by_alias=True)` produces in Python. Every field of
every model is present, and an absent optional field carries its
default — an explicit null for the optional bounds of parameter
constraints and the optional fields of table columns, an absent
`rowKey`, and the zero `debounceMs` of a pure-event binding of the
extension. The generated engines normalize every parsed document into
exactly that form before canonicalizing, so all languages hash the same
bytes (SPEC.md sections 4.2 and 18.1).

## How the models verify the recorded hashes

- `scripts/verify_canonical_hashes.mjs` imports
  `canonicalDocumentHash` from `melt-document.ts` and verifies every
  entry of `fixtures/valid/document-hashes.json` and
  `fixtures/semantic/document-hashes.json`. The semantic extension
  fixtures join the agreement through this separate index, so the v0.1
  suite stays unchanged. `make check` runs the script.
- The Swift harness replays the RFC 8785 reference vectors of
  `tests/contract/jcs/` and verifies both hash indexes:

  ```bash
  swift protocol/generated/MeltDocument.swift "$(pwd)"
  ```

  The Linux gate never requires a Swift toolchain (SPEC.md section
  2.2): `scripts/check.sh` runs the harness only where `swift` exists
  and reports a skip otherwise, and
  `tests/contract/test_protocol_generation.py` does the same. The
  native round (backlog task T030) always has the toolchain and always
  runs it.

## Scope

The models cover the document vocabulary of both `melt/v0.1` and
`melt/semantic-draft-1` — what a native host renders, including the
`collections` and `state` maps of RFC 001. The command and record
schemas are exported as JSON Schema in `protocol/schema/` instead; they
belong to the service boundary, not the renderer. Python stays the
authoritative validator: the generated models check the structural
shape (unknown fields, required fields, types, patterns, closed literal
sets), not the cross-field semantics of the compiler.
