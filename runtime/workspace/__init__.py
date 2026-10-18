"""Workspace service: commands, revisions, receipts, and snapshots (SPEC.md section 12).

The service owns revisions, stored state, permissions, jobs, and
publication. The framed IPC envelope, the mutation transaction against
a private candidate, expected-revision conflicts, compiler read-set
checks, state migrations, request receipts, the single commit sequence,
and ordered epoch-guarded notifications form the core. The service
methods of section 12.6 ride the same envelope: the session handshake,
workspace creation, inspection, preview, subscription, control
activation, and run cancellation and inspection. Artifact and script
tokens are minted by the host capture operation and stay
workspace-scoped for 15 minutes.

Typical use:

```python
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

with WorkspaceService.open(platform, "sales-demo") as service:
    response = service.dispatch(envelope)  # any parsed command envelope
```

Requests also arrive framed on a private channel: a four-byte
big-endian length prefix followed by UTF-8 JSON, rejected above the
control frame limit before any payload is read (SPEC.md section 12.1).

```python
service.serve(read, write)  # one request frame in, one response out
```
"""

from __future__ import annotations

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
    ENTITY_COLLECTIONS,
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    RECEIPT_KIND,
    RUN_KIND,
    SNAPSHOT_KIND,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    pointer_key,
    run_path,
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
    "CAPTURE_KINDS",
    "ENTITY_COLLECTIONS",
    "EntityTouch",
    "LANGUAGE_VERSION",
    "LAYOUT_PATH",
    "MutationPlan",
    "NOTIFICATION_KIND",
    "NotificationWindow",
    "ParameterStateValue",
    "PendingSet",
    "RECEIPT_KIND",
    "REVISION_PRECONDITION_METHODS",
    "RUN_KIND",
    "SNAPSHOT_KIND",
    "SNAPSHOT_REQUIRED",
    "SERVICE_IDENTIFIER",
    "SERVICE_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOKEN_LIFETIME",
    "TOKEN_PREFIX",
    "CapturedArtifact",
    "MeltTokenError",
    "TokenStore",
    "UNANSWERED_METHODS",
    "WorkspaceService",
    "WorkspaceState",
    "active_run_records",
    "current_diagnostics",
    "entity_path",
    "expectation_diagnostics",
    "initial_state_from_document",
    "plan_mutation",
    "pointer_key",
    "preview_effects",
    "run_path",
    "snapshot_result",
    "state_path",
    "values_equal",
]
