"""Round 1 performance measurements (backlog task T023, SPEC.md section 19).

The script measures, on the Linux machine it runs on, the four round 1
dimensions of section 19 through the same service handlers the native
host will drive:

- Compilation: a generated 32-projection document compiled against the
  cached fixture catalogs, warm in-process and cold in a fresh
  interpreter.
- Service commits: the framed ``workspace.apply`` round trip of one
  bound ``parameter.set`` (the service side of local control feedback)
  and of one small definition patch followed by its notification (the
  service side of a committed patch reaching a visible update).
- Artifact publication: ``publish_run`` of one succeeded run of the
  section 13.2 script shape, timed inside the owning transaction. The
  runner process itself is not part of the sample.
- Table paging: ``table.page`` over a generated 1,000,000-row,
  20-column table, warm and with a dropped kernel page cache, plus the
  full-table sort section 19 asks to report separately.

It also times reopening a saved five-pane workspace and reading its
snapshot through the framed channel, which is the round 1 service-side
portion of the pane-restoration target.

Every number the report records derives from this one script, so the
recorded document and the raw JSON of the same run agree:

    uv run --locked python3 scripts/benchmark_round1.py

Cold samples drop the kernel page cache with ``sync`` plus
``sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches'``. Without a
passwordless sudo the cold samples are skipped and the reason is
recorded. The durable workspaces and generated fixtures live under one
private root named with the Looper resource prefix, outside the
repository, and the root's removal is registered with the Looper
cleanup file before anything is created (idempotently, preserving
entries setup already registered).
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import math
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_REPO = Path(__file__).resolve().parents[1]
for _entry in (_REPO / "python", _REPO):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from melt.schema.commands import decode_frame, encode_frame  # noqa: E402
from runtime.compiler import CompileRequest, compile_document  # noqa: E402
from runtime.compiler.catalog import load_catalog  # noqa: E402
from runtime.storage import PlatformAdapter  # noqa: E402
from runtime.storage.worker import PI_DURABLE_PIN  # noqa: E402
from runtime.workspace import WorkspaceService, local_environment  # noqa: E402
from runtime.workspace.importer import measure_artifact  # noqa: E402


def _installed_pi_durable() -> "str | None":
    """The installed adapter version, read from the pinned package."""

    marker = (
        _REPO
        / "runtime"
        / "storage"
        / "pi_adapter"
        / "node_modules"
        / "@earendil-works"
        / "pi-durable"
        / "package.json"
    )
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None

BENCHMARK_ROOT_SUFFIX = "-round1-benchmark-root"
"""The suffix of the private data root, behind the Looper prefix."""

PARQUET = "application/vnd.apache.parquet"
PNG = "image/png"
MARKDOWN = "text/markdown"
PYTHON = "text/x-python"

# The section 13.2 script shape without the plot, so a publication
# sample stays a publication measurement rather than a matplotlib
# import measurement. The staged output is a real Parquet table.
LIGHT_SALES_SCRIPT = (
    "import pandas as pd\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "ui.output('filtered', selected)\n"
)

LIGHT_CONTRACT = {
    "parameters": {
        "minimum": {
            "type": "number",
            "nullable": False,
            "constraints": {"minimum": 0, "maximum": 1000000000},
        }
    },
    "inputs": {"sales": "table"},
    "outputs": {"filtered": "table"},
}

# The specification's own script of section 13.2, copied word for
# word, with the contract that matches it. The five-pane workspace
# registers it so the saved definitions cite a complete registration;
# restoration never executes it.
SPEC_SALES_SCRIPT = (
    "import pandas as pd\n"
    "import matplotlib.pyplot as plt\n"
    "from melt import ui\n"
    "\n"
    "sales = pd.read_parquet(ui.input_path('sales'))\n"
    "selected = sales[sales['revenue'] >= ui.params['minimum']]\n"
    "monthly = selected.groupby('month', sort=True)['revenue'].sum()\n"
    "fig, ax = plt.subplots()\n"
    "monthly.plot(ax=ax)\n"
    "ax.set_ylabel('Revenue')\n"
    "ui.output('filtered', selected)\n"
    "ui.output('monthly', fig)\n"
    "plt.close(fig)\n"
)

FULL_CONTRACT = {
    "parameters": {
        "minimum": {
            "type": "number",
            "nullable": False,
            "constraints": {"minimum": 0, "maximum": 1000000000},
        }
    },
    "inputs": {"sales": "table"},
    "outputs": {"filtered": "table", "monthly": "image"},
}

MINIMUM_PARAMETER = {
    "type": "number",
    "default": 0,
    "nullable": False,
    "constraints": {"minimum": 0, "maximum": 1000000000},
}

# The five targets of SPEC.md section 19, verbatim, so the report and
# the JSON compare against the specification instead of a paraphrase.
TARGETS = {
    "compile-32-projection": {
        "metric": "Compile a 32-projection document with cached catalogs",
        "target": "p95 below 50 ms",
        "p95Ms": 50.0,
    },
    "control-feedback-commit": {
        "metric": "Local control feedback",
        "target": "p95 below 50 ms",
        "p95Ms": 50.0,
    },
    "committed-patch-notification": {
        "metric": "Small committed patch to visible update",
        "target": "p95 below 100 ms",
        "p95Ms": 100.0,
    },
    "restore-five-panes": {
        "metric": "Restore five saved panes",
        "target": "below 2 seconds",
        "p95Ms": 2000.0,
    },
    "table-page-first-warm": {
        "metric": "Warm first table page for 1 million rows and 20 columns",
        "target": "p95 below 300 ms",
        "p95Ms": 300.0,
    },
}

TABLE_ROWS = 1_000_000
TABLE_COLUMNS = 20
RANDOM_SEED = 20261018
"""The seed that makes the generated table byte-deterministic."""


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Return one percentile by the nearest-rank method.

    Section 19 speaks of p95 over repeated measurements. Nearest-rank
    is the method the report declares: sort the samples, take rank
    ceil(fraction x n), and never interpolate between observations.
    """

    if not samples:
        raise ValueError("a percentile needs at least one sample")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def summarize(samples_ms: Sequence[float]) -> dict:
    """Summarize one measurement's samples in milliseconds."""

    ordered = sorted(samples_ms)
    return {
        "n": len(ordered),
        "unit": "ms",
        "min": round(ordered[0], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(percentile(ordered, 0.50), 3),
        "p95": round(percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def _result(
    name: str,
    path: str,
    condition: str,
    samples_ms: Sequence[float],
    notes: str = "",
    target: "dict | None" = None,
    skipped: str = "",
) -> dict:
    result = {
        "name": name,
        "measurement": path,
        "condition": condition,
        "notes": notes,
    }
    if skipped and not samples_ms:
        result["skipped"] = skipped
        return result
    if skipped:
        # Some samples landed before the cache drop failed; keep both
        # the numbers and the reason the run stopped early.
        result["partial"] = skipped
    result.update(summarize(samples_ms))
    if target is not None:
        result["target"] = {
            "metric": target["metric"],
            "target": target["target"],
            "met": result["p95"] < target["p95Ms"],
        }
    return result


# ---------------------------------------------------------------------------
# Environment facts
# ---------------------------------------------------------------------------


def _read_first(path: Path, pattern: str) -> "str | None":
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(pattern):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _mount_facts(path: Path) -> dict:
    """The filesystem type and device of the mount holding one path."""

    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return {"path": str(path)}
    best = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, kind = parts[0], parts[1], parts[2]
        if path == Path(mount) or str(path).startswith(mount.rstrip("/") + "/"):
            if best is None or len(mount) > len(best[1]):
                best = (device, mount, kind)
    if best is None:
        return {"path": str(path)}
    device, mount, kind = best
    return {"path": str(path), "mount": mount, "type": kind, "device": device}


def _os_pretty_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()


def machine_facts(repo: Path, data_root: Path) -> dict:
    """Record the declared machine, its software pins, and build mode."""

    meminfo: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            meminfo[name.strip()] = int(value.strip().split()[0])
    except (OSError, ValueError):
        pass

    import numpy
    import pyarrow
    import pydantic

    packages = {
        "pydantic": pydantic.VERSION,
        "pyarrow": pyarrow.__version__,
        "numpy": numpy.__version__,
    }
    try:
        import matplotlib
        import pandas

        packages["pandas"] = pandas.__version__
        packages["matplotlib"] = matplotlib.__version__
    except ImportError:  # pragma: no cover - the pinned set installs both
        pass

    node_version = shutil.which("node")
    if node_version:
        try:
            node_version = subprocess.run(
                [node_version, "--version"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            node_version = None

    sudo = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True
    ).returncode == 0

    return {
        "recordedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "declaredMachine": (
            "The Linux runner the measurements below ran on. SPEC.md"
            " section 19 names a declared Linux machine with 16 GiB"
            " memory; this runner exposes the memory recorded here,"
            " which is below that class, so the comparison against the"
            " targets carries that deviation openly."
        ),
        "cpu": {
            "model": _read_first(Path("/proc/cpuinfo"), "model name"),
            "logicalCores": os.cpu_count(),
        },
        "memoryKiB": {
            "MemTotal": meminfo.get("MemTotal"),
            "MemAvailable": meminfo.get("MemAvailable"),
            "SwapTotal": meminfo.get("SwapTotal"),
        },
        "kernel": platform.release(),
        "os": _os_pretty_name(),
        "filesystems": {
            "dataRoot": _mount_facts(data_root),
            "repository": _mount_facts(repo),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "packages": packages,
        "node": node_version,
        "piDurablePin": PI_DURABLE_PIN,
        "piDurableInstalled": _installed_pi_durable(),
        "clock": (lambda info: {
            "implementation": info.implementation,
            "monotonic": info.monotonic,
            "adjustable": info.adjustable,
            "resolution": info.resolution,
        })(time.get_clock_info("perf_counter")),
        "passwordlessSudo": sudo,
        "buildMode": (
            "CPython interpreter resolved by `uv sync --locked` from the"
            " committed uv.lock; compiled pydantic-core and pyarrow"
            " extensions; assertions off; the Pi Durable storage worker"
            " is a fresh Node helper process per service open; no"
            " debug or instrumented builds anywhere in the loop"
        ),
        "environmentFingerprint": local_environment().fingerprint,
    }


# ---------------------------------------------------------------------------
# Generated fixtures
# ---------------------------------------------------------------------------


def _split_tree(ids: Sequence[str], depth: int = 0) -> dict:
    """A balanced binary layout over every projection id in order."""

    if len(ids) == 1:
        return {"kind": "pane", "projection": ids[0]}
    middle = len(ids) // 2
    return {
        "kind": "split",
        "axis": "horizontal" if depth % 2 == 0 else "vertical",
        "ratio": 0.5,
        "first": _split_tree(ids[:middle], depth + 1),
        "second": _split_tree(ids[middle:], depth + 1),
    }


def _table_projection(title: str, controls: list, *, source=None) -> dict:
    return {
        "kind": "table",
        "title": title,
        "source": source,
        "config": {"columns": ["month", "revenue"], "selection": "none"},
        "controls": controls,
    }


def _plot_projection(title: str, controls: list, *, source=None) -> dict:
    return {
        "kind": "plot",
        "title": title,
        "source": source,
        "config": {"fit": "contain"},
        "controls": controls,
    }


def _document_projection(title: str, controls: list, source: dict) -> dict:
    return {
        "kind": "document",
        "title": title,
        "source": source,
        "config": {"format": "markdown"},
        "controls": controls,
    }


def _fixture_version(catalog: dict, artifact_key: str) -> dict:
    """One source version built from a catalog artifact entry."""

    artifact = catalog["artifacts"][artifact_key]
    return {
        "artifact": artifact_key,
        "mediaType": artifact["mediaType"],
        "byteLength": artifact["byteLength"],
        "schema": artifact["schema"],
        "origin": {"kind": "import", "label": Path(artifact["path"]).name},
    }


def build_thirty_two_projection_document() -> dict:
    """The 32-projection compile fixture of SPEC.md section 19.

    Every projection limit is saturated (32 of 32), every control kind
    appears, and eight actions own eight distinct output groups, so
    the compiled plan exercises the whole stage pipeline rather than
    one narrow corner.
    """

    catalog = json.loads(
        (_REPO / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    )
    table_ref = {"sourceId": "sales-input", "version": 1}
    image_ref = {"sourceId": "revenue-image", "version": 1}
    text_ref = {"sourceId": "revenue-notes", "version": 1}

    projections: dict[str, dict] = {}
    placements: list[str] = []
    for index in range(8):
        name = f"out-table-{index}"
        projections[name] = _table_projection(
            f"Sales output {index}",
            [
                {
                    "id": "minimum",
                    "kind": "number",
                    "label": "Minimum revenue",
                    "parameter": "minimum",
                },
                {
                    "id": "run",
                    "kind": "button",
                    "label": f"Run analysis {index}",
                },
            ],
        )
        placements.append(name)
    for index in range(8):
        name = f"out-plot-{index}"
        projections[name] = _plot_projection(f"Revenue output {index}", [])
        placements.append(name)
    for index in range(8):
        name = f"view-table-{index}"
        projections[name] = _table_projection(
            f"Sales view {index}",
            [
                {
                    "id": "note",
                    "kind": "text",
                    "label": "View note",
                    "parameter": "note",
                }
            ],
            source=table_ref,
        )
        placements.append(name)
    for index in range(4):
        name = f"view-plot-{index}"
        projections[name] = _plot_projection(
            f"Revenue view {index}",
            [
                {
                    "id": "verbose",
                    "kind": "toggle",
                    "label": "Verbose",
                    "parameter": "verbose",
                }
            ],
            source=image_ref,
        )
        placements.append(name)
    for index in range(4):
        name = f"view-doc-{index}"
        projections[name] = _document_projection(
            f"Notes view {index}",
            [
                {
                    "id": "region",
                    "kind": "select",
                    "label": "Region",
                    "parameter": "region",
                    "options": [
                        {"value": "north", "label": "North"},
                        {"value": "south", "label": "South"},
                    ],
                }
            ],
            text_ref,
        )
        placements.append(name)

    actions: dict[str, dict] = {}
    bindings: dict[str, dict] = {}
    for index in range(8):
        actions[f"analyze-{index}"] = {
            "implementation": {"registrationId": "sales-script-v1"},
            "parameters": {"minimum": {"parameter": "minimum"}},
            "inputs": {"sales": table_ref},
            "outputs": {"filtered": "table", "monthly": "image"},
            "outputGroup": f"results-{index}",
            "targets": {
                "filtered": f"out-table-{index}",
                "monthly": f"out-plot-{index}",
            },
        }
        bindings[f"rerun-{index}"] = {
            "event": {"kind": "parameter.changed", "parameter": "minimum"},
            "effect": {"kind": "action.invoke", "action": f"analyze-{index}"},
            "debounceMs": 250,
        }
        bindings[f"fire-{index}"] = {
            "event": {
                "kind": "control.activate",
                "projection": f"out-table-{index}",
                "control": "run",
            },
            "effect": {"kind": "action.invoke", "action": f"analyze-{index}"},
            "debounceMs": 0,
        }

    return {
        "language": "melt/v0.1",
        "workspaceId": "compile-bench",
        "title": "Compile benchmark",
        "sources": {
            "sales-input": {
                "kind": "table",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:42cb3135d0370d5d9b0712adc0835b30c3d48843aa36d0df333851a42afa4598",
                    )
                },
            },
            "revenue-image": {
                "kind": "image",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:a00604c75ee5bb2b29d883b8017bc117f914aab937130fc0d9270d4fddae41fd",
                    )
                },
            },
            "revenue-notes": {
                "kind": "text",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:c0418ced26872a83a21ae599f576ab90eeb319c07bf4e75cba4d807ad5888554",
                    )
                },
            },
        },
        "parameters": {
            "minimum": MINIMUM_PARAMETER,
            "note": {
                "type": "string",
                "default": "quarterly",
                "nullable": False,
                "constraints": {"maxLength": 256},
            },
            "verbose": {
                "type": "boolean",
                "default": False,
                "nullable": False,
                "constraints": {},
            },
            "region": {
                "type": "string",
                "default": "north",
                "nullable": False,
                "constraints": {"enum": ["north", "south"]},
            },
        },
        "actions": actions,
        "projections": projections,
        "bindings": bindings,
        "layout": {"root": _split_tree(placements), "hidden": []},
    }


