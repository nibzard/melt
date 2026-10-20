"""Prepare an unexecuted sales workspace for the native hero check."""
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))
from common import Demo, parquet_from_csv

output = Path(sys.argv[1])
output.mkdir(parents=True)
here = REPO / "examples/02-sales-analysis"
parquet_from_csv(here / "sales.csv", output / "sales.parquet")
# Hold a real computation open long enough to exercise layout during execution.
script = output / "analyze.py"
script.write_text("import time\ntime.sleep(1)\n" + (here / "analyze.py").read_text())
demo = Demo(output, "native-sales")
demo.call("create")
demo.call("import", "--source", "sales-input", "--file", output / "sales.parquet")
demo.call("apply", "--operations", here / "parameter.json")
registration = demo.call("register", "--script", script, "--contract", here / "contract.json")["result"]["registrationId"]
demo.call("apply", "--operations", here / "views.json", "--registration", registration)
assert demo.call("runs")["runs"] == []
(output / "Sales.meltapp").write_text(json.dumps({"format": "melt-launcher/v1", "root": str(demo.root), "workspaceId": demo.workspace}))
