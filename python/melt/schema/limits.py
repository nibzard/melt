"""Default service limits (SPEC.md section 15.2).

Limits are configurable service policy. Project code can never raise
them through a document or command. Every violation returns an `E_LIMIT`
diagnostic that names the resource and the allowed bound.
"""

from __future__ import annotations

from dataclasses import dataclass

from melt.schema.diagnostics import Diagnostic, DiagnosticCode, with_code

_MIB = 1024 * 1024


@dataclass(frozen=True)
class ServiceLimits:
    """The round-1 default limits from SPEC.md section 15.2."""

    control_frame_bytes: int = 1 * _MIB
    operations_per_mutation: int = 128
    max_projections: int = 32
    max_parameters: int = 256
    max_actions: int = 64
    max_bindings: int = 256
    max_controls_per_projection: int = 32
    max_layout_depth: int = 8
    table_page_rows: int = 256
    table_page_columns: int = 128
    table_page_bytes: int = 1 * _MIB
    native_table_cache_bytes: int = 64 * _MIB
    png_max_axis: int = 16384
    png_max_total_pixels: int = 64_000_000
    displayed_text_bytes: int = 10 * _MIB
    run_log_bytes_per_stream: int = 10 * _MIB
    staged_output_bytes_per_run: int = 1024 * _MIB


DEFAULT_LIMITS = ServiceLimits()


def limit_error(resource: str, allowed: int, message: str = "") -> str:
    """Return a validator message for an `E_LIMIT` violation.

    The message names the resource and the allowed bound, as SPEC.md
    section 15.2 requires.
    """

    bound = f"{resource} exceeds the allowed bound of {allowed}"
    return with_code(DiagnosticCode.LIMIT, f"{bound}. {message}".strip())


def limit_diagnostic(resource: str, allowed: int, path: str) -> Diagnostic:
    """Build an `E_LIMIT` diagnostic for one resource."""
    return Diagnostic(
        code=DiagnosticCode.LIMIT,
        path=path,
        message=f"{resource} exceeds the allowed bound of {allowed}.",
        relatedPaths=(),
        hint="Reduce the request, or ask the operator to raise the service policy.",
    )
