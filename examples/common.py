"""Small command-line helpers shared by the runnable examples."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--output", type=Path, help="an empty directory for saved state and results")
    return result


def output_directory(requested: Path | None, name: str) -> Path:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix=f"melt-{name}-"))
    path = requested.resolve()
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError(f"{path} is not empty; choose another --output directory")
    return path


def parquet_from_csv(source: Path, target: Path) -> None:
    import pyarrow as pa
    import pyarrow.csv as csv_reader
    import pyarrow.parquet as pq

    # Identifier columns remain strings even when all their values are digits.
    table = csv_reader.read_csv(source, convert_options=csv_reader.ConvertOptions(
        column_types={"supplier_id": pa.string(), "month": pa.string()}
    ))
    table = table.cast(pa.schema([
        field.with_nullable(column.null_count > 0)
        for field, column in zip(table.schema, table.columns)
    ]))
    pq.write_table(table, target)


class Demo:
    def __init__(self, output: Path, workspace: str):
        self.output = output
        self.workspace = workspace
        self.root = output / "data"
        self.log = output / "commands"
        self.log.mkdir()
        self.sequence = 0

    def call(self, *arguments: str) -> dict:
        self.sequence += 1
        command = [sys.executable, "-m", "runtime.harness", "--root", str(self.root),
                   "--workspace", self.workspace, *map(str, arguments)]
        completed = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120)
        try:
            answer = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(completed.stderr or completed.stdout or "the harness returned no result") from error
        (self.log / f"{self.sequence:02d}-{arguments[0]}.json").write_text(
            json.dumps(answer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if completed.returncode or answer.get("ok") is False:
            raise RuntimeError(json.dumps(answer, indent=2))
        return answer

    def table(self, source: str, name: str) -> dict:
        page = self.call("table", "--source", source)["result"]
        print(" | ".join(page["columns"]))
        for row in page["rows"]:
            print(" | ".join(str(value) for value in row))
        with (self.output / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(page["columns"])
            writer.writerows(page["rows"])
        return page

    def finish(self) -> None:
        print(f"\nSaved results: {self.output}")
        print("Reopen the saved workspace:")
        import shlex
        print(shlex.join(["uv", "run", "--locked", "python3", "-m", "runtime.harness",
                          "--root", str(self.root), "--workspace", self.workspace, "inspect"]))


def run(main) -> None:
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        print(f"Example failed: {error}", file=sys.stderr)
        raise SystemExit(1)
