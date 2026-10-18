"""The launch harness of one project run (SPEC.md sections 13.1 and 13.2).

The launcher owns the process boundary of a run. It writes the
versioned manifest and the materialized script into the private staging
directory of the run, then spawns one fresh process:

```bash
python -m melt.runner <staging>/manifest.json
```

The harness keeps three boundaries (SPEC.md sections 13.1, 13.2, 15.1):

- Fresh process, no package installation. The child runs the resolved
  interpreter of the registration's environment fingerprint. Nothing
  installs dependencies at invocation; environment repair is a separate
  operation.
- Credential-free child environment. The child environment is built
  through ``project_process_environment`` and guarded with
  ``assert_no_model_credentials``, so no model credential reaches
  project code by inheritance.
- Private working directory. The child works inside the run's staging
  directory, and its stdout and stderr land in bounded log files under
  ``logs/<run-id>/``. Project stdout is never a control channel
  (SPEC.md section 12.1).

Scheduling, admission, and publication belong to the workspace service
(backlog tasks T016 and T017). This module only proves the launch loop
the service drives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from melt.runner.execute import EXIT_COMPLETE
from melt.runner.manifest import (
    MeltManifestError,
    RunManifest,
    parse_manifest,
)
from melt.schema.limits import DEFAULT_LIMITS
from melt.sdk import read_staged_index
from runtime.execution.environment import (
    assert_no_model_credentials,
    project_process_environment,
)
from runtime.storage.artifacts import fsync_directory

RUNNER_MODULE = "melt.runner"
"""The fresh process runs this module with one manifest argument."""

MANIFEST_NAME = "manifest.json"
"""The manifest file name inside the run's staging directory."""

SCRIPT_NAME = "script.py"
"""The materialized script file name inside the staging directory."""

STDOUT_LOG_NAME = "stdout.log"
STDERR_LOG_NAME = "stderr.log"

_COPY_CHUNK_BYTES = 64 * 1024


class MeltLaunchError(RuntimeError):
    """Raised when a run cannot be launched."""


@dataclass(frozen=True)
class RunLaunchResult:
    """The outcome of one launched run process.

    `staged` repeats the runner's staged-output report when the child
    wrote one. It is a report, never proof: the service rereads every
    staged file and validates it independently during publication
    (SPEC.md section 13.4).
    """

    manifest: RunManifest
    manifest_path: Path
    exit_code: int
    staged: "dict | None"
    stdout_path: Path
    stderr_path: Path
    stdout_bytes: int
    stderr_bytes: int
    stdout_dropped: int
    stderr_dropped: int

    @property
    def complete(self) -> bool:
        """True when the child staged every declared output."""

        return self.exit_code == EXIT_COMPLETE


def write_run_manifest(
    staging: Path, manifest: "RunManifest | Mapping"
) -> "tuple[RunManifest, Path]":
    """Validate one manifest and write it into the staging directory.

    Accepts a built `RunManifest` or its mapping shape. The file lands
    through a temporary name and one rename, so a partial write never
    sits under the name the runner reads.
    """

    parsed = (
        manifest
        if isinstance(manifest, RunManifest)
        else parse_manifest(dict(manifest))
    )
    directory = Path(staging)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / MANIFEST_NAME
    temporary = directory / f".{MANIFEST_NAME}.tmp-{uuid.uuid4().hex}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(_manifest_bytes(parsed))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    fsync_directory(directory)
    return parsed, final


def _manifest_bytes(manifest: RunManifest) -> bytes:
    """Return one deterministic JSON encoding of the manifest."""

    text = json.dumps(
        manifest.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        ensure_ascii=False,
    )
    return text.encode("utf-8") + b"\n"


def materialize_script(staging: Path, script: bytes) -> Path:
    """Write the captured script bytes into the staging directory.

    The file is read-only, because the run reads its script and never
    rewrites it. The registration holds the authoritative bytes; this
    copy exists so the child executes exactly what was captured.
    """

    directory = Path(staging)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / SCRIPT_NAME
    temporary = directory / f".{SCRIPT_NAME}.tmp-{uuid.uuid4().hex}"
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o444
        )
        try:
            view = memoryview(script)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, final)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    fsync_directory(directory)
    return final


