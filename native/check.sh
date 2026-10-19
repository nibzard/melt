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
build/native/HostChecks "$check_root/cafe/Little Hours.meltapp" \
    build/native/Melt.app/Contents/Resources/Runtime.json
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
