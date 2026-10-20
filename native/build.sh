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
source native/ghostty-env.sh
app_path="$(pwd)/build/native/Melt.app"
mkdir -p "$app_path/Contents/MacOS" "$app_path/Contents/Resources"
xcrun swiftc -O -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macosx13.0" \
    -framework AppKit "${GHOSTTY_FLAGS[@]}" native/Sources/*.swift -o "$app_path/Contents/MacOS/Melt"
cp native/Info.plist "$app_path/Contents/Info.plist"
uv run --locked python3 - "$app_path/Contents/Resources/Runtime.json" "$(command -v node)" <<'PY'
import json
from pathlib import Path
import sys
from runtime.execution.environment import MODEL_CREDENTIAL_VARIABLES, CREDENTIAL_SUFFIXES
Path(sys.argv[1]).write_text(json.dumps({"repository": str(Path.cwd()), "python": sys.executable,
    "nodeDirectory": str(Path(sys.argv[2]).parent),
    "ghosttyResources": str(Path(sys.argv[1]).parent / "Ghostty"),
    "credentialNames": sorted(MODEL_CREDENTIAL_VARIABLES), "credentialSuffixes": CREDENTIAL_SUFFIXES}, indent=2) + "\n")
PY
mkdir -p "$app_path/Contents/Resources/Ghostty"
cp -R build/dependencies/ghostty-install/share/ghostty/. "$app_path/Contents/Resources/Ghostty/"
cp build/dependencies/ghostty/LICENSE "$app_path/Contents/Resources/Ghostty/LICENSE"
codesign --force --sign - "$app_path"
printf '%s\n' "$app_path"
