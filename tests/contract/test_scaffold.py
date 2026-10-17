"""Package scaffold checks against SPEC.md section 17 (backlog task T001).

Round 1 uses the Linux subset of the required tree. The `native/`
sources belong to round 2 and stay absent here.
"""

from __future__ import annotations

import importlib

import pytest

import melt

# The round-1 subset of the tree that SPEC.md section 17 requires.
REQUIRED_DIRECTORIES = (
    "python/melt/schema",
    "python/melt/sdk",
    "python/melt/runner",
    "runtime/compiler",
    "runtime/workspace",
    "runtime/execution",
    "runtime/storage",
    "protocol/schema",
    "protocol/generated",
    "fixtures/valid",
    "fixtures/invalid",
    "fixtures/sales",
    "tests/contract",
    "tests/recovery",
    "tests/native",
    "tests/integration",
)

ROUND1_PACKAGES = (
    "melt",
    "melt.schema",
    "melt.schema.document",
    "melt.schema.commands",
    "melt.schema.records",
    "melt.schema.canonical",
    "melt.schema.limits",
    "melt.sdk",
    "melt.runner",
    "runtime",
    "runtime.compiler",
    "runtime.workspace",
    "runtime.execution",
    "runtime.storage",
)


@pytest.mark.parametrize("relative", REQUIRED_DIRECTORIES)
def test_required_directory_exists(repo_root, relative):
    assert (repo_root / relative).is_dir(), f"missing scaffold directory {relative}/"


@pytest.mark.parametrize("module_name", ROUND1_PACKAGES)
def test_package_imports(module_name):
    importlib.import_module(module_name)


def test_package_identity():
    assert melt.__version__ == "0.1.0"
    assert melt.LANGUAGE_ID == "melt/v0.1"
