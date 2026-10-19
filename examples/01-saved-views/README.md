# 1. Saved views

Create a small workspace that combines an expense table and a Markdown note.
No analysis script or execution permission is needed.

From the repository root, run:

```bash
uv run --locked python3 examples/01-saved-views/run.py
```

Follow the [shared setup](../README.md#setup) first if needed.

You see three expenses: Travel 120.50, Software 49.00, and Supplies 32.25.
The note gives their total, 201.75. The example reopens the workspace and
checks that both projection definitions remain saved.

The result directory contains `expenses.csv` and `notes.md`.
Use the printed inspection command to read the stored definitions.

## Change it

Edit `expenses.csv` or `notes.md`, then run again. The CSV becomes an
immutable Parquet source during import. `views.json` defines the table,
the note projection, and their split layout.

The note is static text. If you change amounts, update its total too.
The next example computes its results with Python.
