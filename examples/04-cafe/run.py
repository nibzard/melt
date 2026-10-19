"""Build Little Hours with the real runtime, then prove restoration."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Demo, output_directory, parquet_from_csv, parser, run

HERE = Path(__file__).resolve().parent


def create(collection, identity, value):
    return {"op": "entity.create", "collection": collection, "id": identity, "value": value}


def split(first, second, axis="horizontal", ratio=.5):
    return {"kind": "split", "axis": axis, "ratio": ratio, "first": first, "second": second}


def pane(identity):
    return {"kind": "pane", "projection": identity}


def main():
    args = parser(__doc__).parse_args()
    output = output_directory(args.output, "cafe")
    parquet_from_csv(HERE / "days.csv", output / "days.parquet")
    demo = Demo(output, "little-hours")
    demo.call("create", "--language", "melt/semantic-draft-1", "--title", "Little Hours")
    demo.call("import", "--source", "days", "--file", output / "days.parquet")
    definitions = {
        "bean_increase": {"type": "integer", "default": 0, "nullable": False, "constraints": {"minimum": 0, "maximum": 100}},
        "open_mondays": {"type": "boolean", "default": True, "nullable": False, "constraints": {}},
    }
    outputs = {"daily": "table", "profit": "image", "summary": "text", "break_even": "text"}
    contract = {"parameters": {key: {k: v for k, v in value.items() if k != "default"} for key, value in definitions.items()},
                "inputs": {"days": "table"}, "outputs": outputs}
    registration = demo.call("register", "--script", HERE / "analyze.py", "--contract", json.dumps(contract))["result"]["registrationId"]
    operations = [create("parameters", key, value) for key, value in definitions.items()]
    operations.append(create("actions", "analyze-cafe", {
        "implementation": {"registrationId": registration},
        "parameters": {key: {"parameter": key} for key in definitions},
        "inputs": {"days": {"sourceId": "days", "version": 1}},
        "outputs": outputs, "outputGroup": "cafe-results",
    }))
    demo.call("apply", "--operations", json.dumps(operations))
    snapshot = demo.call("inspect")["result"]
    incarnation = snapshot["semantic"]["outputs"]["analyze-cafe"]["incarnation"]

    def projection(identity, kind, output_name, controls=None):
        config = {"table": {"columns": None, "selection": "none"}, "plot": {"fit": "contain"},
                  "document": {"format": "markdown"}}[kind]
        return create("projections", identity, {"kind": kind, "title": identity.replace("-", " ").title(),
            "source": {"actionOutput": {"action": "analyze-cafe", "output": output_name, "incarnation": incarnation}, "follow": "current"},
            "config": config, "controls": controls or []})

    controls = [{"id": "cost", "kind": "number", "label": "Bean costs (%)", "parameter": "bean_increase"},
                {"id": "mondays", "kind": "toggle", "label": "Open Mondays", "parameter": "open_mondays"},
                {"id": "run", "kind": "button", "label": "Run scenario"}]
    base_layout = split(pane("summary"), split(pane("profit"), pane("daily"), "vertical"), ratio=.35)
    operations = [projection("summary", "document", "summary", controls), projection("profit", "plot", "profit"),
        projection("daily", "table", "daily"),
        create("bindings", "run-cafe", {"event": {"kind": "control.activate", "projection": "summary", "control": "run"},
               "effect": {"kind": "action.invoke", "action": "analyze-cafe"}, "debounceMs": 0}),
        {"op": "layout.replace", "value": {"root": base_layout, "hidden": []}}]
    demo.call("apply", "--operations", json.dumps(operations))
    print("Granting project execution permission to the included café calculation.")
    demo.call("approve", "--registration", registration)
    demo.call("activate", "--projection", "summary", "--control", "run", "--launch", "--publish")
    # Each call is a fresh process, so every read also exercises restart.
    demo.call("artifact", "--source", "analyze-cafe-summary", "--output", output / "baseline.md")
    demo.call("set", "--parameter", "bean_increase", "--value", "30")
    demo.call("set", "--parameter", "open_mondays", "--value", "false")
    demo.call("activate", "--projection", "summary", "--control", "run", "--launch", "--publish")
    before = demo.call("inspect")["result"]
    before_runs = demo.call("runs")["runs"]
    # Add a view over an already published output without running code.
    patch = [projection("break-even", "document", "break_even"),
             {"op": "layout.replace", "value": {"root": split(
                 split(pane("summary"), pane("break-even"), "vertical"),
                 split(pane("profit"), pane("daily"), "vertical"), ratio=.35), "hidden": []}}]
    (output / "add-break-even.json").write_text(json.dumps(patch, indent=2) + "\n")
    demo.call("apply", "--operations", json.dumps(patch))
    after = demo.call("inspect")["result"]
    assert before["parameterStates"] == after["parameterStates"]
    assert before_runs == demo.call("runs")["runs"]
    demo.table("analyze-cafe-daily", "scenario.csv")
    for name, suffix in [("summary", "md"), ("break_even", "md"), ("profit", "png")]:
        demo.call("artifact", "--source", f"analyze-cafe-{name}", "--output", output / f"{name}.{suffix}")
    assert "€3,280.00" in (output / "baseline.md").read_text()
    assert "€2,836.00" in (output / "summary.md").read_text()
    descriptor = {"format": "melt-launcher/v1", "workspaceId": demo.workspace, "root": str(demo.root.resolve())}
    (output / "Little Hours.meltapp").write_text(json.dumps(descriptor, indent=2) + "\n")
    (output / "restored.json").write_text(json.dumps(after, indent=2) + "\n")
    print("\nAdded break-even without execution. Reopened with costs +30% and Mondays closed.")
    print("Open Little Hours.meltapp with the native Melt host on macOS.")
    demo.finish()


if __name__ == "__main__":
    run(main)
