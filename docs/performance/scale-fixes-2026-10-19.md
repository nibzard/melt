# Fixes for workspace growth and read contention, 2026-10-19

This run repeats the [scale benchmark](scale-2026-10-19.md) after fixing its three main findings.
The machine, fixtures, request counts, cache budget, and durability settings are unchanged.
The test suite and benchmark run separately.

Storage scans now reduce their page size until the encoded response fits the existing 1 MiB frame limit.
Retries keep the same opaque cursor, so pages preserve record order and do not skip entries.
The Python reader reuses the smaller page size for later scan requests.
Recovery consumes the records already read during header validation, avoiding a duplicate full scan.
An intervening append invalidates that saved scan.

All six workspace fixtures now reopen successfully, including the three that previously failed.

| Projections / parameters | Saved edits | Before reopen median (ms) | After reopen median / p95 (ms) |
|---|---:|---:|---:|
| 8 / 16 | 20 | 79.392 | 74.878 / 81.369 |
| 8 / 16 | 100 | 133.991 | 113.2 / 129.089 |
| 8 / 16 | 500 | 386.219 | 286.065 / 288.374 |
| 32 / 256 | 20 | FAIL: E_LIMIT | 199.135 / 215.445 |
| 32 / 256 | 100 | FAIL: E_LIMIT | 617.769 / 625.032 |
| 32 / 256 | 500 | FAIL: E_LIMIT | 2134.609 / 2150.185 |

Unsorted pages now decode batches of at most 4,096 rows and skip unrelated Parquet row groups.
The cache retains those batches, so a large table no longer needs to fit in memory for scrolling to reuse decoded data.
A first read deep inside a row group can still decode preceding batches in that group.
Filters and sorting still require a full table scan and can allocate more than the retained cache budget.
The 256 MiB budget bounds retained buffers, not all temporary query memory.

A separate one-million-row benchmark measures first pages with an empty service cache.
The median falls from 63.361 ms to 15.298 ms; the new p95 is 17.025 ms over 30 samples.
The filesystem cache remains warm.
[Raw first-page measurements](round1-scale-fixed-2026-10-19.json).

| Table rows | Read callers | Before page median / p95 (ms) | After page median / p95 (ms) | After requests / second |
|---|---:|---:|---:|---:|
| 1,000,000 | 1 | 2.642 / 4.79 | 2.667 / 2.853 | 369.59 |
| 1,000,000 | 4 | 11.182 / 11.458 | 11.152 / 17.696 | 330.39 |
| 1,000,000 | 8 | 23.517 / 27.023 | 22.618 / 39.983 | 314.33 |
| 3,000,000 | 1 | 154.461 / 196.636 | 2.684 / 2.749 | 368.55 |
| 3,000,000 | 4 | 658.83 / 705.288 | 11.033 / 15.919 | 354.22 |
| 3,000,000 | 8 | 1338.806 / 1411.401 | 24.545 / 43.906 | 299.28 |

The service captures the immutable source version, schema, and observed commit sequence under the workspace lock.
It reads, sorts, and encodes the page after releasing that lock.
Every response retains the captured sequence even if an edit commits before the read finishes.
A short separate lock protects cache accounting. Cache-miss decoding and sorting share a query lock to limit temporary allocations.
Cached pages and workspace edits do not acquire that query lock.
Closing the service clears the cache and prevents admitted reads from repopulating it.

| Table rows | Workload | Before edit median / p95 (ms) | After edit median / p95 (ms) | After edit lock wait median (ms) |
|---|---|---:|---:|---:|
| 1,000,000 | One editor | 23.032 / 37.893 | 22.791 / 37.18 | 0.001 |
| 1,000,000 | One editor + 3 readers | 31.475 / 41.069 | 24.589 / 37.429 | 0.764 |
| 1,000,000 | One editor + 7 readers | 42.688 / 56.214 | 31.153 / 48.628 | 6.421 |
| 1,000,000 | One editor + 3 sorting readers | 417.576 / 427.885 | 22.776 / 37.184 | 0.001 |
| 3,000,000 | One editor | 22.33 / 38.027 | 23.0 / 37.222 | 0.001 |
| 3,000,000 | One editor + 3 readers | 496.468 / 548.644 | 26.227 / 39.437 | 0.748 |
| 3,000,000 | One editor + 7 readers | 1182.363 / 1216.251 | 35.364 / 49.219 | 5.64 |
| 3,000,000 | One editor + 3 sorting readers | 1937.151 / 1999.451 | 23.94 / 41.741 | 0.001 |

The benchmark uses concurrent threads calling process_frame on one service, with a warm filesystem cache.
The stdio serve loop still handles one request at a time. These results do not measure concurrent requests over that single channel.
The sort workload changes a nonselective filter on every request to force recomputation of the full row order.
These are exploratory runs; each caller sends 20 requests, and each restore fixture uses five samples.
Every page and edit succeeds. Commit counts, cache bounds, and percentile calculations are checked.
The old failed workspaces also reopen with their original commit sequences after applying the fix.
Their parameter values, revisions, projection counts, and final command receipts are preserved.

`./scripts/check.sh` passes: 1,114 tests pass and two platform tests skip.
The final targeted run covers all 16 cache and concurrency cases, plus six benchmark contract tests.

Regression tests cover UTF-8 scan sizes, ascending and descending cursors, scan reuse and invalidation,
batch and row-group boundaries, bounded batch decoding, cached reads during a blocked sort,
edits during a blocked sort, observed commit sequences, and shutdown during a read.

Reproduce the measurements:

```sh
uv run --locked python3 scripts/benchmark_scale.py \
  --output build/benchmarks/scale-fixed.json
```

[Raw after measurements](scale-fixed-2026-10-19.json) and [raw before measurements](scale-2026-10-19.json).
