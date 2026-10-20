#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test_bundle="${MELT_TEST_BUNDLE:-$PWD/build/native/Melt.app}"
if [[ -z "${MELT_TEST_BUNDLE:-}" ]]; then ./native/build.sh; fi
source native/ghostty-env.sh
xcrun swiftc -O -parse-as-library -swift-version 5 -D MELT_NATIVE_TESTS \
    -target "$(uname -m)-apple-macosx13.0" -framework AppKit \
    "${GHOSTTY_FLAGS[@]}" native/Sources/*.swift native/Tests/HostChecks.swift -o build/native/HostChecks
xcrun swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx13.0" \
    -framework AppKit native/Sources/Bridge.swift native/Tests/LaunchChecks.swift -o build/native/LaunchChecks
check_root="$(mktemp -d "${TMPDIR:-/tmp}/melt-native-check.XXXXXX")"
trap 'rm -rf "$check_root"' EXIT
uv run --locked python3 examples/04-cafe/run.py --output "$check_root/cafe"
uv run --locked python3 examples/03-linked-views/run.py --output "$check_root/linked"
uv run --locked python3 native/Tests/prepare_sales.py "$check_root/sales"
uv run --locked python3 - "$check_root/linked" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
(root / 'Linked.meltapp').write_text(json.dumps({'format': 'melt-launcher/v1',
    'root': str(root / 'data'), 'workspaceId': 'linked-views'}))
PY
build/native/HostChecks "$check_root/cafe/Little Hours.meltapp" \
    "$test_bundle/Contents/Resources/Runtime.json" "${MELT_NATIVE_ARTIFACTS:-build/native/artifacts}" "$check_root/linked/Linked.meltapp" "$check_root/sales/Sales.meltapp"
build/native/LaunchChecks "$test_bundle" "$check_root/cafe/Desktop/Little Hours.meltapp"
uv run --locked python3 -m runtime.harness --root "$check_root/cafe/data" \
    --workspace little-hours runs > "$check_root/runs.json"
uv run --locked python3 - "$check_root/runs.json" <<'PY'
import json
import sys
runs = json.load(open(sys.argv[1]))["runs"]
assert len(runs) == 3, "Restoration must not run the calculation again"
assert all(run["state"] == "succeeded" for run in runs)
print("No project execution occurred while reopening the native app.")
PY
uv run --locked python3 -m runtime.harness --root "$check_root/linked/data" \
    --workspace linked-views runs > "$check_root/linked-runs.json"
uv run --locked python3 - "$check_root/linked-runs.json" <<'PY'
import json
import sys
assert json.load(open(sys.argv[1]))['runs'] == [], 'Selection must not execute project code'
print('Native selection creates no project run.')
PY
uv run --locked python3 - "$check_root/sales/data" <<'PY'
from pathlib import Path
import sys
from runtime.desktop.bridge import DesktopBridge
bridge = DesktopBridge(Path(sys.argv[1]), 'native-sales')
try:
    runs = list(bridge.session.service.runs.values())
    assert len(runs) == 3, 'Restore must not execute sales; explicit invocation must execute once'
    assert sorted(run.parameters['minimum'].value for run in runs) == [0, 200, 200]
    assert all(run.state == 'succeeded' for run in runs)
    assert all(run.inputs['sales'].sourceId == 'sales-input' and run.inputs['sales'].version == 1 for run in runs)
finally:
    bridge.close()
print('Native sales captured the expected parameter values and input version.')
PY
uv run --locked python3 native/Tests/record_environment.py \
    "$test_bundle/Contents/Resources/Runtime.json" \
    "${MELT_NATIVE_ARTIFACTS:-build/native/artifacts}/native-acceptance.json"
