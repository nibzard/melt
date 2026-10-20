# Native macOS host

The AppKit host renders compiled plans through the existing Python workspace
service. It keeps table, plot, document, control, and Ghostty controllers alive
across source and layout updates. The service owns revisions, execution,
permissions, publication, and durable state.

## Development build

Install the [repository toolchain](../README.md#try-it), full Xcode, and its Metal
compiler. If `xcrun -sdk macosx metal -v` reports that the compiler is missing, run
`xcodebuild -downloadComponent MetalToolchain`.

From the repository root:

```bash
make probe-setup pele-setup
make native-check
uv run --locked python3 examples/04-cafe/run.py --output /tmp/little-hours
open -a "$PWD/build/native/Melt.app" "/tmp/little-hours/Little Hours.meltapp"
```

The first build fetches Ghostty commit
`c7cbc9bfd44cb11a9c7eae00f1166624671e363b`, then builds its internal embedding
library with Zig 0.16.0 and ReleaseFast optimization. The setup script downloads
and verifies Zig if that version is unavailable. This C embedding interface is
pinned with its matching header; upgrading Ghostty requires adapter validation.
Swift builds use `-O`. The deployment target is macOS 13; current local checks
run on Apple Silicon macOS 26.6.2. macOS 13 has not been validated.

`build/native/Melt.app` is an ad-hoc-signed development bundle that references
this checkout and its Python environment. Keep both in place.

## Host behavior

- Tables offer bounded paging, sorting, typed filters, and text copying. Sort,
  filter, keyed selection, and pin settings persist in separate host records.
  Restoration validates the selected key against the approved source bytes.
- PNG plots decode away from the main thread, verify their saved content hash,
  support zoom/fit and exact-byte export, and identify the generating run.
- Documents support plain text and Markdown. Markdown does not load remote
  images, embed attachments, or enable executable URI schemes.
- Typed parameter controls and action buttons use the same revision checks as
  the command-line harness. Local controls and saved results need no model.
- Pane headers offer move-first, pin, and hide controls. The toolbar restores
  hidden panes. Divider changes persist through revision-checked commands.
  Small windows use a selector without rewriting the saved layout.
- **Terminal** explicitly creates a Ghostty surface beside the authored layout.
  Hiding or moving Melt panes preserves the terminal surface. Opening a saved
  workspace does not start a shell. The shell uses `/bin/zsh -f`, starts in the
  workspace data directory, and excludes model credentials using the execution
  environment policy. Clipboard paste is a user action; terminal clipboard-read
  requests are denied.
- **Apply change** previews structured operations from a file. Programmatic
  proposals cannot grant execution permission and protect pinned panes and
  active edits. A live model prompt/provider adapter is not implemented.
- **Save to Desktop** saves a `melt-launcher/v1` reference to existing local data,
  not a portable copy of the workspace or a permission grant.

## Checks and evidence

`make check` includes the native suite on macOS and skips AppKit on Linux.
`make native-check` runs just the native harness. It checks controller identity,
focus, input/preedit, accessibility adapters, table controls, clipboard text,
Markdown policy, plot zoom/export, split persistence, hiding, overflow, and
Ghostty input. It drives the café and sales calculations, moves a sales pane
while Python runs, restores five saved panes, and verifies linked selection and
LaunchServices behavior. Saved history verifies restoration never reruns code.

Artifacts are written to `build/native/artifacts/`, including timing samples in
`native-acceptance.json`. AppKit bitmap captures can omit Metal terminal layers;
they are not evidence of Ghostty's on-screen rendering. Automated preedit and
accessibility assertions do not replace manual input-method and screen-reader
validation. See [the native gate record](../docs/gates/native-application.md).

## Standalone local package

Use uv's managed relocatable Python 3.12.3 environment:

```bash
make native-package
./native/package-check.sh --skip-build
# Or rebuild and check together:
make native-package-check
```

`build/package/Melt.app` embeds Python, locked runtime distributions, both pinned
Node adapters, Node 22.19.0, and Ghostty resources. Packaging verifies copied
installed files against their wheel RECORD hashes and records locked wheel
hashes and copied-tree hashes in `Contents/Resources/Package.json`. Python's
standalone build and Node archive hash are recorded too. Runtime imports use
isolated mode without bytecode writes, so calculations do not alter the signed
bundle.

The package check relocates a copy into a path with spaces, checks isolated
imports with a deliberately invalid `PYTHONPATH`, audits native-library paths
and signatures, runs native workflows and LaunchServices against that copy,
and verifies signatures again. Fixture preparation and the test harness still
use development tools on this Mac. This does not establish clean-machine or
minimum-OS compatibility. Bundles use ad-hoc signing; Developer ID signing,
notarization, and distribution remain outstanding.

## Service boundary

Each workspace has one owned `python3 -m runtime.desktop` process with private
pipes, four-byte length prefixes, and a 1 MiB frame limit. Project stdout never
shares the control pipe. Preview truncation preserves page continuation offsets;
saved artifacts remain complete. Transport, table reads, image decoding, and
Markdown parsing run away from the native main thread.

The service uses its nonblocking execution slot. The host publishes completed
runs launched by its session; restoration does not replay uncertain work.
Closing a window drains queued commands and stops owned workers. Quitting waits
for workspace services. No network listener is created.
