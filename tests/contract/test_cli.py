"""Command-line entry point checks (backlog task T001).

`melt validate` and `melt compile` arrive with backlog task T006. Until
then the entry point must still honor the exit code convention of
SPEC.md section 11.3: 0 on success, 1 for invalid input, and 2 for
operational failure.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run_melt(repo_root, *arguments):
    environment = dict(os.environ)
    python_path = [str(repo_root / "python"), str(repo_root)]
    if existing := environment.get("PYTHONPATH"):
        python_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return subprocess.run(
        [sys.executable, "-m", "melt", *arguments],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=environment,
    )


def test_version_reports_package_and_language(repo_root):
    completed = _run_melt(repo_root, "--version")
    assert completed.returncode == 0, completed.stderr
    assert "0.1.0" in completed.stdout
    assert "melt/v0.1" in completed.stdout


def test_unimplemented_command_fails_operationally(repo_root):
    completed = _run_melt(repo_root, "validate", "some-file.json")
    assert completed.returncode == 2, completed.stdout
    assert "T006" in completed.stderr


def test_no_command_fails_operationally(repo_root):
    completed = _run_melt(repo_root)
    assert completed.returncode == 2, completed.stdout
    assert "T006" in completed.stderr
