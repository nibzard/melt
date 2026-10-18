"""Platform adapter for application data paths (SPEC.md sections 2.2 and 14).

Round 1 runs on Linux, where the application data root is
``$XDG_DATA_HOME`` or ``$HOME/.local/share`` when unset. The macOS
branch follows section 14 for the later native round; Linux validation
never depends on it. Every workspace path in the service resolves
through this adapter, and tests inject an isolated temporary root
instead of the user's real data directory.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping

from runtime.storage.errors import PlatformResolutionError

WORKSPACE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}\Z")
"""Workspace identifiers follow the entity identifier syntax (SPEC.md 3)."""

WORKSPACES_ROOT_PARTS = ("melt", "workspaces")
"""Workspaces live under `<data-root>/melt/workspaces` (SPEC.md section 14)."""


def validate_workspace_id(workspace_id: str) -> str:
    """Return the identifier when it matches the workspace ID syntax."""

    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.match(workspace_id):
        raise ValueError(
            f"workspace identifier {workspace_id!r} does not match"
            " [a-z][a-z0-9_-]{0,63}"
        )
    return workspace_id


class PlatformAdapter:
    """Resolves the application data root and workspace directories."""

    def __init__(
        self,
        *,
        system: str | None = None,
        env: Mapping[str, str] | None = None,
        override_root: Path | None = None,
    ):
        self.system = (sys.platform if system is None else system).lower()
        self.env = dict(os.environ if env is None else env)
        self.override_root = Path(override_root) if override_root is not None else None

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        system: str | None = None,
    ) -> "PlatformAdapter":
        """Resolve the platform from the real environment."""

        return cls(system=system, env=env)

    @classmethod
    def with_root(cls, root: Path, *, system: str = "linux") -> "PlatformAdapter":
        """Force one data root, which is how tests isolate themselves."""

        return cls(system=system, env={}, override_root=Path(root))

    @property
    def application_data_root(self) -> Path:
        """The platform application data root (SPEC.md section 14)."""

        if self.override_root is not None:
            return self.override_root
        if self.system.startswith("darwin"):
            home = self.env.get("HOME")
            if home:
                return Path(home) / "Library" / "Application Support"
            raise PlatformResolutionError(
                "$HOME is not set, so the macOS application data root"
                " cannot be resolved"
            )
        # Linux and every other round-1 platform follow the XDG base
        # directory specification. A relative XDG_DATA_HOME is ignored,
        # as the specification requires.
        xdg_data_home = self.env.get("XDG_DATA_HOME")
        if xdg_data_home and os.path.isabs(xdg_data_home):
            return Path(xdg_data_home)
        home = self.env.get("HOME")
        if home:
            return Path(home) / ".local" / "share"
        raise PlatformResolutionError(
            "neither $XDG_DATA_HOME nor $HOME is set, so the application"
            " data root cannot be resolved"
        )

    @property
    def workspaces_root(self) -> Path:
        """The directory that holds every workspace."""

        return self.application_data_root.joinpath(*WORKSPACES_ROOT_PARTS)

    def workspace_directory(self, workspace_id: str) -> Path:
        """The directory of one workspace, validating its identifier."""

        validate_workspace_id(workspace_id)
        return self.workspaces_root / workspace_id


__all__ = [
    "PlatformAdapter",
    "WORKSPACES_ROOT_PARTS",
    "WORKSPACE_ID_PATTERN",
    "validate_workspace_id",
]
