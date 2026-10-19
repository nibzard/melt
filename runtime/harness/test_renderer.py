"""The Linux test renderer of the semantic extension (RFC 001 section 1).

Round 1 has no native renderer. This module is the stand-in the
command-line harness drives (SPEC.md section 2.2): it proves event
routing and plan updates on Linux before any native integration. It
has these parts:

- :meth:`TestRenderer.event_request` builds one renderer event exactly
  the way a host would: it reads a fresh inspection, resolves the
  binding at the event address, and assembles the revision guards the
  envelope carries — the target state record, the projection
  definition, the binding, and the displayed source and collection
  records (RFC 001 sections 5.1 and 5.3).
- :meth:`TestRenderer.render` resolves every observer slot of the
  committed workspace through the service's own inspection view, so
  the rendered answer and the inspected answer agree by construction.
- The installed renderer registry (RFC 001 section 7.2): the host
  ships a fixed set of renderers, and a projection whose pinned
  renderer is missing or version-incompatible renders a fallback with
  source details and diagnostics. It never reinterprets an
  incompatible contract and never originates a gesture from a renderer
  it cannot instantiate.
- :class:`OptimisticSelection` models the host display protocol of
  RFC 001 section 5.3: the attempted selection shows immediately, a
  saved indicator appears only after the service acknowledges the
  commit, and a rejected or unanswered gesture restores the committed
  display with a conflict status.

The renderer never executes project code and never calls a model. A
pure interaction is data in, data out.
"""

from __future__ import annotations

import uuid
from typing import Any


class TestRendererError(RuntimeError):
    """The renderer cannot build or drive one event."""


def event_address(event: str, projection: str) -> str:
    """The address one renderer event binds at (RFC 001 section 5.2)."""

    return f"{event}/{projection}"


