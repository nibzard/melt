"""Command-line entry point checks (SPEC.md section 11.3, task T006).

`melt validate <file>` and `melt compile <file> --catalog <file>` share
one exit code convention: 0 on success, 1 for invalid input, and 2 for
operational failure. `--json` prints one structured result object.

The subprocess tests prove the installed command-line surface
(``python -m melt`` and the ``melt`` console script entry). The
in-process tests drive `melt.cli.main` over every fixture so the whole
contract matrix runs without spawning a process per fixture:

- Every valid document fixture validates and compiles with exit 0.
- Every invalid document fixture fails compilation with exit 1 and the
  diagnostic its manifest entry records. Schema-enforced fixtures also
  fail plain validation; compiler-enforced fixtures pass plain
  validation because they are structurally valid, which is exactly the
  limited coverage plain validation must report.
- Operational failures (unreadable file, malformed catalog, missing
  catalog argument, unknown command) exit 2.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from melt.cli import EXIT_INVALID, EXIT_OK, EXIT_OPERATIONAL, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_melt(repo_root, *arguments):
    environment = dict(os.environ)
    python_path = [str(repo_root / "python"), str(repo_root)]
    if existing := environment.get("PYTHONPATH"):
        python_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return subprocess.run(
        [sys.executable, "-m", "melt", *arguments],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=environment,
    )


def run_main(argv: list[str], capsys) -> "tuple[int, dict, str, str]":
    """Run the command line in process and return code, result, and text."""

    code = main(argv)
    captured = capsys.readouterr()
    result = json.loads(captured.out) if captured.out.startswith("{") else None
    return code, result, captured.out, captured.err


def valid_document_paths(repo_root: Path) -> list[Path]:
    """Every valid document fixture; the hash index is not a document."""

    return sorted(
        path
        for path in (repo_root / "fixtures" / "valid").glob("*.json")
        if path.name != "document-hashes.json"
    )


def manifest_entries(repo_root: Path, kind: "str | None" = None) -> list[dict]:
    """The invalid fixture entries, optionally filtered by kind."""

    manifest = json.loads(
        (repo_root / "fixtures" / "invalid" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    return [
        entry for entry in manifest["fixtures"] if kind is None or entry["kind"] == kind
    ]


def diagnostic_pairs(result: dict) -> set[tuple[str, str]]:
    return {
        (diagnostic["code"], diagnostic["path"])
        for diagnostic in result["diagnostics"]
    }


_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALID_DOCUMENTS = valid_document_paths(_REPO_ROOT)
_INVALID_DOCUMENTS = manifest_entries(_REPO_ROOT, kind="document")


# ---------------------------------------------------------------------------
# The installed command-line surface
# ---------------------------------------------------------------------------


def test_version_reports_package_and_language(repo_root):
    completed = _run_melt(repo_root, "--version")
    assert completed.returncode == 0, completed.stderr
    assert "0.1.0" in completed.stdout
    assert "melt/v0.1" in completed.stdout


def test_no_command_fails_operationally(repo_root):
    completed = _run_melt(repo_root)
    assert completed.returncode == EXIT_OPERATIONAL, completed.stdout
    assert "usage:" in completed.stderr


def test_unknown_command_fails_operationally(repo_root):
    completed = _run_melt(repo_root, "frobnicate", "x.json")
    assert completed.returncode == EXIT_OPERATIONAL, completed.stdout


def test_subprocess_validate_accepts_a_valid_fixture(repo_root):
    completed = _run_melt(
        repo_root, "validate", "fixtures/valid/sales-full.json", "--json"
    )
    assert completed.returncode == EXIT_OK, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["coverage"] == "structural"


def test_subprocess_compile_emits_the_plan(repo_root):
    completed = _run_melt(
        repo_root,
        "compile",
        "fixtures/valid/sales-full.json",
        "--catalog",
        "fixtures/catalog.json",
        "--json",
    )
    assert completed.returncode == EXIT_OK, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["coverage"] == "full"
    assert result["plan"]["workspaceId"] == "sales-demo"
    assert result["planKey"] == result["plan"]["planKey"]


def test_subprocess_compile_without_catalog_fails_operationally(repo_root):
    completed = _run_melt(repo_root, "compile", "fixtures/valid/sales-full.json")
    assert completed.returncode == EXIT_OPERATIONAL, completed.stdout
    assert "--catalog" in completed.stderr


def test_subprocess_missing_file_fails_operationally(repo_root):
    completed = _run_melt(repo_root, "validate", "no-such-document.json", "--json")
    assert completed.returncode == EXIT_OPERATIONAL, completed.stdout
    result = json.loads(completed.stdout)
    assert result["ok"] is False
    assert result["coverage"] == "none"
    assert "no-such-document.json" in result["message"]


def test_console_script_entry_point_is_installed(repo_root):
    """pyproject.toml installs a `melt` command with the same behavior."""

    script = repo_root / ".venv" / "bin" / "melt"
    if not script.is_file():
        pytest.skip(".venv holds no console script; run uv sync --locked")
    completed = subprocess.run(
        [str(script), "--version"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert completed.returncode == 0, completed.stderr
    assert "melt/v0.1" in completed.stdout


# ---------------------------------------------------------------------------
# Valid fixtures: both commands pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _VALID_DOCUMENTS, ids=lambda p: p.name)
def test_validate_accepts_every_valid_fixture(path, capsys) -> None:
    code, result, _, err = run_main(["validate", str(path), "--json"], capsys)
    assert code == EXIT_OK, result["diagnostics"]
    assert result["ok"] is True
    assert result["coverage"] == "structural"
    assert result["diagnostics"] == []
    assert result["documentHash"].startswith("sha256:")
    assert err == ""


@pytest.mark.parametrize("path", _VALID_DOCUMENTS, ids=lambda p: p.name)
def test_compile_accepts_every_valid_fixture(path, capsys, repo_root) -> None:
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, result, _, err = run_main(
        ["compile", str(path), "--catalog", catalog, "--json"], capsys
    )
    assert code == EXIT_OK, result["diagnostics"]
    assert result["ok"] is True
    assert result["coverage"] == "full"
    assert result["diagnostics"] == []
    assert result["plan"]["planVersion"] == 1
    assert err == ""


def test_reported_document_hashes_match_the_recorded_hashes(
    repo_root, capsys
) -> None:
    """The hash `melt validate` reports equals fixtures/valid's index."""

    recorded = json.loads(
        (repo_root / "fixtures" / "valid" / "document-hashes.json").read_text(
            encoding="utf-8"
        )
    )["documents"]
    for name, hash_value in sorted(recorded.items()):
        path = repo_root / "fixtures" / "valid" / name
        _, result, _, _ = run_main(["validate", str(path), "--json"], capsys)
        assert result["documentHash"] == hash_value, name


