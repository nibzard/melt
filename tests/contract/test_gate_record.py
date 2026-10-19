"""The round 1 gate record (T024, SPEC.md section 20 step 5).

`docs/gates/round-1-linux.json` records the evidence that closed the
Linux gate: the four suites with their commands and results, the
compiler and reader measurements, and the no-macOS proof. These tests
keep that record honest against the tree it describes:

- Every module the record names exists, and the recorded test counts
  match the tests the modules actually define. Parametrized suites may
  collect more than they define; never fewer.
- The race suite still holds exactly one test per scenario of
  `SPEC.md` section 18.3, and the fault suite one test per fault of
  section 18.4, under the names the record maps.
- The hero workflow still drives all six steps of section 18.2 over
  the three hero rows.
- The compiler and reader numbers the record quotes still agree with
  `docs/performance/round-1-linux.json` and its own verdicts.
- The no-macOS claim matches the recorded tree: no `native/` sources
  at that revision, a skip-marked round-2 placeholder, and resolvable
  Linux tools only. Later native sources do not rewrite that history.
- The round boundary stays scoped to this gate, and its open-work list
  holds only tracked work that is still open.

When a change to the suites invalidates the record, refresh
`docs/gates/round-1-linux.json` and `round-1-linux.md` from a real
`make check` run; do not edit the record to silence these tests.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE_DIR = REPO / "docs" / "gates"
RECORD = GATE_DIR / "round-1-linux.json"
REPORT = GATE_DIR / "round-1-linux.md"
PERFORMANCE = REPO / "docs" / "performance" / "round-1-linux.json"


def gate_record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def collected_test_functions(module: str) -> set[str]:
    """Return the top-level test function names of one suite module."""

    path = REPO / module
    assert path.is_file(), f"the gate record names a missing module: {module}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def suite(record: dict, name: str) -> dict:
    for entry in record["suites"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"the gate record lost its {name} suite")


def test_the_record_describes_the_closing_run():
    record = gate_record()

    assert record["gate"] == "round-1-linux"
    assert record["requirement"] == "SPEC.md section 20 step 5"
    assert record["language"] == "melt/v0.1"
    assert record["command"] == "./scripts/check.sh"
    # The closing run passed with exactly one skip: the round-2
    # placeholder that claims no behavior. The confirmation run added
    # these record-honesty tests on top of the same suites.
    assert "789 passed" in record["outcome"]["result"]
    assert "1 skipped" in record["outcome"]["result"]
    assert "798 passed" in record["outcome"]["confirmationRunWithRecord"]
    assert len(record["outcome"]["skipped"]) == 1
    assert "test_native_round2.py" in record["outcome"]["skipped"][0]
    # The verified tree is a commit that exists in this repository, so
    # the record points at real reviewed state.
    verified = record["verifiedTree"]
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{verified}^{{commit}}"],
        cwd=REPO,
        check=False,
        capture_output=True,
    )
    assert exists.returncode == 0, f"verifiedTree {verified} is not a commit"


def test_every_named_module_exists_and_counts_match():
    record = gate_record()

    for entry in record["suites"]:
        assert entry["modules"], f"suite {entry['name']} names no module"
        defined = 0
        for module in entry["modules"]:
            defined += len(collected_test_functions(module))
        recorded = entry["collectedTests"]
        # Parametrized suites collect at least what they define; a
        # suite without parametrization collects exactly that.
        assert recorded >= defined, (
            f"suite {entry['name']} records {recorded} collected tests "
            f"but its modules define {defined}; refresh the record"
        )
        parametrized = any(
            "parametrize" in (REPO / module).read_text(encoding="utf-8")
            for module in entry["modules"]
        )
        if not parametrized:
            assert recorded == defined, (
                f"suite {entry['name']} records {recorded} collected "
                f"tests but its unparametrized modules define {defined}; "
                "refresh the record"
            )


def test_the_race_suite_covers_every_section_18_3_scenario():
    record = gate_record()
    entry = suite(record, "race-suite")
    module = entry["modules"][0]
    functions = collected_test_functions(module)

    mapped = set(entry["scenarios"].values())
    assert len(entry["scenarios"]) == 11, "section 18.3 lists eleven scenarios"
    # Every mapped scenario test exists...
    assert mapped <= functions, sorted(mapped - functions)
    # ...and the module defines nothing the record does not map.
    assert functions == mapped, sorted(functions - mapped)


def test_the_fault_suite_covers_every_section_18_4_fault():
    record = gate_record()
    entry = suite(record, "publication-fault-suite")
    module = "tests/recovery/test_restart_recovery.py"
    functions = collected_test_functions(module)

    mapped = set(entry["faults"].values())
    # Section 18.4 names the kill boundaries, disk-full, missing
    # artifacts, missing environments, lost notifications, changed
    # epochs, reused request identifiers, corrupt descriptors,
    # cancelled runs, and incompatible saved versions; section 14
    # adds the seven startup steps.
    expected_keys = {
        "kill-before-artifact-finalization",
        "kill-between-artifact-writes",
        "kill-after-finalization-before-commit",
        "kill-after-commit-before-acknowledgement",
        "kill-before-mutation-commit",
        "kill-after-mutation-commit-before-acknowledgement",
        "kill-during-execution",
        "disk-full",
        "missing-artifacts",
        "missing-environments",
        "lost-notifications",
        "changed-epochs",
        "reused-request-ids",
        "corrupt-descriptors",
        "cancelled-runs",
        "incompatible-saved-versions",
        "seven-startup-steps",
    }
    assert set(entry["faults"]) == expected_keys
    assert mapped <= functions, sorted(mapped - functions)
    assert functions == mapped, sorted(functions - mapped)

    # The publication half of the suite guards the atomic pair.
    publication = collected_test_functions("tests/integration/test_publication.py")
    for required in (
        "test_publication_makes_both_outputs_current_together",
        "test_an_invalid_output_rejects_the_entire_publication",
        "test_a_missing_output_rejects_the_entire_publication",
        "test_the_publication_transaction_is_one_commit_with_all_records",
        "test_publication_records_rebuild_after_a_restart",
    ):
        assert required in publication, required


def test_the_hero_workflow_drives_all_six_steps_over_the_hero_rows():
    record = gate_record()
    entry = suite(record, "hero-workflow")
    module = entry["modules"][0]
    source = (REPO / module).read_text(encoding="utf-8")

    assert collected_test_functions(module) == {
        "test_the_hero_workflow_passes_through_the_harness"
    }
    for step in range(1, 7):
        assert f"Step {step}:" in source, f"hero step {step} lost its marker"
    # The three hero rows of section 18.2 and both headline totals.
    for literal in ('"2026-01", 100.0', '"2026-01", 300.0', '"2026-02", 200.0'):
        assert literal in source, literal
    # The headline monthly totals of minimum 200.
    assert "300" in source and "200" in source
    # The zero-model-call proof scans every persisted byte.
    assert "_assert_no_persisted_credential" in source
    assert len(entry["coverage"]) == 6


def test_compiler_and_reader_measurements_agree_with_the_record():
    record = gate_record()
    performance = json.loads(PERFORMANCE.read_text(encoding="utf-8"))
    results = {item["name"]: item for item in performance["results"]}

    for dimension in ("compiler", "reader"):
        quoted = record["measurements"][dimension]
        measured = results[quoted["result"]]
        assert quoted["p95Ms"] == measured["p95"], dimension
        assert quoted["target"] == measured["target"]["target"], dimension
        assert quoted["met"] is measured["target"]["met"] is True, dimension

    # The report carries the same numbers and the recorded targets.
    report = REPORT.read_text(encoding="utf-8")
    for phrase in (
        "10.546 ms",
        "79.699 ms",
        "p95 below 50 ms",
        "p95 below 300 ms",
        "A valid JSON document alone does not pass",
        "No macOS tools",
    ):
        assert phrase in report, phrase


def test_the_no_macos_claim_matches_the_tree():
    record = gate_record()
    linux_only = record["linuxOnly"]

    # The recorded tools resolve on this machine; nothing Apple does.
    assert linux_only["appleToolsRequired"] == []
    for tool in linux_only["toolsRequired"]:
        assert shutil.which(tool), f"recorded tool {tool} does not resolve"

    # Check the historical claim against the recorded revision. Later
    # round-2 source additions must not invalidate a completed Linux gate.
    native_at_gate = subprocess.run(
        ["git", "ls-tree", record["verifiedTree"], "--", "native"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    assert native_at_gate.stdout == ""
    placeholder = (REPO / "tests" / "native" / "test_native_round2.py").read_text(
        encoding="utf-8"
    )
    assert "pytest.mark.skip" in placeholder

    # The forbidden-import guard is part of the recorded contract suite.
    contract = suite(record, "contract-suite")
    toolchain = "tests/contract/test_toolchain.py"
    assert toolchain in contract["modules"]
    assert "test_round1_sources_import_no_apple_frameworks" in collected_test_functions(
        toolchain
    )


def test_the_round_boundary_stays_scoped_to_this_gate():
    record = gate_record()

    deferred = record["roundBoundary"]["notPartOfThisGate"]
    assert any("T030" in item for item in deferred)
    assert any("T031" in item for item in deferred)
    assert any("T025" in item for item in deferred)
    # The two tracked round-1 items the gate left open at its close —
    # T035 (non-blocking execution slot, process-group cancellation)
    # and T036 (README hero step 3 debounce flag) — completed and
    # integrated afterwards, so the refresh owned by backlog task T039
    # moved both out of the open-work list together with this pin. No
    # tracked round-1 work stays open against this gate; refresh the
    # record and this assertion together when a real item reopens.
    assert record["roundBoundary"]["openTrackedWork"] == []
    # The prose report tells the same story instead of still naming
    # either item as remaining work. Whitespace is normalized because
    # the report wraps its lines.
    report = " ".join(REPORT.read_text(encoding="utf-8").split())
    assert "records no open tracked round-1 work" in report
    assert "remain backlog task T035" not in report


def test_the_readme_points_at_the_gate_record():
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "docs/gates/round-1-linux.md" in readme
    assert "Round 1 gate" in readme
    # The hero step 3 command carries the debounce wait that makes the
    # documented rerun dispatch in one session (backlog task T036).
    assert "--debounce-wait 400" in readme
