#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
./native/build.sh
xcrun swiftc -parse-as-library -swift-version 5 -D MELT_NATIVE_TESTS \
    -target "$(uname -m)-apple-macosx13.0" -framework AppKit \
    native/Sources/*.swift native/Tests/HostChecks.swift -o build/native/HostChecks
check_root="$(mktemp -d "${TMPDIR:-/tmp}/melt-native-check.XXXXXX")"
trap 'rm -rf "$check_root"' EXIT
uv run --locked python3 examples/04-cafe/run.py --output "$check_root/cafe"
uv run --locked python3 examples/03-linked-views/run.py --output "$check_root/linked"
uv run --locked python3 - "$check_root/linked" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
(root / 'Linked.meltapp').write_text(json.dumps({'format': 'melt-launcher/v1',
    'root': str(root / 'data'), 'workspaceId': 'linked-views'}))
PY
build/native/HostChecks "$check_root/cafe/Little Hours.meltapp" \
    build/native/Melt.app/Contents/Resources/Runtime.json build/native/artifacts "$check_root/linked/Linked.meltapp"
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