def build_five_pane_document() -> dict:
    """The saved five-pane workspace of the restoration measurement."""

    catalog = json.loads(
        (_REPO / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    )
    table_ref = {"sourceId": "sales-input", "version": 1}
    image_ref = {"sourceId": "revenue-image", "version": 1}
    text_ref = {"sourceId": "revenue-notes", "version": 1}

    return {
        "language": "melt/v0.1",
        "workspaceId": "five-panes",
        "title": "Five saved panes",
        "sources": {
            "sales-input": {
                "kind": "table",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:42cb3135d0370d5d9b0712adc0835b30c3d48843aa36d0df333851a42afa4598",
                    )
                },
            },
            "revenue-image": {
                "kind": "image",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:a00604c75ee5bb2b29d883b8017bc117f914aab937130fc0d9270d4fddae41fd",
                    )
                },
            },
            "revenue-notes": {
                "kind": "text",
                "currentVersion": 1,
                "versions": {
                    "1": _fixture_version(
                        catalog,
                        "sha256:c0418ced26872a83a21ae599f576ab90eeb319c07bf4e75cba4d807ad5888554",
                    )
                },
            },
        },
        "parameters": {"minimum": MINIMUM_PARAMETER},
        "actions": {
            "analyze-sales": {
                "implementation": {"registrationId": "sales-script-v1"},
                "parameters": {"minimum": {"parameter": "minimum"}},
                "inputs": {"sales": table_ref},
                "outputs": {"filtered": "table", "monthly": "image"},
                "outputGroup": "sales-results",
                "targets": {"filtered": "sales", "monthly": "revenue"},
            }
        },
        "projections": {
            "sales": _table_projection(
                "Sales",
                [
                    {
                        "id": "minimum",
                        "kind": "number",
                        "label": "Minimum revenue",
                        "parameter": "minimum",
                    },
                    {"id": "run", "kind": "button", "label": "Run analysis"},
                ],
            ),
            "revenue": _plot_projection(
                "Revenue by month",
                [],
            ),
            "notes": _document_projection("Notes", [], text_ref),
            "sales-view": _table_projection("Sales view", [], source=table_ref),
            "revenue-view": _plot_projection(
                "Revenue view", [], source=image_ref
            ),
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
                    "projection": "sales",
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
                "second": {
                    "kind": "split",
                    "axis": "vertical",
                    "ratio": 0.5,
                    "first": {"kind": "pane", "projection": "revenue"},
                    "second": {
                        "kind": "split",
                        "axis": "horizontal",
                        "ratio": 0.5,
                        "first": {"kind": "pane", "projection": "notes"},
                        "second": {
                            "kind": "split",
                            "axis": "vertical",
                            "ratio": 0.5,
                            "first": {"kind": "pane", "projection": "sales-view"},
                            "second": {
                                "kind": "pane",
                                "projection": "revenue-view",
                            },
                        },
                    },
                },
            },
            "hidden": [],
        },
    }


