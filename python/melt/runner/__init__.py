"""Captured execution manifest handling (SPEC.md section 13.2).

The runner package lives on the project side of the process boundary.
It reads the versioned manifest, exposes `ui` to the script, stages the
declared outputs, and reports what it staged. The service-side launch
harness lives in `runtime.execution.launcher`: it writes the manifest
and the materialized script into the private staging directory, builds
a credential-free child environment, and spawns this runner in a fresh
process without installing packages.
"""

from __future__ import annotations

from melt.runner.execute import (
    EXIT_COMPLETE,
    EXIT_INCOMPLETE,
    EXIT_OPERATIONAL,
    EXIT_SCRIPT_FAILED,
    execute_manifest,
    load_manifest,
    main,
)
from melt.runner.manifest import (
    RUN_MANIFEST_VERSION,
    MeltManifestError,
    OutputKind,
    RunId,
    RunManifest,
    build_manifest,
    manifest_to_wire,
    parse_manifest,
)

__all__ = [
    "EXIT_COMPLETE",
    "EXIT_INCOMPLETE",
    "EXIT_OPERATIONAL",
    "EXIT_SCRIPT_FAILED",
    "MeltManifestError",
    "OutputKind",
    "RUN_MANIFEST_VERSION",
    "RunId",
    "RunManifest",
    "build_manifest",
    "execute_manifest",
    "load_manifest",
    "main",
    "manifest_to_wire",
    "parse_manifest",
]
