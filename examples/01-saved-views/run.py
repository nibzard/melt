"""Import a table and a note, then reopen their saved views."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Demo, output_directory, parquet_from_csv, parser, run

HERE = Path(__file__).resolve().parent


def main():
    args = parser(__doc__).parse_args()
    output = output_directory(args.output, "saved-views")
    parquet_from_csv(HERE / "expenses.csv", output / "expenses.parquet")
    demo = Demo(output, "saved-views")
    demo.call("create", "--title", "Team expenses")
    demo.call("import", "--source", "expenses", "--file", output / "expenses.parquet")
    demo.call("import", "--source", "notes", "--file", HERE / "notes.md")
    demo.call("apply", "--operations", HERE / "views.json")
    print("Saved expense table\n")
    demo.table("expenses", "expenses.csv")
    demo.call("artifact", "--source", "notes", "--output", output / "notes.md")
    print("\n" + (output / "notes.md").read_text(encoding="utf-8"))
    snapshot = demo.call("inspect")["result"]
    assert len(snapshot["document"]["projections"]) == 2
    assert not snapshot["document"]["actions"]
    print("Both views survive reopening. No project action runs.")
    demo.finish()


if __name__ == "__main__":
    run(main)
