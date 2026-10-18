"""Event origin of admission commands (T018, SPEC.md sections 2.2, 9.1).

Origin derives from the authenticated caller and the command type, not
from the envelope body. The regression this suite pins: an agent or
programmatic caller that invokes ``action.invoke`` or
``control.activate`` through ``dispatch_as(origin="programmatic")``
must produce an admitted intent whose recorded origin is programmatic,
while the host channel keeps answering user origins. Every probe rides
the same service handlers the later native host uses — parsed
envelopes through ``dispatch``, framed bytes through ``process_frame``
— and the request receipt replays the recorded origin unchanged.
"""

from __future__ import annotations

import json

import pytest

from melt.schema.commands import encode_frame
from runtime.storage import PlatformAdapter

# The T016 scheduling harness: seeding, envelopes, parameters.
from test_scheduling import (
    WORKSPACE,
    envelope,
    requires_storage_worker,
    seed_workspace,
    set_parameter,
)

pytestmark = requires_storage_worker


def _intent_of(service) -> tuple[str, object]:
    """The one pending intent of the sales output group."""

    items = service.pending_intents.items()
    assert len(items) == 1, service.pending_intents
    return next(iter(items))


def test_programmatic_action_invoke_records_programmatic_origin(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    response = service.dispatch_as(
        _invoke_envelope("05-agent-invoke"), origin="programmatic"
    )
    assert response.ok, response.errors
    # The admitted intent, not just the answer, records the caller.
    assert response.result["origin"] == "programmatic"
    _, intent = _intent_of(service)
    assert intent.origin == "programmatic"
    assert intent.status == "approvalRequired"


def _invoke_envelope(request_id: str):
    return envelope(
        request_id,
        method="action.invoke",
        expected={"actions/analyze-sales": 1},
        payload={"actionId": "analyze-sales"},
    )


def test_user_action_invoke_records_user_origin(service_adapter: PlatformAdapter):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    response = service.dispatch(_invoke_envelope("05-user-invoke"))
    assert response.ok, response.errors
    assert response.result["origin"] == "user"
    _, intent = _intent_of(service)
    assert intent.origin == "user"


def test_programmatic_control_activation_records_programmatic_origin(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    activation = envelope(
        "05-agent-activate",
        method="control.activate",
        expected={"projections/sales": 1, "bindings/manual-run": 1},
        payload={"projectionId": "sales", "controlId": "run"},
    )
    response = service.dispatch_as(activation, origin="programmatic")
    assert response.ok, response.errors
    assert response.result["origin"] == "programmatic"
    _, intent = _intent_of(service)
    assert intent.origin == "programmatic"
    # The binding context rode along; the activation routed through it.
    assert response.result["binding"] == "manual-run"


def test_unbound_control_activation_reports_the_programmatic_origin(
    service_adapter: PlatformAdapter,
):
    projection = {
        "kind": "table",
        "title": "Unbound",
        "source": {"sourceId": "sales-input", "version": 1},
        "config": {"columns": ["month", "revenue"], "selection": "none"},
        "controls": [
            {"id": "orphan", "kind": "button", "label": "Does nothing"}
        ],
    }
    seeded = seed_workspace(
        service_adapter, grant=False, extra_projections={"unbound": projection}
    )
    service = seeded.service
    activation = envelope(
        "05-agent-unbound",
        method="control.activate",
        expected={"projections/unbound": 1},
        payload={"projectionId": "unbound", "controlId": "orphan"},
    )
    response = service.dispatch_as(activation, origin="programmatic")
    assert response.ok, response.errors
    assert response.result == {
        "status": "no-op",
        "origin": "programmatic",
        "projection": "unbound",
        "control": "orphan",
        "reason": "no binding connects this control to an action",
    }
    assert service.pending_intents == {}


def test_framed_channel_carries_the_caller_origin(
    service_adapter: PlatformAdapter,
):
    """The framed entry the native host drives answers both origins."""

    from melt.schema.commands import decode_frame

    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    body = {
        "protocolVersion": 1,
        "workspaceId": WORKSPACE,
        "requestId": "05-frame-agent",
        "method": "action.invoke",
        "expected": {"actions/analyze-sales": 1},
        "payload": {"actionId": "analyze-sales"},
    }

    def framed(origin: "str | None" = None, request_id: str = "05-frame") -> dict:
        payload = json.dumps({**body, "requestId": request_id}).encode("utf-8")
        answer = service.process_frame(
            encode_frame(payload), **({"origin": origin} if origin else {})
        )
        raw, _ = decode_frame(answer)
        return json.loads(raw.decode("utf-8"))

    answer = framed(origin="programmatic", request_id="05-frame-agent")
    assert answer["ok"] is True, answer
    assert answer["result"]["origin"] == "programmatic"
    _, intent = _intent_of(service)
    assert intent.origin == "programmatic"

    answer = framed(request_id="05-frame-user")
    assert answer["ok"] is True, answer
    assert answer["result"]["origin"] == "user"


def test_a_replayed_admission_keeps_its_recorded_origin(
    service_adapter: PlatformAdapter,
):
    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    first = service.dispatch_as(
        _invoke_envelope("05-replay"), origin="programmatic"
    )
    assert first.ok, first.errors
    replay = service.dispatch_as(
        _invoke_envelope("05-replay"), origin="programmatic"
    )
    assert replay.ok, replay.errors
    assert replay.result == first.result
    assert replay.result["origin"] == "programmatic"


def test_origin_still_decides_which_change_fires_bindings(
    service_adapter: PlatformAdapter,
):
    """A programmatic admission changes no parameter and fires nothing.

    The origin split is observable in both directions: the programmatic
    ``parameter.set`` of the race table stays silent (covered by the
    scheduling suite), and a programmatic invocation of the action
    admits an intent without pretending a user pressed anything.
    """

    seeded = seed_workspace(service_adapter, grant=False)
    service = seeded.service
    quiet = set_parameter(
        service,
        "05-quiet",
        value=250,
        expected_revision=1,
        origin="programmatic",
    )
    assert quiet.ok, quiet.errors
    assert service.pending_intents == {}
    assert service.output_groups == {}
    response = service.dispatch_as(
        _invoke_envelope("05-agent-after-quiet"), origin="programmatic"
    )
    assert response.ok, response.errors
    _, intent = _intent_of(service)
    # The captured value is the committed programmatic one; the origin
    # of the change that produced it stays out of the snapshot.
    assert intent.parameters["minimum"].value == 250
    assert intent.origin == "programmatic"
