# Round 1 performance record

This document records the round 1 performance measurements of
`SPEC.md` section 19 (backlog task T023). One script produced every
number: `scripts/benchmark_round1.py`. The raw JSON record of the run
this document describes sits beside it as `round-1-linux.json`.

Recorded run: 2026-10-18T07:38:50Z. Reproduce it with the steps at the
end of this document.

## Declared machine and software

| Property | Value |
|---|---|
| CPU | 12th Gen Intel(R) Core(TM) i9-12900HK |
| Logical cores | 8 |
| Memory (`MemTotal`) | 12,246,824 KiB (11.7 GiB) |
| Memory (`MemAvailable` at start) | 10,632,952 KiB |
| Swap | 4,194,296 KiB |
| Kernel | 6.8.0-147-generic |
| Operating system | Ubuntu 24.04.5 LTS |
| Filesystem of the data root | ext4 on `/dev/sda1` |
| Python | CPython 3.12.3 |
| pydantic | 2.14.1 |
| pyarrow | 26.0.0 |
| pandas | 3.0.6 |
| matplotlib | 3.11.2 |
| numpy | 2.5.4 |
| Node helper | v24.18.0 |
| Pi Durable adapter | 1.3.0 (pinned and installed) |
| Clock | `clock_gettime(CLOCK_MONOTONIC)`, monotonic, 1 ns resolution |

Build mode: the CPython interpreter comes from `uv sync --locked`
against the committed `uv.lock`. The loop uses the compiled
`pydantic-core` and `pyarrow` extensions. Assertions stay off. Each
service open spawns a fresh Node storage worker. No debug or
instrumented build runs anywhere in the loop.

Persistence settings during every commit sample: `synchronous = 2`
(`FULL`) and `journal_mode = wal` on the owning connection, integrity
check `ok`, adapter 1.3.0. No durability setting was relaxed to reach
these numbers.

### Deviation from the declared memory class

Section 19 names a declared Linux machine with 16 GiB memory. The
runner this run used exposes `MemTotal` of 12,246,824 KiB, which is
below that class. The record states this openly instead of claiming
the named class. The deviation reduces memory headroom. It does not
shorten any timed path. Every target below is compared as recorded.
Re-running this record on a machine that exposes the full 16 GiB
remains open; the suite is one command.

## Fixtures

| Fixture | Size |
|---|---|
| Compile document | 18,627 bytes, 32 projections, 8 actions, 16 bindings |
| Paging table | 61,449,230 bytes, 1,000,000 rows, 20 columns |
| Paging column types | 7 string, 3 int64, 6 float64, 2 bool, 1 date, 1 timestamp |
| Publication staged output | 2,161 bytes of Parquet |

The compile fixture saturates the projection bound (32 of 32) and uses
all five control kinds. The paging fixture is deterministic for seed
20261018. The publication fixture is the section 13.2 script without
the plot, so the sample times publication rather than a matplotlib
import.

## Method

Every service measurement drives the framed channel the native host
will drive: `WorkspaceService.process_frame` of one encoded request
envelope. Measurements:

- **Compilation.** `compile_document` over the 18,627-byte document
  with catalogs parsed once. Warm means one process and cached
  catalogs, per the target's own condition. Cold means a fresh
  interpreter per sample, so process start, imports, and catalog
  parsing are included.
- **Service commits.** A bound `parameter.set` through
  `workspace.apply` (the service side of a control edit), and an
  `entity.replace` title patch plus the `workspace.subscribe`
  answer that carries the change (the service side of a committed
  patch reaching a visible update). The bound edit reserves a
  generation and replaces the pending intent, exactly as a real
  control edit does.
- **Artifact publication.** `publish_run` of one succeeded run:
  staged-output verification, artifact finalization with file and
  directory synchronization, the eligibility recheck, source version
  allocation, and the receipted commit. The runner process is not
  part of the sample.
- **Table paging.** `table.page` at offset 0, limit 50, over the
  1,000,000-row table. Warm means the artifact bytes sit in the
  kernel page cache. Cold means `sync` plus a dropped kernel page
  cache before each sample. Full-table sorting is reported
  separately, as section 19 requires.
- **Restoration.** `WorkspaceService.open` of a saved five-pane
  workspace plus the full `workspace.inspect` snapshot: ownership
  lock, storage worker spawn, state load, validation, and the
  snapshot, without executing project code.

