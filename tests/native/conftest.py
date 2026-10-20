"""Share one real AppKit run across the native regression assertions."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope="session")
def native_acceptance():
    if sys.platform != "darwin":
        pytest.skip("AppKit and Ghostty integration requires macOS")
    root = Path(__file__).resolve().parents[2]
    evidence = root / "build/native/artifacts/native-acceptance.json"
    evidence.unlink(missing_ok=True)
    result = subprocess.run([str(root / "native/check.sh")], cwd=root,
                            capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(evidence.read_text())
