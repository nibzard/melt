"""The error type of the Melt SDK (SPEC.md section 13.2).

Project code meets this error when it misuses `ui`. The runner prints
the message and fails the run; the service never reads the message as
authority. Control data travels in the staged files, never in project
stdout (SPEC.md section 12.1).
"""

from __future__ import annotations


class MeltSDKError(RuntimeError):
    """Raised when project code uses `ui` outside its contract."""


__all__ = ["MeltSDKError"]
