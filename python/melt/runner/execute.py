"""Fresh-process execution of one run manifest (SPEC.md section 13.2).

The service spawns one fresh process per run:

```bash
python -m melt.runner <manifest-file>
```

The process reads its manifest, binds `ui` to the run, executes the
materialized script once, and reports which outputs it staged. It
installs no packages (SPEC.md section 13.1); the resolved environment
already holds every dependency.

Exit codes separate the three failure classes the service cares about:

=====  ==================================================================
Code   Meaning
=====  ==================================================================
0      The script completed and staged every declared output.
1      The script failed: it raised, exited nonzero, or misused `ui`.
2      Operational failure: the manifest, the script file, or the
       staging directory was unreadable or invalid.
3      The script completed without staging every declared output.
=====  ==================================================================

A zero exit status alone does not make a run publishable. The service
validates the staged files independently (SPEC.md sections 7.3, 13.4).
"""

from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path

from melt.runner.manifest import RunManifest, parse_manifest
from melt.schema.diagnostics import MeltSchemaError
from melt.sdk import run_context

EXIT_COMPLETE = 0
EXIT_SCRIPT_FAILED = 1
EXIT_OPERATIONAL = 2
EXIT_INCOMPLETE = 3

USAGE = "usage: python -m melt.runner <manifest-file>"


def load_manifest(path: "str | Path") -> RunManifest:
    """Read and validate one manifest file.

    Malformed JSON and schema failures raise `MeltManifestError` with
    sorted diagnostics; a missing file raises `OSError`. Both are
    operational failures of the run, never failures of the script.
    """

    return parse_manifest(Path(path).read_bytes())


def _fail_operational(message: str) -> int:
    print(f"melt-runner: {message}", file=sys.stderr)
    return EXIT_OPERATIONAL


def execute_manifest(manifest: RunManifest) -> int:
    """Execute one validated manifest and return the exit code."""

    script = Path(manifest.scriptPath)
    if not script.is_file():
        return _fail_operational(
            f"the manifest names no readable script at {script}"
        )
    staging = Path(manifest.stagingDirectory)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return _fail_operational(
            f"the staging directory {staging} is unusable: {error}"
        )

    # A run renders no windows. The Agg backend keeps a headless Figure
    # savable; the launcher sets the same value, and this guard covers a
    # runner that started by hand.
    os.environ.setdefault("MPLBACKEND", "Agg")

    previous_argv = sys.argv
    sys.argv = [str(script)]
    try:
        with run_context(
            run_id=manifest.runId,
            parameters=manifest.parameters,
            inputs=manifest.inputs,
            outputs=manifest.outputs,
            staging=staging,
        ) as context:
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exit_request:
                code = exit_request.code
                if code not in (None, 0):
                    print(
                        f"melt-runner: the script exited with status"
                        f" {code}",
                        file=sys.stderr,
                    )
                    return EXIT_SCRIPT_FAILED
            except BaseException:
                traceback.print_exc()
                return EXIT_SCRIPT_FAILED
            missing = context.missing_outputs()
            if missing:
                print(
                    "melt-runner: the script completed without staging"
                    " every declared output; missing: "
                    + ", ".join(missing),
                    file=sys.stderr,
                )
                return EXIT_INCOMPLETE
            try:
                context.write_index()
            except OSError as error:
                return _fail_operational(
                    f"the staged-output report is unwritable: {error}"
                )
            return EXIT_COMPLETE
    finally:
        sys.argv = previous_argv


def main(argv: "list[str] | None" = None) -> int:
    """Run the command line and return the process exit code."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_OPERATIONAL
    try:
        manifest = load_manifest(arguments[0])
    except MeltSchemaError as error:
        for diagnostic in error.diagnostics:
            print(
                f"melt-runner: {diagnostic.code} at"
                f" {diagnostic.path}: {diagnostic.message}",
                file=sys.stderr,
            )
        return EXIT_OPERATIONAL
    except OSError as error:
        return _fail_operational(f"the manifest file is unreadable: {error}")
    return execute_manifest(manifest)


__all__ = [
    "EXIT_COMPLETE",
    "EXIT_INCOMPLETE",
    "EXIT_OPERATIONAL",
    "EXIT_SCRIPT_FAILED",
    "USAGE",
    "execute_manifest",
    "load_manifest",
    "main",
]
