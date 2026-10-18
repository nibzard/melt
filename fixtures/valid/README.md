# Valid fixtures

Every `*.json` file in this directory is a `melt/v0.1` document that
must validate, and every file under `commands/` is a command envelope
that must validate (SPEC.md sections 4 through 12).

| File | Covers |
|---|---|
| `empty-workspace.json` | Empty collections and an empty layout |
| `projection-table.json` | A table projection over the real sales input |
| `projection-plot.json` | A plot projection over a real PNG |
| `projection-document.json` | A document projection over real text, with a select control |
| `hidden-panes.json` | A hidden pane that an action targets with a null source |
| `nullable-parameters.json` | A nullable string with a null default and a nullable integer |
| `sales-full.json` | The complete example of SPEC.md section 16, with the real artifact hash and byte length |
| `commands/state-migration.json` | A valid migration: narrower constraints plus `parameter.set` with the expected state revision |
| `document-hashes.json` | The canonical RFC 8785 hash of each document, for cross-language checks |

Rules:

- Documents that cite an artifact cite a real one: the hash and byte
  length match a file under `fixtures/sales/` and an entry in
  `fixtures/catalog.json`.
- `document-hashes.json` is generated from the documents. The test
  suite fails when it goes stale, and `scripts/verify_canonical_hashes.mjs`
  recomputes every entry with an independent JavaScript canonicalizer.
- Command envelopes record the workspace state they address in their
  `expected` map. `commands/state-migration.json` addresses
  `parameters/minimum` at entity revision 2 and `state/minimum` at
  revision 4; `tests/contract/test_fixtures.py` drives a service into
  that state and proves the envelope commits.
