"""The service-owned action registration registry (SPEC.md section 7.2).

The service alone registers scripts. A registration record carries
every verified fact of the capture:

- the registration identifier and its immutable revision;
- the captured script artifact with its content hash;
- the project root and working-directory policy;
- the resolved Python environment fingerprint;
- the parameter and input contract with the declared outputs;
- the required execution permission;
- the replay classification, fixed to ``nonReplaySafe`` for arbitrary
  Python.

Registration verifies, never executes: the stored bytes are reread,
hashed, sized, and decoded, and no project code runs at any point. The
registration identifier covers the captured bytes and the resolved
environment, so editing a script produces a new registration, while an
unchanged capture resolves to the registration it already has. A
changed contract over the same capture advances the revision of that
registration; earlier revisions stay immutable in the record stream.

The registry rebuilds from the committed registration records on
restart, the same way the source catalog rebuilds from artifact
records, so a reopened workspace restores its registrations without
rereading script bytes or executing anything (SPEC.md section 14).
"""

from __future__ import annotations

import hashlib
import platform
import sys
from dataclasses import dataclass
from typing import Mapping

from melt.schema.canonical import sha256_hash
from melt.schema.commands import RegistrationContract
from melt.schema.diagnostics import Diagnostic, DiagnosticCode, MeltSchemaError
from melt.schema.records import (
    RECORD_SCHEMA_VERSION,
    RegistrationRecord,
)
from runtime.compiler.catalog import (
    DEFAULT_REQUIRED_PERMISSION,
    RegistrationEntry,
)
from runtime.storage.artifacts import ArtifactStore
from runtime.workspace.tokens import CapturedArtifact

REGISTRATION_ID_PREFIX = "reg-"
"""Service-minted registration identifiers carry this prefix."""

REGISTRATION_ID_HEX = 24
"""Hex characters of derived identity: 96 bits, no collision in practice."""

SCRIPT_KIND = "script"
"""The capture kind of a script artifact (`text/x-python`)."""

SCRIPT_MEDIA_TYPE = "text/x-python"
"""The one media type a script capture accepts (SPEC.md section 12.6)."""

PROJECT_ROOT_POLICY = "workspace-contained"
"""The project root policy of a v0.1 registration.

The captured script runs against the workspace's own artifacts and
staging tree, never against an arbitrary host directory.
"""

WORKING_DIRECTORY_POLICY = "private-staging"
"""Each run works inside a private staging directory (SPEC.md 13.2)."""

REPLAY_CLASSIFICATION = "nonReplaySafe"
"""Arbitrary Python is never replay safe (SPEC.md section 7.2)."""

DEFAULT_ENVIRONMENT_ID = "python-local"
"""The identifier of the environment the service itself runs in."""

_ALLOWED_CONSTRAINT_FIELDS = {
    "string": frozenset(("maxLength", "enum")),
    "number": frozenset(("minimum", "maximum")),
    "integer": frozenset(("minimum", "maximum")),
    "boolean": frozenset(),
}
"""The constraint dimensions each parameter type carries (section 6.1)."""


class MeltRegistrationError(MeltSchemaError):
    """Raised when a script or contract cannot be registered."""


@dataclass(frozen=True)
class MeasuredScript:
    """One script artifact as the service verified it."""

    artifact: str
    byteLength: int
    mediaType: str


@dataclass(frozen=True)
class KnownEnvironment:
    """One Python environment the service resolved."""

    environment_id: str
    fingerprint: str
    description: str


def local_environment() -> KnownEnvironment:
    """Resolve the interpreter this service runs in.

    The Linux harness executes project scripts with the same pinned
    interpreter generation the service itself runs on, so its
    fingerprint is the resolved project environment of v0.1.
    """

    implementation = sys.implementation.name
    fingerprint = f"{implementation}-{platform.python_version()}-{sys.platform}"
    return KnownEnvironment(
        environment_id=DEFAULT_ENVIRONMENT_ID,
        fingerprint=fingerprint,
        description="The pinned interpreter this service runs in",
    )


