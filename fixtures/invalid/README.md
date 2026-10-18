# Invalid fixtures

Each fixture in this directory fails with the diagnostic code and JSON
Pointer that `manifest.json` records for it (SPEC.md sections 11.3 and
18.1). Documents sit at the top level; command envelopes sit under
`commands/`.

Manifest entry fields:

- `file`: path relative to this directory.
- `kind`: `document` or `command`.
- `category`: the failure class from SPEC.md section 18.1.
- `code` and `path`: the expected diagnostic.
- `enforcedBy`: the layer that rejects the fixture.
  - `schema`: `python/melt/schema` rejects it today. The test asserts
    the expected diagnostic appears, and that it is the only one unless
    the entry sets `"exclusive": false`.
  - `compiler`: the document is structurally valid. Full compilation
    against `fixtures/catalog.json` rejects it. The compiler of backlog
    task T005 enforces these entries; `tests/contract/test_fixtures.py`
    runs them through `runtime.compiler.compile_document`.
  - `service`: the command envelope is well formed. The workspace
    service rejects its semantics against the state the envelope's
    `expected` map addresses. The service of backlog task T009 enforces
    these entries; `tests/contract/test_fixtures.py` applies each
    envelope to a seeded `runtime.workspace.WorkspaceService`.
- `ownerTask`: the backlog task that owns a pending check.
- `requires`: extra inputs the check needs, such as the catalog.

The covered categories: unknown fields, duplicate keys, invalid IDs,
unsupported versions, non-finite numbers, unsafe integers, invalid
defaults, wrong parameter types, missing source versions, missing
controls, renderer mismatches, layout errors (duplicate placement,
missing panes, excessive depth, invalid ratios), competing writers,
incomplete mappings, invalid migrations, missing expected revisions,
registry mismatches, and artifact schema drift.

`tests/contract/test_fixtures.py` fails when a fixture is missing from
the manifest or when a manifest entry names a file that does not exist.
