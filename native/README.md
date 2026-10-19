# Native host: first integration

This directory contains the first AppKit host source for macOS 13 or later.
It uses the existing Python compiler, workspace service, runner, and artifact store.
It does not create a second source of workspace state.

**Status:** Source and Linux service-boundary tests exist. The AppKit sources have not been compiled or exercised on this Linux machine.
The native release gate remains open.

## Build on macOS

Install the Xcode command line tools and the [repository toolchain](../README.md#try-it).
Run these commands from the repository root:

```bash
make probe-setup
make pele-setup
./native/check.sh
uv run --locked python3 examples/04-cafe/run.py --output /tmp/little-hours
open -a "$PWD/build/native/Melt.app" "/tmp/little-hours/Little Hours.meltapp"
```

`native/check.sh` builds and signs `build/native/Melt.app` locally.
It also compiles checks for launcher validation, controller continuity during table updates, and recovery after a missing result.
The checks then create a café workspace, change its controls through the native host, run the calculation,
save a launcher, close the window, and reopen it with matching state, plans, and displayed results.
A separate run-history check verifies that reopening did not execute the calculation again.
The resulting bundle references this checkout and its Python environment. Keep both in place.
This is a development bundle, not a standalone distribution.

## Current host scope

- Load the compiled layout and keep pane controllers by projection identity.
- Display bounded table pages, PNG plots, text documents, and record observers.
- Use a native node selector for graph events and linked inspectors.
- Edit typed parameter controls, activate actions, and cancel a run.
- Grant execution permission through a host-generated dialog.
- Preview and apply a saved operation array through **Apply change**.
- Save a local workspace reference with **Save to Desktop**.
- Reopen that reference without running project code.

**Apply change** accepts structured operations from a file. It is not a live model integration.
Programmatic proposals use the same service validation as user commands and cannot grant execution permission.

## Service boundary

The host starts one owned `python3 -m runtime.desktop` process per workspace.
Requests and responses use private pipes with four-byte length prefixes and a 1 MiB frame limit.
The service remains the sole writer and owns revisions, scheduling, permissions, and publication.
No network listener is created.

Calculations run in the service's non-blocking execution slot.
The host polls completed runs and publishes only runs launched by its current session.
Restoration does not publish or replay old uncertain work.
Closing the host pipe asks the service to flush state and stop owned workers.
Quitting the application waits for its workspace services to stop before completing termination.

`melt-launcher/v1` is a host-local descriptor with `root` and `workspaceId` fields.
It contains an absolute reference to existing local data.
It does not contain a copy, grant new authority, install code, or implement portable sharing.

## Checks still required

Run the native checks on macOS before claiming this host works there.
Then verify the café workflow manually:

1. Open the launcher and inspect all four views without a new run.
2. Change both parameters and run the calculation; verify consistent outputs.
3. Save a reference on the desktop and close the window during idle and active work.
4. Open the reference again; verify state restoration and no automatic execution.
5. Add or move a pane through **Apply change**; verify focus and controller continuity.
6. Open a linked-views workspace and verify shared selection and both inspectors.
7. Check keyboard input, input methods, accessibility, small-window behavior, and missing artifacts.

The host currently displays Markdown as source text and only the first 64 rows of each table.
Table paging controls, sorting, filtering, plot zoom/export, graph edges and Pele hit regions, split-position persistence,
Ghostty integration, a live agent prompt, host performance measurements, and distribution packaging remain open.
These limits prevent this host from satisfying the full native application gate in `SPEC.md`.

The repository's `Melt checks` workflow runs the native checks on a macOS 15 ARM runner.
Its current result, rather than the presence of the workflow file, determines whether native validation passes.
