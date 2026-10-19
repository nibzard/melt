"""Semantic collections, incarnations, and reference resolution
(RFC 001 sections 3 and 4, backlog task T025).

This module holds the service-side halves of the
`melt/semantic-draft-1` extension:

- :func:`mint_incarnation` issues the opaque incarnation of one
  collection. Clients cannot choose it, snapshots carry it, and
  deleting and recreating the same collection ID issues another one,
  so an old reference cannot select an object in the new collection
  (RFC 001 section 3.2).
- :func:`verify_collection_keys` reads the pinned table artifact and
  verifies that the key column holds unique, non-null strings. The
  service runs this check when it admits a collection binding, and a
  duplicate or invalid key rejects the update (RFC 001 section 3.1).
- :func:`resolve_reference` resolves one stored reference against the
  collection's current pinned version. It answers ``resolved``,
  ``missing``, or ``unavailable``; a missing key never clears or
  rewrites the stored reference (RFC 001 section 3.3).
- :func:`semantic_snapshot_view` builds the inspection view: the
  collection bindings with their incarnations and revisions, and every
  stored reference state value with its resolution (RFC 001 9).

Nothing here executes project code, calls a model, or writes state:
pure interaction stays pure (RFC 001 section 2).
"""

from __future__ import annotations

import io
import secrets
from typing import Callable, Mapping

from melt.schema.diagnostics import Diagnostic, DiagnosticCode
from melt.schema.semantic import SemanticReference

INCARNATION_PREFIX = "inc-"
"""The prefix of every service-issued collection incarnation."""

INCARNATION_ENTROPY_BYTES = 12
"""The random body of one incarnation token."""


def mint_incarnation() -> str:
    """Issue one opaque collection incarnation (RFC 001 section 3.2).

    The token is random and never reused, so a client cannot guess the
    incarnation of a collection before the service creates it, and a
    recreated collection ID cannot inherit the identity of its
    predecessor.
    """

    return INCARNATION_PREFIX + secrets.token_urlsafe(INCARNATION_ENTROPY_BYTES)


def _diagnostic(
    code: str,
    path: str,
    message: str,
    hint: str = "",
) -> Diagnostic:
    return Diagnostic(code=code, path=path, message=message, hint=hint)


def _declared_column(
    schema: Mapping, key: str
) -> "Mapping | None":
    """Return the declared column one key names, or None."""

    for column in schema.get("columns") or ():
        if column.get("id") == key:
            return column
    return None


def _parquet_name(column: Mapping) -> str:
    """The Parquet field name one declared column was imported under."""

    return column.get("label") or column["id"]


def _key_column_values(
    payload: bytes, schema: Mapping, key: str, path: str
) -> "tuple[list[str | None], None] | tuple[None, list[Diagnostic]]":
    """Read one key column from verified artifact bytes.

    Returns the column values, or the diagnostics that reject the read.
    The declared column identifiers map to the Parquet field names the
    import adapter retained (SPEC.md section 5.1).
    """

    import pyarrow.parquet as pq

    column = _declared_column(schema, key)
    if column is None:
        return None, [
            _diagnostic(
                DiagnosticCode.REFERENCE,
                f"{path}/key",
                f"key column {key!r} is not in the declared schema of the"
                " pinned version",
            )
        ]
    try:
        table = pq.read_table(io.BytesIO(payload))
        field = table.column(_parquet_name(column))
    except Exception as error:
        return None, [
            _diagnostic(
                DiagnosticCode.ARTIFACT,
                f"{path}/source",
                f"the pinned artifact is no longer a readable Parquet"
                f" file: {error}",
            )
        ]
    return field.to_pylist(), None


def verify_collection_keys(
    payload: bytes, schema: Mapping, key: str, path: str
) -> list[Diagnostic]:
    """Verify that one key column holds unique, non-null strings.

    The service runs this check when it admits a collection binding: a
    duplicate or invalid key rejects the update, and the workspace
    keeps its previous definitions (RFC 001 section 3.1). Display labels
    and row positions are never identities.
    """

    values, errors = _key_column_values(payload, schema, key, path)
    if errors is not None:
        return errors
    seen: dict[str, int] = {}
    diagnostics: list[Diagnostic] = []
    for position, value in enumerate(values):
        if value is None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/key",
                    f"key column {key!r} carries a null at row"
                    f" {position}; keys must be non-null",
                )
            )
            continue
        if not isinstance(value, str):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/key",
                    f"key column {key!r} carries the non-string value"
                    f" {value!r} at row {position}; keys are strings in"
                    " this extension",
                )
            )
            continue
        first = seen.get(value)
        if first is not None:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.REFERENCE,
                    f"{path}/key",
                    f"key column {key!r} repeats the value {value!r} at"
                    f" rows {first} and {position}; keys must be unique",
                )
            )
        else:
            seen[value] = position
    return diagnostics


