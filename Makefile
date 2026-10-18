.PHONY: check lock probe-setup probe fixtures

# One command that runs the full Melt test suite (backlog task T001).
check:
	./scripts/check.sh

# Re-resolve dependencies and rewrite uv.lock after editing pyproject.toml.
lock:
	uv lock

# Install the pinned Pi Durable storage adapter (backlog task T002,
# SPEC.md section 14). check.sh runs this when the pin is missing.
probe-setup:
	cd runtime/storage/pi_adapter && npm ci --no-audit --no-fund

# Run the Pi Durable storage probe alone. The root stays outside the
# repository and carries the Looper resource prefix when one is set.
probe:
	node runtime/storage/pi_adapter/probe.mjs --root "$${TMPDIR:-/tmp}/$${LOOPER_RESOURCE_PREFIX:-melt}-pi-durable-probe-root"

# Regenerate the deterministic fixture artifact bytes under fixtures/sales
# (backlog task T004). The test suite compares regeneration against the
# committed files, so a changed toolchain must rewrite them and the catalog
# together. Append --check to compare without writing.
fixtures:
	uv run --locked python3 scripts/build_fixture_artifacts.py
