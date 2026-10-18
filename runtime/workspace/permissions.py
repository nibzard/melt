"""Execution permission grants and their rechecks (SPEC.md 15.1).

Document validity is not execution authority. A valid document, source
text, model output, and terminal output never grant permission: only
host-generated permission controls do, through the authenticated native
permission endpoint on the service (sections 12.6 and 15.1). Grants
live in the service record stream, outside every exported document, and
every grant is rechecked at admission, immediately before launch, and
again at publication (sections 13.1, 13.3, and 15.1).

Two grant kinds exist:

- ``specificRun`` covers exactly one captured admission intent. It pins
  the action, registration, and output-group revisions it approved, so
  an approval never executes a newer request under an older one: when
  the definitions change, the grant stops covering and a new admission
  with its own captured values is required (SPEC.md section 12.6).
- ``projectExecution`` covers every admission of one trusted
  registration under the explicit project trust policy the host
  recorded. The registration identifier covers the captured script
  bytes and the resolved environment, so an edited script is a new
  registration the grant does not cover (SPEC.md section 7.2).

Revocation is durable and immediate. A revoked grant covers nothing,
and a run whose authority was revoked is denied at launch and at
publication; there, revocation blocks publication and requests
cancellation (SPEC.md sections 13.3 and 13.5).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable, Mapping

from melt.schema.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    MeltSchemaError,
)
from melt.schema.records import (
    RECORD_SCHEMA_VERSION,
    PermissionGrantRecord,
    RunRecord,
    parse_record,
)

GRANT_ID_PREFIX = "meltgrant_"
"""Service-minted grant identifiers carry this prefix."""

INTENT_ID_PREFIX = "meltintent_"
"""Service-minted admission intent references carry this prefix."""

_ID_ENTROPY_BYTES = 18
"""Random bytes per identifier: 144 bits, far beyond guessing range."""

GRANT_KINDS = ("specificRun", "projectExecution")
"""The two grant kinds of SPEC.md section 15.1."""

REVOKED_REASON = "permission-revoked"
MISSING_REASON = "missing-grant"
DRIFTED_REASON = "grant-no-longer-covers"


class MeltPermissionError(MeltSchemaError):
    """Raised when a grant cannot be minted or revoked."""


@dataclass(frozen=True)
class AuthorityDecision:
    """The outcome of one permission recheck.

    ``grant_id`` names the live grant that covers the request, or stays
    None when no grant does. ``required_permission`` is the permission
    the checkpoint asked about, or None when the caller could not name
    one. ``reason`` is ``granted`` on success, or one of
    ``missing-grant``, ``permission-revoked``, and
    ``grant-no-longer-covers``.
    """

    granted: bool
    required_permission: "str | None"
    grant_id: "str | None" = None
    reason: str = ""


def mint_grant_id() -> str:
    """Mint one opaque grant identifier."""

    return GRANT_ID_PREFIX + secrets.token_urlsafe(_ID_ENTROPY_BYTES)


def mint_intent_id() -> str:
    """Mint one opaque admission intent reference.

    Admission issues this reference with every approval-required result,
    so the host approval binds to the exact captured intent (SPEC.md
    section 12.6). The scheduling subsystem keeps the persisted intent
    records; the reference stays opaque to the host.
    """

    return INTENT_ID_PREFIX + secrets.token_urlsafe(_ID_ENTROPY_BYTES)


def build_grant(**facts) -> PermissionGrantRecord:
    """Build one grant record from verified facts alone.

    The record model itself enforces the kind-specific scope: a
    ``specificRun`` grant pins its intent, and a ``projectExecution``
    grant pins nothing. Raises ``MeltPermissionError`` with the record's
    own diagnostics when the facts do not form a grant.
    """

    payload = {
        "recordType": "permissionGrant",
        "schemaVersion": RECORD_SCHEMA_VERSION,
        **facts,
    }
    try:
        return parse_record(payload)
    except MeltSchemaError as error:
        raise MeltPermissionError(error.diagnostics) from error


def is_live(record: PermissionGrantRecord) -> bool:
    """Report whether one grant still covers anything.

    Revocation is durable: a revoked grant never covers again, at any
    checkpoint (SPEC.md sections 13.3 and 15.1).
    """

    return record.revokedAt is None


def revocation_blocks(decision: AuthorityDecision) -> bool:
    """Report whether a denial comes from revocation.

    The launch and publication paths answer True exactly when SPEC.md
    section 13.5 applies: block the publication immediately and request
    cancellation of the owning run.
    """

    return not decision.granted and decision.reason == REVOKED_REASON


# ---------------------------------------------------------------------------
# Coverage of one request by one grant
# ---------------------------------------------------------------------------


def _same_permission(grant: PermissionGrantRecord, required: "str | None") -> bool:
    return required is None or grant.requiredPermission == required


def _covers_registration(
    grant: PermissionGrantRecord,
    registration_id: str,
    required_permission: "str | None",
) -> bool:
    """A project execution grant covers one trusted registration."""

    return (
        grant.kind == "projectExecution"
        and grant.registrationId == registration_id
        and _same_permission(grant, required_permission)
    )


def _intent_pins_match(
    grant: PermissionGrantRecord,
    *,
    registration_id: str,
    registration_revision: "int | None",
    action_id: "str | None",
    action_revision: "int | None",
    output_group: "str | None",
    output_group_revision: "int | None" = None,
    generation: "int | None" = None,
) -> bool:
    """Report whether a specific-run grant still pins these definitions.

    A dimension the grant leaves open (``outputGroupRevision`` and
    ``generation`` before the scheduling subsystem owns them) matches
    any value, so the grant never silently narrows a captured intent.
    """

    return (
        grant.kind == "specificRun"
        and grant.registrationId == registration_id
        and grant.registrationRevision == registration_revision
        and grant.actionId == action_id
        and grant.actionRevision == action_revision
        and grant.outputGroup == output_group
        and (
            grant.outputGroupRevision is None
            or grant.outputGroupRevision == output_group_revision
        )
        and (grant.generation is None or grant.generation == generation)
    )


def _search(
    grants: Mapping[str, PermissionGrantRecord],
    covers: Callable[[PermissionGrantRecord], bool],
) -> "tuple[str | None, str | None]":
    """Return the live covering grant and the revoked matching grant.

    Both identifiers are the lowest grant identifier in sorted order, so
    the decision is deterministic for one grant set. A live grant always
    wins over a revoked one with the same scope.
    """

    live: "str | None" = None
    revoked: "str | None" = None
    for grant_id in sorted(grants):
        grant = grants[grant_id]
        if not covers(grant):
            continue
        if is_live(grant):
            live = live if live is not None else grant_id
        else:
            revoked = revoked if revoked is not None else grant_id
    return live, revoked


def _decide(
    required_permission: "str | None",
    live: "str | None",
    revoked: "str | None",
    drifted: bool,
) -> AuthorityDecision:
    if live is not None:
        return AuthorityDecision(
            True, required_permission, grant_id=live, reason="granted"
        )
    if revoked is not None:
        return AuthorityDecision(
            False, required_permission, reason=REVOKED_REASON
        )
    reason = DRIFTED_REASON if drifted else MISSING_REASON
    return AuthorityDecision(False, required_permission, reason=reason)


# ---------------------------------------------------------------------------
# The three rechecks (SPEC.md sections 13.1, 13.3, and 15.1)
# ---------------------------------------------------------------------------


def authority_at_admission(
    grants: Mapping[str, PermissionGrantRecord],
    *,
    required_permission: str,
    registration_id: str,
    registration_revision: int,
    action_id: str,
    action_revision: int,
    output_group: str,
    output_group_revision: "int | None" = None,
    generation: "int | None" = None,
    intent_id: str,
) -> AuthorityDecision:
    """Recheck authority before one intent is admitted (section 13.1).

    A specific-run grant covers only its exact intent: the intent
    reference and every pinned definition revision must match. A project
    execution grant covers the registration across admissions. Without
    a covering live grant the caller answers the successful
    approval-required admission result, which grants nothing.
    """

    def _exact(grant: PermissionGrantRecord) -> bool:
        return (
            grant.intentId == intent_id
            and _same_permission(grant, required_permission)
            and _intent_pins_match(
                grant,
                registration_id=registration_id,
                registration_revision=registration_revision,
                action_id=action_id,
                action_revision=action_revision,
                output_group=output_group,
                output_group_revision=output_group_revision,
                generation=generation,
            )
        )

    def _project(grant: PermissionGrantRecord) -> bool:
        return _covers_registration(grant, registration_id, required_permission)

    live, revoked = _search(grants, _exact)
    if live is not None:
        return _decide(required_permission, live, revoked, False)
    # A live project grant still covers when the intent's own grant was
    # revoked, so the broader scope is searched before denying. An
    # approval whose definitions changed no longer covers: the intent
    # needs a new admission with its own captured values (SPEC.md
    # section 12.6).
    drifted = any(
        grant.kind == "specificRun" and grant.intentId == intent_id
        for grant in grants.values()
    )
    project_live, project_revoked = _search(grants, _project)
    return _decide(
        required_permission,
        project_live,
        revoked if revoked is not None else project_revoked,
        drifted,
    )


def _authority_for_run(
    grants: Mapping[str, PermissionGrantRecord],
    run: RunRecord,
    required_permission: "str | None",
) -> AuthorityDecision:
    """Recheck authority for one admitted run at a later checkpoint."""

    def _captured(grant: PermissionGrantRecord) -> bool:
        return (
            grant.grantId == run.permissionDecision
            and _same_permission(grant, required_permission)
            and _intent_pins_match(
                grant,
                registration_id=run.registrationId,
                registration_revision=run.registrationRevision,
                action_id=run.actionId,
                action_revision=run.actionRevision,
                output_group=run.outputGroup,
                output_group_revision=run.outputGroupRevision,
                generation=run.generation,
            )
        )

    def _specific(grant: PermissionGrantRecord) -> bool:
        return _same_permission(
            grant, required_permission
        ) and _intent_pins_match(
            grant,
            registration_id=run.registrationId,
            registration_revision=run.registrationRevision,
            action_id=run.actionId,
            action_revision=run.actionRevision,
            output_group=run.outputGroup,
            output_group_revision=run.outputGroupRevision,
            generation=run.generation,
        )

    def _project(grant: PermissionGrantRecord) -> bool:
        return _covers_registration(grant, run.registrationId, required_permission)

    # A live broader grant still covers when the narrower one was
    # revoked, so every scope is searched before a denial is settled.
    revoked_seen: "str | None" = None
    for covers in (_captured, _specific, _project):
        live, revoked = _search(grants, covers)
        if live is not None:
            return _decide(required_permission, live, revoked_seen, False)
        revoked_seen = revoked_seen if revoked_seen is not None else revoked
    return _decide(required_permission, None, revoked_seen, False)


def authority_at_launch(
    grants: Mapping[str, PermissionGrantRecord],
    run: RunRecord,
    *,
    required_permission: "str | None" = None,
) -> AuthorityDecision:
    """Recheck authority immediately before launch (section 13.1).

    The scheduling path calls this with the admitted run record right
    before it starts the project process. The run carries its captured
    permission decision; a grant that was revoked after admission
    denies the launch.
    """

    return _authority_for_run(grants, run, required_permission)


def authority_at_publication(
    grants: Mapping[str, PermissionGrantRecord],
    run: RunRecord,
    *,
    required_permission: "str | None" = None,
) -> AuthorityDecision:
    """Recheck authority inside the publication transaction (13.3).

    A run is eligible to become current only while its execution
    permission has not been revoked. A denial with the revocation
    reason blocks the publication immediately and requests cancellation
    of the owning run (SPEC.md sections 13.3 and 13.5).
    """

    return _authority_for_run(grants, run, required_permission)


def permission_error(
    message: str, path: str, hint: str = "", code: str = DiagnosticCode.PERMISSION
) -> MeltPermissionError:
    """Build one error of the host permission endpoint."""

    return MeltPermissionError(
        (
            Diagnostic(
                code=code,
                path=path,
                message=message,
                hint=hint,
            ),
        )
    )


__all__ = [
    "DRIFTED_REASON",
    "GRANT_ID_PREFIX",
    "GRANT_KINDS",
    "INTENT_ID_PREFIX",
    "MISSING_REASON",
    "REVOKED_REASON",
    "AuthorityDecision",
    "MeltPermissionError",
    "authority_at_admission",
    "authority_at_launch",
    "authority_at_publication",
    "build_grant",
    "is_live",
    "mint_grant_id",
    "mint_intent_id",
    "permission_error",
    "revocation_blocks",
]
