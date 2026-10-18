"""Melt Python package: schema models, SDK, and runner.

The package follows the layout that SPEC.md section 17 requires. The
`melt` name replaces the earlier Malleable name in all new code
(SPEC.md section 1).

Project scripts import `ui` from this package (SPEC.md section 13.2).
The runner binds it to the active run, so the import stays valid in
every environment that holds the package.
"""

from __future__ import annotations

from melt.sdk import ui

__version__ = "0.1.0"

LANGUAGE_ID = "melt/v0.1"

__all__ = ["LANGUAGE_ID", "ui", "__version__"]