# ---------------------------------------------------------------------------
# Invalid fixtures: both commands fail with the expected code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry", _INVALID_DOCUMENTS, ids=lambda entry: Path(entry["file"]).stem
)
def test_compile_rejects_every_invalid_document_fixture(
    entry, capsys, repo_root
) -> None:
    path = str(repo_root / "fixtures" / "invalid" / entry["file"])
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, result, _, _ = run_main(
        ["compile", path, "--catalog", catalog, "--json"], capsys
    )
    assert code == EXIT_INVALID, entry["file"]
    assert result["ok"] is False
    assert (entry["code"], entry["path"]) in diagnostic_pairs(result), (
        entry["file"]
    )


@pytest.mark.parametrize(
    "entry", _INVALID_DOCUMENTS, ids=lambda entry: Path(entry["file"]).stem
)
def test_validate_with_catalog_rejects_every_invalid_document_fixture(
    entry, capsys, repo_root
) -> None:
    path = str(repo_root / "fixtures" / "invalid" / entry["file"])
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, result, _, _ = run_main(
        ["validate", path, "--catalog", catalog, "--json"], capsys
    )
    assert code == EXIT_INVALID, entry["file"]
    assert (entry["code"], entry["path"]) in diagnostic_pairs(result), (
        entry["file"]
    )


@pytest.mark.parametrize(
    "entry", _INVALID_DOCUMENTS, ids=lambda entry: Path(entry["file"]).stem
)
def test_plain_validation_reports_schema_and_compiler_layers(
    entry, capsys, repo_root
) -> None:
    """Plain validation fails on schema entries and flags its limits
    on compiler entries, which are structurally valid (SPEC.md 11.3)."""

    path = str(repo_root / "fixtures" / "invalid" / entry["file"])
    code, result, _, _ = run_main(["validate", path, "--json"], capsys)
    if entry["enforcedBy"] == "schema":
        assert code == EXIT_INVALID, entry["file"]
        assert (entry["code"], entry["path"]) in diagnostic_pairs(result), (
            entry["file"]
        )
    else:
        assert code == EXIT_OK, entry["file"]
        assert result["coverage"] == "structural"
        assert "catalog" in result["coverageNote"].lower()
        assert result["diagnostics"] == []


