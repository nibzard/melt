"""Melt command-line entry point (SPEC.md section 11.3).

``python -m melt`` runs the same command line the installed ``melt``
script runs:

```bash
python -m melt validate fixtures/valid/sales-full.json
python -m melt compile fixtures/valid/sales-full.json \
    --catalog fixtures/catalog.json --json
```

Exit codes: 0 on success, 1 for invalid input, and 2 for operational
failure. See `melt.cli` for the command surface.
"""

from __future__ import annotations

from melt.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
