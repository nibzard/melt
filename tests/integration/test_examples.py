"""Run the public example commands and check their exported results."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from runtime.storage import storage_worker_available

AVAILABLE, REASON = storage_worker_available()
pytestmark = pytest.mark.skipif(not AVAILABLE, reason=REASON)
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("example,arguments", [
    ("01-saved-views", []),
    ("02-sales-analysis", ["--minimum", "250"]),
    ("03-linked-views", ["--key", "S-42"]),
])
def test_runnable_examples(tmp_path, example, arguments):
    output = tmp_path / "results with spaces"
    command = [sys.executable, str(REPO / "examples" / example / "run.py"),
               "--output", str(output), *arguments]
    completed = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert f"Saved results: {output}" in completed.stdout
    assert (output / "commands").is_dir()
    if example == "01-saved-views":
        with (output / "expenses.csv").open() as handle:
            assert len(list(csv.DictReader(handle))) == 3
        assert "201.75" in (output / "notes.md").read_text()
    elif example == "02-sales-analysis":
        with (output / "after.csv").open() as handle:
            assert list(csv.DictReader(handle)) == [{"month": "2026-01", "revenue": "300.0"}]
        for name in ("before.png", "after.png"):
            assert (output / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    else:
        selection = json.loads((output / "selection.json").read_text())
        for projection in ("inspector", "risk-inspector"):
            assert selection["observers"][projection]["record"]["cells"]["supplier_id"] == "S-42"
        assert "<svg" in (output / "network.svg").read_text()
    # A repeated command must preserve the workspace and exported files.
    saved = {path: path.read_bytes() for path in output.glob("commands/*.json")}
    repeated = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120)
    assert repeated.returncode == 1
    assert "is not empty" in repeated.stderr
    assert saved == {path: path.read_bytes() for path in saved}
