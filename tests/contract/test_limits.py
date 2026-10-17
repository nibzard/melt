"""Default limit table tests (SPEC.md section 15.2)."""

from __future__ import annotations

from melt.schema.diagnostics import DiagnosticCode
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits, limit_diagnostic


def test_default_limits_match_the_specification():
    limits = DEFAULT_LIMITS
    assert limits.control_frame_bytes == 1024 * 1024
    assert limits.operations_per_mutation == 128
    assert limits.max_projections == 32
    assert limits.max_parameters == 256
    assert limits.max_actions == 64
    assert limits.max_bindings == 256
    assert limits.max_controls_per_projection == 32
    assert limits.max_layout_depth == 8
    assert limits.table_page_rows == 256
    assert limits.table_page_columns == 128
    assert limits.table_page_bytes == 1024 * 1024
    assert limits.native_table_cache_bytes == 64 * 1024 * 1024
    assert limits.png_max_axis == 16384
    assert limits.png_max_total_pixels == 64_000_000
    assert limits.displayed_text_bytes == 10 * 1024 * 1024
    assert limits.run_log_bytes_per_stream == 10 * 1024 * 1024
    assert limits.staged_output_bytes_per_run == 1024 * 1024 * 1024


def test_limits_are_frozen_service_policy():
    limits = ServiceLimits()
    assert limits == DEFAULT_LIMITS
    try:
        limits.max_projections = 1000  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("service limits must stay frozen")


def test_limit_diagnostic_names_the_resource_and_bound():
    diagnostic = limit_diagnostic("operations per mutation", 128, "/operations")
    assert diagnostic.code == DiagnosticCode.LIMIT
    assert diagnostic.path == "/operations"
    assert "operations per mutation" in diagnostic.message
    assert "128" in diagnostic.message
