"""The run context one project script executes in (SPEC.md section 13.2).

The runner reads the versioned manifest and opens one run context from
it. The context holds everything the manifest captured:

- the run identifier;
- the captured parameter values, read-only for the script;
- the absolute path of every captured input artifact;
- the declared output names with their kinds;
- the private staging directory of the run.

The `ui` object reads the active context. A script that imports `ui`
outside a run gets a clear error on first use, not a silent empty one.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

from melt.schema.canonical import parse_json
from melt.schema.limits import DEFAULT_LIMITS
from melt.sdk.errors import MeltSDKError
from melt.sdk.materialize import (
    OUTPUT_KINDS,
    StagedOutput,
    fsync_directory,
    stage_value,
)

OUTPUTS_DIRECTORY_NAME = "outputs"
"""Staged output files land in this directory of the staging tree."""

STAGED_INDEX_NAME = "staged-outputs.json"
"""The runner writes the staged-output report under this name."""

STAGED_INDEX_VERSION = 1
"""The version of the staged-output report format."""

_ACTIVE: "RunContext | None" = None


def active_context() -> "RunContext":
    """Return the context of the running script.

    Outside a run there is no context. The message tells the caller what
    to do, because `melt.ui` exists in every environment that holds the
    package, not only inside a run.
    """

    if _ACTIVE is None:
        raise MeltSDKError(
            "no run is active; the melt runner binds ui from the run"
            " manifest, so ui works only inside a Melt run"
        )
    return _ACTIVE


@dataclass
class RunContext:
    """One bound run: captured inputs, parameters, and staged outputs."""

    runId: str
    parameters: dict[str, object]
    inputs: dict[str, Path]
    outputs: dict[str, str]
    staging: Path
    staged: dict[str, StagedOutput] = field(default_factory=dict)

    @property
    def outputs_directory(self) -> Path:
        """The directory staged output files land in."""

        return self.staging / OUTPUTS_DIRECTORY_NAME

    def input_path(self, name: str) -> Path:
        """Return the path of one captured input artifact.

        The manifest captured the path at admission. An unknown name and
        a missing file are both errors, so a script never reads a path
        the run did not capture.
        """

        if name not in self.inputs:
            declared = ", ".join(sorted(self.inputs)) or "none"
            raise MeltSDKError(
                f"no captured input named {name!r}; this run captured:"
                f" {declared}"
            )
        path = self.inputs[name]
        if not path.is_file():
            raise MeltSDKError(
                f"captured input {name!r} has no file at {path}"
            )
        return path

    def staged_bytes(self) -> int:
        """Return the total size of every staged output."""

        return sum(output.byteLength for output in self.staged.values())

    def missing_outputs(self) -> "list[str]":
        """Return the declared outputs the script has not staged."""

        return sorted(name for name in self.outputs if name not in self.staged)

    def stage(self, name: str, value: object) -> StagedOutput:
        """Stage one declared output and return its staged record.

        The name must be declared in the manifest and must not be staged
        already (SPEC.md section 13.2). The staged bytes of one run stay
        inside the service limit, and project code cannot raise it.
        """

        if name not in self.outputs:
            declared = ", ".join(sorted(self.outputs)) or "none"
            raise MeltSDKError(
                f"output {name!r} is not declared; this run declares:"
                f" {declared}"
            )
        if name in self.staged:
            raise MeltSDKError(
                f"output {name!r} is staged already; ui.output accepts"
                " each declared name once"
            )
        output = stage_value(
            self.outputs[name], name, value, self.outputs_directory
        )
        total = self.staged_bytes() + output.byteLength
        bound = DEFAULT_LIMITS.staged_output_bytes_per_run
        if total > bound:
            output.path.unlink(missing_ok=True)
            raise MeltSDKError(
                f"staged output bytes exceed the allowed bound of"
                f" {bound}; staging {name!r} would bring this run to"
                f" {total} bytes"
            )
        self.staged[name] = output
        return output

    def index_document(self) -> dict:
        """Return the staged-output report of this run.

        The report names each staged file with its kind, media type,
        path, length, and hash. The service rereads every file and
        validates it independently, so nothing here is trusted input
        (SPEC.md sections 13.2 and 13.4).
        """

        return {
            "indexVersion": STAGED_INDEX_VERSION,
            "runId": self.runId,
            "outputs": {
                name: self.staged[name].to_wire() for name in sorted(self.staged)
            },
        }

    def write_index(self) -> Path:
        """Write the staged-output report into the staging directory."""

        final = self.staging / STAGED_INDEX_NAME
        temporary = self.staging / f".{STAGED_INDEX_NAME}.tmp-{os.getpid()}"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.index_document(), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        fsync_directory(self.staging)
        return final


def read_staged_index(staging: Path) -> "dict | None":
    """Return the staged-output report of one staging directory.

    The report exists only after a run staged its declared outputs and
    closed cleanly. A missing report answers None; a malformed one
    raises, because the caller asked for it by name.
    """

    path = Path(staging) / STAGED_INDEX_NAME
    if not path.is_file():
        return None
    return parse_json(path.read_text(encoding="utf-8"))


@contextmanager
def run_context(
    *,
    run_id: str,
    parameters: Mapping[str, object],
    inputs: "Mapping[str, str | Path]",
    outputs: Mapping[str, str],
    staging: "str | Path",
) -> Iterator[RunContext]:
    """Bind one run context around the body.

    The runner calls this with the validated manifest fields. A nested
    call replaces the active context and restores the previous one on
    exit, so tests can nest runs without leaking state.
    """

    global _ACTIVE

    unknown = sorted(
        name for name, kind in outputs.items() if kind not in OUTPUT_KINDS
    )
    if unknown:
        raise MeltSDKError(
            "unknown output kind for: " + ", ".join(unknown)
            + f"; declared kinds are {', '.join(sorted(OUTPUT_KINDS))}"
        )
    staging_path = Path(staging)
    staging_path.mkdir(parents=True, exist_ok=True)
    context = RunContext(
        runId=run_id,
        parameters=dict(parameters),
        inputs={name: Path(path) for name, path in sorted(inputs.items())},
        outputs=dict(outputs),
        staging=staging_path,
    )
    previous = _ACTIVE
    _ACTIVE = context
    try:
        yield context
    finally:
        _ACTIVE = previous


__all__ = [
    "OUTPUTS_DIRECTORY_NAME",
    "RunContext",
    "STAGED_INDEX_NAME",
    "STAGED_INDEX_VERSION",
    "active_context",
    "read_staged_index",
    "run_context",
]
