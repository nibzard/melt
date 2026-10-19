"""Compile and exercise the first native host when macOS tools exist."""
from pathlib import Path
import subprocess
import sys

import pytest

@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit host checks require macOS")
def test_native_host_compiles_and_preserves_pane_controllers():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([str(root / "native/check.sh")], cwd=root,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
