# Native application gate

The native implementation and local package have automated acceptance coverage.
The application release gate remains open. A relocated package on a development
Mac does not establish clean-machine, minimum-OS, or signed-distribution support.

## Implemented and exercised

`native/check.sh` compiles the optimized AppKit host and pinned Ghostty library,
then uses isolated café, sales, and linked-view workspaces. The suite exercises:

- Stable pane, table, and terminal surface identities through updates, movement,
  hiding, and small-window overflow; focus survives layout changes.
- Ghostty keyboard submission to a real shell, marked-text handling, accessible
  viewport text, and exclusion of a synthetic model credential.
- Service-backed table sorting and clearing, bounded page continuation, stable
  key selection callbacks, and clipboard text serialization.
- Safe Markdown, hash-verified PNG decoding, zoom/fit, and exact-byte export.
- Revision-checked divider persistence and separate durable host settings for
  sort, filters, keyed selection, and pinning. Same-ID recreation resets settings.
- Café edits, execution, publication, saving, five-pane restoration, and queued
  edit draining on close. Restoration creates no run.
- The specified sales input: three rows at minimum zero, two at minimum 200,
  movement during live Python execution, preserved layout after publication,
  offline restoration, and explicit reinvocation. History checks captured input
  version 1 and parameter values 0, 200, 200 across exactly three successful runs.
- Linked supplier selection updating both inspectors without project execution,
  and LaunchServices opening, shutdown, and reopening.

The native placeholder was replaced with assertions backed by the real harness.
Linux still skips AppKit and Ghostty. The historical Linux gate checks its
recorded revision rather than requiring today's native suite to remain a skip.

Final `make check` results on October 20, 2026: macOS **1,148 passed, 1 skipped**
(Linux `strace` evidence is unavailable on macOS); Linux arm64 container
**1,146 passed, 3 skipped** (Swift generation compilation and the two AppKit
checks). Both gates include protocol drift and canonicalization checks. The
Linux run uses the pinned Python/Node/uv toolchain and an init process for
correct subprocess-death observation.

## Package isolation

`native/package.py` embeds the managed Python 3.12.3 runtime, locked Python
runtime packages, pinned Node 22.19.0, both Node adapters, and Ghostty resources.
Runtime paths are bundle-relative. Python runs with isolated imports and disabled
bytecode writes. The package manifest records dependency versions and hashes.

`native/package-check.sh` copies the bundle into a temporary path with spaces,
checks imports with poisoned `PYTHONPATH`, checks all 151 Mach-O dependencies and
signatures, runs the native workflows against the relocated runtime, exercises
LaunchServices, and checks signatures again after calculation. No checkout path
or third-party absolute library dependency is required by the bundled runtime.
The test fixture preparation and harness still use this development machine.

The rebuilt package passed this complete check on October 20, 2026, including
all 151 dependency/signature checks both before and after the native workflows.
[Package runtime evidence](native-package.json) records the actual bundled Node
v22.19.0 run separately from the development runtime. The local application is
at `build/package/Melt.app`; it remains ad-hoc-signed.

## Measurement method and limits

The [committed JSON evidence](native-application.json) records hardware, versions, build mode, individual
samples, and method. The test machine is an Apple M5 Pro, Mac17,9, 48 GiB,
macOS 26.6.2 arm64. Swift uses `-O`; Ghostty uses ReleaseFast at commit
`c7cbc9bfd44cb11a9c7eae00f1166624671e363b`.

Patch latency covers 25 title-only revision-checked mutations from request to
acknowledged visible title. It excludes model calls and Python execution.
Restoration measures a fresh service and five decoded/rendered panes with a warm
filesystem; it is one sample, not a cold-cache distribution. A 10 ms main-loop
timer during calculation measures responsiveness. Its interval p95 is a proxy,
not a measurement of local input feedback latency. These observations do not
satisfy the specified 16 GiB baseline.

Local development measurements on October 20, 2026:
The Linux container gate also ran during this host check; the machine was not
otherwise isolated for benchmarking.

| Measurement | Observation | Scope |
|---|---|---|
| Small patch to acknowledged visible title | p95 53.1 ms, 25 samples | Below the 100 ms target on this 48 GiB Mac |
| Restore five panes | 488.7 ms, one sample | Below the 2 second target; warm filesystem |
| Main-loop timer during calculation | p95 interval 13.5 ms | Responsiveness proxy; not the input-feedback target |

The final native window capture also confirms the complete plot remains visible
after adding a fifth pane. Fit uses the viewport frame rather than magnified
clip bounds, and remains stable when applied repeatedly. Divider persistence
uses controller pane items rather than counting AppKit's internal subviews.

## Remaining release conditions

1. Run the package on a clean Mac with no checkout, uv, Node, Python development
   environment, or Xcode; repeat calculation and restart recovery. Validate the
   intended minimum macOS version, including Ghostty compatibility.
2. Complete Developer ID signing, notarization, and Gatekeeper validation for
   distribution. Current signatures are ad hoc; nothing has been published.
3. Measure local control feedback, patch latency, and restoration on the declared
   16 GiB Apple Silicon baseline. Investigate any missed target.
4. Complete on-screen Ghostty/Metal, native clipboard, keyboard traversal, a real
   input method, and VoiceOver validation. Automated preedit and accessibility
   adapter checks do not establish the complete assistive-technology experience.
   AppKit bitmap captures omit Metal layers. The attempted local UI-control
   session returned stale-state and timeout errors before opening the test
   workspace, so no manual terminal-rendering success is claimed.

The toolbar's structured-operation preview/apply route is available independently
of document layout. A live model prompt/provider adapter is not implemented.
Local controls and saved results require no model connection.
