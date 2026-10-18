"""Workspace service: commands, revisions, receipts, and snapshots (SPEC.md section 12).

The service owns revisions, stored state, permissions, jobs, and
publication. The framed IPC envelope, the mutation transaction against
a private candidate, expected-revision conflicts, compiler read-set
checks, state migrations, request receipts, the single commit sequence,
and ordered epoch-guarded notifications form the core. The service
methods of section 12.6 ride the same envelope: the session handshake,
workspace creation, inspection, preview, subscription, control
activation, and run cancellation and inspection. ``source.import``
registers one captured artifact as a new immutable source version: the
service verifies the stored bytes, their hash, size, kind, media type,
and measured schema before anything is committed, and no document
command can forge an artifact record. ``action.register`` stores one
trusted registration: the captured script artifact and hash, the
project root and working-directory policy, the environment
fingerprint, the contract, the required permission, and the fixed
``nonReplaySafe`` classification. Registration verifies bytes and
never executes the script; edited scripts become new registrations.
Artifact and script tokens are
minted by the host capture operation and stay workspace-scoped for
15 minutes.

Execution authority is separate from every document (SPEC.md section
15.1). Specific-run and project execution grants live in the service
record stream, minted and revoked only through the authenticated host
permission endpoint that no command envelope reaches; admission,
launch, and publication recheck them, and revocation blocks
publication immediately. Model credentials never reach a project
process: launch sites build child environments through
``runtime.execution.project_process_environment``.

The service owns scheduling (SPEC.md sections 7.3, 9.3, and 13.1):
admissions capture the invocation snapshot, reserve their generation
in the admitting transaction, and keep one pending intent per output
group; debounce timers dispatch due intents, and one launch path
rechecks permission, registration availability, and the environment
fingerprint immediately before the fresh process starts.

Typical use:

```python
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

with WorkspaceService.open(platform, "sales-demo") as service:
    response = service.dispatch(envelope)  # any parsed command envelope
    service.dispatch_due_intents()          # fire due debounce timers
    service.launch_next_run()               # drive the one execution slot
```

Requests also arrive framed on a private channel: a four-byte
big-endian length prefix followed by UTF-8 JSON, rejected above the
control frame limit before any payload is read (SPEC.md section 12.1).

```python
service.serve(read, write)  # one request frame in, one response out
```
"""

from __future__ import annotations

from runtime.workspace.importer import (
    IMPORT_KINDS,
    MeasuredArtifact,
    MeltImportError,
    append_source_version,
    import_version,
    measure_artifact,
    next_version_number,
    register_artifact,
    run_version,
)
from runtime.workspace.inspection import (
    ACTIVE_RUN_STATES,
    active_run_records,
    current_diagnostics,
    preview_effects,
    snapshot_result,
)
from runtime.workspace.mutations import (
    EntityTouch,
    MutationPlan,
    PendingSet,
    expectation_diagnostics,
    initial_state_from_document,
    plan_mutation,
)
from melt.schema.records import (
    OutputGroupRecord,
    PendingIntentRecord,
    PermissionGrantRecord,
)
from runtime.workspace.permissions import (
    GRANT_ID_PREFIX,
    GRANT_KINDS,
    INTENT_ID_PREFIX,
    AuthorityDecision,
    MeltPermissionError,
    authority_at_admission,
    authority_at_launch,
    authority_at_publication,
    is_live,
    mint_grant_id,
    mint_intent_id,
    revocation_blocks,
)
from runtime.workspace.registry import (
    DEFAULT_ENVIRONMENT_ID,
    EnvironmentCatalog,
    KnownEnvironment,
    MeltRegistrationError,
    MeasuredScript,
    build_registration,
    contract_of,
    local_environment,
    registration_entry_of,
    registration_identity,
    register_script,
    validate_contract,
)
from runtime.workspace.scheduling import (
    AdmissionRecords,
    InvocationSnapshot,
    RunLaunchOutcome,
    build_pending_intent,
    build_run_record,
    changed_output_groups,
    due_intents,
    is_newest_generation,
    mint_run_id,
    reserve_generation,
    triggered_actions,
)
from runtime.workspace.service import (
    LANGUAGE_VERSION,
    REVISION_PRECONDITION_METHODS,
    SERVICE_IDENTIFIER,
    SERVICE_VERSION,
    SNAPSHOT_REQUIRED,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNANSWERED_METHODS,
    NotificationWindow,
    WorkspaceService,
)
from runtime.workspace.state import (
    ARTIFACT_KIND,
    ENTITY_COLLECTIONS,
    INTENT_COLLECTION,
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    OUTPUT_GROUP_COLLECTION,
    OUTPUT_GROUP_KIND,
    PENDING_INTENT_KIND,
    PERMISSION_COLLECTION,
    PERMISSION_KIND,
    RECEIPT_KIND,
    REGISTRATION_COLLECTION,
    REGISTRATION_KIND,
    RUN_KIND,
    SNAPSHOT_KIND,
    SOURCE_COLLECTION,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    intent_path,
    output_group_path,
    permission_path,
    pointer_key,
    registration_path,
    run_path,
    source_path,
    state_path,
    values_equal,
)
from runtime.workspace.tokens import (
    CAPTURE_KINDS,
    TOKEN_LIFETIME,
    TOKEN_PREFIX,
    CapturedArtifact,
    MeltTokenError,
    TokenStore,
)

