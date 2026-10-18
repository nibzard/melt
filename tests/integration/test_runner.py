"""Python runner and launch harness (T015, SPEC.md section 13.2).

The tests prove the process boundary of a run through real child
processes:

- the specification's own sales script runs in one fresh process and
  stages Parquet and PNG outputs with the hero numbers of section 18.2;
- the child works in the private staging directory, with the resolved
  interpreter, without model credentials, and with a non-interactive
  render backend;
- a script that fails, exits nonzero, misuses `ui`, or leaves a
  declared output unstaged produces its distinct exit code, and nothing
  publishes;
- the run logs are bounded with an explicit truncation marker;
- the service validates the staged files independently: capture and
  measurement accept the runner's Parquet and PNG bytes on the same
  path every import uses.

Everything runs against an isolated temporary root named with the
Looper resource prefix, never against the user's data directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

from melt.runner import (
    EXIT_COMPLETE,
    EXIT_INCOMPLETE,
    EXIT_OPERATIONAL,
    EXIT_SCRIPT_FAILED,
    build_manifest,
)
from runtime.execution import (
    MANIFEST_NAME,
    MeltLaunchError,
    launch_run,
    materialize_script,
    runner_environment,
    write_run_manifest,
)
from runtime.storage import PlatformAdapter, storage_worker_available

STORAGE_AVAILABLE, STORAGE_UNAVAILABLE_REASON = storage_worker_available()

requires_storage_worker = pytest.mark.skipif(
    not STORAGE_AVAILABLE, reason=STORAGE_UNAVAILABLE_REASON
)

PARQUET = "application/vnd.apache.parquet"
PNG = "image/png"
PYTHON = "text/x-python"

# The example script of SPEC.md section 13.2, copied word for word
# except for the figure close the example performs after staging.
SPEC_SALES_SCRIPT = (
    "import pandas as pd\n"
    "import matplotlib.pyplot as plt\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "monthly = selected.groupby('month', sort=True)['revenue'].sum()\n"
    "fig, ax = plt.subplots()\n"
    "monthly.plot(ax=ax)\n"
    "ax.set_ylabel('Revenue')\n"
    "ui.output('filtered', selected)\n"
    "ui.output('monthly', fig)\n"
    "plt.close(fig)\n"
)


def _load_builder():
    """Load scripts/build_fixture_artifacts.py as a module."""

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "build_fixture_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("build_fixture_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


class RunCase:
    """One prepared run: input artifact, script, staging, and logs."""

    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.run_id = run_id
        self.staging = root / "staging" / run_id
        self.logs = root / "logs" / run_id
        self.input_file = root / f"input-{run_id}.parquet"
        root.mkdir(parents=True, exist_ok=True)
        self.input_file.write_bytes(BUILDER.sales_parquet_bytes())
        self.staging.mkdir(parents=True)
        self.logs.mkdir(parents=True)

    def prepare(
        self,
        script: str,
        *,
        parameters: dict | None = None,
        outputs: dict | None = None,
    ) -> Path:
        """Materialize the script and manifest; return the manifest path."""

        script_path = materialize_script(self.staging, script.encode("utf-8"))
        manifest = build_manifest(
            run_id=self.run_id,
            workspace_id="sales-demo",
            action_id="analyze-sales",
            script_path=script_path,
            parameters={"minimum": 200} if parameters is None else parameters,
            inputs={"sales": str(self.input_file)},
            outputs=(
                {"filtered": "table", "monthly": "image"}
                if outputs is None
                else outputs
            ),
            staging_directory=self.staging,
        )
        _, manifest_path = write_run_manifest(self.staging, manifest)
        return manifest_path

    def launch(self, manifest_path: Path, **kwargs):
        return launch_run(manifest_path, log_directory=self.logs, **kwargs)


@pytest.fixture
def run_case(service_root: Path) -> RunCase:
    """One private run case under the Looper-prefixed root."""

    return RunCase(
        service_root / f"runner-case-{uuid.uuid4().hex[:12]}",
        f"run-{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------------------
# The specification example in one fresh process
# ---------------------------------------------------------------------------


def test_the_specification_script_completes(run_case: RunCase):
    manifest_path = run_case.prepare(SPEC_SALES_SCRIPT, parameters={"minimum": 0})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE
    assert result.complete is True
    # The staging directory holds the manifest, the read-only script,
    # the output files, and the staged-output report.
    assert (run_case.staging / MANIFEST_NAME).is_file()
    script_file = run_case.staging / "script.py"
    assert script_file.read_text(encoding="utf-8") == SPEC_SALES_SCRIPT
    assert not os.access(script_file, os.W_OK)
    assert result.staged["runId"] == run_case.run_id
    assert sorted(result.staged["outputs"]) == ["filtered", "monthly"]
    filtered = result.staged["outputs"]["filtered"]
    monthly = result.staged["outputs"]["monthly"]
    assert filtered["kind"] == "table"
    assert filtered["mediaType"] == PARQUET
    assert monthly["kind"] == "image"
    assert monthly["mediaType"] == PNG
    for entry in (filtered, monthly):
        payload = Path(entry["path"]).read_bytes()
        assert entry["byteLength"] == len(payload)
        assert entry["sha256"] == "sha256:" + __import__("hashlib").sha256(
            payload
        ).hexdigest()
    # The run logs exist and stay empty for a clean run.
    assert result.stdout_bytes == 0 and result.stderr_bytes == 0
    assert result.stdout_path.read_bytes() == b""
    assert result.stderr_path.read_bytes() == b""


def test_minimum_zero_yields_three_rows(run_case: RunCase):
    manifest_path = run_case.prepare(SPEC_SALES_SCRIPT, parameters={"minimum": 0})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE
    filtered = pd.read_parquet(result.staged["outputs"]["filtered"]["path"])
    assert len(filtered) == 3


def test_minimum_two_hundred_yields_the_hero_numbers(run_case: RunCase):
    manifest_path = run_case.prepare(SPEC_SALES_SCRIPT, parameters={"minimum": 200})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE
    filtered = pd.read_parquet(result.staged["outputs"]["filtered"]["path"])
    assert len(filtered) == 2
    monthly = filtered.groupby("month", sort=True)["revenue"].sum()
    assert monthly.tolist() == [300.0, 200.0]


# ---------------------------------------------------------------------------
# The fresh-process boundary
# ---------------------------------------------------------------------------


FACTS_SCRIPT = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "from melt import ui\n"
    "facts = {\n"
    "    'pid': os.getpid(),\n"
    "    'parent': os.getppid(),\n"
    "    'executable': sys.executable,\n"
    "    'cwd': os.getcwd(),\n"
    "    'argv': sys.argv,\n"
    "    'mplbackend': os.environ.get('MPLBACKEND'),\n"
    "    'credential_variables': sorted(\n"
    "        name for name in os.environ\n"
    "        if name.endswith('_API_KEY') or name.endswith('_AUTH_TOKEN')\n"
    "    ),\n"
    "    'pythonpath': os.environ.get('PYTHONPATH', ''),\n"
    "}\n"
    "Path('facts.json').write_text(json.dumps(facts), encoding='utf-8')\n"
    "ui.output('summary', 'done')\n"
)


def test_each_run_uses_one_fresh_credential_free_process(run_case: RunCase):
    manifest_path = run_case.prepare(
        FACTS_SCRIPT, outputs={"summary": "text"}
    )
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE, result.stderr_path.read_text()
    facts = json.loads((run_case.staging / "facts.json").read_text(encoding="utf-8"))
    # A fresh process: not this process, not its parent.
    assert facts["pid"] != os.getpid()
    assert facts["pid"] != os.getppid()
    # The resolved environment: the same interpreter the service runs.
    assert facts["executable"] == sys.executable
    # The working-directory policy: the private staging directory.
    assert facts["cwd"] == str(run_case.staging)
    # The script runs as the main module with its own path as argv.
    assert facts["argv"] == [str(run_case.staging / "script.py")]
    # Model credentials never reach project code (SPEC.md section 15.1).
    assert facts["credential_variables"] == []
    # Runs render no windows; the launcher pins the non-interactive
    # backend so a headless Figure stays savable.
    assert facts["mplbackend"] == "Agg"


def test_runner_environment_removes_credentials_and_pins_the_backend(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-of-the-service")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "also-a-secret")
    env = runner_environment()
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert env["MPLBACKEND"] == "Agg"


def test_no_package_installation_happens_at_invocation(run_case: RunCase):
    """The launch path never runs an installer.

    The child imports `melt` through the resolved interpreter with no
    install step: it reports the loaded package location, which points
    at the same tree the service runs. A failed import would exit with
    the operational code instead.
    """

    script = (
        "import json, melt\n"
        "from pathlib import Path\n"
        "from melt import ui\n"
        "Path('package.json').write_text("
        "json.dumps({'melt': melt.__file__}), encoding='utf-8')\n"
        "ui.output('summary', 'imported')\n"
    )
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE
    package = json.loads(
        (run_case.staging / "package.json").read_text(encoding="utf-8")
    )
    assert package["melt"] == str(Path(melt_init()).resolve())


def melt_init() -> str:
    import melt

    return melt.__file__


# ---------------------------------------------------------------------------
# Failure classes and exit codes
# ---------------------------------------------------------------------------


def test_a_failing_script_exits_with_the_script_code(run_case: RunCase):
    script = (
        "from melt import ui\n"
        "raise RuntimeError('the analysis exploded')\n"
    )
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_SCRIPT_FAILED
    stderr = result.stderr_path.read_text(encoding="utf-8")
    assert "the analysis exploded" in stderr
    assert "RuntimeError" in stderr
    # Nothing staged, nothing reported, nothing publishable.
    assert result.staged is None
    assert not (run_case.staging / "outputs").exists()


def test_a_nonzero_sys_exit_is_a_script_failure(run_case: RunCase):
    script = "import sys\nsys.exit('stop here')\n"
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_SCRIPT_FAILED
    assert "stop here" in result.stderr_path.read_text(encoding="utf-8")


def test_sdk_misuse_fails_the_script(run_case: RunCase):
    script = (
        "from melt import ui\n"
        "ui.output('undeclared', 'no such output')\n"
    )
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_SCRIPT_FAILED
    stderr = result.stderr_path.read_text(encoding="utf-8")
    assert "not declared" in stderr


def test_a_duplicate_output_fails_the_script(run_case: RunCase):
    script = (
        "from melt import ui\n"
        "ui.output('summary', 'first')\n"
        "ui.output('summary', 'second')\n"
    )
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_SCRIPT_FAILED
    assert "staged already" in result.stderr_path.read_text(encoding="utf-8")


def test_an_unstaged_declared_output_is_incomplete(run_case: RunCase):
    script = "from melt import ui\nui.output('kept', 'staged')\n"
    manifest_path = run_case.prepare(
        script, outputs={"kept": "text", "dropped": "text"}
    )
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_INCOMPLETE
    stderr = result.stderr_path.read_text(encoding="utf-8")
    assert "missing: dropped" in stderr
    # The staged file itself exists, but no report claims completeness.
    assert (run_case.staging / "outputs" / "kept.txt").is_file()
    assert result.staged is None


def test_a_zero_exit_without_any_output_is_incomplete(run_case: RunCase):
    script = "from melt import ui\nassert ui.params['minimum'] == 200\n"
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_INCOMPLETE
    assert "summary" in result.stderr_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The runner command line and the launch harness guards
# ---------------------------------------------------------------------------


def test_the_runner_command_line_reports_operational_failures():
    no_arguments = subprocess.run(
        [sys.executable, "-m", "melt.runner"],
        capture_output=True,
        text=True,
        env=runner_environment(),
    )
    assert no_arguments.returncode == EXIT_OPERATIONAL
    assert "usage" in no_arguments.stderr

    missing = subprocess.run(
        [sys.executable, "-m", "melt.runner", "/no/such/manifest.json"],
        capture_output=True,
        text=True,
        env=runner_environment(),
    )
    assert missing.returncode == EXIT_OPERATIONAL
    assert "unreadable" in missing.stderr


def test_a_broken_manifest_reports_its_diagnostics(run_case: RunCase):
    broken = run_case.staging / "broken.json"
    broken.write_text('{"manifestVersion": 99}', encoding="utf-8")
    answer = subprocess.run(
        [sys.executable, "-m", "melt.runner", str(broken)],
        capture_output=True,
        text=True,
        env=runner_environment(),
    )
    assert answer.returncode == EXIT_OPERATIONAL
    assert "E_VERSION at /manifestVersion" in answer.stderr


def test_the_launcher_refuses_an_unreadable_manifest(run_case: RunCase):
    with pytest.raises(MeltLaunchError, match="unreadable"):
        launch_run(
            run_case.staging / "no-manifest-here.json",
            log_directory=run_case.logs,
        )


def test_the_launcher_refuses_a_missing_staging_directory(
    run_case: RunCase, tmp_path: Path,
):
    script_path = materialize_script(run_case.staging, b"pass\n")
    manifest = build_manifest(
        run_id=run_case.run_id,
        workspace_id="sales-demo",
        action_id="analyze-sales",
        script_path=script_path,
        parameters={},
        inputs={},
        outputs={},
        staging_directory=tmp_path / "a-staging-tree-that-does-not-exist",
    )
    outside = tmp_path / "manifest.json"
    outside.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(MeltLaunchError, match="does not exist"):
        launch_run(outside, log_directory=run_case.logs)


def test_the_written_manifest_matches_the_runner_contract(run_case: RunCase):
    """The manifest the launcher writes is the one the runner reads."""

    script_path = materialize_script(run_case.staging, b"pass\n")
    built = build_manifest(
        run_id=run_case.run_id,
        workspace_id="sales-demo",
        action_id="analyze-sales",
        script_path=script_path,
        parameters={"minimum": 0},
        inputs={"sales": str(run_case.input_file)},
        outputs={"filtered": "table"},
        staging_directory=run_case.staging,
    )
    parsed, path = write_run_manifest(run_case.staging, built)
    assert path == run_case.staging / MANIFEST_NAME
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread == json.loads(json.dumps(built.model_dump(mode="json")))
    # A second write replaces the file idempotently and keeps one copy.
    write_run_manifest(run_case.staging, built)
    assert path.read_text(encoding="utf-8") == json.dumps(
        built.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    ) + "\n"
    # A mapping shape validates identically.
    from melt.runner import parse_manifest

    assert parse_manifest(reread) == parsed


# ---------------------------------------------------------------------------
# Bounded run logs
# ---------------------------------------------------------------------------


def test_run_logs_are_bounded_with_an_explicit_marker(run_case: RunCase):
    script = (
        "import sys\n"
        "sys.stdout.write('x' * 5000)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('y' * 3000)\n"
        "sys.stderr.flush()\n"
        "from melt import ui\n"
        "ui.output('summary', 'done')\n"
    )
    manifest_path = run_case.prepare(script, outputs={"summary": "text"})
    result = run_case.launch(manifest_path, log_limit=1024)
    assert result.exit_code == EXIT_COMPLETE
    assert result.stdout_bytes == 1024
    assert result.stdout_dropped == 5000 - 1024
    assert result.stderr_bytes == 1024
    assert result.stderr_dropped == 3000 - 1024
    stdout = result.stdout_path.read_text(encoding="utf-8")
    assert stdout.startswith("x" * 1024)
    assert (
        f"[melt runner] truncated: {5000 - 1024} bytes beyond the"
        " 1024-byte run log limit" in stdout
    )
    stderr = result.stderr_path.read_text(encoding="utf-8")
    assert stderr.startswith("y" * 1024)
    assert "truncated" in stderr


# ---------------------------------------------------------------------------
# The service validates staged files independently
# ---------------------------------------------------------------------------


@pytest.fixture
def service(service_adapter: PlatformAdapter):
    from runtime.workspace import WorkspaceService

    opened = WorkspaceService.open(service_adapter, "sales-demo")
    try:
        created = opened.create_workspace(
            {
                "language": "melt/v0.1",
                "workspaceId": "sales-demo",
                "title": "Sales analysis",
                "sources": {},
                "parameters": {},
                "actions": {},
                "projections": {},
                "bindings": {},
                "layout": {"root": None, "hidden": []},
            },
            "01-create",
        )
        assert created.ok, created.errors
        yield opened
    finally:
        opened.close()


@requires_storage_worker
def test_the_service_validates_staged_files_independently(
    service, run_case: RunCase
):
    """Staged bytes pass the same verification every import uses.

    The runner's report is not proof. The service captures the staged
    Parquet and PNG bytes, and the trusted measurement path accepts
    them with the schemas the run actually produced
    (SPEC.md sections 5.1 and 13.4).
    """

    from runtime.workspace.importer import register_artifact

    manifest_path = run_case.prepare(SPEC_SALES_SCRIPT, parameters={"minimum": 0})
    result = run_case.launch(manifest_path)
    assert result.exit_code == EXIT_COMPLETE, result.stderr_path.read_text()

    parquet_bytes = Path(
        result.staged["outputs"]["filtered"]["path"]
    ).read_bytes()
    png_bytes = Path(result.staged["outputs"]["monthly"]["path"]).read_bytes()

    captured_table = service.capture_artifact_token(parquet_bytes, PARQUET)
    measured_table = register_artifact(service.store.artifacts, captured_table)
    assert measured_table.kind == "table"
    assert [column["id"] for column in measured_table.schema["columns"]] == [
        "month",
        "revenue",
    ]
    assert measured_table.byteLength == len(parquet_bytes)

    captured_image = service.capture_artifact_token(png_bytes, PNG)
    measured_image = register_artifact(service.store.artifacts, captured_image)
    assert measured_image.kind == "image"
    assert measured_image.schema["width"] > 0
    assert measured_image.schema["height"] > 0