@pytest.mark.parametrize(
    "entry",
    manifest_entries(_REPO_ROOT, kind="command"),
    ids=lambda entry: Path(entry["file"]).stem,
)
def test_command_envelopes_are_invalid_documents(entry, capsys, repo_root) -> None:
    """Service-enforced fixtures are envelopes, not documents."""

    path = str(repo_root / "fixtures" / "invalid" / entry["file"])
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, result, _, _ = run_main(
        ["compile", path, "--catalog", catalog, "--json"], capsys
    )
    assert code == EXIT_INVALID, entry["file"]
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Limited coverage reporting
# ---------------------------------------------------------------------------


def test_plain_validation_reports_its_limited_coverage(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    code, result, out, _ = run_main(["validate", path], capsys)
    assert code == EXIT_OK
    assert "structural validation only" in out
    assert "catalog" in out.lower()


def test_validate_with_catalog_reports_full_coverage(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, result, _, _ = run_main(
        ["validate", path, "--catalog", catalog, "--json"], capsys
    )
    assert code == EXIT_OK
    assert result["coverage"] == "full"
    assert result["catalog"] == catalog


# ---------------------------------------------------------------------------
# Operational failures
# ---------------------------------------------------------------------------


def test_unreadable_document_fails_operationally(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "no-such-file.json")
    code, result, _, _ = run_main(["validate", path, "--json"], capsys)
    assert code == EXIT_OPERATIONAL
    assert "no-such-file.json" in result["message"]
    code, _, _, err = run_main(
        [
            "compile",
            path,
            "--catalog",
            str(repo_root / "fixtures" / "catalog.json"),
        ],
        capsys,
    )
    assert code == EXIT_OPERATIONAL
    assert "no-such-file.json" in err


def test_directory_input_fails_operationally(repo_root, capsys) -> None:
    code, _, _, err = run_main(
        ["validate", str(repo_root / "fixtures" / "valid")], capsys
    )
    assert code == EXIT_OPERATIONAL
    assert err.startswith("melt:")


def test_compile_without_catalog_fails_operationally(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    code, result, _, err = run_main(["compile", path, "--json"], capsys)
    assert code == EXIT_OPERATIONAL
    assert "--catalog" in result["message"]
    assert err == ""
    code, _, _, err = run_main(["compile", path], capsys)
    assert code == EXIT_OPERATIONAL
    assert "--catalog" in err


def test_missing_catalog_file_fails_operationally(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    code, _, _, err = run_main(
        ["compile", path, "--catalog", str(repo_root / "fixtures" / "no-catalog.json")],
        capsys,
    )
    assert code == EXIT_OPERATIONAL
    assert "no-catalog.json" in err


def test_malformed_catalog_fails_operationally(repo_root, tmp_path, capsys) -> None:
    catalog = tmp_path / "broken-catalog.json"
    catalog.write_text("not a catalog", encoding="utf-8")
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    code, result, _, _ = run_main(
        ["compile", path, "--catalog", str(catalog), "--json"], capsys
    )
    assert code == EXIT_OPERATIONAL
    assert result["ok"] is False
    assert result["coverage"] == "none"
    assert "malformed" in result["message"]


def test_operational_failure_prints_one_json_object(repo_root, capsys) -> None:
    code, result, out, err = run_main(
        ["validate", str(repo_root / "fixtures" / "absent.json"), "--json"], capsys
    )
    assert code == EXIT_OPERATIONAL
    assert json.loads(out) == result
    assert result["diagnostics"] == []


# ---------------------------------------------------------------------------
# Plain output shape
# ---------------------------------------------------------------------------


def test_invalid_document_prints_diagnostics_on_stderr(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "invalid" / "unknown-field.json")
    code, _, out, err = run_main(["validate", path], capsys)
    assert code == EXIT_INVALID
    assert out == ""
    assert "E_SCHEMA /metadata" in err
    assert "invalid: " in err


def test_compile_success_prints_the_plan_key(repo_root, capsys) -> None:
    path = str(repo_root / "fixtures" / "valid" / "sales-full.json")
    catalog = str(repo_root / "fixtures" / "catalog.json")
    code, _, out, err = run_main(["compile", path, "--catalog", catalog], capsys)
    assert code == EXIT_OK
    assert err == ""
    assert out.startswith("compiled: ")
    assert "plan key: sha256:" in out


def test_non_utf8_document_is_invalid_input(repo_root, tmp_path, capsys) -> None:
    path = tmp_path / "broken-utf8.json"
    path.write_bytes(b'{"title": "\xff\xfe"}')
    code, result, _, _ = run_main(["validate", str(path), "--json"], capsys)
    assert code == EXIT_INVALID
    assert {item["code"] for item in result["diagnostics"]} == {"E_PARSE"}
