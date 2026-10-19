# Performance optimizations, 2026-10-19

These measurements precede the [workspace scale fixes](scale-fixes-2026-10-19.md).

These measurements compare the local implementation immediately before and after optimization.
The fixture contains one million rows and 20 columns. Each page contains 50 rows.
The Parquet artifact occupies 61,449,230 bytes.

Both recorded runs use the same Linux machine and locked dependencies.
The final after-run runs separately from the test suite.
The kernel page cache stays warm. Cold disk latency is not measured.
These are command-line service measurements, not native rendering measurements.

| Operation | Samples | Before median (ms) | After median (ms) | Before p95 (ms) | After p95 (ms) |
|---|---:|---:|---:|---:|---:|
| Compile 32 projections | 200 | 10.511 | 7.626 | 10.67 | 7.763 |
| Parameter commit | 100 | 7.017 | 5.896 | 7.996 | 6.563 |
| Repeated first page | 30 | 65.725 | 2.957 | 72.979 | 3.004 |
| Full sort, empty service cache | 10 | 379.566 | 192.244 | 416.271 | 196.529 |
| Restore five panes | 10 | 65.938 | 63.17 | 69.132 | 70.637 |

The repeated-page comparison includes the benefit of the new service cache.
The old reader decodes the artifact on every request.

`table-page-cache-miss`: median 63.361 ms, p95 86.412 ms, 30 samples.
`table-page-cached-sort`: median 2.812 ms, p95 2.859 ms, 30 samples.

The compiler computes the canonical document hash once and reuses it for the plan key.
The byte reader uses an Arrow buffer without an extra BytesIO copy.
The service reads Parquet through a safely opened file handle.
It combines decoded chunks once to make small row gathers cheap.
Filters and sorting process only query columns and row positions.
The reader gathers only the rows needed for the requested page.

The service caches decoded tables and query row positions with least-recently-used eviction.
It retains at most 256 MiB of Arrow buffers, plus bounded metadata, across 128 entries.
Offsets and page limits do not affect query identity, so scrolling reuses the row order.
File identity, size, and modification timestamps form part of each cache key.
Every request safely opens the artifact and checks its file type before using cached data.
The cache clears when the service closes.

The measured table and sorted indices retain 170,452,991 accounted bytes.
The memory bound covers retained data. It does not bound temporary decoding or sorting allocations.
Tables larger than the budget can still require repeated decoding.
The first page still decodes the complete table. Partial Parquet reads remain future work.
Table reads still hold the service lock. Moving query execution outside that lock remains future work.
Revision checks, commit receipts, SQLite durability, and recovery behavior stay unchanged.

`./scripts/check.sh` passes: 1,103 tests pass and two platform tests skip.
After the final test additions, all ten cache tests and six benchmark contract tests pass.
The cache tests cover page parity, reuse, budgets, file replacement, corruption, symlinks, and pipes.

Raw measurements: [before](optimization-2026-10-19-before.json) and [after](optimization-2026-10-19-after.json).
The records include environment facts, sample counts, and fixture sizes.

Reproduce the after-run with a new data directory:

```sh
uv run --locked python3 scripts/benchmark_round1.py --no-cold \
  --root /tmp/melt-perf-new-run --output build/benchmarks/perf-new.json \
  --publication-samples 5 --cold-compile-samples 5 \
  --restore-samples 10 --sort-samples 10
```
