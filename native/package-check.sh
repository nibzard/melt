#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" != --skip-build ]]; then uv run --locked python3 native/package.py; fi
package_root="$(mktemp -d "${TMPDIR:-/tmp}/melt-package-check.XXXXXX")"
trap 'rm -rf "$package_root"' EXIT
bundle="$package_root/App with spaces/Melt.app"
mkdir -p "$(dirname "$bundle")"
ditto build/package/Melt.app "$bundle"
uv run --locked python3 native/verify_package.py "$bundle"
MELT_TEST_BUNDLE="$bundle" MELT_EXPLICIT_REOPEN=1 \
    MELT_NATIVE_ARTIFACTS=build/native/artifacts/package ./native/check.sh
uv run --locked python3 native/verify_package.py "$bundle"
