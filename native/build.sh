#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Darwin ]]; then
    echo 'The native host requires macOS and the Xcode command line tools.' >&2
    exit 2
fi
command -v node >/dev/null
command -v uv >/dev/null
uv sync --locked
app_path="$(pwd)/build/native/Melt.app"
mkdir -p "$app_path/Contents/MacOS" "$app_path/Contents/Resources"
xcrun swiftc -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx13.0" \
    -framework AppKit native/Sources/*.swift -o "$app_path/Contents/MacOS/Melt"
cp native/Info.plist "$app_path/Contents/Info.plist"
uv run --locked python3 - "$app_path/Contents/Resources/Runtime.json" "$(command -v node)" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps({"repository": str(Path.cwd()), "python": sys.executable,
    "nodeDirectory": str(Path(sys.argv[2]).parent)}, indent=2) + "\n")
PY
codesign --force --sign - "$app_path"
printf '%s\n' "$app_path"