def big_table_bytes(rows: int = TABLE_ROWS) -> bytes:
    """The 20-column paging fixture, byte-deterministic for the seed.

    The column mix covers every wire cell kind of section 5.1: seven
    string columns, three int64, six float64, two boolean, one date,
    and one timestamp. Nullable columns exercise the null handling of
    the reader without making every column sparse.
    """

    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(RANDOM_SEED)
    months = np.array([f"2026-{month:02d}" for month in range(1, 13)])
    regions = np.array([f"region-{index:02d}" for index in range(10)])
    channels = np.array(["retail", "online", "partner", "direct", "mixed"])
    products = np.array([f"P{index:03d}" for index in range(200)])
    tags = np.array([f"tag-{index:02d}" for index in range(50)])
    codes = np.array([f"code-{index:02d}" for index in range(16)])

    def strings(values: np.ndarray, mask=None) -> pa.Array:
        array = pa.array(values)
        if mask is None:
            return array
        return pa.array(values, mask=mask)

    columns: list[tuple[str, pa.Array]] = [
        ("month", strings(months[rng.integers(0, 12, rows)])),
        ("region", strings(regions[rng.integers(0, 10, rows)])),
        ("channel", strings(channels[rng.integers(0, 5, rows)])),
        ("product", strings(products[rng.integers(0, 200, rows)])),
        (
            "row_id",
            strings(
                np.array([f"row-{index:07d}" for index in range(rows)])
            ),
        ),
        (
            "tag",
            strings(
                tags[rng.integers(0, 50, rows)],
                mask=rng.random(rows) < 0.05,
            ),
        ),
        ("code", strings(codes[rng.integers(0, 16, rows)])),
        ("quantity", pa.array(rng.integers(0, 10_001, rows))),
        ("unit_price", pa.array(np.round(rng.uniform(1, 500, rows), 4))),
        (
            "discount",
            pa.array(
                np.round(rng.uniform(0, 0.5, rows), 4),
                mask=rng.random(rows) < 0.03,
            ),
        ),
        ("revenue", pa.array(np.round(rng.normal(2500, 800, rows), 2))),
        ("margin", pa.array(np.round(rng.normal(0.32, 0.08, rows), 4))),
        ("score", pa.array(np.round(rng.uniform(-1e6, 1e6, rows), 3))),
        ("ok", pa.array(rng.random(rows) < 0.9)),
        (
            "verified",
            pa.array(rng.random(rows) < 0.8, mask=rng.random(rows) < 0.02),
        ),
        ("day", pa.array((rng.integers(0, 3650, rows)).astype("int32"), type=pa.date32())),
        (
            "moment",
            pa.array(
                (1_760_000_000_000_000 + np.arange(rows) * 1_000).astype(
                    "int64"
                ),
                type=pa.timestamp("us"),
            ),
        ),
        (
            "flag",
            pa.array(
                rng.integers(-1_000_000, 1_000_000, rows),
                mask=rng.random(rows) < 0.01,
            ),
        ),
        ("amount", pa.array(rng.integers(0, 2**40, rows))),
        ("ratio", pa.array(np.round(rng.uniform(0, 1, rows), 6))),
    ]
    assert len(columns) == TABLE_COLUMNS
    table = pa.table({name: array for name, array in columns})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


