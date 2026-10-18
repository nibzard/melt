"""Compiler: validation, resolution, plans, and diagnostics (SPEC.md 11).

Backlog task T005 implements the seven compiler stages. The compiler
emits render, state, interaction, action, dependency, identity, and
layout plans, and reports deterministic diagnostics sorted by path and
code. It performs no project execution, network access, or package
installation.

Typical use:

```python
from runtime.compiler import CompileRequest, load_catalog

source_catalog, action_registry = load_catalog(catalog_text)
result = compile_document(
    CompileRequest(
        document=document_text,
        source_catalog=source_catalog,
        action_registry=action_registry,
    )
)
plan = result.plan  # None when result.diagnostics is not empty
```
"""

from __future__ import annotations

from runtime.compiler.catalog import (
    ActionRegistry,
    Catalog,
    CatalogArtifact,
    CatalogRegistration,
    DEFAULT_REQUIRED_PERMISSION,
    EMPTY_ACTION_REGISTRY,
    EMPTY_SOURCE_CATALOG,
    MeltCatalogError,
    RegistrationEntry,
    SourceCatalog,
    load_catalog,
    parse_catalog,
    split_catalog,
)
from runtime.compiler.compiler import (
    COMPILER_IDENTIFIER,
    COMPILER_VERSION,
    CompileRequest,
    CompileResult,
    MeltCompileError,
    ParameterStateInput,
    RENDERER_REGISTRY,
    RendererSpec,
    build_plan,
    compile_document,
    compile_json,
    parameter_value_error,
    stage_decode,
    stage_references,
    stage_state,
    stage_structure,
    stage_types,
)
from runtime.compiler.plans import (
    PLAN_VERSION,
    CompiledPlan,
)

__all__ = [
    "ActionRegistry",
    "Catalog",
    "CatalogArtifact",
    "CatalogRegistration",
    "COMPILER_IDENTIFIER",
    "COMPILER_VERSION",
    "CompileRequest",
    "CompileResult",
    "CompiledPlan",
    "DEFAULT_REQUIRED_PERMISSION",
    "EMPTY_ACTION_REGISTRY",
    "EMPTY_SOURCE_CATALOG",
    "MeltCatalogError",
    "MeltCompileError",
    "PLAN_VERSION",
    "ParameterStateInput",
    "RENDERER_REGISTRY",
    "RegistrationEntry",
    "RendererSpec",
    "SourceCatalog",
    "build_plan",
    "compile_document",
    "compile_json",
    "load_catalog",
    "parse_catalog",
    "split_catalog",
    "stage_decode",
    "stage_references",
    "stage_state",
    "stage_structure",
    "stage_types",
]