# ---------------------------------------------------------------------------
# Resolution (RFC 001 section 3.3)
# ---------------------------------------------------------------------------

RESOLVED = "resolved"
MISSING = "missing"
UNAVAILABLE = "unavailable"
NULL_SELECTION = "null"
"""The resolution statuses of one stored reference.

``missing`` means the key is absent in an available source;
``unavailable`` means the source or its index cannot be read. Both
differ from a null selection, which is the cleared state."""


def _null_result() -> dict:
    return {"status": NULL_SELECTION}


def resolve_reference(
    document: Mapping,
    collection_incarnations: Mapping[str, str],
    value: "SemanticReference | None",
    read_artifact: "Callable[[str], bytes]",
) -> dict:
    """Resolve one stored reference against the current definitions.

    Resolution uses the collection's current pinned version; references
    carry no version of their own (RFC 001 section 3.2). An unreadable
    source answers ``unavailable`` without changing any stored value,
    and a key the pinned version omits answers ``missing`` while the
    reference stays exactly as stored (section 3.3).
    """

    if value is None:
        return _null_result()
    collections = document.get("collections") or {}
    collection = collections.get(value.collection)
    if collection is None:
        return {
            "status": UNAVAILABLE,
            "reason": "collection is not defined in the document",
        }
    incarnation = collection_incarnations.get(value.collection)
    if incarnation is None or incarnation != value.incarnation:
        return {
            "status": UNAVAILABLE,
            "reason": "collection holds another incarnation; the reference"
            " names a previous collection identity",
        }
    source = (document.get("sources") or {}).get(
        collection["source"]["sourceId"]
    )
    version = (source or {}).get("versions", {}).get(
        str(collection["source"]["version"])
    )
    if source is None or version is None:
        return {
            "status": UNAVAILABLE,
            "reason": "the pinned source version is not available",
        }
    try:
        payload = read_artifact(version["artifact"])
    except OSError as error:
        return {
            "status": UNAVAILABLE,
            "reason": f"the pinned artifact is unreadable: {error}",
        }
    values, errors = _key_column_values(
        payload, version["schema"], collection["key"], "/collections"
    )
    if errors is not None:
        return {"status": UNAVAILABLE, "reason": errors[0].message}
    status = RESOLVED if value.key in values else MISSING
    return {"status": status}


# ---------------------------------------------------------------------------
# Inspection view (RFC 001 section 9)
# ---------------------------------------------------------------------------


def semantic_snapshot_view(
    state,
    read_artifact: "Callable[[str], bytes]",
) -> dict:
    """Build the semantic view of one committed workspace state.

    Inspection exposes the collection bindings with their revisions and
    service-issued incarnations, and every stored reference state value
    with its resolution, so a caller can see an unresolved reference
    without changing it (RFC 001 sections 3.3 and 9).
    """

    document = state.document or {}
    collections: dict[str, dict] = {}
    for collection_id, definition in sorted(
        (document.get("collections") or {}).items()
    ):
        collections[collection_id] = {
            "source": dict(definition["source"]),
            "key": definition["key"],
            "incarnation": state.collection_incarnations.get(collection_id),
            "revision": state.entity_revisions.get(
                f"collections/{collection_id}", 0
            ),
        }
    reference_states: dict[str, dict] = {}
    for state_id, stored in sorted(state.reference_states.items()):
        reference_states[state_id] = {
            "value": None
            if stored.value is None
            else {
                "collection": stored.value.collection,
                "incarnation": stored.value.incarnation,
                "key": stored.value.key,
            },
            "revision": stored.revision,
            "resolution": resolve_reference(
                document,
                state.collection_incarnations,
                stored.value,
                read_artifact,
            ),
        }
    return {
        "language": document.get("language"),
        "collections": collections,
        "referenceStates": reference_states,
    }


__all__ = [
    "INCARNATION_PREFIX",
    "MISSING",
    "NULL_SELECTION",
    "RESOLVED",
    "UNAVAILABLE",
    "mint_incarnation",
    "resolve_reference",
    "semantic_snapshot_view",
    "verify_collection_keys",
]
