"""Pele rendering adapter probe (RFC 001 section 7.3, backlog task T028).

The pinned renderer is a Node package, so the probe lives under
``runtime/rendering/pele_adapter`` beside the application-owned adapter
interface. These tests run the probe and gate on its verdict, so
``make check`` fails when the pinned renderer stops providing the
rendered-element identity mapping, the sanitization guarantees, or the
locked-down output this extension relies on.

The probe records Pele 1.6.0's limitations as findings rather than
claiming interaction from SVG generation: the renderer exposes no
hit-testing or event API, so hit testing, focus, keyboard selection, and
accessibility stay in the trusted host adapter (RFC 001 section 7.3).
Findings and evidence live in ``runtime/rendering/pele_adapter/README.md``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parents[2] / "runtime" / "rendering" / "pele_adapter"
PROBE = PROBE_DIR / "probe.mjs"
GRAPH = PROBE_DIR.parents[2] / "fixtures" / "semantic" / "supplier-network.graph.json"
PINNED_PELE = "1.6.0"
MINIMUM_NODE_MAJOR = 20

REQUIRED_FINDING_KINDS = {
    # The renderer provides no interaction API; SVG generation alone is
    # not interaction. This is the limitation RFC 001 section 7.3 makes
    # the probe report instead of claiming.
    "no-hit-testing-api",
    # Edge identity cannot be read from rendered edge ids; only the node
    # mapping is verified.
    "edge-data-id-ambiguity",
    # Why the adapter sanitizes labels before lowering.
    "labels-structural-characters-rejected",
    # Why the render options are locked by the application.
    "links-disabled-by-option",
}


def _node() -> str | None:
    """Return the node executable, or None when the prerequisite is missing."""
    return shutil.which("node")


def _pele_installed() -> bool:
    package = PROBE_DIR / "node_modules" / "pele" / "package.json"
    if not package.is_file():
        return False
    try:
        return json.loads(package.read_text(encoding="utf-8"))["version"] == PINNED_PELE
    except (OSError, ValueError, KeyError):
        return False


@pytest.fixture(scope="module")
def verdict() -> dict:
    """Run the probe once and return its verdict document."""
    node = _node()
    if node is None:
        pytest.skip(f"node >= {MINIMUM_NODE_MAJOR} is required to run the Pele probe")
    if not _pele_installed():
        pytest.skip(
            f"pinned Pele renderer {PINNED_PELE} is not installed; "
            "run `make pele-setup` (npm ci in runtime/rendering/pele_adapter)"
        )
    completed = subprocess.run(
        [node, str(PROBE), "--graph", str(GRAPH), "--json"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=PROBE_DIR,
    )
    try:
        document = json.loads(completed.stdout)
    except ValueError as error:
        raise AssertionError(
            f"probe printed no verdict (exit {completed.returncode}): "
            f"{completed.stderr[-2000:]}"
        ) from error
    document["exitCode"] = completed.returncode
    document["stderr"] = completed.stderr
    return document


def _failures(verdict: dict) -> str:
    failed = [
        f"{result['suite']}/{result['name']}: {result.get('error')}"
        for result in verdict["results"]
        if result["status"] == "fail"
    ]
    return "; ".join(failed)


def _detail(verdict: dict, suite: str, name: str):
    for result in verdict["results"]:
        if result["suite"] == suite and result["name"] == name:
            return result.get("detail") if result["status"] == "pass" else None
    return None


def _passed_names(verdict: dict, suite: str) -> set[str]:
    return {
        result["name"]
        for result in verdict["results"]
        if result["suite"] == suite and result["status"] == "pass"
    }


def test_probe_passes(verdict: dict) -> None:
    assert verdict["exitCode"] == 0
    assert verdict["verdict"] == "pass", _failures(verdict)
    assert verdict["summary"]["failed"] == 0
    assert verdict["summary"]["passed"] >= 30


def test_pinned_renderer_is_the_one_probed(verdict: dict) -> None:
    assert verdict["pinnedPele"] == PINNED_PELE
    assert verdict["resolvedPele"] == PINNED_PELE
    assert verdict["environment"]["domGlobals"] == []


def test_rendered_identity_maps_to_graph_node_ids(verdict: dict) -> None:
    """RFC 001 section 7.3: the adapter must map rendered elements to node IDs.

    The verified renderer mapping is the node group ``data-id``, which the
    probe shows equals the lowered graph's node IDs verbatim, and the
    adapter fails closed when the rendered set drifts.
    """

    mapping = _detail(verdict, "mapping", "rendered data-ids equal the graph node ids")
    assert mapping is not None
    assert mapping["rendered"] == ["s-42", "s-73"]
    assert mapping["expected"] == ["s-42", "s-73"]
    assert "a tampered identity set fails closed" in _passed_names(verdict, "mapping")
    assert "entity-syntax ids survive verbatim" in _passed_names(verdict, "mapping")
    assert "identical input renders byte-identical svg" in _passed_names(verdict, "mapping")


def test_host_hit_regions_resolve_semantic_references(verdict: dict) -> None:
    """RFC 001 section 7.3: mapping through host-owned hit regions.

    A click in SVG user space resolves a node ID, and the graph's identity
    map (never the SVG) resolves the semantic reference.
    """

    resolved = _detail(verdict, "hitregions", "a click on each node resolves its semantic reference")
    assert resolved is not None
    by_key = {entry["key"]: entry for entry in resolved["resolved"]}
    assert set(by_key) == {"S-42", "S-73"}
    assert all(entry["collection"] == "suppliers" for entry in by_key.values())
    assert by_key["S-42"]["nodeId"] == "s-42"
    assert by_key["S-73"]["nodeId"] == "s-73"
    misses = _detail(
        verdict, "hitregions", "a click between nodes and outside the canvas misses"
    )
    assert misses == {"gap": None, "left": None, "far": None}


def test_labels_are_sanitized_and_output_stays_closed(verdict: dict) -> None:
    assert "every hostile label renders sanitized without markup" in _passed_names(
        verdict, "sanitize"
    )
    assert "raw structural characters fail the mermaid parse" in _passed_names(
        verdict, "sanitize"
    )
    assert "rendered text equals the sanitized label" in _passed_names(verdict, "sanitize")
    assert "a pre-escaped label is not double-decoded" in _passed_names(verdict, "sanitize")
    security = _passed_names(verdict, "security")
    assert {
        "the fixture output carries only allowlisted elements",
        "a click callback statement adds no script",
        "a javascript href is dropped entirely",
        "locked options remove anchors a default render emits",
        "verification rejects injected markup",
    } <= security


def test_renderer_limitations_are_reported(verdict: dict) -> None:
    kinds = {finding["kind"] for finding in verdict["findings"]}
    missing = REQUIRED_FINDING_KINDS - kinds
    assert not missing, f"the probe stopped reporting: {sorted(missing)}"
    for finding in verdict["findings"]:
        assert finding["summary"], "a finding carries no summary"
        assert finding.get("evidence"), f"finding {finding['kind']} carries no evidence"


def test_external_interface_facts_are_recorded(verdict: dict) -> None:
    """The actual Linux interface of the pinned renderer (RFC 001 7.3)."""

    surface = _detail(verdict, "interface", "the module surface this adapter depends on")
    assert surface is not None
    assert set(surface["used"]) == {"render", "parse", "PeleError", "detectType"}
    errors = _detail(
        verdict, "interface", "syntax errors carry code, line, column, snippet"
    )
    assert errors is not None
    assert errors["code"] == "syntax"
    assert errors["type"] == "flowchart"
    assert errors["line"] == 2
    assert errors["column"] == 5
    assert errors["snippetChars"] > 0
