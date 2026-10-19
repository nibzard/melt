# 2. Sales analysis

Turn a saved interface into a repeatable calculation. A minimum-revenue
parameter controls a registered Python action. That action produces a
filtered table and a plot of monthly totals.

From the repository root, run:

```bash
uv run --locked python3 examples/02-sales-analysis/run.py
```

Follow the [shared setup](../README.md#setup) first if needed.
Running this example grants execution permission to `analyze.py` in its
new workspace. The script reads the captured sales input and stages outputs.

## What you see

The first result has three rows. January totals 400 and February totals 200.
The example changes the minimum from 0 to 200 through the parameter control.
The binding runs the action again. The second table has two rows, and the
monthly totals become 300 and 200.

Open `before.png` and `after.png` in the printed result directory.
The matching tables are `before.csv` and `after.csv`.
Both outputs publish in one transaction. The workflow needs no model calls.

Try another minimum:

```bash
uv run --locked python3 examples/02-sales-analysis/run.py --minimum 250
```

Only the January row with revenue 300 remains. This small demo accepts
minimum values above 0 and at most 300, so its plot always contains data.

## Change it

- Edit `sales.csv` to change the input data.
- Edit `analyze.py` to change the computation.
- Edit `views.json` to change controls, bindings, or pane layout.
- Inspect `contract.json` for the script's input and output types.

Run again in a fresh directory after each change. The saved workspace
retains the exact script, input versions, and parameters used for each run.
