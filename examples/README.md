# Runnable examples

Run these examples in order from the repository root. Each example prints
readable results and saves a workspace you can inspect afterward.

These examples use the Linux command-line harness. Tables and selections
appear in the terminal. Plots and graphs are saved as image files.

## Setup

Use Python 3.12, uv 0.10.x, and Node 22.19 or newer with npm.
Install the pinned dependencies once:

```bash
uv sync --locked
make probe-setup
make pele-setup
```

## Choose an example

| Example | Command | What you learn |
|---|---|---|
| [1. Saved views](01-saved-views/README.md) | `uv run --locked python3 examples/01-saved-views/run.py` | Import data and restore a table and note without computation |
| [2. Sales analysis](02-sales-analysis/README.md) | `uv run --locked python3 examples/02-sales-analysis/run.py` | Change a parameter and publish a matching table and plot |
| [3. Linked views](03-linked-views/README.md) | `uv run --locked python3 examples/03-linked-views/run.py` | Share selection between views and add another observer |

The second example grants execution permission to its included Python
script inside its new workspace. Read `02-sales-analysis/analyze.py`
before running it. The first and third examples run no project action.
No example requires a model account or credentials.

## Saved results

Each run creates a fresh directory under your system's temporary directory.
The final output prints its location and a command to inspect the saved
workspace. Temporary files can disappear when your system clears them.

Choose an empty directory to keep results longer:

```bash
uv run --locked python3 examples/02-sales-analysis/run.py \
  --output "$HOME/melt-sales-example"
```

An existing directory must be empty. A repeated run uses a fresh default
directory or another `--output` path. It never resets a saved workspace.

Every result directory includes:

- `data/`: the durable workspace, artifacts, and run history.
- `commands/`: numbered JSON responses from the real harness commands.
- Exported results, such as CSV tables, PNG plots, or an SVG graph.

Each harness command opens and closes the workspace. Later commands restore
committed state from disk. Example files stay editable: change their CSV,
JSON definitions, or analysis script, then run again in a fresh directory.

Example 3 uses the experimental `melt/semantic-draft-1` language.
Examples 1 and 2 use `melt/v0.1`.
