"""Native suite placeholder for round 2.

SPEC.md section 18.5 defers native checks to the macOS round: terminal
controller identity, focus, keyboard navigation, clipboard, input
methods, accessibility labels, pane hiding, and small-window overflow.
Backlog task T030 owns that work.

The Linux gate must not require a macOS machine or Apple tools, so this
module only keeps `tests/native` in the collected suite and marks the
deferred coverage explicitly. It claims no behavior.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="native integration belongs to round 2, backlog task T030 "
    "(SPEC.md sections 2.2 and 18.5)"
)


def test_native_controller_identity_survives_updates():
    """Verify controller identity while moving and updating Melt panes."""
