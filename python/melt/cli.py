"""The Melt command-line compiler (SPEC.md section 11.3).

Two commands share one exit code convention:

- ``melt validate <file> [--catalog <file>] [--json]`` validates one
  document. Without ``--catalog`` it performs structural validation
  alone and reports that limited coverage: the artifact, registration,
  and mapping checks need the trusted catalog and registry. With
  ``--catalog`` it runs the full compiler stages and reports the same
  diagnostics ``melt compile`` reports.
- ``melt compile <file> --catalog <file> [--json]`` runs full
  compilation and emits the compiled plan. The catalog file carries the
  trusted artifact catalog and the action registry together;
  ``fixtures/catalog.json`` holds both. A compile without a catalog is
  an operational failure, because full compilation requires the catalog
  and registry fixtures.

Exit codes follow the specification: 0 on success, 1 for invalid input
(a document with diagnostics), and 2 for operational failure (an
unreadable file, a malformed catalog, a missing catalog argument, or an
unknown command). ``--json`` prints exactly one structured result
object; plain output stays a short human summary. Diagnostics print to
stderr when the input is invalid, so a failed run leaves stdout empty
outside ``--json`` mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from melt import LANGUAGE_ID, __version__
from melt.schema.canonical import document_hash, model_to_wire
from melt.schema.diagnostics import Diagnostic
from melt.schema.document import MeltDocumentError, parse_document

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_OPERATIONAL = 2
"""The exit codes of SPEC.md section 11.3."""

COVERAGE_NONE = "none"
COVERAGE_STRUCTURAL = "structural"
COVERAGE_FULL = "full"

NOTHING_CHECKED_NOTE = (
    "Nothing was validated: the command failed before any check ran."
)
STRUCTURAL_NOTE = (
    "Structural validation only. No catalog or registry was supplied, so"
    " artifact, registration, and mapping checks were not run. Full"
    " compilation requires both: run `melt compile <file> --catalog <file>`"
    " (SPEC.md section 11.3)."
)
FULL_NOTE = (
    "Validated against the trusted artifact catalog and the action registry"
    " of the supplied catalog file."
)


class MeltOperationalError(RuntimeError):
    """An operational failure of the command itself (exit code 2)."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _read_file(path: str, what: str) -> bytes:
    """Read one input file, or fail operationally."""

    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise MeltOperationalError(
            f"cannot read the {what} {path!r}: {error.strerror or error}"
        ) from error


def _load_compiler_inputs(catalog_path: str):
    """Load the trusted catalog and registry from one catalog file."""

    try:
        from runtime.compiler import load_catalog
        from runtime.compiler.catalog import MeltCatalogError
    except ImportError as error:
        raise MeltOperationalError(
            "the runtime compiler package is not installed; melt needs it"
            " for catalog-checked compilation (SPEC.md section 11.3)"
        ) from error
    text = _read_file(catalog_path, "catalog")
    try:
        return load_catalog(text)
    except MeltCatalogError as error:
        raise MeltOperationalError(
            f"the catalog {catalog_path!r} is malformed: {error}"
        ) from error


