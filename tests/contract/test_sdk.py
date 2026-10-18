"""Python SDK contract (T015, SPEC.md section 13.2).

`ui.params`, `ui.input_path`, and `ui.output` are the whole surface a
project script sees. The tests prove each rule of the contract:

- captured parameters are read-only;
- input paths come from the manifest alone;
- `ui.output` materializes Parquet, PNG, and UTF-8 bytes;
- duplicate and undeclared names are refused;
- values that do not match the declared kind are refused;
- staged bytes stay inside the service limit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest
from matplotlib.figure import Figure

import melt.sdk.context as context_module
from melt import ui
from melt.sdk import (
    MeltSDKError,
    StagedOutput,
    read_staged_index,
    run_context,
)


@pytest.fixture
def case_directory(tmp_path: Path) -> Path:
    """One staging directory with one captured input artifact."""

    input_file = tmp_path / "captured.parquet"
    frame = pd.DataFrame(
        {
            "month": ["2026-01", "2026-01", "2026-02"],
            "revenue": [100.0, 300.0, 200.0],
        }
    )
    frame.to_parquet(input_file, index=False)
    return tmp_path


def bind(case_directory: Path, **overrides):
    """Open one run context around the body of a test."""

    fields = {
        "run_id": "run-0001",
        "parameters": {"minimum": 200},
        "inputs": {"sales": case_directory / "captured.parquet"},
        "outputs": {"filtered": "table", "monthly": "image", "summary": "text"},
        "staging": case_directory / "staging",
    }
    fields.update(overrides)
    return run_context(**fields)


def test_ui_outside_a_run_reports_no_active_run():
    with pytest.raises(MeltSDKError, match="no run is active"):
        ui.params
    with pytest.raises(MeltSDKError, match="no run is active"):
        ui.input_path("sales")
    with pytest.raises(MeltSDKError, match="no run is active"):
        ui.output("filtered", "not a table")


def test_captured_parameters_are_read_only(case_directory: Path):
    with bind(case_directory):
        captured = ui.params
        assert dict(captured) == {"minimum": 200}
        assert len(captured) == 1
        assert list(captured) == ["minimum"]
        assert captured["minimum"] == 200
        assert "minimum" in captured
        with pytest.raises(KeyError):
            captured["maximum"]
        for mutate in (
            lambda: captured.__setitem__("minimum", 0),
            lambda: captured.__delitem__("minimum"),
            captured.clear,
            lambda: captured.pop("minimum"),
            captured.popitem,
            lambda: captured.update({"minimum": 0}),
            lambda: captured.setdefault("maximum", 0),
        ):
            with pytest.raises(MeltSDKError, match="read-only"):
                mutate()
        # The context itself still holds the captured value.
        assert ui.params["minimum"] == 200


def test_input_paths_come_from_the_manifest(case_directory: Path):
    with bind(case_directory) as context:
        assert ui.input_path("sales") == case_directory / "captured.parquet"
        with pytest.raises(MeltSDKError, match="no captured input named 'other'"):
            ui.input_path("other")
        context.inputs["sales"] = case_directory / "missing.parquet"
        with pytest.raises(MeltSDKError, match="has no file at"):
            ui.input_path("sales")


def test_a_table_output_materializes_parquet(case_directory: Path):
    frame = pd.DataFrame(
        {"month": ["2026-01", "2026-02"], "revenue": [300.0, 200.0]}
    )
    with bind(case_directory) as context:
        staged = ui.output("filtered", frame)
        assert isinstance(staged, StagedOutput)
        assert staged.kind == "table"
        assert staged.mediaType == "application/vnd.apache.parquet"
        assert staged.path.name == "filtered.parquet"
        assert staged.path.is_file()
        payload = staged.path.read_bytes()
        assert staged.byteLength == len(payload)
        assert staged.sha256 == "sha256:" + hashlib.sha256(payload).hexdigest()
        assert payload[:4] == b"PAR1" and payload[-4:] == b"PAR1"
        reread = pd.read_parquet(staged.path)
        assert reread.equals(frame)


def test_a_table_output_keeps_a_named_index(case_directory: Path):
    frame = pd.DataFrame(
        {"revenue": [300.0, 200.0]}, index=pd.Index(["2026-01", "2026-02"], name="month")
    )
    with bind(case_directory):
        staged = ui.output("filtered", frame)
        # A named index carries data, so the Parquet file keeps it as a
        # column instead of dropping it silently. pandas restores it as
        # the index on reread; the file itself carries both columns.
        import pyarrow.parquet as pq

        names = pq.ParquetFile(staged.path).schema_arrow.names
        assert sorted(names) == ["month", "revenue"]


def test_an_image_output_materializes_png(case_directory: Path):
    figure = Figure()
    axis = figure.subplots()
    axis.plot([1, 2, 3], [300.0, 200.0, 100.0])
    with bind(case_directory) as context:
        staged = ui.output("monthly", figure)
        assert staged.kind == "image"
        assert staged.mediaType == "image/png"
        assert staged.path.name == "monthly.png"
        payload = staged.path.read_bytes()
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
        assert staged.byteLength == len(payload)


def test_a_text_output_materializes_utf_8_bytes(case_directory: Path):
    text = "Umsätze 2026 — Januar bis Februar\n"
    with bind(case_directory) as context:
        staged = ui.output("summary", text)
        assert staged.kind == "text"
        assert staged.mediaType == "text/plain"
        assert staged.path.name == "summary.txt"
        assert staged.path.read_bytes() == text.encode("utf-8")


def test_duplicate_output_names_are_refused(case_directory: Path):
    frame = pd.DataFrame({"value": [1.0]})
    with bind(case_directory) as context:
        first = ui.output("filtered", frame)
        with pytest.raises(MeltSDKError, match="staged already"):
            ui.output("filtered", frame)
        # The first staging stays the recorded one.
        assert context.staged["filtered"] is first


def test_undeclared_output_names_are_refused(case_directory: Path):
    frame = pd.DataFrame({"value": [1.0]})
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match="not declared") as failure:
            ui.output("quarterly", frame)
        # The message names every declared output.
        assert "filtered" in str(failure.value)
        assert "monthly" in str(failure.value)


@pytest.mark.parametrize(
    "name, value, match",
    [
        ("filtered", "a string", "requires a pandas DataFrame"),
        ("filtered", [1, 2, 3], "requires a pandas DataFrame"),
        ("filtered", None, "requires a pandas DataFrame"),
    ],
)
def test_table_outputs_refuse_non_frames(
    case_directory: Path, name: str, value: object, match: str
):
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match=match):
            ui.output(name, value)


def test_a_series_is_refused_with_a_repair_hint(case_directory: Path):
    frame = pd.DataFrame(
        {"month": ["2026-01", "2026-02"], "revenue": [300.0, 200.0]}
    )
    series = frame.groupby("month")["revenue"].sum()
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match="reset_index"):
            ui.output("filtered", series)


def test_an_axes_is_refused_with_a_repair_hint(case_directory: Path):
    figure = Figure()
    axis = figure.subplots()
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match="Figure, not an Axes"):
            ui.output("monthly", axis)


@pytest.mark.parametrize(
    "value", ["not a figure", 3, None, [[1, 2], [3, 4]]]
)
def test_image_outputs_refuse_non_figures(case_directory: Path, value: object):
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match="requires a matplotlib Figure"):
            ui.output("monthly", value)


@pytest.mark.parametrize("value", [b"raw bytes", 42, None, ["lines"]])
def test_text_outputs_refuse_non_strings(case_directory: Path, value: object):
    with bind(case_directory):
        with pytest.raises(MeltSDKError, match="requires a str"):
            ui.output("summary", value)


def test_values_must_match_the_declared_kind(case_directory: Path):
    frame = pd.DataFrame({"value": [1.0]})
    figure = Figure()
    with bind(case_directory):
        # A table value for an image slot and an image value for a text
        # slot are both refused, even though each value is valid for its
        # own kind.
        with pytest.raises(MeltSDKError, match="matplotlib Figure"):
            ui.output("monthly", frame)
        with pytest.raises(MeltSDKError, match="requires a str"):
            ui.output("summary", figure)


def test_staged_bytes_stay_inside_the_service_limit(
    case_directory: Path, monkeypatch: pytest.MonkeyPatch
):
    from melt.schema.limits import ServiceLimits

    monkeypatch.setattr(
        context_module,
        "DEFAULT_LIMITS",
        ServiceLimits(staged_output_bytes_per_run=8),
    )
    with bind(case_directory) as context:
        with pytest.raises(MeltSDKError, match="exceed the allowed bound"):
            ui.output("summary", "more than eight bytes")
        # The refused file leaves nothing behind, staged or on disk.
        assert context.staged == {}
        assert not (context.outputs_directory / "summary.txt").exists()
        ui.output("summary", "eight bt")
        assert context.staged_bytes() == 8


def test_missing_outputs_are_named(case_directory: Path):
    frame = pd.DataFrame({"value": [1.0]})
    with bind(case_directory) as context:
        assert context.missing_outputs() == ["filtered", "monthly", "summary"]
        ui.output("filtered", frame)
        assert context.missing_outputs() == ["monthly", "summary"]


def test_the_staged_index_reports_every_staged_output(case_directory: Path):
    frame = pd.DataFrame({"month": ["2026-01"], "revenue": [300.0]})
    figure = Figure()
    with bind(case_directory) as context:
        ui.output("filtered", frame)
        ui.output("monthly", figure)
        index = context.index_document()
        assert index["indexVersion"] == 1
        assert index["runId"] == "run-0001"
        assert sorted(index["outputs"]) == ["filtered", "monthly"]
        entry = index["outputs"]["filtered"]
        assert entry["kind"] == "table"
        assert entry["sha256"].startswith("sha256:")
        assert Path(entry["path"]).is_file()
        assert entry["byteLength"] == Path(entry["path"]).stat().st_size
        # The index file itself lands in the staging directory.
        assert read_staged_index(case_directory / "staging") is None
        written = context.write_index()
        assert written.name == "staged-outputs.json"
        assert read_staged_index(case_directory / "staging") == index


def test_nested_run_contexts_restore_the_previous_run(case_directory: Path):
    with bind(case_directory, parameters={"minimum": 0}) as outer:
        assert ui.params["minimum"] == 0
        with bind(case_directory, parameters={"minimum": 200}) as inner:
            assert inner.runId == "run-0001"
            assert ui.params["minimum"] == 200
        assert ui.params["minimum"] == 0
        assert dict(outer.parameters) == {"minimum": 0}


def test_unknown_output_kinds_are_refused_at_binding(case_directory: Path):
    with pytest.raises(MeltSDKError, match="unknown output kind"):
        with bind(case_directory, outputs={"filtered": "movie"}):
            pass
