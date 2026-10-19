"""Run sales analysis, change a parameter, and publish a new table and plot."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Demo, output_directory, parquet_from_csv, parser, run

HERE = Path(__file__).resolve().parent


def main():
    cli = parser(__doc__)
    cli.add_argument("--minimum", type=float, default=200, help="minimum revenue, above 0 and at most 300")
    args = cli.parse_args()
    if not 0 < args.minimum <= 300:
        cli.error("--minimum must be above 0 and at most 300")
    output = output_directory(args.output, "sales-analysis")
    parquet_from_csv(HERE / "sales.csv", output / "sales.parquet")
    demo = Demo(output, "sales-analysis")
    demo.call("create")
    demo.call("import", "--source", "sales-input", "--file", output / "sales.parquet")
    demo.call("apply", "--operations", HERE / "parameter.json")
    registration = demo.call("register", "--script", HERE / "analyze.py",
                             "--contract", HERE / "contract.json")["result"]["registrationId"]
    demo.call("apply", "--operations", HERE / "views.json", "--registration", registration)
    print("Granting project execution permission to this example's analysis script.")
    demo.call("approve", "--registration", registration)
    demo.call("invoke", "--action", "analyze-sales", "--launch", "--publish")
    print("\nBefore: minimum revenue = 0\n")
    before = demo.table("analyze-sales-filtered", "before.csv")
    demo.call("artifact", "--source", "analyze-sales-monthly", "--output", output / "before.png")
    demo.call("set", "--parameter", "minimum", "--value", str(args.minimum),
              "--dispatch", "--debounce-wait", "300", "--launch", "--publish")
    print(f"\nAfter: minimum revenue = {args.minimum:g}\n")
    after = demo.table("analyze-sales-filtered", "after.csv")
    demo.call("artifact", "--source", "analyze-sales-monthly", "--output", output / "after.png")
    assert len(after["rows"]) == sum(row[1] >= args.minimum for row in before["rows"])
    print("\nThe parameter binding reruns Python and publishes both outputs together.")
    print("Open before.png and after.png to compare the monthly totals.")
    demo.finish()


if __name__ == "__main__":
    run(main)