def _compile(document: bytes, catalog_path: str):
    """Run the seven compiler stages against the loaded catalog."""

    from runtime.compiler import CompileRequest, compile_document

    source_catalog, action_registry = _load_compiler_inputs(catalog_path)
    return compile_document(
        CompileRequest(
            document=document,
            source_catalog=source_catalog,
            action_registry=action_registry,
        )
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one command proved, and the exit code it earns."""

    exit_code: int
    command: str
    file: "str | None"
    catalog: "str | None"
    ok: bool
    coverage: str
    coverage_note: str
    diagnostics: "tuple[Diagnostic, ...]" = ()
    document_hash: "str | None" = None
    plan_key: "str | None" = None
    plan: object = None
    message: "str | None" = None

    def payload(self) -> dict:
        """The structured result object of ``--json``."""

        value = {
            "command": self.command,
            "file": self.file,
            "catalog": self.catalog,
            "ok": self.ok,
            "coverage": self.coverage,
            "coverageNote": self.coverage_note,
            "diagnostics": [model_to_wire(item) for item in self.diagnostics],
        }
        if self.document_hash is not None:
            value["documentHash"] = self.document_hash
        if self.plan_key is not None:
            value["planKey"] = self.plan_key
        if self.plan is not None:
            value["plan"] = model_to_wire(self.plan)
        if self.message is not None:
            value["message"] = self.message
        return value

    def report(self, as_json: bool) -> int:
        """Print the result and return the process exit code."""

        if as_json:
            json.dump(
                self.payload(),
                sys.stdout,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            sys.stdout.write("\n")
            return self.exit_code
        if self.message is not None:
            print(f"melt: {self.message}", file=sys.stderr)
            return self.exit_code
        stream = sys.stdout if self.ok else sys.stderr
        noun = "valid" if self.command == "validate" else "compiled"
        if self.ok:
            scope = (
                f" against the catalog {self.catalog}"
                if self.coverage == COVERAGE_FULL
                else " (structural validation only)"
            )
            print(f"{noun}: {self.file}{scope}", file=stream)
            if self.document_hash is not None:
                print(f"document hash: {self.document_hash}", file=stream)
            if self.plan_key is not None:
                print(f"plan key: {self.plan_key}", file=stream)
        else:
            print(f"invalid: {self.file}", file=stream)
            for diagnostic in self.diagnostics:
                print(
                    f"  {diagnostic.code} {diagnostic.path}:"
                    f" {diagnostic.message}",
                    file=stream,
                )
                if diagnostic.hint:
                    print(f"      hint: {diagnostic.hint}", file=stream)
        if self.coverage != COVERAGE_FULL:
            print(f"coverage: {self.coverage_note}", file=stream)
        return self.exit_code


def _operational(command: str, args: argparse.Namespace, message: str) -> Outcome:
    """Build the outcome of a command that failed before any check."""

    return Outcome(
        exit_code=EXIT_OPERATIONAL,
        command=command,
        file=getattr(args, "file", None),
        catalog=getattr(args, "catalog", None),
        ok=False,
        coverage=COVERAGE_NONE,
        coverage_note=NOTHING_CHECKED_NOTE,
        message=message,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_validate(args: argparse.Namespace) -> Outcome:
    """Validate one document, structurally or against a catalog."""

    document = _read_file(args.file, "document")
    if args.catalog is None:
        try:
            parsed = parse_document(document)
        except MeltDocumentError as error:
            return Outcome(
                EXIT_INVALID,
                "validate",
                args.file,
                None,
                False,
                COVERAGE_STRUCTURAL,
                STRUCTURAL_NOTE,
                diagnostics=error.diagnostics,
            )
        return Outcome(
            EXIT_OK,
            "validate",
            args.file,
            None,
            True,
            COVERAGE_STRUCTURAL,
            STRUCTURAL_NOTE,
            document_hash=document_hash(parsed),
        )
    result = _compile(document, args.catalog)
    if result.ok:
        return Outcome(
            EXIT_OK,
            "validate",
            args.file,
            args.catalog,
            True,
            COVERAGE_FULL,
            FULL_NOTE,
            document_hash=result.plan.documentHash,
        )
    return Outcome(
        EXIT_INVALID,
        "validate",
        args.file,
        args.catalog,
        False,
        COVERAGE_FULL,
        FULL_NOTE,
        diagnostics=result.diagnostics,
    )


def run_compile(args: argparse.Namespace) -> Outcome:
    """Compile one document against the trusted catalog and registry."""

    if args.catalog is None:
        raise MeltOperationalError(
            "melt compile requires --catalog <file>: full compilation needs"
            " the trusted catalog and registry fixtures (SPEC.md section"
            " 11.3)"
        )
    document = _read_file(args.file, "document")
    result = _compile(document, args.catalog)
    if result.ok:
        return Outcome(
            EXIT_OK,
            "compile",
            args.file,
            args.catalog,
            True,
            COVERAGE_FULL,
            FULL_NOTE,
            document_hash=result.plan.documentHash,
            plan_key=result.plan.planKey,
            plan=result.plan,
        )
    return Outcome(
        EXIT_INVALID,
        "compile",
        args.file,
        args.catalog,
        False,
        COVERAGE_FULL,
        FULL_NOTE,
        diagnostics=result.diagnostics,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the command line."""

    parser = argparse.ArgumentParser(
        prog="melt",
        description=(
            "Melt command-line compiler: validate and compile melt/v0.1"
            " documents (SPEC.md section 11.3)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"melt {__version__} ({LANGUAGE_ID})",
    )
    commands = parser.add_subparsers(title="commands", dest="command", required=True)

    validate = commands.add_parser(
        "validate",
        help="validate one document",
        description=(
            "Validate one melt/v0.1 document. Without --catalog the check"
            " is structural and reports its limited coverage; with"
            " --catalog it runs the full compiler stages."
        ),
    )
    _add_common_arguments(validate)
    validate.set_defaults(handler=run_validate)

    compile_command = commands.add_parser(
        "compile",
        help="compile one document against a trusted catalog",
        description=(
            "Run full compilation of one melt/v0.1 document. Full"
            " compilation requires the catalog and registry fixtures, so"
            " --catalog is mandatory."
        ),
    )
    _add_common_arguments(compile_command)
    compile_command.set_defaults(handler=run_compile)
    return parser


def _add_common_arguments(command: argparse.ArgumentParser) -> None:
    """Add the arguments validate and compile share."""

    command.add_argument("file", help="path of the melt/v0.1 document")
    command.add_argument(
        "--catalog",
        metavar="FILE",
        default=None,
        help=(
            "trusted catalog file holding the measured artifacts and the"
            " action registry (for example fixtures/catalog.json)"
        ),
    )
    command.add_argument(
        "--json",
        action="store_true",
        help="print one structured result object instead of a summary",
    )


def main(argv: "list[str] | None" = None) -> int:
    """Run the command line and return the process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        outcome = args.handler(args)
    except MeltOperationalError as error:
        outcome = _operational(args.command, args, str(error))
    return outcome.report(args.json)


__all__ = [
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_OPERATIONAL",
    "MeltOperationalError",
    "Outcome",
    "build_parser",
    "main",
    "run_compile",
    "run_validate",
]
