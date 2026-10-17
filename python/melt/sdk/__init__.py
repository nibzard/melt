"""Python SDK for Melt project scripts (SPEC.md section 13.2).

The SDK exposes `ui.params`, `ui.input_path(name)`, and
`ui.output(name, value)` to a running script. Backlog task T015
implements it. A run cannot change its own action, bindings,
permissions, or layout.
"""

from __future__ import annotations
