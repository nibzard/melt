"""Workspace service: commands, revisions, receipts, and snapshots (SPEC.md section 12).

The service owns revisions, stored state, permissions, jobs, and
publication. Backlog task T009 implements the framed IPC envelope, the
mutation transaction against a private candidate, expected-revision
conflicts, compiler read-set checks, state migrations, request
receipts, the single commit sequence, and ordered epoch-guarded
notifications. Task T010 adds the remaining service methods on top.

Typical use:

```python
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

with WorkspaceService.open(platform, "sales-demo") as service:
    response = service.apply(envelope)  # a parsed command envelope
```

Requests also arrive framed on a private channel: a four-byte
big-endian length prefix followed by UTF-8 JSON, rejected above the
control frame limit before any payload is read (SPEC.md section 12.1).

```python
service.serve(read, write)  # one request frame in, one response out
```
"""

from __future__ import annotations

from runtime.workspace.mutations import (
    EntityTouch,
    MutationPlan,
    PendingSet,
    initial_state_from_document,
    plan_mutation,
)
from runtime.workspace.service import (
    SNAPSHOT_REQUIRED,
    NotificationWindow,
    WorkspaceService,
)
from runtime.workspace.state import (
    ENTITY_COLLECTIONS,
    LAYOUT_PATH,
    NOTIFICATION_KIND,
    RECEIPT_KIND,
    SNAPSHOT_KIND,
    ParameterStateValue,
    WorkspaceState,
    entity_path,
    pointer_key,
    state_path,
    values_equal,
)

__all__ = [
    "ENTITY_COLLECTIONS",
    "EntityTouch",
    "LAYOUT_PATH",
    "MutationPlan",
    "NOTIFICATION_KIND",
    "NotificationWindow",
    "ParameterStateValue",
    "PendingSet",
    "RECEIPT_KIND",
    "SNAPSHOT_KIND",
    "SNAPSHOT_REQUIRED",
    "WorkspaceService",
    "WorkspaceState",
    "entity_path",
    "initial_state_from_document",
    "plan_mutation",
    "pointer_key",
    "state_path",
    "values_equal",
]
