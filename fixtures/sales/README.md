# Sales fixture artifacts

The real artifact bytes that the contract fixtures cite. The task
suite for the hero workflow (SPEC.md section 18.2, backlog task T019)
reuses the sales table.

| File | Contents |
|---|---|
| `sales.parquet` | The hero input: rows (2026-01, 100), (2026-01, 300), and (2026-02, 200), columns `month` (string) and `revenue` (float64), both non-nullable, no row key |
| `revenue.png` | A plain 64 by 48 RGB PNG that stands in for a published plot |
| `notes.md` | A UTF-8 markdown text source |

`scripts/build_fixture_artifacts.py` builds all three from fixed
inputs under the pinned toolchain, so regeneration is byte-identical:

```bash
uv run python3 scripts/build_fixture_artifacts.py         # rewrite
uv run python3 scripts/build_fixture_artifacts.py --check # compare
```

The test suite regenerates the bytes in memory and compares, so
`make check` fails when regeneration would change a committed file.
Parquet output is deterministic for the pinned `pyarrow` version; a
toolchain change that alters the bytes must update
`fixtures/catalog.json` and every citing document together.
