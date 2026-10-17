"""Melt command-line entry point.

Backlog task T006 implements `melt validate` and `melt compile`
(SPEC.md section 11.3). Until then this module only reports the pinned
package and language versions, so the scaffold stays testable.

Exit codes follow the specification: 0 on success, 1 for invalid input,
and 2 for operational failure. An unimplemented command is an
operational failure, so it exits with 2.
"""

from __future__ import annotations

import argparse
import sys

from melt import LANGUAGE_ID, __version__

_NOT_IMPLEMENTED = (
    "melt accepts no commands yet. Backlog task T006 implements "
    "`melt validate` and `melt compile` (SPEC.md section 11.3)."
)


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the command line."""
    parser = argparse.ArgumentParser(
        prog="melt",
        description="Melt compiler command line (round 1 scaffold).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"melt {__version__} ({LANGUAGE_ID})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return the process exit code."""
    parser = build_parser()
    _, remaining = parser.parse_known_args(argv)
    print(_NOT_IMPLEMENTED, file=sys.stderr)
    if remaining:
        print(f"unrecognized arguments: {' '.join(remaining)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
