"""The versioned run manifest (SPEC.md section 13.2).

The runner receives its contract through one dedicated manifest file.
The service builds the manifest from the admission snapshot; the
manifest carries no authority of its own. It holds:

- the run identifier the service issued;
- the workspace and action the run belongs to;
- the path of the materialized script inside the staging tree;
- the captured parameter values;
- the absolute path of every captured input artifact;
- the declared output names with their kinds;
- the private staging directory of the run.

Changes to the workspace cannot change an admitted run's inputs
(SPEC.md section 7.3), so the manifest is written once, before the
process starts, and never rewritten while the process lives.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Annotated, Literal, Mapping, Union

from pydantic import AfterValidator, StrictStr, StringConstraints, ValidationError

from melt.schema.canonical import parse_json
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostics_from_pydantic_errors,
    with_code,
)
from melt.schema.document import (
    EntityId,
    JsonScalar,
    MeltModel,
    PositiveSafeInt,
)

RUN_MANIFEST_VERSION = 1
"""The manifest version of `melt/v0.1` runs."""

RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\z"
"""Run identifiers follow the service run-directory syntax (section 14).

The service validates this shape before it names a staging directory.
The manifest repeats the check, so a malformed identifier fails at the
boundary instead of inside a run.
"""

RunId = Annotated[StrictStr, StringConstraints(pattern=RUN_ID_PATTERN)]

OutputKind = Literal["table", "image", "text"]


def _absolute_path(value: str) -> str:
    """Require one absolute path, as the staging contract states."""

    if not PurePath(value).is_absolute():
        raise ValueError(
            f"path {value!r} is not absolute; the manifest names files"
            " inside the workspace tree by absolute path"
        )
    return value


AbsolutePath = Annotated[StrictStr, AfterValidator(_absolute_path)]

CapturedValue = Union[JsonScalar, None]
"""A captured parameter value: a JSON scalar or null."""


def _manifest_version(value: int) -> int:
    if value != RUN_MANIFEST_VERSION:
        raise ValueError(
            with_code(
                DiagnosticCode.VERSION,
                f"unsupported manifest version {value}; this runner"
                f" reads version {RUN_MANIFEST_VERSION}",
            )
        )
    return value


ManifestVersion = Annotated[PositiveSafeInt, AfterValidator(_manifest_version)]


class RunManifest(MeltModel):
    """One captured execution manifest.

    Every field is required. Unknown fields are refused, so a future
    manifest version fails loudly instead of silently dropping facts.
    """

    manifestVersion: ManifestVersion
    runId: RunId
    workspaceId: EntityId
    actionId: EntityId
    scriptPath: AbsolutePath
    parameters: dict[EntityId, CapturedValue]
    inputs: dict[EntityId, AbsolutePath]
    outputs: dict[EntityId, OutputKind]
    stagingDirectory: AbsolutePath


class MeltManifestError(MeltSchemaError):
    """Raised when a run manifest fails validation."""


def parse_manifest(data: "str | bytes | bytearray | Mapping") -> RunManifest:
    """Parse and validate one run manifest.

    An unsupported manifest version reports `E_VERSION` before any
    other field is checked, so a future format identifies itself
    instead of failing on an unrelated field.
    """

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltManifestError(error.diagnostics) from error
    else:
        value = dict(data)
    version = value.get("manifestVersion") if isinstance(value, dict) else None
    if version != RUN_MANIFEST_VERSION:
        raise MeltManifestError(
            (
                Diagnostic(
                    code=DiagnosticCode.VERSION,
                    path="/manifestVersion",
                    message=f"unsupported manifest version {version!r};"
                    f" this runner reads version {RUN_MANIFEST_VERSION}",
                    hint="Regenerate the manifest with the service that"
                    " admitted the run.",
                ),
            )
        )
    try:
        return RunManifest.model_validate(value)
    except ValidationError as error:
        raise MeltManifestError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error


def manifest_to_wire(manifest: RunManifest) -> dict:
    """Return the wire shape of one manifest."""

    return manifest.model_dump(mode="json", by_alias=True)


def build_manifest(
    *,
    run_id: str,
    workspace_id: str,
    action_id: str,
    script_path: "str | PurePath",
    parameters: Mapping[str, object],
    inputs: "Mapping[str, str | PurePath]",
    outputs: Mapping[str, str],
    staging_directory: "str | PurePath",
) -> RunManifest:
    """Build one manifest from its parts and validate it.

    The service-side launcher uses this helper, so every manifest it
    writes passes the same validation the runner applies on read.
    """

    return parse_manifest(
        {
            "manifestVersion": RUN_MANIFEST_VERSION,
            "runId": run_id,
            "workspaceId": workspace_id,
            "actionId": action_id,
            "scriptPath": str(script_path),
            "parameters": dict(parameters),
            "inputs": {name: str(path) for name, path in inputs.items()},
            "outputs": dict(outputs),
            "stagingDirectory": str(staging_directory),
        }
    )


__all__ = [
    "RUN_MANIFEST_VERSION",
    "RunId",
    "RunManifest",
    "MeltManifestError",
    "OutputKind",
    "build_manifest",
    "manifest_to_wire",
    "parse_manifest",
]
