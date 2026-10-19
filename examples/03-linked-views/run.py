"""Select a supplier and add another view of the saved selection."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Demo, output_directory, parquet_from_csv, parser, run

HERE = Path(__file__).resolve().parent


def show(snapshot):
    semantic = snapshot["semantic"]
    for projection, view in semantic["observers"].items():
        if view["slot"] == "highlight":
            print(f"Graph highlights: {', '.join(view['highlightedNodes'])}")
        elif view.get("display") == "record":
            print(f"{projection}: " + json.dumps(view["record"]["cells"], ensure_ascii=False))


def main():
    cli = parser(__doc__)
    cli.add_argument("--key", choices=["S-42", "S-73"], default="S-73", help="the second supplier to select")
    args = cli.parse_args()
    output = output_directory(args.output, "linked-views")
    parquet_from_csv(HERE / "suppliers.csv", output / "suppliers.parquet")
    demo = Demo(output, "linked-views")
    demo.call("create", "--language", "melt/semantic-draft-1")
    demo.call("import", "--source", "supplier-records", "--file", output / "suppliers.parquet")
    demo.call("import", "--source", "supplier-network", "--file", HERE / "suppliers.graph.json")
    demo.call("apply", "--operations", HERE / "views.json")
    demo.call("select", "--projection", "network", "--key", "S-42")
    before = demo.call("inspect")["result"]
    print("Select S-42\n")
    show(before)
    demo.call("apply", "--operations", HERE / "add-inspector.json")
    extended = demo.call("inspect")["result"]
    assert extended["semantic"]["referenceStates"] == before["semantic"]["referenceStates"]
    print("\nAdd a risk inspector. The saved selection stays unchanged.\n")
    show(extended)
    demo.call("select", "--projection", "network", "--key", args.key)
    restored = demo.call("inspect")["result"]
    print(f"\nSelect {args.key}, then reopen the workspace\n")
    show(restored)
    assert restored["semantic"]["observers"]["inspector"]["record"]["cells"]["supplier_id"] == args.key
    assert restored["semantic"]["observers"]["risk-inspector"]["record"]["cells"]["supplier_id"] == args.key
    assert not restored["document"]["actions"]
    subprocess.run(["node", str(HERE / "render.mjs"), str(HERE / "suppliers.graph.json"),
                    str(output / "network.svg")], check=True, timeout=30)
    (output / "selection.json").write_text(json.dumps(restored["semantic"], indent=2) + "\n")
    print("\nBoth inspectors follow the same selection. No project action or model call is needed.")
    print("Open network.svg for the graph. Selection and highlighting appear in the terminal output.")
    demo.finish()


if __name__ == "__main__":
    run(main)