class EnvironmentCatalog:
    """The environments this service may register scripts against.

    ``action.register`` requires an environment already known to the
    service (SPEC.md section 12.6). The local interpreter is always
    known; the host may declare further environments when it opens the
    workspace, each mapping an identifier to a resolved fingerprint.
    """

    __slots__ = ("_environments",)

    def __init__(self, declared: "Mapping[str, str] | None" = None):
        self._environments: dict[str, KnownEnvironment] = {
            local_environment().environment_id: local_environment()
        }
        for environment_id, fingerprint in (declared or {}).items():
            self._environments[environment_id] = KnownEnvironment(
                environment_id=environment_id,
                fingerprint=fingerprint,
                description="Declared by the host service configuration",
            )

    def resolve(self, environment_id: str) -> "KnownEnvironment | None":
        return self._environments.get(environment_id)

    def entries(self) -> dict[str, KnownEnvironment]:
        return dict(self._environments)


def _fail(message: str, path: str, hint: str = "", code: str = "") -> None:
    raise MeltRegistrationError(
        (
            Diagnostic(
                code=code or DiagnosticCode.ARTIFACT,
                path=path,
                message=message,
                hint=hint,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Script verification (SPEC.md sections 7.2 and 12.6)
# ---------------------------------------------------------------------------


def register_script(
    artifacts: ArtifactStore, captured: CapturedArtifact
) -> MeasuredScript:
    """Verify one captured script against its stored bytes.

    Registration never executes the script. The stored file must still
    hash to the captured reference, its size must match, and the bytes
    must decode as UTF-8, exactly as the capture validated them.
    """

    if captured.kind != SCRIPT_KIND:
        _fail(
            f"a {captured.kind!r} capture cannot register a script;"
            f" scripts arrive as {SCRIPT_MEDIA_TYPE!r}",
            path="/payload/scriptToken",
            hint="Capture the script with the text/x-python media type.",
        )
    try:
        payload = artifacts.read(captured.artifact)
    except OSError as error:
        _fail(
            f"captured script {captured.artifact} is unreadable: {error}",
            path="/payload/scriptToken",
            hint="Capture the script again; a restart withdraws every"
            " outstanding token.",
        )
    digest = sha256_hash(payload)
    if digest != captured.artifact:
        _fail(
            f"stored bytes hash to {digest}, not to the captured"
            f" reference {captured.artifact}",
            path="/payload/scriptToken",
        )
    if len(payload) != captured.byteLength:
        _fail(
            f"stored bytes hold {len(payload)} bytes, but the capture"
            f" recorded {captured.byteLength}",
            path="/payload/scriptToken",
        )
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(
            f"script bytes are not valid UTF-8: {error}",
            path="/payload/scriptToken",
        )
    return MeasuredScript(
        artifact=captured.artifact,
        byteLength=captured.byteLength,
        mediaType=captured.mediaType,
    )


def validate_contract(contract: RegistrationContract) -> None:
    """Reject a contract whose constraints do not fit their slot types.

    A registration contract is trusted compiler input, so a constraint
    dimension a slot type cannot carry is refused at registration
    instead of surfacing as compiler misbehavior later.
    """

    for name, slot in sorted(contract.parameters.items()):
        allowed = _ALLOWED_CONSTRAINT_FIELDS[slot.type]
        constraints = slot.constraints
        present = (
            {
                field
                for field in ("minimum", "maximum", "maxLength", "enum")
                if getattr(constraints, field) is not None
            }
            if constraints is not None
            else set()
        )
        unexpected = sorted(present - allowed)
        if unexpected:
            _fail(
                f"constraints for slot type {slot.type!r} cannot carry"
                f" {', '.join(unexpected)}",
                path=f"/payload/contract/parameters/{name}/constraints",
                hint="Carry only the constraint dimensions of the"
                " declared slot type.",
                code=DiagnosticCode.SCHEMA,
            )


# ---------------------------------------------------------------------------
# Registration identity and records (SPEC.md section 7.2)
# ---------------------------------------------------------------------------


def registration_identity(
    script_artifact: str, environment_fingerprint: str
) -> str:
    """Derive the registration identifier of one captured script.

    The identifier covers the script content hash and the resolved
    environment fingerprint. Editing the script therefore produces a
    new registration, and re-registering an unchanged capture in the
    same environment resolves to the registration it already has.
    """

    digest = hashlib.sha256(
        environment_fingerprint.encode("utf-8")
        + b"\0"
        + script_artifact.encode("ascii")
    ).hexdigest()
    return REGISTRATION_ID_PREFIX + digest[:REGISTRATION_ID_HEX]


def build_registration(
    *,
    registration_id: str,
    revision: int,
    script: MeasuredScript,
    environment_fingerprint: str,
    contract: RegistrationContract,
    created_at: str,
) -> RegistrationRecord:
    """Build one registration record from verified facts alone."""

    return RegistrationRecord.model_validate(
        {
            "recordType": "registration",
            "schemaVersion": RECORD_SCHEMA_VERSION,
            "registrationId": registration_id,
            "revision": revision,
            "script": {
                "artifact": script.artifact,
                "byteLength": script.byteLength,
                "mediaType": script.mediaType,
            },
            "projectRoot": PROJECT_ROOT_POLICY,
            "workingDirectoryPolicy": WORKING_DIRECTORY_POLICY,
            "environmentFingerprint": environment_fingerprint,
            "parameters": {
                name: slot.model_dump(mode="json", exclude_none=True)
                for name, slot in sorted(contract.parameters.items())
            },
            "inputs": dict(sorted(contract.inputs.items())),
            "outputs": dict(sorted(contract.outputs.items())),
            "requiredPermission": DEFAULT_REQUIRED_PERMISSION,
            "replayClassification": REPLAY_CLASSIFICATION,
            "createdAt": created_at,
        }
    )


def contract_of(record: RegistrationRecord) -> RegistrationContract:
    """Return the contract one committed registration declares."""

    return RegistrationContract(
        parameters=dict(record.parameters),
        inputs=dict(record.inputs),
        outputs=dict(record.outputs),
    )


def registration_entry_of(record: RegistrationRecord) -> RegistrationEntry:
    """Return the registry entry one committed registration rebuilds.

    The entry carries the admission facts of the record — the script
    artifact hash and the resolved environment fingerprint — so a
    merged registry answers the invocation-snapshot facts of SPEC.md
    section 7.3 for its committed registrations as well.
    """

    return RegistrationEntry(
        record.registrationId,
        record.revision,
        contract_of(record),
        record.requiredPermission,
        script_artifact=record.script.artifact,
        environment_fingerprint=record.environmentFingerprint,
    )


def registration_to_wire(record: RegistrationRecord) -> dict:
    """Convert one registration record into its compact wire form.

    Unset constraint dimensions stay absent instead of traveling as
    nulls, so a committed record names exactly the facts it captured.
    The result still parses back through ``parse_record``.
    """

    return record.model_dump(mode="json", by_alias=True, exclude_none=True)


__all__ = [
    "DEFAULT_ENVIRONMENT_ID",
    "DEFAULT_REQUIRED_PERMISSION",
    "EnvironmentCatalog",
    "KnownEnvironment",
    "MeltRegistrationError",
    "MeasuredScript",
    "PROJECT_ROOT_POLICY",
    "REPLAY_CLASSIFICATION",
    "REGISTRATION_ID_HEX",
    "REGISTRATION_ID_PREFIX",
    "SCRIPT_KIND",
    "SCRIPT_MEDIA_TYPE",
    "WORKING_DIRECTORY_POLICY",
    "build_registration",
    "contract_of",
    "local_environment",
    "registration_entry_of",
    "registration_identity",
    "registration_to_wire",
    "register_script",
    "validate_contract",
]
