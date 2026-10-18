"""Python SDK for Melt project scripts (SPEC.md section 13.2).

The SDK exposes three names to a running script:

- `ui.params`: the read-only captured parameter values;
- `ui.input_path(name)`: the path of one captured input artifact;
- `ui.output(name, value)`: stage one declared output.

`ui.output` accepts a pandas DataFrame for a table, a matplotlib Figure
for an image, and a `str` for text. It materializes Parquet, PNG, or
UTF-8 bytes inside the private staging directory of the run, and
refuses duplicate and undeclared names. The service validates the
actual files independently before anything publishes.

The first SDK separates definition authoring from run output. A run
cannot change its own action, bindings, permissions, or layout.
"""

from __future__ import annotations

from melt.sdk.context import (
    OUTPUTS_DIRECTORY_NAME,
    STAGED_INDEX_NAME,
    STAGED_INDEX_VERSION,
    RunContext,
    active_context,
    read_staged_index,
    run_context,
)
from melt.sdk.errors import MeltSDKError
from melt.sdk.materialize import (
    IMAGE_KIND,
    OUTPUT_KINDS,
    StagedOutput,
    TABLE_KIND,
    TEXT_KIND,
    stage_value,
)
from melt.sdk.ui import CapturedParams, UI, ui

__all__ = [
    "IMAGE_KIND",
    "OUTPUT_KINDS",
    "OUTPUTS_DIRECTORY_NAME",
    "RunContext",
    "STAGED_INDEX_NAME",
    "STAGED_INDEX_VERSION",
    "StagedOutput",
    "TABLE_KIND",
    "TEXT_KIND",
    "CapturedParams",
    "MeltSDKError",
    "UI",
    "active_context",
    "read_staged_index",
    "run_context",
    "stage_value",
    "ui",
]
