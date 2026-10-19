# Native host: first integration

This directory contains the first AppKit host source for macOS 13 or later.
It uses the existing Python compiler, workspace service, runner, and artifact store.
It does not create a second source of workspace state.

**Status:** The prototype compiles and passes native workflow checks on macOS 15 ARM in [CI](https://github.com/nibzard/melt/actions/runs/39244987090/job/118034632407).
The build targets macOS 13 or later; macOS 13 itself has not been tested. The full native release gate remains open.

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
The checks also select a supplier through the native control and verify both inspectors.
A LaunchServices check opens the built application, quits it, and opens the saved file through its default handler.
Run-history checks verify that reopening and selection do not execute project code.
Window captures are written to `build/native/artifacts/`.
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

The automated checks cover the café workflow and default file handler.
Complete the remaining native acceptance work before claiming a release:

1. Verify keyboard input, input methods, clipboard, and accessibility with the native controls.
2. Add, move, and hide panes; verify focus, controller continuity, and small-window behavior.
3. Exercise missing environments, unavailable artifacts, and conflicts through the native interface.
4. Add paging, sorting, filtering, plot navigation, Markdown rendering, and split-position persistence.
5. Integrate Ghostty and the live agent prompt through tested host adapters.
6. Record native performance measurements and test packaging on a clean Mac.

The host currently displays Markdown as source text and only the first 64 rows of each table.
Table paging controls, sorting, filtering, plot zoom/export, graph edges and Pele hit regions, split-position persistence,
Ghostty integration, a live agent prompt, host performance measurements, and distribution packaging remain open.
These limits prevent this host from satisfying the full native application gate in `SPEC.md`.

The repository's `Melt checks` workflow runs the native checks on a macOS 15 ARM runner.
Its current result, rather than the presence of the workflow file, determines whether native validation passes.