# ---------------------------------------------------------------------------
# Framed service driving
# ---------------------------------------------------------------------------


def framed(
    service: WorkspaceService,
    workspace: str,
    method: str,
    *,
    payload: "dict | None" = None,
    expected: "dict | None" = None,
    operations: "list | None" = None,
    request_id: str,
    origin: str = "user",
) -> dict:
    """One request through the framed channel the native host drives."""

    body: dict[str, Any] = {
        "protocolVersion": 1,
        "workspaceId": workspace,
        "requestId": request_id,
        "method": method,
        "expected": expected,
    }
    if operations is not None:
        body["operations"] = operations
    else:
        body["payload"] = {} if payload is None else payload
    answer = service.process_frame(
        encode_frame(json.dumps(body, allow_nan=False).encode("utf-8")),
        origin=origin,
    )
    raw, _ = decode_frame(answer)
    return json.loads(raw.decode("utf-8"))


def _builder_module():
    spec = importlib.util.spec_from_file_location(
        "build_fixture_artifacts", _REPO / "scripts" / "build_fixture_artifacts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_sales_workspace(
    adapter: PlatformAdapter, workspace: str, *, script: str, contract: dict
) -> str:
    """Create one sales workspace through the real service operations.

    Returns the registration the definitions cite. The flow is the
    interactive path of SPEC.md section 12.6: create empty, import,
    register, apply definitions, grant execution permission.
    """

    builder = _builder_module()
    service = WorkspaceService.open(adapter, workspace)
    try:
        created = service.create_workspace(
            {
                "language": "melt/v0.1",
                "workspaceId": workspace,
                "title": "Performance benchmark",
                "sources": {},
                "parameters": {"minimum": MINIMUM_PARAMETER},
                "actions": {},
                "projections": {},
                "bindings": {},
                "layout": {"root": None, "hidden": []},
            },
            f"{workspace}-create",
        )
        assert created.ok, created.errors

        token = service.capture_artifact_token(
            builder.sales_parquet_bytes(), PARQUET
        ).token
        imported = framed(
            service,
            workspace,
            "source.import",
            expected={"sources/sales-input": 0},
            payload={
                "sourceId": "sales-input",
                "artifactToken": token,
                "originLabel": "sales.csv",
            },
            request_id=f"{workspace}-import",
        )
        assert imported["ok"], imported["errors"]

        script_token = service.capture_artifact_token(
            script.encode("utf-8"), PYTHON
        ).token
        registered = framed(
            service,
            workspace,
            "action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": contract,
            },
            request_id=f"{workspace}-register",
        )
        assert registered["ok"], registered["errors"]
        registration = registered["result"]["registrationId"]

        applied = framed(
            service,
            workspace,
            "workspace.apply",
            expected={
                "actions/analyze-sales": 0,
                "projections/sales": 0,
                "bindings/minimum-rerun": 0,
                "bindings/manual-run": 0,
                "layout": 1,
            },
            operations=[
                {
                    "op": "entity.create",
                    "collection": "actions",
                    "id": "analyze-sales",
                    "value": {
                        "implementation": {"registrationId": registration},
                        "parameters": {"minimum": {"parameter": "minimum"}},
                        "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
                        "outputs": {"filtered": "table"},
                        "outputGroup": "sales-results",
                        "targets": {"filtered": "sales"},
                    },
                },
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": "sales",
                    "value": _table_projection(
                        "Sales",
                        [
                            {
                                "id": "minimum",
                                "kind": "number",
                                "label": "Minimum revenue",
                                "parameter": "minimum",
                            },
                            {
                                "id": "run",
                                "kind": "button",
                                "label": "Run analysis",
                            },
                        ],
                    ),
                },
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": "minimum-rerun",
                    "value": {
                        "event": {
                            "kind": "parameter.changed",
                            "parameter": "minimum",
                        },
                        "effect": {
                            "kind": "action.invoke",
                            "action": "analyze-sales",
                        },
                        "debounceMs": 250,
                    },
                },
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": "manual-run",
                    "value": {
                        "event": {
                            "kind": "control.activate",
                            "projection": "sales",
                            "control": "run",
                        },
                        "effect": {
                            "kind": "action.invoke",
                            "action": "analyze-sales",
                        },
                        "debounceMs": 0,
                    },
                },
                {
                    "op": "layout.replace",
                    "value": {
                        "root": {"kind": "pane", "projection": "sales"},
                        "hidden": [],
                    },
                },
            ],
            request_id=f"{workspace}-definitions",
        )
        assert applied["ok"], applied["errors"]

        service.grant_execution_permission(
            kind="projectExecution",
            registration_id=registration,
            granted_by="benchmark-permission-control/approve",
        )
        return registration
    finally:
        service.close()


