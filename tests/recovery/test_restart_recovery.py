"""Recovery suite placeholder.

Backlog task T022 implements the fault-injection tests that SPEC.md
section 18.4 requires: kills before and after artifact finalization,
database commit, and acknowledgement, plus disk-full, missing artifact,
lost notification, changed epoch, reused request ID, corrupt
descriptor, cancelled run, and incompatible saved version scenarios.

This module keeps the `tests/recovery` directory in the collected suite
and marks the missing coverage explicitly. It claims no behavior.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="recovery fault-injection tests land with backlog task T022 "
    "(SPEC.md section 18.4)"
)


def test_kill_points_recover_as_specified():
    """Assert acknowledged references always point to complete files."""
