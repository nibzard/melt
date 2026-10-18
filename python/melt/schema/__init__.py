"""Pydantic models for the `melt/v0.1` document and its contracts.

Modules:

- `document`: canonical document models, SPEC.md sections 4 through 10.
- `commands`: command envelope, operations, payloads, and framing, section 12.
- `records`: versioned persisted record schemas, section 17.
- `canonical`: RFC 8785 canonicalization and document hashing, section 4.2.
- `limits`: default service limits, section 15.2.
- `diagnostics`: shared diagnostic codes and shape, section 11.3.

Backlog task T003 implements these models. Parsing entry points are
`parse_document`, `parse_command`, and `parse_record`. Each raises its
error type with sorted diagnostics and precise JSON Pointer paths.
"""

from __future__ import annotations

from melt.schema.diagnostics import (
    DIAGNOSTIC_CODES,
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
)
from melt.schema.limits import DEFAULT_LIMITS, ServiceLimits

__all__ = [
    "DEFAULT_LIMITS",
    "DIAGNOSTIC_CODES",
    "Diagnostic",
    "DiagnosticCode",
    "MeltSchemaError",
    "ServiceLimits",
]
