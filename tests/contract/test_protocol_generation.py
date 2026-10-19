"""Generated protocol artifacts (SPEC.md section 17, backlog task T007).

One repeatable generation command exports the JSON Schema and generates
the TypeScript and Swift models from the single restricted vocabulary
in ``python/melt/schema``. This module keeps that promise honest:

- Regenerating the committed files changes nothing (the same gate
  ``scripts/check.sh`` runs, so continuous integration fails when
  regeneration changes committed output).
- Every generated file carries the do-not-edit header and the
  regeneration command.
- The exported JSON Schema files describe both languages and the
  versioned record and command contracts.
- The semantic extension fixtures join the canonical hash agreement
  through their own index, separate from the v0.1 set, and the
  generated TypeScript models reproduce every recorded hash of both
  indexes (SPEC.md sections 4.2 and 18.1).
- The generated Swift models run their verification harness wherever a
  Swift toolchain exists; the Linux gate never requires one (SPEC.md
  section 2.2).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from melt.schema.canonical import document_hash
from melt.schema.semantic import parse_document_for_language

REPO_ROOT = Path(__file__).resolve().parents[2]

GENERATED_FILES = (
    "protocol/schema/document.schema.json",
    "protocol/schema/semantic-document.schema.json",
    "protocol/schema/command.schema.json",
    "protocol/schema/record.schema.json",
    "protocol/generated/melt-document.ts",
    "protocol/generated/MeltDocument.swift",
)

HEADER_MARKERS = ("do not edit by hand", "make protocol")


def test_regeneration_leaves_the_committed_output_unchanged() -> None:
    """The drift gate of scripts/check.sh passes against this tree."""

    completed = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            "scripts/generate_protocol.py",
            "--check",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "are current" in completed.stdout


def test_the_drift_gate_rejects_a_tampered_generated_file() -> None:
    """A hand edit to a generated file fails the regeneration check."""

    target = REPO_ROOT / "protocol/generated/melt-document.ts"
    original = target.read_text(encoding="utf-8")
    target.write_text(
        original + "\n// a hand edit the generator would never make\n",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "python3",
                "scripts/generate_protocol.py",
                "--check",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    finally:
        target.write_text(original, encoding="utf-8")
    assert completed.returncode == 1
    assert "regeneration changes the committed output" in completed.stderr


def test_every_generated_file_carries_the_header() -> None:
    """Provenance is self-describing in every generated file."""

    for relative in GENERATED_FILES:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        if relative.endswith(".json"):
            marker = json.loads(text)["x-generated"]
            assert marker["command"].startswith("make protocol"), relative
            assert "python/melt/schema" in marker["source"], relative
            assert "do not edit by hand" in marker["note"], relative
            continue
        head = "\n".join(text.splitlines()[:5])
        for marker in HEADER_MARKERS:
            assert marker in head, (relative, marker)
        assert "python/melt/schema" in head, relative


def test_the_schema_exports_describe_both_languages_and_the_records() -> None:
    document = json.loads(
        (REPO_ROOT / "protocol/schema/document.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert document["$schema"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert document["$ref"] == "#/$defs/Document"
    assert (
        document["$defs"]["Document"]["properties"]["language"]["const"]
        == "melt/v0.1"
    )
    # The recursive layout union is inlined by the exporter; its
    # member models carry the definitions.
    for name in ("SourceVersion", "Binding", "SplitNode", "TableProjection"):
        assert name in document["$defs"], name

    semantic = json.loads(
        (
            REPO_ROOT / "protocol/schema/semantic-document.schema.json"
        ).read_text(encoding="utf-8")
    )
    semantic_language = semantic["$defs"]["SemanticDocument"]["properties"][
        "language"
    ]["const"]
    assert semantic_language == "melt/semantic-draft-1"
    for name in ("SemanticCollection", "GraphSource", "RecordProjection"):
        assert name in semantic["$defs"], name

    command = json.loads(
        (REPO_ROOT / "protocol/schema/command.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert command["$ref"] == "#/$defs/CommandEnvelope"
    for name in ("CommandEnvelope", "OkResponse", "RejectedResponse"):
        assert name in command["$defs"], name

    record = json.loads(
        (REPO_ROOT / "protocol/schema/record.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["$ref"] == "#/$defs/MeltRecord"
    for name in (
        "RunRecord",
        "RegistrationRecord",
        "ArtifactRecord",
        "ParameterState",
        "CommandReceipt",
        "Snapshot",
        "ChangeNotification",
        "PageRequest",
        "PageResponse",
    ):
        assert name in record["$defs"], name


def test_the_semantic_hash_index_agrees_with_python() -> None:
    """The extension fixtures join the hash agreement in their own index.

    The index stays separate from ``fixtures/valid/document-hashes.json``
    so the v0.1 suite runs unchanged (backlog task T007).
    """

    index = json.loads(
        (
            REPO_ROOT / "fixtures/semantic/document-hashes.json"
        ).read_text(encoding="utf-8")
    )
    documents = index["documents"]
    assert documents, "the semantic hash index lists at least one document"
    for name, recorded in documents.items():
        text = (REPO_ROOT / "fixtures/semantic" / name).read_text(
            encoding="utf-8"
        )
        assert recorded == document_hash(parse_document_for_language(text)), name


def test_the_generated_typescript_models_reproduce_both_hash_indexes() -> None:
    """canonicalDocumentHash agrees with Python on every entry."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the generated TypeScript models")
    models = json.dumps(
        (REPO_ROOT / "protocol/generated/melt-document.ts").as_posix()
    )
    root = json.dumps(REPO_ROOT.as_posix())
    probe = (
        f"import {{ canonicalDocumentHash }} from {models};\n"
        "import { readFileSync } from 'node:fs';\n"
        f"const root = {root};\n"
        "const indexes = [\n"
        "  ['fixtures/valid', 'document-hashes.json'],\n"
        "  ['fixtures/semantic', 'document-hashes.json'],\n"
        "];\n"
        "const report = [];\n"
        "for (const [directory, indexName] of indexes) {\n"
        "  const recorded = JSON.parse(readFileSync(\n"
        "    `${root}/${directory}/${indexName}`, 'utf8',\n"
        "  )).documents;\n"
        "  for (const name of Object.keys(recorded).sort()) {\n"
        "    const hash = canonicalDocumentHash(readFileSync(\n"
        "      `${root}/${directory}/${name}`, 'utf8',\n"
        "    ));\n"
        "    if (hash !== recorded[name]) {\n"
        "      report.push(`${directory}/${name}`);\n"
        "    }\n"
        "  }\n"
        "}\n"
        "if (report.length > 0) {\n"
        "  console.error(`disagreements: ${report.join(', ')}`);\n"
        "  process.exit(1);\n"
        "}\n"
    )
    completed = subprocess.run(
        [node, "--input-type=module", "--eval", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_generated_swift_models_verify_the_hash_indexes() -> None:
    """The generated Swift harness agrees wherever a toolchain exists.

    The Linux gate never requires Apple tools (SPEC.md section 2.2), so
    this runs only where `swift` is on PATH and skips otherwise;
    scripts/check.sh applies the same condition.
    """

    swift = shutil.which("swift")
    if swift is None:
        pytest.skip("a Swift toolchain is required for the generated models")
    completed = subprocess.run(
        [
            swift,
            "protocol/generated/MeltDocument.swift",
            REPO_ROOT.as_posix(),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "agree with every recorded hash" in completed.stdout