__all__ = [
    "ACTIVE_RUN_STATES",
    "ARTIFACT_KIND",
    "CAPTURE_KINDS",
    "DEFAULT_ENVIRONMENT_ID",
    "ENTITY_COLLECTIONS",
    "EntityTouch",
    "GRANT_ID_PREFIX",
    "GRANT_KINDS",
    "IMPORT_KINDS",
    "INTENT_COLLECTION",
    "INTENT_ID_PREFIX",
    "LANGUAGE_VERSION",
    "LAYOUT_PATH",
    "MutationPlan",
    "NOTIFICATION_KIND",
    "NotificationWindow",
    "OUTPUT_GROUP_COLLECTION",
    "OUTPUT_GROUP_KIND",
    "PENDING_INTENT_KIND",
    "ParameterStateValue",
    "PendingSet",
    "PERMISSION_COLLECTION",
    "PERMISSION_KIND",
    "PermissionGrantRecord",
    "OutputGroupRecord",
    "PendingIntentRecord",
    "RECEIPT_KIND",
    "REVISION_PRECONDITION_METHODS",
    "REGISTRATION_COLLECTION",
    "REGISTRATION_KIND",
    "RUN_KIND",
    "SNAPSHOT_KIND",
    "SNAPSHOT_REQUIRED",
    "SERVICE_IDENTIFIER",
    "SERVICE_VERSION",
    "SOURCE_COLLECTION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOKEN_LIFETIME",
    "TOKEN_PREFIX",
    "AdmissionRecords",
    "AuthorityDecision",
    "CapturedArtifact",
    "EnvironmentCatalog",
    "InvocationSnapshot",
    "KnownEnvironment",
    "MeasuredArtifact",
    "MeasuredScript",
    "MeltImportError",
    "MeltPermissionError",
    "MeltRegistrationError",
    "MeltTokenError",
    "RunLaunchOutcome",
    "TokenStore",
    "UNANSWERED_METHODS",
    "WorkspaceService",
    "WorkspaceState",
    "active_run_records",
    "append_source_version",
    "authority_at_admission",
    "authority_at_launch",
    "authority_at_publication",
    "build_pending_intent",
    "build_registration",
    "build_run_record",
    "changed_output_groups",
    "contract_of",
    "current_diagnostics",
    "due_intents",
    "entity_path",
    "expectation_diagnostics",
    "import_version",
    "initial_state_from_document",
    "intent_path",
    "is_live",
    "is_newest_generation",
    "local_environment",
    "measure_artifact",
    "mint_grant_id",
    "mint_intent_id",
    "mint_run_id",
    "next_version_number",
    "output_group_path",
    "permission_path",
    "plan_mutation",
    "pointer_key",
    "preview_effects",
    "register_artifact",
    "register_script",
    "registration_entry_of",
    "registration_identity",
    "registration_path",
    "reserve_generation",
    "revocation_blocks",
    "run_path",
    "run_version",
    "snapshot_result",
    "source_path",
    "state_path",
    "triggered_actions",
    "validate_contract",
    "values_equal",
]
