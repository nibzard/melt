# Fixtures

Shared fixtures detect disagreement between the Python schema package,
the compiler, and later native checks (SPEC.md section 18.1).

- `valid/`: documents and command envelopes that must pass. The set
  covers the empty workspace, every projection kind, hidden panes,
  nullable parameters, a state migration, and the full sales example
  with a real artifact hash.
- `invalid/`: documents and command envelopes that must fail. Each
  fixture has an entry in `invalid/manifest.json` with its expected
  diagnostic code and JSON Pointer.
- `sales/`: the deterministic artifact bytes that the fixtures cite.
  `scripts/build_fixture_artifacts.py` regenerates them byte for byte.
- `catalog.json`: the trusted catalog for full compilation. It records
  the measured schema, media type, and byte length of each artifact,
  and the registration contracts of `sales-script-v1` and
  `summarize-script-v1` (SPEC.md sections 7.2 and 16). The compiler of
  backlog task T005 consumes it through
  `runtime.compiler.load_catalog`, `melt compile <file> --catalog <file>`
  consumes it from the command line (SPEC.md section 11.3), and the
  `registry-mismatches` and `artifact-schema-drift` invalid fixtures
  are checked against it.

`tests/contract/test_fixtures.py` runs the whole set. It proves the
valid fixtures parse, the schema-enforced invalid fixtures fail with
exactly their expected diagnostic, the catalog matches the bytes on
disk, and regeneration changes nothing.
