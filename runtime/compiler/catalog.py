"""Trusted catalog and registry inputs of the compiler (SPEC.md 11.1).

Compilation reads two service-owned metadata sets besides the document:

- The source catalog: measured schema, media type, kind, and byte
  length of every artifact the document may cite.
- The action registry: the registration contracts that action mappings
  must satisfy exactly (SPEC.md section 7.2).

The fixture catalog `fixtures/catalog.json` carries both in one file.
`load_catalog` splits it into a `SourceCatalog` and an `ActionRegistry`.
Both are compiler inputs, never document fields: project code cannot
forge them (SPEC.md sections 5.1 and 7.2).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field, StrictStr, TypeAdapter, ValidationError

from melt.schema.canonical import parse_json
from melt.schema.commands import RegistrationContract
from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
    diagnostics_from_pydantic_errors,
)
from melt.schema.document import (
    ArtifactHash,
    MeltModel,
    NonEmptyStr,
    NonNegativeSafeInt,
    PositiveSafeInt,
)
from melt.schema.semantic import AnyMeasuredSchema

DEFAULT_REQUIRED_PERMISSION = "execute_project_code"
"""The permission every fixture registration requires (note.md section 4)."""


class CatalogArtifact(MeltModel):
    """One artifact as the service measured it (SPEC.md section 5.1).

    The measured schema is any supported language's: a v0.1 table,
    image, or text schema, or the graph identity map of
    `melt/semantic-draft-1` (RFC 001 section 7.1).
    """

    path: NonEmptyStr
    kind: StrictStr
    mediaType: StrictStr
    byteLength: NonNegativeSafeInt
    schema_: AnyMeasuredSchema = Field(alias="schema")

    @property
    def schema(self) -> AnyMeasuredSchema:
        """The measured schema, under its wire name."""

        return self.schema_


def _wrap_registration(value: object):
    """Move the contract keys of a registration entry under `contract`.

    The catalog form carries the wire contract of `action.register`
    beside a revision and an optional permission. Reusing that model
    keeps a single definition of a contract (SPEC.md section 17).
    """

    if not isinstance(value, dict):
        raise ValueError("registration must be an object")
    contract = {
        key: value[key] for key in ("parameters", "inputs", "outputs") if key in value
    }
    wrapped = {
        key: item
        for key, item in value.items()
        if key not in ("parameters", "inputs", "outputs")
    }
    if contract:
        wrapped["contract"] = contract
    return wrapped


class CatalogRegistration(MeltModel):
    """One registration contract with its immutable revision (7.2).

    The catalog form omits the service-only capture fields of a full
    `RegistrationRecord`, such as the script artifact and environment
    fingerprint. `requiredPermission` defaults to the project execution
    permission, because no fixture overrides it.
    """

    revision: PositiveSafeInt
    requiredPermission: NonEmptyStr = DEFAULT_REQUIRED_PERMISSION
    contract: RegistrationContract


class Catalog(MeltModel):
    """The combined trusted catalog and action registry fixture shape."""

    artifacts: dict[ArtifactHash, CatalogArtifact]
    registrations: dict[NonEmptyStr, Annotated[CatalogRegistration, BeforeValidator(_wrap_registration)]]


class MeltCatalogError(MeltSchemaError):
    """Raised when a catalog input itself is malformed.

    A malformed catalog is an operational failure, not an invalid
    document: the caller loaded the wrong file (SPEC.md section 11.3).
    """


_CATALOG_ADAPTER: TypeAdapter | None = None


def _catalog_adapter() -> TypeAdapter:
    global _CATALOG_ADAPTER
    if _CATALOG_ADAPTER is None:
        _CATALOG_ADAPTER = TypeAdapter(Catalog)
    return _CATALOG_ADAPTER


def parse_catalog(data: "str | bytes | bytearray | dict") -> Catalog:
    """Parse the combined catalog fixture into a `Catalog` model.

    Raises `MeltCatalogError` when the catalog itself does not hold a
    valid measured schema or registration contract.
    """

    if isinstance(data, (str, bytes, bytearray)):
        try:
            value = parse_json(data)
        except MeltSchemaError as error:
            raise MeltCatalogError(error.diagnostics) from error
    else:
        value = data
    if not isinstance(value, dict):
        raise MeltCatalogError(
            (
                Diagnostic(
                    code=DiagnosticCode.SCHEMA,
                    path="",
                    message="catalog must be a JSON object",
                ),
            )
        )
    try:
        return _catalog_adapter().validate_python(value)
    except ValidationError as error:
        raise MeltCatalogError(
            diagnostics_from_pydantic_errors(error.errors(), DiagnosticCode.SCHEMA)
        ) from error


class SourceCatalog:
    """The measured artifact metadata the compiler checks against."""

    __slots__ = ("artifacts",)

    def __init__(self, artifacts: "dict[str, CatalogArtifact] | None" = None):
        self.artifacts: dict[str, CatalogArtifact] = dict(artifacts or {})

    def entry(self, artifact: str) -> "CatalogArtifact | None":
        return self.artifacts.get(artifact)


class ActionRegistry:
    """The registration contracts the compiler resolves against."""

    __slots__ = ("registrations",)

    def __init__(self, registrations: "dict[str, RegistrationEntry] | None" = None):
        self.registrations: dict[str, RegistrationEntry] = dict(registrations or {})

    def entry(self, registration_id: str) -> "RegistrationEntry | None":
        return self.registrations.get(registration_id)


class RegistrationEntry:
    """One registration as the compiler consumes it.

    ``script_artifact`` and ``environment_fingerprint`` are optional
    admission facts: a committed service registration always carries
    them, while a fixture catalog entry may cite them so an admission
    against it can capture its invocation snapshot (SPEC.md section
    7.3). The compiler itself never reads them.
    """

    __slots__ = (
        "registration_id",
        "revision",
        "contract",
        "required_permission",
        "script_artifact",
        "environment_fingerprint",
    )

    def __init__(
        self,
        registration_id: str,
        revision: int,
        contract: RegistrationContract,
        required_permission: str = DEFAULT_REQUIRED_PERMISSION,
        *,
        script_artifact: "str | None" = None,
        environment_fingerprint: "str | None" = None,
    ):
        self.registration_id = registration_id
        self.revision = revision
        self.contract = contract
        self.required_permission = required_permission
        self.script_artifact = script_artifact
        self.environment_fingerprint = environment_fingerprint


EMPTY_SOURCE_CATALOG = SourceCatalog()
EMPTY_ACTION_REGISTRY = ActionRegistry()


def split_catalog(catalog: Catalog) -> tuple[SourceCatalog, ActionRegistry]:
    """Split a parsed catalog into its two compiler inputs."""

    source_catalog = SourceCatalog(catalog.artifacts)
    registry = ActionRegistry(
        {
            registration_id: RegistrationEntry(
                registration_id,
                entry.revision,
                entry.contract,
                entry.requiredPermission,
            )
            for registration_id, entry in catalog.registrations.items()
        }
    )
    return source_catalog, registry


def load_catalog(data: "str | bytes | bytearray | dict") -> tuple[SourceCatalog, ActionRegistry]:
    """Parse the catalog fixture and return both compiler inputs."""

    return split_catalog(parse_catalog(data))


__all__ = [
    "ActionRegistry",
    "Catalog",
    "CatalogArtifact",
    "CatalogRegistration",
    "DEFAULT_REQUIRED_PERMISSION",
    "EMPTY_ACTION_REGISTRY",
    "EMPTY_SOURCE_CATALOG",
    "MeltCatalogError",
    "RegistrationEntry",
    "SourceCatalog",
    "load_catalog",
    "parse_catalog",
    "split_catalog",
]