def _repository_roots() -> "list[Path]":
    """Return the source roots that hold `melt` and `runtime`.

    The resolved environment of v0.1 is the interpreter the service
    itself runs in. When that interpreter loads the packages from the
    repository layout instead of an installed wheel, the child needs
    the same roots on its path. Prepending them changes nothing when
    the packages are installed, because the roots are the same code.
    """

    roots: list[Path] = []
    for module_name in ("melt", "runtime"):
        module = sys.modules.get(module_name)
        if module is None or getattr(module, "__file__", None) is None:
            continue
        candidate = Path(module.__file__).resolve().parent.parent
        if candidate.name == "python":
            candidate = candidate.parent
        if (candidate / "pyproject.toml").is_file():
            roots.append(candidate)
    seen: set[Path] = set()
    unique = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def runner_environment(
    environ: "Mapping[str, str] | None" = None,
) -> dict[str, str]:
    """Return the environment one runner process receives.

    The environment starts credential-free (SPEC.md section 15.1), pins
    a non-interactive matplotlib backend, and extends the path with the
    roots that hold the melt packages when they run from source.
    """

    env = project_process_environment(environ)
    assert_no_model_credentials(env)
    env["MPLBACKEND"] = "Agg"
    path_parts = _repository_roots()
    existing = env.get("PYTHONPATH")
    if existing:
        path_parts.extend(
            Path(part)
            for part in existing.split(os.pathsep)
            if part
        )
    merged: list[str] = []
    merged_names: set[str] = set()
    for part in path_parts:
        name = str(part)
        if name not in merged_names:
            merged_names.add(name)
            merged.append(name)
    if merged:
        env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def _copy_bounded(source, target, limit: int) -> "tuple[int, int]":
    """Copy one stream into a file up to `limit` bytes.

    Returns the written and the dropped byte counts. Everything past
    the limit is counted and dropped, so the log cannot grow past the
    service bound while the pipe still drains.
    """

    written = 0
    dropped = 0
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        room = limit - written
        if room > 0:
            payload = chunk[:room]
            target.write(payload)
            written += len(payload)
            dropped += len(chunk) - len(payload)
        else:
            dropped += len(chunk)
    return written, dropped


def _truncation_note(dropped: int, limit: int) -> bytes:
    """Return the explicit truncation marker of one bounded log."""

    message = (
        f"\n[melt runner] truncated: {dropped} bytes beyond the"
        f" {limit}-byte run log limit\n"
    )
    return message.encode("utf-8")


def launch_run(
    manifest: "Path | RunManifest",
    *,
    log_directory: Path,
    log_limit: "int | None" = None,
) -> RunLaunchResult:
    """Launch one run process and wait for it.

    The staging directory of the manifest becomes the working directory
    of the child. `log_directory` receives `stdout.log` and `stderr.log`,
    each bounded by `log_limit` bytes with an explicit truncation
    marker (SPEC.md section 15.2).
    """

    if isinstance(manifest, RunManifest):
        parsed = manifest
        manifest_path = Path(
            Path(manifest.stagingDirectory) / MANIFEST_NAME
        )
        if not manifest_path.is_file():
            raise MeltLaunchError(
                f"no manifest file at {manifest_path}; write it with"
                " write_run_manifest before launching"
            )
    else:
        manifest_path = Path(manifest).resolve()
        try:
            parsed = parse_manifest(manifest_path.read_bytes())
        except (OSError, MeltManifestError) as error:
            raise MeltLaunchError(
                f"the manifest at {manifest_path} is unreadable: {error}"
            ) from error
    staging = Path(parsed.stagingDirectory)
    if not staging.is_dir():
        raise MeltLaunchError(
            f"the staging directory {staging} does not exist; the"
            " service creates it before launch"
        )
    logs = Path(log_directory)
    logs.mkdir(parents=True, exist_ok=True)
    stdout_path = logs / STDOUT_LOG_NAME
    stderr_path = logs / STDERR_LOG_NAME
    limit = (
        DEFAULT_LIMITS.run_log_bytes_per_stream
        if log_limit is None
        else log_limit
    )

    counts: dict[str, "tuple[int, int]"] = {}
    with open(stdout_path, "wb") as stdout_log, open(
        stderr_path, "wb"
    ) as stderr_log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                RUNNER_MODULE,
                str(manifest_path),
            ],
            cwd=staging,
            env=runner_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threads: list[threading.Thread] = []
        for name, source, target in (
            ("stdout", process.stdout, stdout_log),
            ("stderr", process.stderr, stderr_log),
        ):
            thread = threading.Thread(
                target=_drain,
                args=(name, source, target, limit, counts),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        exit_code = process.wait()
        for thread in threads:
            thread.join()
        for name, target in (
            ("stdout", stdout_log),
            ("stderr", stderr_log),
        ):
            dropped = counts[name][1]
            if dropped:
                target.write(_truncation_note(dropped, limit))

    stdout_bytes, stdout_dropped = counts["stdout"]
    stderr_bytes, stderr_dropped = counts["stderr"]
    return RunLaunchResult(
        manifest=parsed,
        manifest_path=manifest_path,
        exit_code=exit_code,
        staged=read_staged_index(staging),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stdout_dropped=stdout_dropped,
        stderr_dropped=stderr_dropped,
    )


def _drain(
    name: str, source, target, limit: int, counts: dict
) -> None:
    """Drain one pipe into its log file and record the byte counts."""

    counts[name] = _copy_bounded(source, target, limit)


__all__ = [
    "MANIFEST_NAME",
    "MeltLaunchError",
    "SCRIPT_NAME",
    "RunLaunchResult",
    "STDERR_LOG_NAME",
    "STDOUT_LOG_NAME",
    "launch_run",
    "materialize_script",
    "runner_environment",
    "write_run_manifest",
]
