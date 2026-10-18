"""Shared fixtures for the Melt round-1 test suite."""

from __future__ import annotations

import copy
import os
import shlex
import tempfile
import uuid
from pathlib import Path

import pytest

# A well-formed artifact hash for fixtures. The sales fixture generator
# substitutes the hash of real bytes (SPEC.md section 16).
FIXTURE_HASH = "sha256:" + "1a" * 32

WORKSPACE_SERVICE_ROOT_SUFFIX = "-workspace-service"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root that contains pyproject.toml."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def service_root() -> Path:
    """A private root for workspace service tests, outside the repository.

    The directory name carries the Looper resource prefix, and its
    removal is registered with the Looper cleanup file before anything
    is created. The registered guard removes the directory only when its
    path still carries this run's prefix, so cleanup never removes a
    directory it does not own. Entries already registered by setup are
    preserved: registration is skipped when the marker is present.
    """

    prefix = os.environ.get("LOOPER_RESOURCE_PREFIX", "melt")
    root = Path(tempfile.gettempdir()) / f"{prefix}{WORKSPACE_SERVICE_ROOT_SUFFIX}"
    _register_root_cleanup(root, prefix)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _register_root_cleanup(root: Path, prefix: str) -> None:
    cleanup_file = os.environ.get("LOOPER_RESOURCE_CLEANUP_FILE")
    if not cleanup_file:
        return
    path = Path(cleanup_file)
    marker = f"# workspace-service-root: {root}"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if marker in existing:
        return
    header = existing or (
        "#!/usr/bin/env bash\n"
        "# Looper resource cleanup. Registered resources only; idempotent.\n"
        "set -u\n"
    )
    quoted = shlex.quote(str(root))
    path.write_text(
        header
        + f"\n{marker}\n"
        + f"if [ -d {quoted} ]; then\n"
        + f"  case {quoted} in\n"
        + f"    *{shlex.quote(prefix)}{WORKSPACE_SERVICE_ROOT_SUFFIX}) rm -rf -- {quoted} ;;\n"
        + "  esac\n"
        + "fi\n",
        encoding="utf-8",
    )


@pytest.fixture
def service_adapter(service_root: Path):
    """One isolated data root per test."""

    from runtime.storage import PlatformAdapter

    return PlatformAdapter.with_root(
        service_root / f"case-{uuid.uuid4().hex[:12]}"
    )


@pytest.fixture(scope="session")
def sales_document() -> dict:
    """Return the complete sales example of SPEC.md section 16."""

    return {
        "language": "melt/v0.1",
        "workspaceId": "sales-demo",
        "title": "Sales analysis",
        "sources": {
            "sales-input": {
                "kind": "table",
                "currentVersion": 1,
                "versions": {
                    "1": {
                        "artifact": FIXTURE_HASH,
                        "mediaType": "application/vnd.apache.parquet",
                        "byteLength": 4200,
                        "schema": {
                            "columns": [
                                {"id": "month", "type": "string", "nullable": False},
                                {"id": "revenue", "type": "float64", "nullable": False},
                            ],
                            "rowKey": None,
                        },
                        "origin": {"kind": "import", "label": "sales.csv"},
                    }
                },
            }
        },
        "parameters": {
            "minimum": {
                "type": "number",
                "default": 0,
                "nullable": False,
                "constraints": {"minimum": 0, "maximum": 1000000000},
            }
        },
        "actions": {
            "analyze-sales": {
                "implementation": {"registrationId": "sales-script-v1"},
                "parameters": {"minimum": {"parameter": "minimum"}},
                "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
                "outputs": {"filtered": "table", "monthly": "image"},
                "outputGroup": "sales-results",
                "targets": {"filtered": "sales", "monthly": "revenue"},
            }
        },
        "projections": {
            "sales": {
                "kind": "table",
                "title": "Sales",
                "source": None,
                "config": {"columns": ["month", "revenue"], "selection": "none"},
                "controls": [],
            },
            "revenue": {
                "kind": "plot",
                "title": "Revenue by month",
                "source": None,
                "config": {"fit": "contain"},
                "controls": [
                    {
                        "id": "minimum",
                        "kind": "number",
                        "label": "Minimum revenue",
                        "parameter": "minimum",
                    },
                    {"id": "run", "kind": "button", "label": "Run analysis"},
                ],
            },
        },
        "bindings": {
            "minimum-rerun": {
                "event": {"kind": "parameter.changed", "parameter": "minimum"},
                "effect": {"kind": "action.invoke", "action": "analyze-sales"},
                "debounceMs": 250,
            },
            "manual-run": {
                "event": {
                    "kind": "control.activate",
                    "projection": "revenue",
                    "control": "run",
                },
                "effect": {"kind": "action.invoke", "action": "analyze-sales"},
                "debounceMs": 0,
            },
        },
        "layout": {
            "root": {
                "kind": "split",
                "axis": "horizontal",
                "ratio": 0.55,
                "first": {"kind": "pane", "projection": "sales"},
                "second": {"kind": "pane", "projection": "revenue"},
            },
            "hidden": [],
        },
    }


@pytest.fixture
def sales_document_copy(sales_document) -> dict:
    """Return a deep copy of the sales document for mutation."""

    return copy.deepcopy(sales_document)