def seed_five_pane_workspace(adapter: PlatformAdapter) -> None:
    """Build the saved five-pane workspace, then leave it on disk."""

    builder = _builder_module()
    document = build_five_pane_document()
    service = WorkspaceService.open(adapter, "five-panes")
    try:
        created = service.create_workspace(
            {
                "language": "melt/v0.1",
                "workspaceId": "five-panes",
                "title": "Five saved panes",
                "sources": {},
                "parameters": {"minimum": MINIMUM_PARAMETER},
                "actions": {},
                "projections": {},
                "bindings": {},
                "layout": {"root": None, "hidden": []},
            },
            "five-panes-create",
        )
        assert created.ok, created.errors

        for source_id, payload, media_type in (
            ("sales-input", builder.sales_parquet_bytes(), PARQUET),
            ("revenue-image", builder.revenue_png_bytes(), PNG),
            ("revenue-notes", builder.notes_markdown_bytes(), MARKDOWN),
        ):
            token = service.capture_artifact_token(payload, media_type).token
            imported = framed(
                service,
                "five-panes",
                "source.import",
                expected={f"sources/{source_id}": 0},
                payload={
                    "sourceId": source_id,
                    "artifactToken": token,
                    "originLabel": f"{source_id}.bin",
                },
                request_id=f"five-panes-import-{source_id}",
            )
            assert imported["ok"], imported["errors"]

        script_token = service.capture_artifact_token(
            SPEC_SALES_SCRIPT.encode("utf-8"), PYTHON
        ).token
        registered = framed(
            service,
            "five-panes",
            "action.register",
            payload={
                "scriptToken": script_token,
                "environmentId": local_environment().environment_id,
                "contract": FULL_CONTRACT,
            },
            request_id="five-panes-register",
        )
        assert registered["ok"], registered["errors"]
        registration = registered["result"]["registrationId"]

        action = document["actions"]["analyze-sales"]
        action["implementation"]["registrationId"] = registration
        operations = [
            {
                "op": "entity.create",
                "collection": "actions",
                "id": "analyze-sales",
                "value": action,
            },
            *(
                {
                    "op": "entity.create",
                    "collection": "projections",
                    "id": name,
                    "value": value,
                }
                for name, value in document["projections"].items()
            ),
            *(
                {
                    "op": "entity.create",
                    "collection": "bindings",
                    "id": name,
                    "value": value,
                }
                for name, value in document["bindings"].items()
            ),
            {"op": "layout.replace", "value": document["layout"]},
        ]
        expected = {
            "actions/analyze-sales": 0,
            **{f"projections/{name}": 0 for name in document["projections"]},
            **{f"bindings/{name}": 0 for name in document["bindings"]},
            "layout": 1,
        }
        applied = framed(
            service,
            "five-panes",
            "workspace.apply",
            expected=expected,
            operations=operations,
            request_id="five-panes-definitions",
        )
        assert applied["ok"], applied["errors"]

        # One committed parameter change, so restored state is not the
        # untouched default (SPEC.md section 14, step 2).
        committed = framed(
            service,
            "five-panes",
            "workspace.apply",
            expected={"state/minimum": 1},
            operations=[
                {"op": "parameter.set", "id": "minimum", "value": 120.0}
            ],
            request_id="five-panes-set",
        )
        assert committed["ok"], committed["errors"]
    finally:
        service.close()


def seed_page_workspace(adapter: PlatformAdapter, payload: bytes) -> dict:
    """Import the generated table as the only source of a workspace."""

    service = WorkspaceService.open(adapter, "page-bench")
    try:
        created = service.create_workspace(
            {
                "language": "melt/v0.1",
                "workspaceId": "page-bench",
                "title": "Paging benchmark",
                "sources": {},
                "parameters": {},
                "actions": {},
                "projections": {},
                "bindings": {},
                "layout": {"root": None, "hidden": []},
            },
            "page-bench-create",
        )
        assert created.ok, created.errors
        token = service.capture_artifact_token(payload, PARQUET).token
        imported = framed(
            service,
            "page-bench",
            "source.import",
            expected={"sources/big": 0},
            payload={
                "sourceId": "big",
                "artifactToken": token,
                "originLabel": "big.parquet",
            },
            request_id="page-bench-import",
        )
        assert imported["ok"], imported["errors"]
        return measure_artifact(payload, kind="table", media_type=PARQUET)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The measurements
# ---------------------------------------------------------------------------


