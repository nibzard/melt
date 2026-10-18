"""The `ui` object one project script sees (SPEC.md section 13.2).

```python
import pandas as pd
import matplotlib.pyplot as plt
from melt import ui

sales = pd.read_parquet(ui.input_path("sales"))
selected = sales[sales["revenue"] >= ui.params["minimum"]]
ui.output("filtered", selected)
```

The module holds one `ui` instance. The runner binds it to the active
run context before it executes the script, so the same import works in
every environment that holds the package. A run cannot change its own
action, bindings, permissions, or layout: `ui` exposes captured inputs,
captured parameters, and output staging, nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Iterator

from melt.sdk.context import active_context
from melt.sdk.errors import MeltSDKError
from melt.sdk.materialize import StagedOutput

_READ_ONLY_MESSAGE = (
    "captured parameters are read-only; the manifest captured them at"
    " admission and a run cannot change them"
)


class CapturedParams(Mapping):
    """A read-only view of the captured parameter values.

    Reads behave like a plain mapping. Every mutation raises, so a
    script cannot mistake its own copy for parameter state.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, object]):
        self._values = dict(values)

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"CapturedParams({self._values!r})"

    def __setitem__(self, key: str, value: object) -> None:
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def __delitem__(self, key: str) -> None:
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def clear(self) -> None:
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def pop(self, *args: object) -> object:
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def popitem(self) -> "tuple[str, object]":
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def update(self, *args: object, **kwargs: object) -> None:
        raise MeltSDKError(_READ_ONLY_MESSAGE)

    def setdefault(self, key: str, default: object = None) -> object:
        raise MeltSDKError(_READ_ONLY_MESSAGE)


class UI:
    """The SDK surface of one run.

    Every method resolves the active run context, so a stale reference
    cannot outlive the run that created it.
    """

    __slots__ = ()

    @property
    def params(self) -> CapturedParams:
        """The captured parameter values of this run, read-only."""

        return CapturedParams(active_context().parameters)

    def input_path(self, name: str) -> Path:
        """Return the path of one captured input artifact."""

        return active_context().input_path(name)

    def output(self, name: str, value: object) -> StagedOutput:
        """Stage one declared output of this run."""

        return active_context().stage(name, value)


ui = UI()
"""The one `ui` instance a project script imports."""


__all__ = ["CapturedParams", "UI", "ui"]
