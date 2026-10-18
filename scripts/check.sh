#!/usr/bin/env bash
# Run the full Melt round-1 test suite.
#
# This is the single check command promised by backlog task T001:
#   ./scripts/check.sh        (or: make check)
#
# The suite includes the contract fixture suite of SPEC.md section 18.1
# (backlog task T020): pytest runs tests/contract/test_fixtures.py and
# tests/contract/test_cli.py over every fixture in fixtures/valid and
# fixtures/invalid/manifest.json, and step 4 verifies the canonical
# hashes across the supported languages. A failure fails this command,
# and this command is the merge gate: do not merge while it fails.
#
# The command needs a Python 3.12 interpreter, uv, the committed uv.lock,
# Node with npm, and the committed Pi Durable adapter pin. It never uses
# macOS or Apple tools (SPEC.md section 2.2).
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. uv syncs .venv strictly from the committed lockfile. --locked
#    errors when pyproject.toml changed without a matching `make lock`.
command -v uv >/dev/null 2>&1 || {
    echo "check.sh: uv is required to sync the locked environment" >&2
    exit 2
}
uv sync --locked

# 2. Refuse an unpinned interpreter generation before running tests.
#    pyproject.toml pins requires-python to >=3.12,<3.13.
".venv/bin/python3" - <<'PY'
import sys

if sys.version_info[:2] != (3, 12):
    sys.exit(
        "Melt round 1 pins Python 3.12 (pyproject.toml requires-python "
        f">=3.12,<3.13); the test environment runs {sys.version.split()[0]}."
    )
PY

# 3. Sync the pinned Pi Durable storage adapter (backlog task T002,
#    SPEC.md section 14). The storage probe is part of the suite, so the
#    pin must be installed before pytest runs it.
command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || {
    echo "check.sh: node and npm are required for the Pi Durable storage probe" >&2
    exit 2
}
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' || {
    echo "check.sh: the Pi Durable adapter requires Node >= 22.19" >&2
    exit 2
}
if [ ! -d runtime/storage/pi_adapter/node_modules/@earendil-works/pi-durable ]; then
    (cd runtime/storage/pi_adapter && npm ci --no-audit --no-fund)
fi

# 4. Verify the canonical hashes across the supported languages
#    (backlog task T020, SPEC.md sections 4.2 and 18.1). The verifier
#    runs the RFC 8785 reference vectors and every valid document
#    through an independent JavaScript canonicalizer and compares the
#    hashes against fixtures/valid/document-hashes.json. The contract
#    suite runs it again inside pytest.
node scripts/verify_canonical_hashes.mjs

# 5. Run the whole suite: contract, recovery, native, and integration.
uv run --locked python3 -m pytest

echo "check.sh: full Melt test suite passed"