Model calls and Python execution are absent from every patch-latency
sample by construction: no model exists in the loop, and publication
timing excludes the runner process.

Percentiles use the nearest-rank method: sort the samples, take rank
`ceil(0.95 x n)`, and never interpolate. The table lists `n` for every
row. `p95 below X ms` means the measured p95 is strictly below X.

## Results

| Measurement | Condition | n | p50 (ms) | p95 (ms) | max (ms) |
|---|---|---|---|---|---|
| compile-32-projection | warm: in-process, cached catalogs | 200 | 10.441 | 10.546 | 27.647 |
| compile-32-projection-cold-process | cold: fresh process per sample, kernel page cache warm | 10 | 146.256 | 149.890 | 149.890 |
| control-feedback-commit | warm: one owning service session, fsynced commits | 100 | 6.396 | 7.762 | 24.839 |
| committed-patch | warm: one owning service session, fsynced commits | 100 | 5.701 | 6.860 | 12.673 |
| committed-patch-notification | warm: one owning service session, fsynced commits | 100 | 5.839 | 6.998 | 12.815 |
| artifact-publication | warm: fsynced artifact and commit paths | 30 | 12.051 | 18.443 | 25.307 |
| table-page-first-warm | warm: artifact bytes in the kernel page cache | 30 | 67.728 | 79.699 | 91.116 |
| table-page-first-cold | cold: kernel page cache dropped before each sample | 5 | 87.162 | 91.574 | 91.574 |
| table-page-full-sort | warm: artifact bytes in the kernel page cache | 15 | 389.186 | 414.737 | 414.737 |
| restore-five-panes | warm: kernel page cache warm | 15 | 62.951 | 72.096 | 72.096 |
| restore-five-panes-cold | cold: kernel page cache dropped before each sample | 5 | 153.294 | 178.278 | 178.278 |

## Comparison against the section 19 targets

| Target | Measured p95 | Bound | Headroom | Verdict |
|---|---|---|---|---|
| Compile a 32-projection document with cached catalogs | 10.546 ms | p95 below 50 ms | 4.7x | met |
| Local control feedback | 7.762 ms | p95 below 50 ms | 6.4x | met |
| Small committed patch to visible update | 6.998 ms | p95 below 100 ms | 14.3x | met |
| Restore five saved panes | 72.096 ms | below 2 seconds | 27.7x | met |
| Warm first table page for 1 million rows and 20 columns | 79.699 ms | p95 below 300 ms | 3.8x | met |

Every round 1 target is met. No investigation duty opened under
section 19, and no persistence or correctness rule was weakened.

Two of the five targets are native-round targets in the specification.
Round 1 measures their service-side portion on Linux, as recorded
above: the framed commit behind local control feedback, the committed
patch plus its notification, and the service restore beneath pane
restoration. Round 2 repeats them through native controls on the
declared Apple Silicon machine, per section 19. The native portion is
not blocked by these numbers.

## Full-table sorting, reported separately

A `table.page` sorted by the `score` column descending over all
1,000,000 rows reaches p95 414.737 ms. The v0.1 reader decodes the
whole artifact, filters, then sorts every row with the original
position as tie-breaker, before it slices the 50-row window. Section 19
sets no target for full-table sorting; the number is context for later
paging work, not a gate.

The cost splits into artifact read and decode (about 68 ms at p50,
the unsorted figure) plus sort and take of the full table (the
remaining about 320 ms at p50). A future version could keep per-source
sort orders or a decoded-table cache. That is a design change for a
later specification round, not a fix this record requires.

## Reproduce

1. Run `uv sync --locked`.
2. Run `make benchmark-round1`, or
   `uv run --locked python3 scripts/benchmark_round1.py`.
3. Read the summary table it prints and the JSON it writes under
   `build/benchmarks/`.
4. For cold samples, allow passwordless `sudo -n` for
   `drop_caches`. Without it, pass `--no-cold`; the record then marks
   the cold rows as skipped with the reason.
5. To refresh this committed record, write to
   `docs/performance/round-1-linux.json` with `--output` and update
   this document from the same run.

The benchmark keeps its durable workspaces under
`$TMPDIR/$LOOPER_RESOURCE_PREFIX-round1-benchmark-root`, outside the
repository, and registers that root with the Looper cleanup file
before it creates anything.