class TestRenderer:
    """One renderer session over one workspace session.

    ``installed_renderers`` maps the renderer IDs the host ships to
    their contract versions (RFC 001 section 7.2). The default models a
    host that ships everything the saved plan pinned. An entry the map
    omits, or a contract version it disagrees with, makes that
    renderer unavailable: the projection renders a fallback and its
    events refuse to build.
    """

    def __init__(self, session, installed_renderers=None):
        self._session = session
        self._installed = installed_renderers

    # ------------------------------------------------------------------
    # The installed renderer registry (RFC 001 section 7.2)
    # ------------------------------------------------------------------

    def _pinned_renderer(self, projection_id: str, snapshot: dict) -> "dict | None":
        """The renderer one projection pinned, from the inspect view."""

        renderers = (snapshot.get("semantic") or {}).get("renderers") or {}
        return renderers.get(projection_id)

    def renderer_status(
        self, projection_id: str, snapshot: "dict | None" = None
    ) -> "tuple[str, str | None]":
        """The availability of one projection's pinned renderer.

        Returns ``("available", None)`` or ``("unavailable", reason)``.
        The comparison reads the pinned renderer versions inspection
        already exposes, so the host needs no compilation to detect a
        renderer it cannot instantiate.
        """

        snapshot = snapshot if snapshot is not None else self._session.snapshot()
        pinned = self._pinned_renderer(projection_id, snapshot)
        if pinned is None or self._installed is None:
            # Nothing extension-specific is pinned to compare against.
            return "available", None
        renderer_id = pinned.get("id")
        installed = self._installed.get(renderer_id)
        if installed is None:
            return (
                "unavailable",
                f"the installed host ships no {renderer_id!r} renderer",
            )
        contract = pinned.get("contractVersion")
        if int(installed) != int(contract):
            return (
                "unavailable",
                f"renderer {renderer_id!r} pins contract version"
                f" {contract} but the installed host ships {installed}",
            )
        return "available", None

    # ------------------------------------------------------------------
    # Event routing (RFC 001 sections 5.1 and 5.3)
    # ------------------------------------------------------------------

    def event_request(
        self,
        *,
        projection: str,
        event: str,
        key: "str | None" = None,
        if_revisions: "dict[str, int] | None" = None,
    ) -> "tuple[str, dict, dict[str, int]]":
        """Return the method, payload, and expected map of one event.

        ``key`` names the object a select gesture chose; a clear event
        carries none. ``if_revisions`` overrides the observed revision
        of any guarded record, so a caller can replay a stale gesture
        on purpose.
        """

        if event not in ("object.select", "selection.clear"):
            raise TestRendererError(f"unknown renderer event {event!r}")
        if (event == "object.select") == (key is None):
            raise TestRendererError(
                f"the {event!r} event takes"
                f" {'no key' if key is None else 'one key'}"
            )
        snapshot = self._session.snapshot()
        document = snapshot["document"]
        definition = (document.get("projections") or {}).get(projection)
        if definition is None:
            raise TestRendererError(f"projection {projection!r} does not exist")
        status, reason = self.renderer_status(projection, snapshot)
        if status != "available":
            raise TestRendererError(
                f"the renderer of projection {projection!r} is"
                f" unavailable: {reason}"
            )
        semantic = snapshot.get("semantic") or {}
        revisions = dict(snapshot.get("entityRevisions") or {})
        expected: dict[str, int] = {}

        def _guard(path: str) -> None:
            expected[path] = (
                if_revisions.get(path)
                if if_revisions is not None and path in if_revisions
                else revisions.get(path, 0)
            )

        _guard(f"projections/{projection}")
        collection_id = definition.get("collection")
        if collection_id:
            _guard(f"collections/{collection_id}")
        source = definition.get("source")
        if isinstance(source, dict) and "sourceId" in source:
            _guard(f"sources/{source['sourceId']}")
        payload: dict[str, Any] = {"projectionId": projection}
        if key is not None:
            if not collection_id:
                raise TestRendererError(
                    f"projection {projection!r} declares no collection to"
                    " select in"
                )
            incarnation = (
                (semantic.get("collections") or {}).get(collection_id) or {}
            ).get("incarnation")
            if incarnation is None:
                raise TestRendererError(
                    f"collection {collection_id!r} holds no incarnation"
                )
            payload["objectRef"] = {
                "collection": collection_id,
                "incarnation": incarnation,
                "key": key,
            }
        matching = sorted(
            binding_id
            for binding_id, binding in (
                document.get("bindings") or {}
            ).items()
            if (binding.get("event") or {}).get("kind") == event
            and (binding.get("event") or {}).get("projection") == projection
        )
        for binding_id in matching:
            _guard(f"bindings/{binding_id}")
            target = (document["bindings"][binding_id]["effect"]).get("target")
            if target:
                states = semantic.get("referenceStates") or {}
                stored = states.get(target)
                revision = (
                    if_revisions.get(
                        f"refstate/{target}",
                        stored["revision"] if stored else 0,
                    )
                    if if_revisions is not None
                    else (stored["revision"] if stored else 0)
                )
                expected[f"refstate/{target}"] = revision
        return event, payload, expected

    # ------------------------------------------------------------------
    # Rendering (RFC 001 sections 6 and 10.2)
    # ------------------------------------------------------------------

    def render(self) -> dict:
        """Resolve every projection view of the committed workspace.

        A projection whose pinned renderer the installed host lacks
        renders the fallback of RFC 001 section 7.2 instead: source
        details and diagnostics, never a reinterpretation.
        """

        snapshot = self._session.snapshot()
        document = snapshot.get("document") or {}
        semantic = snapshot.get("semantic") or {}
        observers = semantic.get("observers") or {}
        subscriptions = self._subscription_resolutions(semantic)
        panes: list[dict] = []
        for projection_id, projection in sorted(
            (document.get("projections") or {}).items()
        ):
            status, reason = self.renderer_status(projection_id, snapshot)
            if status != "available":
                panes.append(
                    self._fallback_pane(
                        projection_id, projection, snapshot, reason
                    )
                )
                continue
            kind = projection.get("kind")
            if kind == "graph":
                panes.append(
                    self._graph_pane(projection_id, projection, observers)
                )
            elif kind == "record":
                panes.append(
                    self._record_pane(projection_id, projection, observers)
                )
            else:
                source = projection.get("source")
                pane = {
                    "projection": projection_id,
                    "renderer": f"melt.{kind}",
                    "kind": kind,
                    "source": source,
                }
                if isinstance(source, dict) and "actionOutput" in source:
                    # A subscriber renders the source its subscription
                    # currently resolves, with the version status it
                    # must show (RFC 001 section 8.2).
                    pane["subscription"] = subscriptions.get(projection_id)
                panes.append(pane)
        return {
            "workspaceId": snapshot.get("workspaceId"),
            "language": document.get("language"),
            "selected": {
                state_id: {
                    "value": entry.get("value"),
                    "revision": entry.get("revision"),
                    "resolution": entry.get("resolution"),
                }
                for state_id, entry in sorted(
                    (semantic.get("referenceStates") or {}).items()
                )
            },
            "panes": panes,
        }

    @staticmethod
    def _subscription_resolutions(semantic: dict) -> dict:
        """Index the resolved subscription of every subscriber pane."""

        resolutions: dict = {}
        for action_entry in (semantic.get("outputs") or {}).values():
            for output in (action_entry.get("outputs") or {}).values():
                for subscriber in output.get("subscribers") or ():
                    resolutions[subscriber["projection"]] = subscriber
        return resolutions

    def _fallback_pane(
        self,
        projection_id: str,
        projection: dict,
        snapshot: dict,
        reason: "str | None",
    ) -> dict:
        """The fallback an unavailable renderer displays (RFC 001 7.2).

        The pane names the pinned renderer and why it cannot run, keeps
        the saved source details the host can still show, and carries a
        diagnostic. It executes nothing and reinterprets nothing.
        """

        pinned = self._pinned_renderer(projection_id, snapshot) or {}
        pane: dict[str, Any] = {
            "projection": projection_id,
            "kind": projection.get("kind"),
            "renderer": pinned.get("id"),
            "rendererStatus": "unavailable",
            "reason": reason,
            "source": projection.get("source"),
            "diagnostics": [
                {
                    "code": "E_VERSION",
                    "path": f"/projections/{projection_id}/renderer",
                    "message": reason or "the pinned renderer is unavailable",
                }
            ],
        }
        if projection.get("collection"):
            pane["collection"] = projection["collection"]
        return pane

    def _graph_pane(
        self, projection_id: str, projection: dict, observers: dict
    ) -> dict:
        """Render one graph pane: nodes, edges, and the highlight."""

        document = self._session.snapshot().get("document") or {}
        source = (document.get("sources") or {}).get(
            projection["source"]["sourceId"]
        )
        version = (source or {}).get("versions", {}).get(
            str(projection["source"]["version"])
        )
        schema = (version or {}).get("schema") or {}
        observer = observers.get(projection_id) or {}
        highlighted = set(observer.get("highlightedNodes") or ())
        show_labels = projection.get("config", {}).get("nodeLabels") != "hide"
        nodes = [
            {
                "nodeId": node["nodeId"],
                "label": node["label"] if show_labels else None,
                "objectRef": {
                    "collection": node["collection"],
                    "key": node["key"],
                },
                "highlighted": node["nodeId"] in highlighted,
            }
            for node in schema.get("nodes") or ()
        ]
        return {
            "projection": projection_id,
            "renderer": "melt.graph",
            "kind": "graph",
            "source": projection["source"],
            "collection": projection["collection"],
            "observing": observer.get("state"),
            "resolution": observer.get("resolution"),
            "nodes": nodes,
            "edges": [
                {"edgeId": edge["edgeId"], "from": edge["from"], "to": edge["to"]}
                for edge in schema.get("edges") or ()
            ],
            "highlightedNodes": sorted(highlighted),
        }

    def _record_pane(
        self, projection_id: str, projection: dict, observers: dict
    ) -> dict:
        """Render one record pane through its observer slot."""

        observer = observers.get(projection_id) or {}
        return {
            "projection": projection_id,
            "renderer": "melt.record",
            "kind": "record",
            "collection": projection["collection"],
            "observing": observer.get("state"),
            "value": observer.get("value"),
            "resolution": observer.get("resolution"),
            "display": observer.get("display"),
            "record": observer.get("record"),
        }