def drop_page_cache() -> "tuple[bool, str]":
    """Drop the kernel page cache, or report why that was impossible."""

    subprocess.run(["sync"], check=True)
    completed = subprocess.run(
        ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False, (completed.stderr.strip() or "sudo -n failed")
    return True, ""


class Benchmarker:
    """One run of every round 1 measurement over one private root."""

    def __init__(self, root: Path, args: argparse.Namespace):
        self.root = root
        self.args = args
        self.results: list[dict] = []
        self.fixture_sizes: dict[str, Any] = {}

    def _scale(self, count: int) -> int:
        return max(1, int(count * self.args.samples_scale))

    def emit(self, result: dict) -> None:
        self.results.append(result)
        if "skipped" in result:
            print(f"[skip] {result['name']}: {result['skipped']}", flush=True)
        else:
            target = ""
            if "target" in result:
                target = (
                    f" target '{result['target']['target']}'"
                    f" -> {'met' if result['target']['met'] else 'MISSED'}"
                )
            print(
                f"[ok]   {result['name']} ({result['condition']})"
                f" n={result['n']} p50={result['p50']}ms"
                f" p95={result['p95']}ms{target}",
                flush=True,
            )

    # -- compilation ------------------------------------------------------

    def bench_compile(self) -> None:
        document = build_thirty_two_projection_document()
        document_bytes = json.dumps(document, allow_nan=False).encode("utf-8")
        self.fixture_sizes["compileDocumentBytes"] = len(document_bytes)
        self.fixture_sizes["compileProjections"] = len(document["projections"])
        catalog, registry = load_catalog(
            (_REPO / "fixtures" / "catalog.json").read_text(encoding="utf-8")
        )

        # Warm: cached catalogs, one process, repeated compiles. The
        # first compile warms pydantic's schema caches; it is part of
        # the warm-up, not the samples.
        compile_document(
            CompileRequest(
                document=document_bytes,
                source_catalog=catalog,
                action_registry=registry,
            )
        )
        samples = []
        request = CompileRequest(
            document=document_bytes,
            source_catalog=catalog,
            action_registry=registry,
        )
        for _ in range(self._scale(self.args.compile_samples)):
            start = time.perf_counter_ns()
            result = compile_document(request)
            elapsed = (time.perf_counter_ns() - start) / 1e6
            assert result.ok, [d.code for d in result.diagnostics]
            samples.append(elapsed)
        self.emit(
            _result(
                "compile-32-projection",
                "runtime.compiler.compile_document over the JSON document"
                " bytes with catalogs parsed once (cached catalogs)",
                "warm: in-process, cached catalogs",
                samples,
                notes=(
                    "The sample covers the seven stages from decoding to"
                    " the built plan; the catalogs stay parsed across"
                    " samples, which is the cached condition of the"
                    " target."
                ),
                target=TARGETS["compile-32-projection"],
            )
        )

        # Cold: a fresh interpreter per sample, so the number carries
        # process start, imports, catalog parsing, and one compile.
        program = (
            "import sys,time\n"
            "t0=time.perf_counter_ns()\n"
            "sys.path[:0]=[sys.argv[1],sys.argv[2]]\n"
            "from runtime.compiler import CompileRequest, compile_document\n"
            "from runtime.compiler.catalog import load_catalog\n"
            "catalog,registry=load_catalog(open(sys.argv[3],encoding='utf-8').read())\n"
            "document=sys.stdin.buffer.read()\n"
            "result=compile_document(CompileRequest(document=document,"
            " source_catalog=catalog, action_registry=registry))\n"
            "assert result.ok\n"
            "print((time.perf_counter_ns()-t0)/1e6)\n"
        )
        cold_samples = []
        for index in range(self._scale(self.args.cold_compile_samples)):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(_REPO / "python"),
                    str(_REPO),
                    str(_REPO / "fixtures" / "catalog.json"),
                ],
                input=document_bytes,
                capture_output=True,
                check=True,
            )
            cold_samples.append(float(completed.stdout))
        self.emit(
            _result(
                "compile-32-projection-cold-process",
                "one compile in a fresh interpreter: process start,"
                " imports, catalog parsing, and one compile_document call",
                "cold: fresh process per sample, kernel page cache warm",
                cold_samples,
                notes=(
                    "Context for the warm target above: a cold process"
                    " pays interpreter and import costs no in-process"
                    " compile sees. No section 19 target governs this"
                    " condition."
                ),
            )
        )

    # -- service commits --------------------------------------------------

    def bench_commits(self) -> None:
        import copy

        adapter = PlatformAdapter.with_root(self.root / "commit-bench")
        seed_sales_workspace(
            adapter, "commit-bench", script=LIGHT_SALES_SCRIPT, contract=LIGHT_CONTRACT
        )
        service = WorkspaceService.open(adapter, "commit-bench")
        try:
            durability = service.store.durability_facts()
            # Control feedback: one bound parameter.set through the
            # framed channel. The value alternates so every sample is a
            # real change, and the binding reserves a generation and
            # replaces the pending intent exactly as a real control
            # edit does (SPEC.md sections 6.2 and 9.3).
            revision = 1
            samples = []
            for index in range(self._scale(self.args.commit_samples)):
                value = 10.0 if index % 2 == 0 else 20.0
                start = time.perf_counter_ns()
                answer = framed(
                    service,
                    "commit-bench",
                    "workspace.apply",
                    expected={"state/minimum": revision},
                    operations=[
                        {"op": "parameter.set", "id": "minimum", "value": value}
                    ],
                    request_id=f"commit-bench-set-{index}",
                )
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert answer["ok"], answer["errors"]
                revision += 1
                samples.append(elapsed)
            self.emit(
                _result(
                    "control-feedback-commit",
                    "framed workspace.apply of one bound parameter.set"
                    " (the service side of a control edit)",
                    "warm: one owning service session, fsynced commits",
                    samples,
                    notes=(
                        "Round 1 measures the service side: the framed"
                        " round trip that validates, compiles the"
                        " candidate, reserves the generation of the"
                        " bound action, replaces its pending intent, and"
                        " commits durably. Native feedback latency is a"
                        " round 2 measurement. Model and Python"
                        " execution are absent from the sample."
                    ),
                    target=TARGETS["control-feedback-commit"],
                )
            )

            # Small committed patch plus its notification: the visible
            # update a host applies. The patch replaces a projection
            # title, a change that preserves state and controllers.
            snapshot = framed(
                service,
                "commit-bench",
                "workspace.inspect",
                request_id="commit-bench-snapshot",
            )
            assert snapshot["ok"], snapshot["errors"]
            projection = snapshot["result"]["document"]["projections"]["sales"]
            base = copy.deepcopy(projection)
            epoch = snapshot["result"]["epoch"]
            revision = int(
                snapshot["result"]["entityRevisions"]["projections/sales"]
            )
            patch_samples = []
            visible_samples = []
            for index in range(self._scale(self.args.patch_samples)):
                candidate = copy.deepcopy(base)
                candidate["title"] = (
                    "Sales" if index % 2 == 0 else "Sales quarterly"
                )
                before = service.commit_sequence
                start = time.perf_counter_ns()
                answer = framed(
                    service,
                    "commit-bench",
                    "workspace.apply",
                    expected={"projections/sales": revision},
                    operations=[
                        {
                            "op": "entity.replace",
                            "collection": "projections",
                            "id": "sales",
                            "value": candidate,
                        }
                    ],
                    request_id=f"commit-bench-patch-{index}",
                )
                patch_elapsed = (time.perf_counter_ns() - start) / 1e6
                assert answer["ok"], answer["errors"]
                revision += 1
                notification = framed(
                    service,
                    "commit-bench",
                    "workspace.subscribe",
                    payload={"afterSequence": before, "epoch": epoch},
                    request_id=f"commit-bench-changes-{index}",
                )
                visible_elapsed = (time.perf_counter_ns() - start) / 1e6
                assert notification["ok"], notification["errors"]
                assert notification["result"]["status"] == "changes"
                patch_samples.append(patch_elapsed)
                visible_samples.append(visible_elapsed)
            self.emit(
                _result(
                    "committed-patch",
                    "framed workspace.apply of one entity.replace"
                    " (projection title) with its candidate compilation",
                    "warm: one owning service session, fsynced commits",
                    patch_samples,
                    notes=(
                        "The commit alone. No section 19 target governs"
                        " the commit without its notification."
                    ),
                )
            )
            self.emit(
                _result(
                    "committed-patch-notification",
                    "the same committed patch plus the"
                    " workspace.subscribe answer a host applies to make"
                    " the change visible",
                    "warm: one owning service session, fsynced commits",
                    visible_samples,
                    notes=(
                        "Round 1 measures the service side of the"
                        " committed-patch target: apply round trip plus"
                        " the ordered notification that carries the"
                        " change to the visible interface. Python and"
                        " model time are absent by construction."
                    ),
                    target=TARGETS["committed-patch-notification"],
                )
            )
            self.fixture_sizes["durabilityFacts"] = durability
        finally:
            service.close()

    # -- artifact publication ---------------------------------------------

    def bench_publication(self) -> None:
        adapter = PlatformAdapter.with_root(self.root / "publish-bench")
        seed_sales_workspace(
            adapter,
            "publish-bench",
            script=LIGHT_SALES_SCRIPT,
            contract=LIGHT_CONTRACT,
        )
        service = WorkspaceService.open(adapter, "publish-bench")
        try:
            samples = []
            staged_bytes = None
            for index in range(self._scale(self.args.publication_samples)):
                admitted = framed(
                    service,
                    "publish-bench",
                    "action.invoke",
                    expected={"actions/analyze-sales": 1},
                    payload={"actionId": "analyze-sales"},
                    request_id=f"publish-bench-invoke-{index}",
                )
                assert admitted["ok"], admitted["errors"]
                run_id = admitted["result"]["run"]["runId"]
                outcome = service.launch_admitted_run(run_id)
                assert outcome.launched and outcome.state == "succeeded", (
                    outcome.refusal
                )
                if staged_bytes is None:
                    staged_bytes = sum(
                        Path(item["path"]).stat().st_size
                        for item in outcome.staged["outputs"].values()
                    )
                start = time.perf_counter_ns()
                publication = service.publish_run(run_id)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert publication.outcome == "current", (
                    publication.outcome,
                    publication.reason,
                )
                samples.append(elapsed)
            self.fixture_sizes["publicationStagedBytes"] = staged_bytes
            self.emit(
                _result(
                    "artifact-publication",
                    "WorkspaceService.publish_run of one succeeded run:"
                    " staged-output verification, immutable artifact"
                    " finalization with file and directory"
                    " synchronization, eligibility recheck, source"
                    " version allocation, and the receipted commit",
                    "warm: fsynced artifact and commit paths in the"
                    " owning session",
                    samples,
                    notes=(
                        "Each sample publishes a fresh run of the"
                        f" section 13.2 script shape; {staged_bytes}"
                        " bytes of staged Parquet output. The runner"
                        " process time is excluded: the target of"
                        " section 19 is the publication machinery, not"
                        " Python execution."
                    ),
                )
            )
        finally:
            service.close()

    # -- table paging ------------------------------------------------------

    def _page(
        self, service: WorkspaceService, request_id: str, sort: "list | None" = None,
        offset: int = 0,
    ) -> dict:
        answer = framed(
            service,
            "page-bench",
            "table.page",
            payload={
                "source": {"sourceId": "big", "version": 1},
                "sort": sort or [],
                "filters": [],
                "offset": offset,
                "limit": 50,
            },
            request_id=request_id,
        )
        assert answer["ok"], answer["errors"]
        return answer["result"]

    def bench_table_page(self) -> None:
        payload = big_table_bytes(self.args.table_rows)
        measured = seed_page_workspace(
            PlatformAdapter.with_root(self.root / "page-bench"), payload
        )
        self.fixture_sizes["tableRows"] = self.args.table_rows
        self.fixture_sizes["tableColumns"] = len(measured["columns"])
        self.fixture_sizes["tableArtifactBytes"] = len(payload)
        self.fixture_sizes["tableColumnTypes"] = [
            column["type"] for column in measured["columns"]
        ]
        del payload

        adapter = PlatformAdapter.with_root(self.root / "page-bench")
        service = WorkspaceService.open(adapter, "page-bench")
        try:
            # Warm: the service retains the requested batches after warmup.
            self._page(service, "page-bench-warmup")
            samples = []
            for index in range(self._scale(self.args.page_samples)):
                gc.collect()
                start = time.perf_counter_ns()
                page = self._page(service, f"page-bench-warm-{index}")
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert page["offset"] == 0 and len(page["rows"]) == 50
                samples.append(elapsed)
            self.emit(
                _result(
                    "table-page-first-warm",
                    "framed table.page at offset 0, limit 50, no filters"
                    " or sort",
                    "warm: requested table batches cached in the owning service",
                    samples,
                    notes="The service retains at most 256 MiB of Arrow buffers.",
                    target=TARGETS["table-page-first-warm"],
                )
            )

            samples = []
            for index in range(self._scale(self.args.page_samples)):
                service._table_cache.clear()
                gc.collect()
                start = time.perf_counter_ns()
                page = self._page(service, f"page-bench-miss-{index}")
                samples.append((time.perf_counter_ns() - start) / 1e6)
                assert len(page["rows"]) == 50
            self.emit(_result(
                "table-page-cache-miss", "unsorted first page with empty service cache",
                "warm: kernel page cache; empty service cache", samples,
                notes="Includes opening Parquet metadata and decoding the requested batches.",
            ))

            # Full-table sorting, reported separately per section 19.
            samples = []
            for index in range(self._scale(self.args.sort_samples)):
                service._table_cache.clear()
                gc.collect()
                start = time.perf_counter_ns()
                page = self._page(
                    service,
                    f"page-bench-sort-{index}",
                    sort=[{"column": "score", "direction": "desc"}],
                )
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert page["offset"] == 0 and len(page["rows"]) == 50
                samples.append(elapsed)
            self.emit(
                _result(
                    "table-page-full-sort",
                    "framed table.page at offset 0, limit 50, sorted by"
                    " the score column descending over every row",
                    "warm: artifact bytes in the kernel page cache",
                    samples,
                    notes=(
                        "Section 19 asks full-table sorting to be"
                        " reported separately; no target governs it."
                    ),
                )
            )
            samples = []
            for index in range(self._scale(self.args.page_samples)):
                start = time.perf_counter_ns()
                page = self._page(
                    service, f"page-bench-sort-cached-{index}",
                    offset=(index * 50) % max(1, self.args.table_rows - 49),
                    sort=[{"column": "score", "direction": "desc"}],
                )
                samples.append((time.perf_counter_ns() - start) / 1e6)
                assert len(page["rows"]) == 50
            self.fixture_sizes["tableRetainedCacheBytes"] = service._table_cache.bytes
            self.fixture_sizes["tableCacheBudgetBytes"] = service._table_cache.max_bytes
            self.emit(_result(
                "table-page-cached-sort", "successive sorted pages with cached table and row order",
                "warm: decoded table and sorted indices cached", samples,
                notes="Full-sort samples clear the service cache before each request.",
            ))
        finally:
            service.close()

        if not self.args.cold:
            self.emit(
                _result(
                    "table-page-first-cold",
                    "framed table.page at offset 0, limit 50",
                    "cold: kernel page cache dropped before each sample",
                    (),
                    skipped="--no-cold was given",
                )
            )
            return
        service = WorkspaceService.open(adapter, "page-bench")
        try:
            samples = []
            skipped = ""
            for index in range(self._scale(self.args.cold_page_samples)):
                dropped, reason = drop_page_cache()
                if not dropped:
                    skipped = f"drop_caches unavailable: {reason}"
                    break
                service._table_cache.clear()
                start = time.perf_counter_ns()
                page = self._page(service, f"page-bench-cold-{index}")
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert page["offset"] == 0 and len(page["rows"]) == 50
                samples.append(elapsed)
            self.emit(
                _result(
                    "table-page-first-cold",
                    "framed table.page at offset 0, limit 50",
                    "cold: kernel page cache dropped before each sample",
                    samples,
                    notes=(
                        "Each sample pays the disk read of the artifact"
                        " the warm condition avoided. No section 19"
                        " target governs the cold page."
                    ),
                    skipped=skipped,
                )
            )
        finally:
            service.close()

    # -- restoration -------------------------------------------------------

    def bench_restore(self) -> None:
        seed_five_pane_workspace(PlatformAdapter.with_root(self.root / "five-panes"))
        adapter = PlatformAdapter.with_root(self.root / "five-panes")

        def restore_once(request_id: str) -> float:
            start = time.perf_counter_ns()
            service = WorkspaceService.open(adapter, "five-panes")
            try:
                snapshot = framed(
                    service,
                    "five-panes",
                    "workspace.inspect",
                    request_id=request_id,
                )
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert snapshot["ok"], snapshot["errors"]
                document = snapshot["result"]["document"]
                assert len(document["projections"]) == 5
                assert (
                    snapshot["result"]["parameterStates"]["minimum"]["value"]
                    == 120.0
                )
                return elapsed
            finally:
                service.close()

        samples = [
            restore_once(f"five-panes-restore-{index}")
            for index in range(self._scale(self.args.restore_samples))
        ]
        self.emit(
            _result(
                "restore-five-panes",
                "WorkspaceService.open of the saved five-pane workspace"
                " plus the framed workspace.inspect snapshot: ownership"
                " lock, storage worker spawn, state load, validation,"
                " and the full snapshot",
                "warm: kernel page cache warm",
                samples,
                notes=(
                    "The round 1 service side of the restoration target:"
                    " every durable record and source reference of five"
                    " saved panes is loaded and verified without"
                    " executing project code. Native pane-controller"
                    " restoration is the round 2 measurement of section"
                    " 19; this number is the service portion beneath it."
                ),
                target=TARGETS["restore-five-panes"],
            )
        )

        if not self.args.cold:
            self.emit(
                _result(
                    "restore-five-panes-cold",
                    "the same reopen and snapshot",
                    "cold: kernel page cache dropped before each sample",
                    (),
                    skipped="--no-cold was given",
                )
            )
            return
        samples = []
        skipped = ""
        for index in range(self._scale(self.args.cold_restore_samples)):
            dropped, reason = drop_page_cache()
            if not dropped:
                skipped = f"drop_caches unavailable: {reason}"
                break
            samples.append(restore_once(f"five-panes-restore-cold-{index}"))
        self.emit(
            _result(
                "restore-five-panes-cold",
                "the same reopen and snapshot",
                "cold: kernel page cache dropped before each sample",
                samples,
                notes=(
                    "Each sample also re-reads the interpreter, Node,"
                    " and database pages the warm condition had cached."
                ),
                skipped=skipped,
            )
        )

    # -- driver ------------------------------------------------------------

    def run(self) -> None:
        print("benchmark: compiling", flush=True)
        self.bench_compile()
        print("benchmark: service commits", flush=True)
        self.bench_commits()
        print("benchmark: artifact publication", flush=True)
        self.bench_publication()
        print("benchmark: table paging", flush=True)
        self.bench_table_page()
        print("benchmark: restoration", flush=True)
        self.bench_restore()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def markdown_report(payload: dict) -> str:
    """The summary block the recorded document embeds verbatim."""

    lines = [
        "| Measurement | Condition | n | p50 (ms) | p95 (ms) | max (ms) | Target | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in payload["results"]:
        if "skipped" in result:
            lines.append(
                f"| {result['name']} | {result['condition']} | - | - | - | - |"
                f" - | skipped: {result['skipped']} |"
            )
            continue
        target = result.get("target")
        if target:
            lines.append(
                f"| {result['name']} | {result['condition']} | {result['n']} |"
                f" {result['p50']} | {result['p95']} | {result['max']} |"
                f" {target['target']} |"
                f" {'met' if target['met'] else 'missed'} |"
            )
        else:
            lines.append(
                f"| {result['name']} | {result['condition']} | {result['n']} |"
                f" {result['p50']} | {result['p95']} | {result['max']} |"
                " reported separately | - |"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Root handling and entry point
# ---------------------------------------------------------------------------


def benchmark_root(args: argparse.Namespace) -> Path:
    """The private data root, registered for Looper cleanup."""

    prefix = os.environ.get("LOOPER_RESOURCE_PREFIX", "melt")
    root = Path(args.root) if args.root else Path(tempfile.gettempdir()) / (
        f"{prefix}{BENCHMARK_ROOT_SUFFIX}"
    )
    _register_root_cleanup(root, prefix)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _register_root_cleanup(root: Path, prefix: str) -> None:
    cleanup_file = os.environ.get("LOOPER_RESOURCE_CLEANUP_FILE")
    if not cleanup_file:
        return
    path = Path(cleanup_file)
    marker = f"# round1-benchmark-root: {root}"
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
        + f"    *{shlex.quote(prefix)}{BENCHMARK_ROOT_SUFFIX}) rm -rf -- {quoted} ;;\n"
        + "  esac\n"
        + "fi\n",
        encoding="utf-8",
    )


def parse_args(argv: "Sequence[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/benchmark_round1.py",
        description=(
            "Record the round 1 performance measurements of SPEC.md"
            " section 19 (backlog task T023)."
        ),
    )
    parser.add_argument(
        "--root",
        help=(
            "private data root for the benchmark workspaces (default:"
            " $TMPDIR/$LOOPER_RESOURCE_PREFIX-round1-benchmark-root)"
        ),
    )
    parser.add_argument(
        "--output",
        help=(
            "write the raw JSON record here (default:"
            " build/benchmarks/round1-measurements.json, which is"
            " ignored)"
        ),
    )
    parser.add_argument("--compile-samples", type=int, default=200)
    parser.add_argument("--cold-compile-samples", type=int, default=10)
    parser.add_argument("--commit-samples", type=int, default=100)
    parser.add_argument("--patch-samples", type=int, default=100)
    parser.add_argument("--publication-samples", type=int, default=30)
    parser.add_argument("--page-samples", type=int, default=30)
    parser.add_argument("--sort-samples", type=int, default=15)
    parser.add_argument("--cold-page-samples", type=int, default=5)
    parser.add_argument("--restore-samples", type=int, default=15)
    parser.add_argument("--cold-restore-samples", type=int, default=5)
    parser.add_argument("--table-rows", type=int, default=TABLE_ROWS)
    parser.add_argument(
        "--samples-scale",
        type=float,
        default=1.0,
        help="multiply every sample count by this factor",
    )
    parser.add_argument(
        "--no-cold",
        action="store_true",
        help="skip the measurements that drop the kernel page cache",
    )
    args = parser.parse_args(argv)
    args.cold = not args.no_cold
    return args


def main(argv: "Sequence[str] | None" = None) -> int:
    args = parse_args(argv)
    root = benchmark_root(args)
    print(f"benchmark: private data root {root}", flush=True)

    facts = machine_facts(_REPO, root)
    benchmarker = Benchmarker(root, args)
    benchmarker.run()

    payload = {
        "schema": "melt/round1-performance-record/1",
        "specification": "SPEC.md section 19",
        "task": "T023",
        "facts": facts,
        "targets": TARGETS,
        "fixtureSizes": benchmarker.fixture_sizes,
        "results": benchmarker.results,
        "reproduce": (
            "uv run --locked python3 scripts/benchmark_round1.py"
            " (the pinned environment comes from `uv sync --locked`)"
        ),
    }
    output = Path(
        args.output
        or (_REPO / "build" / "benchmarks" / "round1-measurements.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"\nbenchmark: raw JSON written to {output}", flush=True)
    print("\n" + markdown_report(payload) + "\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
