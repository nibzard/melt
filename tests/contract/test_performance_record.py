"""The round 1 performance record (T023, SPEC.md section 19).

The suite keeps the recorded measurements honest at the edges the
specification pins:

- The compile fixture really is a 32-projection document that compiles
  against the cached fixture catalogs, so the compile number measures
  the saturated projection bound rather than a toy.
- The five-pane fixture compiles and holds five projections, so the
  restoration number measures five saved panes.
- The paging fixture is byte-deterministic for the seed and measures
  exactly 20 columns of the declared wire types.
- The percentile is the nearest-rank method the record declares.
- The committed record and report name every section 19 target and
  every measurement dimension, and the record's verdicts agree with
  its own numbers.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from runtime.compiler import CompileRequest, compile_document
from runtime.compiler.catalog import load_catalog

REPO = Path(__file__).resolve().parents[2]
REPORT = REPO / "docs" / "performance" / "round-1-linux.md"
RECORD = REPO / "docs" / "performance" / "round-1-linux.json"

COLUMN_TYPES = {
    "string",
    "bool",
    "int64",
    "float64",
    "decimal",
    "date",
    "timestamp",
}


def benchmark_module():
    """Load scripts/benchmark_round1.py as a module."""

    path = REPO / "scripts" / "benchmark_round1.py"
    spec = importlib.util.spec_from_file_location("benchmark_round1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compiled(document: dict):
    catalog, registry = load_catalog(
        (REPO / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    )
    return compile_document(
        CompileRequest(
            document=json.dumps(document).encode("utf-8"),
            source_catalog=catalog,
            action_registry=registry,
        )
    )


def test_the_compile_fixture_saturates_the_projection_bound():
    module = benchmark_module()
    document = module.build_thirty_two_projection_document()

    assert len(document["projections"]) == 32
    assert len(document["actions"]) == 8
    assert len(document["bindings"]) == 16

    result = compiled(document)
    assert result.ok, [(item.code, item.path) for item in result.diagnostics]
    # Every projection lands exactly once in the validated layout.
    placements = result.plan.layout.placements
    assert set(placements) == set(document["projections"])
    assert all(item.placement == "tree" for item in placements.values())


def test_the_five_pane_fixture_holds_five_saved_panes():
    module = benchmark_module()
    document = module.build_five_pane_document()

    assert len(document["projections"]) == 5
    result = compiled(document)
    assert result.ok, [(item.code, item.path) for item in result.diagnostics]


def test_the_paging_fixture_is_deterministic_and_twenty_columns_wide():
    module = benchmark_module()

    first = module.big_table_bytes(250)
    second = module.big_table_bytes(250)
    assert first == second

    measured = module.measure_artifact(
        first, kind="table", media_type="application/vnd.apache.parquet"
    )
    columns = measured["columns"]
    assert len(columns) == module.TABLE_COLUMNS == 20
    assert {column["type"] for column in columns} <= COLUMN_TYPES
    # The nullable columns of the fixture keep the null paths alive.
    assert any(column["nullable"] for column in columns)


def test_percentiles_use_the_nearest_rank_method():
    module = benchmark_module()
    samples = list(range(1, 21))

    assert module.percentile(samples, 0.50) == 10
    assert module.percentile(samples, 0.95) == 19
    assert module.percentile(samples, 0.05) == 1
    # A single sample is its own percentile at every rank.
    assert module.percentile([7], 0.95) == 7


def test_the_committed_record_covers_every_section_19_target():
    record = json.loads(RECORD.read_text(encoding="utf-8"))

    assert record["specification"] == "SPEC.md section 19"
    names = {result["name"] for result in record["results"]}
    for expected in (
        "compile-32-projection",
        "control-feedback-commit",
        "committed-patch-notification",
        "artifact-publication",
        "table-page-first-warm",
        "table-page-first-cold",
        "table-page-full-sort",
        "restore-five-panes",
    ):
        assert expected in names

    # Every target comparison the record reports agrees with its own
    # p95 and the bound of the specification target.
    for result in record["results"]:
        target = result.get("target")
        if target is None:
            continue
        bound = record["targets"][result["name"]]["p95Ms"]
        assert target["met"] == (result["p95"] < bound)
        assert target["target"] == record["targets"][result["name"]]["target"]

    # The four measured dimensions and the warm and cold conditions
    # are all present in one recorded run.
    conditions = " ".join(result["condition"] for result in record["results"])
    assert "warm" in conditions
    assert "cold" in conditions


def test_the_committed_report_records_the_measurements():
    report = REPORT.read_text(encoding="utf-8")

    # Every section 19 target, verbatim.
    for phrase in (
        "p95 below 50 ms",
        "p95 below 100 ms",
        "below 2 seconds",
        "p95 below 300 ms",
        "1 million rows",
    ):
        assert phrase in report

    # Every round 1 measurement dimension.
    for word in ("ompil", "ommit", "ublication", "able pag", "estor"):
        assert word in report

    # The declared environment and the deviation from the 16 GiB class.
    assert "16 GiB" in report
    assert "MemTotal" in report