class OptimisticSelection:
    """The host display of one selection state record (RFC 001 5.3).

    The host validates an event against its installed plan and MAY
    display an optimistic selection immediately; a saved indicator
    appears only after the service acknowledges the commit. This class
    is that protocol against the same service handlers the native host
    will use:

    - :meth:`select` and :meth:`clear` display the attempted value at
      once with status ``optimistic``, then dispatch the event the
      renderer built with its revision guards.
    - An acknowledged response turns the display ``committed``.
    - A rejected gesture restores the committed value and exposes the
      conflict; an unanswered one (a failed commit or a lost channel)
      restores the committed value and stays ``unacknowledged``.

    :meth:`committed` reads the authoritative durable value from the
    service, so a caller can prove the two views never confuse one
    another: nothing the host displays optimistically ever reads as
    durable before the commit lands.
    """

    def __init__(
        self,
        session,
        *,
        projection: str = "network",
        state_id: str = "selected_supplier",
    ):
        self._session = session
        self._renderer = TestRenderer(session)
        self._projection = projection
        self._state_id = state_id
        committed = self.committed()
        self._value: "str | None" = _key_of(committed[0])
        self._status = "committed"
        self._conflict: "list[dict] | None" = None

    # ------------------------------------------------------------------
    # The durable view
    # ------------------------------------------------------------------

    def committed(self) -> "tuple[dict | None, int]":
        """The committed reference value and revision the service holds."""

        snapshot = self._session.snapshot()
        states = (snapshot.get("semantic") or {}).get("referenceStates") or {}
        entry = states.get(self._state_id) or {}
        return entry.get("value"), int(entry.get("revision", 0))

    # ------------------------------------------------------------------
    # The host display
    # ------------------------------------------------------------------

    def view(self) -> dict:
        """What the host shows: the displayed key and its save status."""

        return {
            "value": self._value,
            "status": self._status,
            "conflict": self._conflict,
        }

    def select(
        self,
        key: str,
        *,
        request_id: "str | None" = None,
        if_revisions: "dict[str, int] | None" = None,
    ) -> dict:
        """Display and dispatch one select gesture.

        ``if_revisions`` overrides the observed revision of any guarded
        record, so a caller can replay a gesture the host raced, the
        same hook `TestRenderer.event_request` exposes.
        """

        return self._gesture(
            "object.select",
            key=key,
            request_id=request_id,
            if_revisions=if_revisions,
        )

    def clear(self, *, request_id: "str | None" = None) -> dict:
        """Display and dispatch one clear gesture."""

        return self._gesture("selection.clear", request_id=request_id)

    def _gesture(
        self,
        event: str,
        *,
        key=None,
        request_id=None,
        if_revisions: "dict[str, int] | None" = None,
    ) -> dict:
        _, payload, expected = self._renderer.event_request(
            projection=self._projection,
            event=event,
            key=key,
            if_revisions=if_revisions,
        )
        # The optimistic display: the attempted key shows immediately,
        # before any acknowledgement (RFC 001 section 5.3).
        self._value = key
        self._status = "optimistic"
        self._conflict = None
        try:
            answer = self._session.request(
                event,
                payload=payload,
                expected=expected,
                request_id=request_id
                or f"host-{uuid.uuid4().hex[:12]}",
            )
        except Exception as error:
            # No acknowledgement arrived. Restore the committed display
            # and expose the failure; nothing is labelled saved.
            committed, _ = self.committed()
            self._value = _key_of(committed)
            self._status = "unacknowledged"
            self._conflict = [{"message": str(error)}]
            raise
        if answer.get("ok"):
            result = answer.get("result") or {}
            self._value = _key_of(result.get("value"))
            self._status = "committed"
            return answer
        # The service rejected the gesture: restore the committed
        # selection and expose a brief conflict status (RFC 001 5.3).
        committed, _ = self.committed()
        self._value = _key_of(committed)
        self._status = "conflict"
        self._conflict = [
            {
                "code": item.get("code"),
                "path": item.get("path"),
                "message": item.get("message"),
            }
            for item in answer.get("errors") or ()
        ]
        return answer


def _key_of(value: "dict | None") -> "str | None":
    """The selection key of one serialized reference value."""

    return None if value is None else value.get("key")


__all__ = [
    "OptimisticSelection",
    "TestRenderer",
    "TestRendererError",
    "event_address",
]
