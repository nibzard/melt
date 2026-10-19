# Generated JSON Schema

This directory holds the JSON Schema exported from the single
restricted Pydantic vocabulary in `python/melt/schema` (SPEC.md
sections 2.1 and 17). Every file here is generated. Do not edit these
files by hand and do not author an independent schema: change the
models in `python/melt/schema` and regenerate.

## The generation command

```bash
make protocol
# or: uv run --locked python3 scripts/generate_protocol.py
```

`scripts/generate_protocol.py` exports the JSON Schema through the
pinned Pydantic version of `uv.lock`, so the same vocabulary always
produces the same bytes. `scripts/check.sh` runs the same generator
with `--check` and fails when regeneration changes committed output
(SPEC.md section 17), which makes `make check` the enforcement point.

## Every generated file

| File | Contents |
|---|---|
| `document.schema.json` | The `melt/v0.1` canonical document and every model it references (SPEC.md sections 4 through 10). The root `$ref` points at `Document`. |
| `semantic-document.schema.json` | The provisional `melt/semantic-draft-1` document of RFC 001: the v0.1 shapes plus the `collections` and `state` maps, the graph source, the graph and record projections, output subscriptions, and pure-event bindings. The root `$ref` points at `SemanticDocument`. |
| `command.schema.json` | The framed command envelope, its `OkResponse` and `RejectedResponse` shapes, every method payload of SPEC.md section 12.6, and the mutation operations of section 12.2. The root `$ref` points at `CommandEnvelope`. |
| `record.schema.json` | The versioned persisted records of SPEC.md section 17: `RunRecord`, `RegistrationRecord`, `PermissionGrantRecord`, `ArtifactRecord`, `OutputGroupRecord`, `PendingIntentRecord`, `PublicationRecord`, `ParameterState`, `ReferenceState`, `CommandReceipt`, `Snapshot`, `ChangeNotification`, `PageRequest`, and `PageResponse`. The root `$ref` points at the `MeltRecord` union. |

Each file is a self-contained schema library: a `$schema` declaration,
a stable `$id` under `https://melt.dev/protocol/`, and one `$defs` map
that carries every referenced model, so a consumer resolves every
`$ref` inside the same file. Property names use the wire spellings
(`schema` for the measured source schema, `from` for a graph edge
endpoint), matching what the Python models serialize through
`model_dump(by_alias=True)`.

The persisted record and command schemas are exported documentation of
the versioned wire contracts; the semantic models join the canonical
hash agreement through the generated language models described in
`protocol/generated/README.md`.
