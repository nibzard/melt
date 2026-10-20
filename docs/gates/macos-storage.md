# macOS storage verification

Verified locally on October 19, 2026, for backlog task T032 and SPEC.md section 14.

The owning Pi Durable connection now enables and reads back `synchronous=FULL`,
`fullfsync=ON`, and `checkpoint_fullfsync=ON` on macOS. The Python worker client
also rejects an open whose facts do not report these settings. The probe checks
that they survive adapter migrations, commits, and a WAL checkpoint; workspace
tests verify settings after a killed worker is reopened. Linux continues to
require FULL and WAL without depending on macOS settings.

Environment: Apple M5 Pro (Mac17,9), 48 GiB memory, macOS 26.6.2 (25G83),
Darwin 25.6.0 arm64, APFS on `/dev/disk3s5`, Node v26.4.0,
`@earendil-works/pi-durable` 1.3.0. This machine exceeds the specification's
16 GiB performance baseline; this is a correctness probe, not a performance result.

Reproduce from the repository root after `make probe-setup`:

```bash
node runtime/storage/pi_adapter/probe.mjs \
  --root "$(mktemp -d /tmp/melt-macos-storage.XXXXXX)" --json
```

Result: **25 cases passed, zero failed, one skipped**. The skipped case requires
Linux `strace`. Passing cases cover atomic updates, committed observation,
SIGKILL recovery, complete artifact references, SQLite integrity, connection
configuration, and all 24 cases of upstream storage conformance.

The crash model is process death through SIGKILL after acknowledged commits.
Power loss and operating-system crashes were not injected. SQLite full-sync
support is verified through pragma read-back; this probe does not trace actual
`F_FULLFSYNC` calls or prove hardware behavior. Artifact publication uses the
existing file-sync, rename, directory-sync ordering.

This closes the macOS storage prerequisite. T030's complete native integration
and T031's application release acceptance remain open; the development host
does not yet include Ghostty or standalone distribution packaging.

## Cross-platform review validation

The implementation review also hardened invalid table filter diagnostics,
including impossible calendar values, malformed UTC offsets, int64 overflow,
and unrepresentable decimals. Regression cases exercise the direct reader,
service dispatch, and framed channel, then verify the next valid frame succeeds.
Cancellation checks publish readiness after signal setup and complete PID files.
Platform-specific tests now inspect real macOS processes and resolve directory
identity without assuming Linux paths.

The final Linux `make check` run passed with **1,136 passed, 3 skipped in
165.54 seconds** in an isolated arm64 Docker container using Python 3.12.3,
Node 24.21, and uv 0.10.12. The skips are generated Swift verification and the
two native tests. Linux syscall tracing ran successfully. Docker requires
`--init` for the service-death check: without a reaper an exited orphan remains
a zombie and `kill(pid, 0)` still succeeds. A small process probe reproduced
that behavior and confirmed that `--init` reaps the orphan.

The final local macOS `make check` run passed with **1,137 passed, 2 skipped
in 211.64 seconds**, using Python 3.12.3 and uv 0.10.12. Generated Swift
canonicalization and the AppKit café/linked-view/LaunchServices checks passed.
The skips are Linux syscall tracing and the deferred full native acceptance
placeholder. Raw local logs are retained under `build/review/` (ignored).

The macOS CI job now runs the full `make check` command, including storage,
recovery, generated Swift verification, and the AppKit workflow, rather than
only the prototype's native checks. No remote CI run is claimed for this
uncommitted review.
