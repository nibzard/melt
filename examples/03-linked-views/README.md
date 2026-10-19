# 3. Linked views

Make several views share one supplier selection. Then add another view
without changing the selection or the graph's event binding.

This example uses the experimental `melt/semantic-draft-1` language.

From the repository root, run:

```bash
uv run --locked python3 examples/03-linked-views/run.py
```

Follow the [shared setup](../README.md#setup) first if needed.

## What you see

1. Select supplier `S-42`. The graph highlights node `s-42`, and the inspector shows Alpha Components.
2. Add a risk inspector. It immediately shows the same supplier, with risk Low and country Germany.
3. Select supplier `S-73`. Both inspectors show Beta Systems, with risk High and country France.
4. Reopen the workspace. The selection remains saved.

The example checks that adding the inspector preserves the selection's
value and revision. Selection updates use typed service events. They run
no project action and make no model calls.

Open `network.svg` in the result directory to see the supplier graph.
It is a static graph export. The terminal shows selection and highlighting;
`selection.json` contains the saved observer results. This example does
not provide a clickable graphical interface.

Choose the second selection:

```bash
uv run --locked python3 examples/03-linked-views/run.py --key S-42
```

## Change it

- Edit `suppliers.csv` to change supplier records.
- Edit `suppliers.graph.json` to change graph nodes or edges.
- Edit `add-inspector.json` to change the new inspector's fields.

Keep supplier keys unique. Every graph key must identify a supplier.
Run again in a fresh directory after each change.
