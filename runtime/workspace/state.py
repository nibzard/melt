"""Current workspace state and its record paths (SPEC.md section 12).

The service keeps one :class:`WorkspaceState` per open workspace: the
committed document, the revision of every record path, the stored value
of every parameter, and the identity incarnation of every parameter
identifier. The state is derived from the committed record stream, so a
reopened workspace restores the last committed definitions and state
without executing project code (SPEC.md section 14).

Record paths address one durable record each:

- ``<collection>/<id>`` for the four mutable entity collections,
- ``layout`` for the layout tree,
- ``state/<parameter-id>`` for stored parameter values.

The ``expected`` map of a command envelope uses exactly these paths.
Zero means the record is expected to be absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LAYOUT_PATH = "layout"
"""The record path of the layout tree."""

ENTITY_COLLECTIONS = ("parameters", "actions", "projections", "bindings")
"""The collections a mutation operation may address (SPEC.md 12.2)."""

SNAPSHOT_KIND = "melt.workspaceSnapshot"
"""The committed record that holds the workspace snapshot of a commit."""

RECEIPT_KIND = "melt.commandReceipt"
"""The committed record that holds one request receipt."""

NOTIFICATION_KIND = "melt.changeNotification"
"""The committed record that holds one change notification."""


def entity_path(collection: str, entity_id: str) -> str:
    """Return the record path of one entity."""

    return f"{collection}/{entity_id}"


def state_path(parameter_id: str) -> str:
    """Return the record path of one stored parameter value."""

    return f"state/{parameter_id}"


def pointer_key(path: str) -> str:
    """Escape a record path for use as a JSON Pointer token."""

    return path.replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class ParameterStateValue:
    """The stored value and revision of one parameter (section 6.2)."""

    value: "bool | float | str | None"
    revision: int


def values_equal(first, second) -> bool:
    """Report whether two stored parameter values are equal.

    Types are compared strictly, so a boolean never equals a number even
    though Python compares them equal. Values are already normalized to
    ``bool``, ``float``, ``str``, or ``None`` by the schema layer.
    """

    if first is None or second is None:
        return first is None and second is None
    if isinstance(first, bool) != isinstance(second, bool):
        return False
    if isinstance(first, str) != isinstance(second, str):
        return False
    return first == second


@dataclass
class WorkspaceState:
    """The committed state one service transaction reads and writes."""

    workspace_id: str
    epoch: int
    commit_sequence: int
    document: "dict | None" = None
    entity_revisions: dict[str, int] = field(default_factory=dict)
    parameter_states: dict[str, ParameterStateValue] = field(
        default_factory=dict
    )
    incarnations: dict[str, int] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        """Whether a workspace document exists for this workspace."""

        return self.document is not None

    def current_revision(self, path: str) -> int:
        """Return the revision of one record path; zero when absent."""

        if path.startswith("state/"):
            state = self.parameter_states.get(path[len("state/") :])
            return 0 if state is None else state.revision
        return self.entity_revisions.get(path, 0)

    def read_set(self) -> dict[str, int]:
        """Return the revision of every record a full validation reads.

        Compilation of a candidate resolves references across the whole
        document, so every current record path forms the compiler read
        set that the service checks again at commit (SPEC.md 12.2).
        """

        observed = dict(self.entity_revisions)
        observed[LAYOUT_PATH] = self.entity_revisions.get(LAYOUT_PATH, 0)
        for parameter_id, state in self.parameter_states.items():
            observed[state_path(parameter_id)] = state.revision
        return observed

    def with_commit(self, commit_sequence: int) -> "WorkspaceState":
        """Return the state advanced to one committed sequence."""

        return WorkspaceState(
            workspace_id=self.workspace_id,
            epoch=self.epoch,
            commit_sequence=commit_sequence,
            document=self.document,
            entity_revisions=dict(self.entity_revisions),
            parameter_states=dict(self.parameter_states),
            incarnations=dict(self.incarnations),
        )


__all__ = [
    "ENTITY_COLLECTIONS",
    "LAYOUT_PATH",
    "NOTIFICATION_KIND",
    "ParameterStateValue",
    "RECEIPT_KIND",
    "SNAPSHOT_KIND",
    "WorkspaceState",
    "entity_path",
    "pointer_key",
    "state_path",
    "values_equal",
]
