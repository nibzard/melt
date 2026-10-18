"""Build the deterministic fixture artifact bytes (SPEC.md sections 16 and 18.1).

The valid sales fixture must reference a real artifact hash, not the
all-zero placeholder of the specification example. This module builds
the three fixture artifacts from fixed inputs, so regeneration is
byte-identical under the pinned toolchain:

- `sales/sales.parquet`: the hero input of SPEC.md section 18.2 with
  rows (2026-01, 100), (2026-01, 300), and (2026-02, 200).
- `sales/revenue.png`: a plain RGB PNG that stands in for a published
  plot until the runner produces a real one.
- `sales/notes.md`: a UTF-8 text source.

Run without arguments to (re)write the files under `fixtures/`. Run
with `--check` to compare the committed files against regeneration and
exit nonzero on drift. `tests/contract/test_fixtures.py` enforces the
same comparison in memory, so `make check` fails when regeneration
changes committed output.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import zlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"

# The hero input rows of SPEC.md section 18.2. Array order is row order.
SALES_ROWS: tuple[tuple[str, float], ...] = (
    ("2026-01", 100.0),
    ("2026-01", 300.0),
    ("2026-02", 200.0),
)

# The declared fixture schema (SPEC.md section 16): two non-nullable
# columns and no row key, because the month values repeat.
SALES_TABLE_SCHEMA = pa.schema(
    [
        pa.field("month", pa.string(), nullable=False),
        pa.field("revenue", pa.float64(), nullable=False),
    ]
)

PNG_WIDTH = 64
PNG_HEIGHT = 48
PNG_RGB = (30, 90, 180)

NOTES_MD = """# Sales notes

The sales input holds three rows across two months.

- 2026-01 holds revenue 100 and 300.
- 2026-02 holds revenue 200.

This text artifact backs the `document` projection fixtures of the
contract suite (SPEC.md section 18.1).
"""


def sha256_of(data: bytes) -> str:
    """Return the Melt artifact hash string of raw bytes."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def sales_table() -> pa.Table:
    """Return the hero input as a typed Arrow table."""

    months = [row[0] for row in SALES_ROWS]
    revenues = [row[1] for row in SALES_ROWS]
    arrays = [
        pa.array(months, type=pa.string()),
        pa.array(revenues, type=pa.float64()),
    ]
    return pa.Table.from_arrays(arrays, schema=SALES_TABLE_SCHEMA)


def sales_parquet_bytes() -> bytes:
    """Return the Parquet encoding of the hero input table."""

    sink = pa.BufferOutputStream()
    pq.write_table(sales_table(), sink, compression="snappy", version="2.6")
    return sink.getvalue().to_pybytes()


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return length + tag + data + crc


def revenue_png_bytes() -> bytes:
    """Return a minimal deterministic RGB PNG."""

    row = bytes(PNG_RGB) * PNG_WIDTH
    raw = b"".join(b"\x00" + row for _ in range(PNG_HEIGHT))
    header = struct.pack(">IIBBBBB", PNG_WIDTH, PNG_HEIGHT, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def notes_markdown_bytes() -> bytes:
    """Return the UTF-8 text artifact bytes."""

    return NOTES_MD.encode("utf-8")


def fixture_files() -> dict[str, bytes]:
    """Return every fixture artifact keyed by its path under fixtures/."""

    return {
        "sales/sales.parquet": sales_parquet_bytes(),
        "sales/revenue.png": revenue_png_bytes(),
        "sales/notes.md": notes_markdown_bytes(),
    }


def write_all(root: Path = FIXTURES_DIR) -> list[Path]:
    """Write every fixture artifact under root and return the paths."""

    written: list[Path] = []
    for relative, data in sorted(fixture_files().items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    return written


def check_all(root: Path = FIXTURES_DIR) -> list[str]:
    """Return one message per committed file that no longer matches."""

    problems: list[str] = []
    for relative, expected in sorted(fixture_files().items()):
        path = root / relative
        if not path.is_file():
            problems.append(f"{relative}: missing; run this script to create it")
            continue
        committed = path.read_bytes()
        if committed != expected:
            problems.append(
                f"{relative}: committed bytes differ from regeneration"
                f" (committed {len(committed)} bytes, regenerated"
                f" {len(expected)} bytes)"
            )
    return problems


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=FIXTURES_DIR,
        help="fixture root that holds sales/ (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare committed files against regeneration instead of writing",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        problems = check_all(arguments.root)
        for problem in problems:
            print(f"build_fixture_artifacts: {problem}", file=sys.stderr)
        if problems:
            return 1
        print("build_fixture_artifacts: committed fixture artifacts match")
        return 0
    for path in write_all(arguments.root):
        print(f"build_fixture_artifacts: wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
