"""Projection observer views (RFC 001 section 6, backlog task T026).

Observers are selectors in the compiled dependency plan: they read
state and sources, they write neither. This module resolves what one
committed workspace currently shows through each observer slot:

- ``highlight`` maps the observed reference to the graph nodes that
  carry it. No matching node means no highlight; the selection stays
  valid for other projections.
- ``record`` resolves the observed reference in the projection's
  declared collection and reads the row the key names. Null displays
  an empty-selection state, missing displays the missing key, and
  unavailable displays a recoverable source error. No state changes
  through any of these display conditions.

The Linux test renderer and the semantic inspection view both call
this module, so the rendered answer and the inspected answer agree by
construction.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from runtime.workspace.reader import PageQuery, read_table_page
from runtime.workspace.semantic import (
    MISSING,
    NULL_SELECTION,
    RESOLVED,
    UNAVAILABLE,
    resolve_reference,
)

ReadArtifact = Callable[[str], bytes]
"""One artifact reader: the content-addressed store of the workspace."""


def _reference_wire(value) -> "dict | None":
    if value is None:
        return None
    return {
        "collection": value.collection,
        "incarnation": value.incarnation,
        "key": value.key,
    }


def graph_view(
    projection: Mapping[str, Any],
    version_schema: Mapping[str, Any],
    value,
) -> dict:
    """Resolve the ``highlight`` selector of one graph projection."""

    highlighted: list[str] = []
    if value is not None:
        for node in version_schema.get("nodes") or ():
            if (
                node.get("collection") == value.collection
                and node.get("key") == value.key
            ):
                highlighted.append(node["nodeId"])
    return {
        "slot": "highlight",
        "state": projection["observe"]["highlight"]["state"],
        "value": _reference_wire(value),
        "highlightedNodes": sorted(highlighted),
    }


def record_view(
    projection: Mapping[str, Any],
    collection: Mapping[str, Any],
    version: Mapping[str, Any],
    value,
    read_artifact: ReadArtifact,
) -> dict:
    """Resolve the ``record`` selector of one record projection.

    Resolution runs through the trusted table reader with one exact
    filter on the collection's key column, so the displayed cells carry
    the same wire shapes a table page carries. An artifact the store
    can no longer read answers ``unavailable`` with the stored
    reference intact: one damaged file never blocks the other views or
    the inspection that carries them (RFC 001 sections 6 and 10.5).
    """

    view: dict[str, Any] = {
        "slot": "record",
        "state": projection["observe"]["record"]["state"],
        "value": _reference_wire(value),
    }
    if value is None:
        view["resolution"] = {"status": NULL_SELECTION}
        view["display"] = "empty"
        return view
    try:
        payload = read_artifact(version["artifact"])
    except OSError as error:
        view["resolution"] = {
            "status": UNAVAILABLE,
            "reason": f"the pinned artifact is unreadable: {error}",
        }
        view["display"] = "unavailable"
        return view
    page, errors = read_table_page(
        payload,
        version["schema"],
        PageQuery(
            source=dict(collection["source"]),
            filters=[
                {
                    "column": collection["key"],
                    "operator": "eq",
                    "value": value.key,
                }
            ],
            limit=1,
        ),
    )
    if errors is not None:
        view["resolution"] = {"status": UNAVAILABLE, "reason": errors[0].message}
        view["display"] = "unavailable"
        return view
    if not page["rows"]:
        view["resolution"] = {"status": MISSING}
        view["display"] = "missing"
        return view
    fields = projection.get("config", {}).get("fields")
    if fields is None:
        fields = list(page["columns"])
    columns = list(page["columns"])
    view["resolution"] = {"status": RESOLVED}
    view["display"] = "record"
    view["record"] = {
        "source": dict(collection["source"]),
        "cells": {
            field: page["rows"][0][columns.index(field)]
            for field in fields
            if field in columns
        },
    }
    return view


def observer_views(
    document: Mapping[str, Any],
    collection_incarnations: Mapping[str, str],
    reference_states: Mapping[str, Any],
    read_artifact: ReadArtifact,
) -> dict[str, dict]:
    """Resolve every observer slot of one committed document.

    Returns one view per projection that observes state, keyed by
    projection ID. A projection whose pinned data cannot be read
    answers ``unavailable``; an unreadable source never blocks the
    other views (RFC 001 sections 6 and 10.5).
    """

    views: dict[str, dict] = {}
    for projection_id, projection in sorted(
        (document.get("projections") or {}).items()
    ):
        kind = projection.get("kind")
        observe = projection.get("observe") or {}
        if kind == "graph" and "highlight" in observe:
            state_id = observe["highlight"]["state"]
            stored = reference_states.get(state_id)
            value = stored.value if stored is not None else None
            source = (document.get("sources") or {}).get(
                projection["source"]["sourceId"]
            )
            version = (source or {}).get("versions", {}).get(
                str(projection["source"]["version"])
            )
            if version is None:
                views[projection_id] = {
                    "slot": "highlight",
                    "state": state_id,
                    "value": _reference_wire(value),
                    "resolution": {
                        "status": UNAVAILABLE,
                        "reason": "the pinned graph version is not"
                        " available",
                    },
                    "highlightedNodes": [],
                }
                continue
            view = graph_view(projection, version.get("schema") or {}, value)
            view["resolution"] = resolve_reference(
                document,
                collection_incarnations,
                value,
                read_artifact,
            )
            views[projection_id] = view
        elif kind == "record" and "record" in observe:
            state_id = observe["record"]["state"]
            stored = reference_states.get(state_id)
            value = stored.value if stored is not None else None
            collection = (document.get("collections") or {}).get(
                projection["collection"]
            )
            source = (
                (document.get("sources") or {}).get(
                    collection["source"]["sourceId"]
                )
                if collection is not None
                else None
            )
            version = (source or {}).get("versions", {}).get(
                str(collection["source"]["version"])
            ) if collection is not None else None
            if collection is None or version is None:
                views[projection_id] = {
                    "slot": "record",
                    "state": state_id,
                    "value": _reference_wire(value),
                    "resolution": {
                        "status": UNAVAILABLE,
                        "reason": "the collection or its pinned version is"
                        " not available",
                    },
                    "display": "unavailable",
                }
                continue
            views[projection_id] = record_view(
                projection, collection, version, value, read_artifact
            )
    return views


def renderer_versions(document: Mapping[str, Any]) -> dict[str, dict]:
    """The pinned renderer of every semantic projection (RFC 001 9).

    Inspection exposes the renderer versions the plan pinned, so a
    caller can compare them against its installed registry without
    compiling anything.
    """

    from runtime.compiler.compiler import renderer_registry

    registry = renderer_registry(document)
    versions: dict[str, dict] = {}
    for projection_id, projection in sorted(
        (document.get("projections") or {}).items()
    ):
        spec = registry.get(projection.get("kind"))
        if spec is None:
            continue
        versions[projection_id] = {
            "id": spec.id,
            "contractVersion": spec.contract_version,
        }
    return versions


__all__ = [
    "ReadArtifact",
    "graph_view",
    "observer_views",
    "record_view",
    "renderer_versions",
]
