#!/usr/bin/env bash
# Run the full Melt round-1 test suite.
#
# This is the single check command promised by backlog task T001:
#   ./scripts/check.sh        (or: make check)
#
# The suite includes the contract fixture suite of SPEC.md section 18.1
# (backlog task T020): pytest runs tests/contract/test_fixtures.py and
# tests/contract/test_cli.py over every fixture in fixtures/valid and
# fixtures/invalid/manifest.json, and step 5 verifies the canonical
# hashes across the supported languages. Step 4 is the regeneration
# drift gate of backlog task T007: regenerating the JSON Schema export
# and the TypeScript and Swift models must leave the committed files
# unchanged, so continuous integration fails when regeneration changes
# committed output (SPEC.md section 17). A failure fails this command,
# and this command is the merge gate: do not merge while it fails.
#
# The command needs a Python 3.12 interpreter, uv, the committed uv.lock,
# Node with npm, and the committed Pi Durable adapter pin. It never
# requires macOS or Apple tools (SPEC.md section 2.2): the generated
# Swift models are verified only where a Swift toolchain happens to
# exist.
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
#    SPEC.md section 14) and the pinned Pele renderer (backlog task
#    T028, RFC 001 section 7.3). Both probes are part of the suite, so
#    the pins must be installed before pytest runs them.
command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || {
    echo "check.sh: node and npm are required for the pinned Node probes" >&2
    exit 2
}
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' || {
    echo "check.sh: the Pi Durable adapter requires Node >= 22.19" >&2
    exit 2
}
if [ ! -d runtime/storage/pi_adapter/node_modules/@earendil-works/pi-durable ]; then
    (cd runtime/storage/pi_adapter && npm ci --no-audit --no-fund)
fi
if [ ! -d runtime/rendering/pele_adapter/node_modules/pele ]; then
    (cd runtime/rendering/pele_adapter && npm ci --no-audit --no-fund)
fi

# 4. Regeneration drift gate (backlog task T007, SPEC.md section 17):
#    the JSON Schema export under protocol/schema and the TypeScript
#    and Swift models under protocol/generated are committed generated
#    files. Regenerating them must reproduce the committed bytes, or
#    this command fails. Regenerate with `make protocol` after every
#    vocabulary change and commit the result.
uv run --locked python3 scripts/generate_protocol.py --check

# 5. Verify the canonical hashes across the supported languages
#    (backlog tasks T020 and T007, SPEC.md sections 4.2, 17, and 18.1).
#    The verifier runs the RFC 8785 reference vectors and every valid
#    document through an independent JavaScript canonicalizer, then
#    every entry of fixtures/valid/document-hashes.json and
#    fixtures/semantic/document-hashes.json through the generated
#    TypeScript models. The contract suite runs it again inside pytest.
node scripts/verify_canonical_hashes.mjs

# 6. Verify the generated Swift models where a Swift toolchain exists.
#    The Linux gate must not require Apple tools (SPEC.md section 2.2),
#    so this step is best effort: it runs the generated harness when
#    `swift` is on PATH and reports a skip otherwise. The native round
#    (backlog task T030) always has the toolchain and always runs it.
if command -v swift >/dev/null 2>&1; then
    swift protocol/generated/MeltDocument.swift "$(pwd)"
else
    echo "check.sh: swift not found; skipping the generated Swift model verification"
fi

# 7. Run the whole suite: contract, recovery, native, and integration.
uv run --locked python3 -m pytest

echo "check.sh: full Melt test suite passed"
