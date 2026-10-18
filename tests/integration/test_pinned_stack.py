"""Integration checks over the pinned data stack (backlog task T001).

The tests prove that pydantic, pyarrow with pandas, and matplotlib work
together headless on Linux. No macOS or Apple tool takes part, and no
display server is needed.
"""

from __future__ import annotations

import pydantic
import pytest


class _Sample(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    workspace_id: str
    minimum: float


def test_pydantic_round_trip():
    sample = _Sample(workspace_id="sales-demo", minimum=0)
    restored = _Sample.model_validate_json(sample.model_dump_json())
    assert restored == sample


def test_pydantic_rejects_unknown_fields():
    with pytest.raises(pydantic.ValidationError):
        _Sample.model_validate(
            {"workspaceId": "sales-demo", "minimum": 0, "surprise": True}
        )


def test_parquet_round_trip(tmp_path):
    pandas = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")

    frame = pandas.DataFrame(
        {"month": ["2026-01", "2026-01", "2026-02"], "revenue": [100.0, 300.0, 200.0]}
    )
    path = tmp_path / "sales.parquet"
    frame.to_parquet(path, engine="pyarrow")

    restored = pandas.read_parquet(path, engine="pyarrow")
    pandas.testing.assert_frame_equal(restored, frame)


def test_matplotlib_writes_png_without_display(tmp_path):
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg", force=True)
    pyplot = pytest.importorskip("matplotlib.pyplot")

    figure, axis = pyplot.subplots()
    axis.plot(["2026-01", "2026-02"], [300.0, 200.0])
    axis.set_ylabel("Revenue")
    path = tmp_path / "monthly.png"
    figure.savefig(path, format="png")
    pyplot.close(figure)

    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 0
