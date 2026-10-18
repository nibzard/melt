"""`python -m melt.runner <manifest-file>` runs one captured manifest.

The service launches this module in a fresh process for every run
(SPEC.md section 13.2). See `melt.runner.execute` for the exit codes.
"""

from __future__ import annotations

from melt.runner.execute import main

if __name__ == "__main__":
    raise SystemExit(main())
