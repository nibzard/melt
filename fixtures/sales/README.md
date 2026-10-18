# Sales fixture artifacts

The real artifact bytes that the contract fixtures cite. The task
suite for the hero workflow (SPEC.md section 18.2, backlog task T019)
reuses the sales table. The command-line harness (backlog task T018)
drives its full loop from these files, and the hero proof of task T019
runs the six steps over them.

| File | Contents |
|---|---|
| `sales.parquet` | The hero input: rows (2026-01, 100), (2026-01, 300), and (2026-02, 200), columns `month` (string) and `revenue` (float64), both non-nullable, no row key |
| `revenue.png` | A plain 64 by 48 RGB PNG that stands in for a published plot |
| `notes.md` | A UTF-8 markdown text source |
| `workspace.json` | The creation document of the harness loop: the `minimum` parameter, empty `sources` and `actions` (SPEC.md section 12.6) |
| `sales-script.py` | The registered script of the loop: the example script of SPEC.md section 13.2 |
| `contract.json` | The registration contract of the script (parameters, inputs, outputs) |
| `definitions.json` | The definition apply of the loop: the action, both projections, both bindings, and the layout, with a `${registration}` placeholder for the minted registration identifier |
| `layout-move.json` | The pane move of hero step 4: one `layout.replace` that stacks the split vertically with the plot moved above the table |

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
