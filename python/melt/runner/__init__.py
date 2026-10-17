"""Captured execution manifest handling (SPEC.md section 13.2).

The runner receives a versioned manifest with the run ID, captured
parameters, input artifact paths, declared outputs, and a private
staging directory. It executes the registered script in a fresh
process without installing packages. Backlog task T015 implements it.
"""

from __future__ import annotations
