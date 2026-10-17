"""Pi Durable storage adapter probe (SPEC.md section 14, backlog task T002).

The durability contract of the pinned adapter is proven by a Node probe
under ``runtime/storage/pi_adapter`` because the adapter is a Node library
over ``node:sqlite``. These tests run the probe and gate on its verdict, so
``make check`` fails when the pinned adapter stops providing atomic updates,
committed observation, or the verified ``synchronous=FULL`` connection.

Findings and the tested filesystem and crash model are recorded in
``runtime/storage/pi_adapter/README.md``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parents[2] / "runtime" / "storage" / "pi_adapter"
PROBE = PROBE_DIR / "probe.mjs"
PINNED_ADAPTER = "1.3.0"
MINIMUM_NODE_MAJOR = 22
PROBE_ROOT_SUFFIX = "-pi-durable-probe"


def _node() -> str | None:
    """Return the node executable, or None when the prerequisite is missing."""
    return shutil.which("node")


def _adapter_installed() -> bool:
    package = PROBE_DIR / "node_modules" / "@earendil-works" / "pi-durable" / "package.json"
    if not package.is_file():
        return False
    try:
        return json.loads(package.read_text(encoding="utf-8"))["version"] == PINNED_ADAPTER
    except (OSError, ValueError, KeyError):
        return False


@pytest.fixture(scope="module")
def probe_root() -> Path:
    """A private temporary root, named with the Looper resource prefix."""
    prefix = os.environ.get("LOOPER_RESOURCE_PREFIX", "melt")
    root = Path(tempfile.gettempdir()) / f"{prefix}{PROBE_ROOT_SUFFIX}"
    root.mkdir(parents=True, exist_ok=True)
    _register_cleanup(root, prefix)
    return root


def _register_cleanup(root: Path, prefix: str) -> None:
    """Register idempotent removal of the private root with Looper cleanup.

    The guard removes the directory only when its path still carries this
    run's resource prefix, so cleanup never removes a directory it does not
    own.
    """
    cleanup_file = os.environ.get("LOOPER_RESOURCE_CLEANUP_FILE")
    if not cleanup_file:
        return
    path = Path(cleanup_file)
    marker = f"# probe-root: {root}"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if marker in existing:
        return
    header = existing or (
        "#!/usr/bin/env bash\n"
        "# Looper resource cleanup. Registered resources only; idempotent.\n"
        "set -u\n"
    )
    quoted = shlex.quote(str(root))
    path.write_text(
        header
        + f"\n{marker}\n"
        + f'if [ -d {quoted} ]; then\n'
        + f'  case {quoted} in\n'
        + f'    *{shlex.quote(prefix)}{PROBE_ROOT_SUFFIX}) rm -rf -- {quoted} ;;\n'
        + "  esac\n"
        + "fi\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def verdict(probe_root: Path) -> dict:
    """Run the probe once and return its verdict document."""
    node = _node()
    if node is None:
        pytest.skip(f"node >= {MINIMUM_NODE_MAJOR} is required to run the Pi Durable probe")
    if not _adapter_installed():
        pytest.skip(
            f"pinned Pi Durable adapter {PINNED_ADAPTER} is not installed; "
            "run `make probe-setup` (npm ci in runtime/storage/pi_adapter)"
        )
    run_root = probe_root / f"run-{uuid.uuid4().hex[:8]}"
    completed = subprocess.run(
        [node, str(PROBE), "--root", str(run_root), "--json"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        document = json.loads(completed.stdout)
    except ValueError as error:
        raise AssertionError(
            f"probe printed no verdict (exit {completed.returncode}): "
            f"{completed.stderr[-2000:]}"
        ) from error
    document["exitCode"] = completed.returncode
    document["stderr"] = completed.stderr
    return document


def test_probe_passes(verdict: dict) -> None:
    assert verdict["exitCode"] == 0
    assert verdict["verdict"] == "pass", _failures(verdict)
    assert verdict["summary"]["failed"] == 0
    assert verdict["summary"]["passed"] >= 20


def test_pinned_adapter_is_the_one_probed(verdict: dict) -> None:
    assert verdict["pinnedAdapter"] == PINNED_ADAPTER
    assert verdict["resolvedAdapter"] == PINNED_ADAPTER


def test_owning_connection_runs_synchronous_full_in_wal(verdict: dict) -> None:
    wrapper = _detail(verdict, "config", "wrapper applies and verifies synchronous FULL")
    assert wrapper is not None, "the wrapper verification case is missing"
    assert wrapper["synchronous"] == 2, "owning connection is not synchronous=FULL"
    assert wrapper["synchronousDefault"] == 1, "adapter default changed; update the probe and README"
    assert wrapper["journalMode"] == "wal"
    fresh = _detail(verdict, "config", "a fresh connection starts at the adapter default again")
    assert fresh == {"synchronous": 1}


def test_full_is_evidenced_by_fsyncs(verdict: dict) -> None:
    fsync = _detail(verdict, "fsync", "FULL fsyncs per commit, the shipped default does not")
    if fsync is None:
        pytest.skip("strace unavailable; fsync evidence not collected on this machine")
    assert fsync["fsyncsFull"] - fsync["fsyncsNormal"] >= fsync["commits"]


def test_atomic_updates(verdict: dict) -> None:
    names = {
        result["name"]
        for result in verdict["results"]
        if result["suite"] == "atomic" and result["status"] == "pass"
    }
    assert {
        "one commit spans every record kind",
        "a commit that fails midway stores nothing",
        "unknown document change is rejected without writes",
        "committed record IDs are never reused",
        "one live incarnation per document address",
    } <= names


def test_committed_observation_after_sigkill(verdict: dict) -> None:
    crash = [
        result
        for result in verdict["results"]
        if result["suite"] == "crash" and result["status"] == "pass"
    ]
    assert len(crash) >= 5, "the SIGKILL rounds did not run"
    killed_rounds = 0
    for result in crash:
        detail = result["detail"]
        expected_acks = (detail.get("stop") or {}).get("acks")
        if expected_acks is not None:
            # An ack-driven round waits for its acks before the kill, so
            # they all arrived. The first ack is the root conversation
            # commit, which carries no entry; entries begin with the
            # second ack.
            assert detail["acked"] >= expected_acks
            assert detail["committed"] >= expected_acks - 1
        # A timer round kills at a fixed time instead, so it can kill the
        # worker before any commit resolved and guarantees no minimum
        # count; the probe case itself verifies that every commit that
        # did land survived whole. Unacknowledged commits may exist, so
        # the committed entries can exceed the acked count minus the
        # root, never fall below it.
        assert detail["committed"] >= detail["acked"] - 1
        killed = detail.get("killed")
        if killed is None:
            continue
        assert killed["signal"] == "SIGKILL"
        killed_rounds += 1
    assert killed_rounds >= 4, "the probe stopped killing its worker"
    artifacts = next(
        (
            result["detail"]
            for result in crash
            if result["name"].startswith("acknowledged artifact references")
        ),
        None,
    )
    assert artifacts is not None
    assert artifacts["artifactsVerified"] >= 1


def test_upstream_conformance_passes_through_the_wrapper(verdict: dict) -> None:
    conformance = _detail(
        verdict, "conformance", "upstream storage conformance over the durable wrapper"
    )
    assert conformance is not None
    assert conformance["cases"] >= 20
    assert conformance["passed"] == conformance["cases"]


def test_adapter_gaps_are_recorded(verdict: dict) -> None:
    kinds = {finding["kind"] for finding in verdict["findings"]}
    # F1 is the SPEC.md section 14 gap this task exists to resolve; the
    # wrapper closes it, and the probe must keep reporting it so a future
    # adapter release cannot change durability behavior silently.
    assert "adapter-default-synchronous-normal" in kinds
    assert "no-single-writer-fence" in kinds


def test_environment_and_crash_model_are_recorded(verdict: dict) -> None:
    environment = verdict["environment"]
    assert environment["platform"] == "linux"
    assert environment["node"].startswith("v")
    mount = environment["filesystem"]["mount"]
    kind = environment["filesystem"]["type"]
    assert "ext4" in mount or kind != "unknown", "the filesystem was not identified"
    assert "SIGKILL" in environment["crashModel"]
    assert "synchronous=FULL" in environment["crashModel"]


def _failures(verdict: dict) -> str:
    failed = [
        f'{result["suite"]}/{result["name"]}: {result.get("error")}'
        for result in verdict["results"]
        if result["status"] == "fail"
    ]
    return "; ".join(failed) or verdict.get("stderr", "")


def _detail(verdict: dict, suite: str, name: str) -> dict | None:
    for result in verdict["results"]:
        if result["suite"] == suite and result["name"] == name:
            if result["status"] == "pass":
                return result["detail"]
            return None
    return None
