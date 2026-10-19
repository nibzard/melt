"""The non-blocking execution slot and signal cancellation (T035).

Round 1 launched a run by blocking its caller through the process
wait, so a launching harness process answered no framed request until
the project process exited, and ``run.cancel`` only rewrote the run
record. The two acceptance criteria of backlog task T035 run here for
real (SPEC.md sections 13.1, 13.5, 18.3, and 20 step 5):

- The framed service channel keeps answering while a project process
  executes. ``launch_admitted_run(run_id, wait=False)`` parks the
  process wait on the service's own execution thread, and the tests
  below drive ``serve`` — the framed loop the native host drives —
  through inspection, table paging, mutation, and cancellation while a
  gated process is provably alive.
- A cancellation request stops scheduling and signals the owned process
  group, escalating when necessary. The launcher starts each runner as
  the leader of its own session, so the signalled group covers every
  process the run spawned; the tests prove the terminal signal, the
  ``SIGKILL`` escalation against a script that ignores ``SIGTERM``, and
  that the group signal reaches a helper the script spawned.

The gate mechanism comes from the race suite: the script writes its
started marker, then waits for the release marker the test controls.
Every cancellation here fires with the gate closed, so a process that
stops only proves the signal stopped it.
"""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

from melt.schema.commands import decode_frame, encode_frame
from runtime.storage import PlatformAdapter
from runtime.workspace import WorkspaceService

from test_race_suite import (
    GATE_TIMEOUT_SECONDS,
    POLL_SECONDS,
    RELEASE_NAME,
    STARTED_NAME,
    ExecutingRun,
    gated_script,
    seed_race,
    status_of,
)
from test_scheduling import (
    WORKSPACE,
    invoke,
    requires_storage_worker,
)

pytestmark = requires_storage_worker

PID_NAME = "run-pid"
"""The marker one gated process writes with its own process id."""

CHILD_PID_NAME = "child-pid"
"""The marker one gated process writes with its spawned helper's id."""

CHILD_WAIT_CODE = "import time; time.sleep(600)"
"""The helper one gated script spawns into its own process group."""


def pid_gated_script(
    *, ignore_term: bool = False, spawn_child: bool = False
) -> str:
    """Return the gated sales loop with process markers.

    Beyond the race-suite gate, the process writes its own process id
    and may ignore ``SIGTERM`` and spawn a sleeping helper, so a test
    can prove the group signal reached every process the run owns
    (SPEC.md section 13.5).
    """

    lines = [
        "import os",
        "import signal",
        "import subprocess",
        "import sys",
        "import time",
        "from pathlib import Path",
        "import pandas as pd",
        "from melt import ui",
        "",
        "control = Path.cwd()",
        f"(control / {STARTED_NAME!r}).write_text('', encoding='utf-8')",
        f"(control / {PID_NAME!r}).write_text(str(os.getpid()),"
        " encoding='utf-8')",
    ]
    if spawn_child:
        # The helper spawns before any ignore disposition, so it keeps
        # the default SIGTERM behavior and the group's terminal signal
        # reaches it while its leader still ignores that signal.
        lines.extend(
            [
                "child = subprocess.Popen(",
                f"    [sys.executable, '-c', {CHILD_WAIT_CODE!r}],",
                ")",
                f"(control / {CHILD_PID_NAME!r}).write_text("
                "str(child.pid), encoding='utf-8')",
            ]
        )
    if ignore_term:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    lines.extend(
        [
            f"deadline = time.monotonic() + {int(GATE_TIMEOUT_SECONDS)}",
            f"while not (control / {RELEASE_NAME!r}).exists():",
            "    if time.monotonic() > deadline:",
            "        raise RuntimeError('the race gate never opened')",
            f"    time.sleep({POLL_SECONDS})",
            "sales = pd.read_parquet(ui.input_path('sales'))",
            "selected = sales[sales['revenue'] >= ui.params['minimum']]",
            "ui.output('filtered', selected)",
        ]
    )
    return "\n".join(lines) + "\n"


class FrameChannel:
    """One in-memory framed channel driving the ``serve`` loop."""

    def __init__(self, frames: "list[bytes]"):
        self._frames = list(frames)
        self._buffer = b""
        self.responses: "list[bytes]" = []

    def read(self, count: int) -> bytes:
        """Hand the caller at most `count` bytes, then clean EOF."""

        while not self._buffer and self._frames:
            self._buffer += self._frames.pop(0)
        data = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return data

    def write(self, data: bytes) -> None:
        self.responses.append(data)


def frame_bytes(
    request_id: str,
    *,
    method: str,
    payload: "dict | None" = None,
    expected: "dict | None" = None,
    operations: "list | None" = None,
) -> bytes:
    """Encode one command envelope exactly the way a host does."""

    body = {
        "protocolVersion": 1,
        "workspaceId": WORKSPACE,
        "requestId": request_id,
        "method": method,
        "expected": expected,
    }
    if operations is not None:
        body["operations"] = operations
    else:
        body["payload"] = {} if payload is None else payload
    return encode_frame(json.dumps(body).encode("utf-8"))


def decode_responses(channel: FrameChannel) -> "list[dict]":
    """Decode every framed answer the channel collected."""

    answers = []
    for data in channel.responses:
        raw, _ = decode_frame(data)
        answers.append(json.loads(raw.decode("utf-8")))
    return answers


def wait_for_marker(path: Path, *, timeout: float = GATE_TIMEOUT_SECONDS) -> None:
    """Wait until one gate marker exists."""

    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared within {timeout} s")
        time.sleep(POLL_SECONDS)


def read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def pid_state(pid: int) -> "str | None":
    """The scheduler state of one process id, or None when gone."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
    except OSError:
        return None
    return fields[0].decode("ascii")


def pid_stopped(pid: int) -> bool:
    """True when one process id exited; a zombie already stopped."""

    state = pid_state(pid)
    return state is None or state == "Z"


def wait_until_stopped(pid: int, *, timeout: float = GATE_TIMEOUT_SECONDS) -> None:
    """Wait until one process id is gone or reaped as a zombie."""

    deadline = time.monotonic() + timeout
    while not pid_stopped(pid):
        if time.monotonic() > deadline:
            raise AssertionError(
                f"process {pid} still runs in state"
                f" {pid_state(pid)!r} after {timeout} s"
            )
        time.sleep(POLL_SECONDS)


def launch_parked(
    service: WorkspaceService,
    run_id: str,
    markers: "tuple[str, ...]" = (),
) -> Path:
    """Park one gated run on the execution slot; return its staging.

    The run parks once the started marker and every marker named in
    ``markers`` exists. The gated process writes its pid and child-pid
    markers after the started one, at its own pace, so a caller that
    reads one of those markers waits for it here the same way this
    helper waits for the started marker instead of racing the process.
    """

    started = service.launch_admitted_run(run_id, wait=False)
    assert started.launched, started.refusal
    assert started.state == "running"
    assert service.execution_run_id == run_id
    staging = service.store.layout.staging_directory(run_id)
    wait_for_marker(staging / STARTED_NAME)
    for name in markers:
        wait_for_marker(staging / name)
    assert service.runs[run_id].state == "running"
    return staging


# ---------------------------------------------------------------------------
# The framed channel stays live while a project process executes
# ---------------------------------------------------------------------------


def test_the_framed_channel_answers_while_a_process_executes(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    staging = launch_parked(service, run_id)

    # The gated process is provably alive: it wrote its started marker
    # and its release marker does not exist.
    assert not (staging / RELEASE_NAME).exists()

    revision = service.parameter_state["minimum"].revision
    channel = FrameChannel(
        [
            frame_bytes("10-inspect", method="workspace.inspect"),
            frame_bytes(
                "11-table",
                method="table.page",
                payload={
                    "source": {"sourceId": "sales-input", "version": 1},
                    "sort": [],
                    "filters": [],
                    "offset": 0,
                    "limit": 10,
                },
            ),
            frame_bytes(
                "12-apply",
                method="workspace.apply",
                expected={"state/minimum": revision},
                operations=[
                    {
                        "op": "parameter.set",
                        "id": "minimum",
                        "value": 200,
                    }
                ],
            ),
            frame_bytes(
                "13-run", method="run.inspect", payload={"runId": run_id}
            ),
        ]
    )
    service.serve(channel.read, channel.write)

    inspect, table, applied, run = decode_responses(channel)
    assert inspect["ok"], inspect["errors"]
    assert [item["runId"] for item in inspect["result"]["activeRuns"]] == [
        run_id
    ]
    assert table["ok"], table["errors"]
    assert len(table["result"]["rows"]) == 3, "the hero input has three rows"
    assert applied["ok"], applied["errors"]

    # The mutation committed during execution: the parameter saved, the
    # bound action reserved the next generation, and one newest pending
    # request remains (SPEC.md sections 9.3 and 18.3 row 1).
    assert service.parameter_state["minimum"].value == 200
    assert service.output_groups["sales-results"].generation == 2
    assert service.pending_intents["sales-results"].status == "pending"
    assert run["ok"], run["errors"]
    # The admitted snapshot never moved: the captured parameters of the
    # live run are the ones it launched with (SPEC.md section 7.3).
    assert run["result"]["run"]["state"] == "running"
    assert run["result"]["run"]["parameters"]["minimum"]["value"] == 0
    assert service.runs[run_id].state == "running"
    assert service.wait_for_execution(timeout=0) is None

    (staging / RELEASE_NAME).write_text("", encoding="utf-8")
    outcome = service.wait_for_execution(timeout=GATE_TIMEOUT_SECONDS)
    assert outcome is not None and outcome.launched
    assert outcome.state == "succeeded", outcome.refusal
    assert service.execution_run_id is None
    # The run finished before the debounce fired, so the reserved
    # generation makes it historical output only (SPEC.md section 9.3).
    publication = service.publish_run(run_id)
    assert publication.outcome == "superseded"
    assert "reserved generation 2" in publication.reason


# ---------------------------------------------------------------------------
# run.cancel signals the owned process group
# ---------------------------------------------------------------------------


def test_run_cancel_through_the_channel_signals_the_owned_process_group(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(service_adapter, script=pid_gated_script())
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    staging = launch_parked(service, run_id, markers=(PID_NAME,))
    pid = read_pid(staging / PID_NAME)
    assert not pid_stopped(pid), "the gated process must be alive"

    channel = FrameChannel(
        [
            frame_bytes(
                "06-cancel", method="run.cancel", payload={"runId": run_id}
            )
        ]
    )
    service.serve(channel.read, channel.write)
    (cancel,) = decode_responses(channel)

    assert cancel["ok"], cancel["errors"]
    assert cancel["result"]["state"] == "cancelled"
    cancellation = cancel["result"]["cancellation"]
    assert cancellation["status"] == "cancelled"
    assert cancellation["signal"]["result"] == "terminated"
    assert cancellation["signal"]["signals"] == ["SIGTERM"]
    assert cancellation["signal"]["exitCode"] == -signal.SIGTERM
    # The gate never opened, so only the group signal stopped the run.
    assert not (staging / RELEASE_NAME).exists()
    wait_until_stopped(pid)

    outcome = service.wait_for_execution(timeout=GATE_TIMEOUT_SECONDS)
    assert outcome is not None and outcome.launched
    assert outcome.state == "cancelled"
    assert outcome.exit_code == -signal.SIGTERM
    assert outcome.staged is None, "a cancelled run staged nothing"
    assert service.execution_run_id is None

    # A cancelled run cannot publish, and the group reports its
    # cancellation as the error state of section 13.5.
    refused = service.publish_run(run_id)
    assert refused.outcome == "not-publishable"
    assert refused.refusal[0].code == "E_CONFLICT"
    status = status_of(service)
    assert status["status"] == "error"
    assert status["reason"] == "the newest request was cancelled"

    # Cancellation stays idempotent after the process stopped.
    again = service.dispatch(_cancel_envelope("07-cancel-again", run_id))
    assert again.ok, again.errors
    assert again.result["cancellation"]["status"] == "alreadyCancelled"


def _cancel_envelope(request_id: str, run_id: str):
    """One parsed run.cancel envelope for direct dispatch."""

    from test_scheduling import envelope

    return envelope(
        request_id, method="run.cancel", payload={"runId": run_id}
    )


def test_cancellation_escalates_and_reaches_the_whole_group(
    service_adapter: PlatformAdapter,
):
    seeded = seed_race(
        service_adapter,
        script=pid_gated_script(ignore_term=True, spawn_child=True),
        open_kwargs={"cancel_grace_seconds": 0.5},
    )
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    staging = launch_parked(
        service, run_id, markers=(PID_NAME, CHILD_PID_NAME)
    )
    pid = read_pid(staging / PID_NAME)
    child = read_pid(staging / CHILD_PID_NAME)
    assert not pid_stopped(pid)
    assert not pid_stopped(child), "the spawned helper must be alive"

    cancel = service.dispatch(_cancel_envelope("06-cancel", run_id))
    assert cancel.ok, cancel.errors
    escalation = cancel.result["cancellation"]["signal"]
    # The script ignored SIGTERM, so the service escalated (13.5).
    assert escalation["result"] == "escalated"
    assert escalation["signals"] == ["SIGTERM", "SIGKILL"]
    assert escalation["exitCode"] == -signal.SIGKILL
    # The group signal reached the spawned helper too: it slept far
    # beyond the test window and only a group signal could stop it.
    wait_until_stopped(pid)
    wait_until_stopped(child)

    outcome = service.wait_for_execution(timeout=GATE_TIMEOUT_SECONDS)
    assert outcome is not None
    assert outcome.state == "cancelled"
    assert outcome.exit_code == -signal.SIGKILL


def test_a_blocking_launch_can_be_cancelled_through_the_channel(
    service_adapter: PlatformAdapter,
):
    """The blocking driver owns the same signalable handle.

    The race suite drives ``launch_admitted_run`` on a helper thread;
    with the owned-process slot, a framed ``run.cancel`` from the test
    thread stops that live process too (SPEC.md sections 13.5 and 18.3).
    """

    seeded = seed_race(service_adapter, script=pid_gated_script())
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]

    with ExecutingRun(service, run_id) as executing:
        assert executing.state == "running"
        cancel = service.dispatch(_cancel_envelope("06-cancel", run_id))
        assert cancel.ok, cancel.errors
        signal_report = cancel.result["cancellation"]["signal"]
        assert signal_report["result"] == "terminated"
        assert signal_report["signals"] == ["SIGTERM"]
        assert executing.state == "cancelled"

    assert executing.outcome.launched
    assert executing.outcome.state == "cancelled"
    assert executing.outcome.exit_code == -signal.SIGTERM
    assert service.execution_run_id is None


# ---------------------------------------------------------------------------
# The single execution slot and service quit
# ---------------------------------------------------------------------------


def test_one_project_execution_runs_at_a_time(service_adapter: PlatformAdapter):
    seeded = seed_race(service_adapter, script=gated_script())
    service = seeded.service
    first = invoke(service, "05-invoke").result["run"]["runId"]
    second = invoke(service, "06-invoke-again").result["run"]["runId"]
    staging = launch_parked(service, first)

    refused = service.launch_admitted_run(second)
    assert refused.launched is False
    assert refused.state == "queued"
    assert [(item.code, item.path) for item in refused.refusal] == [
        ("E_CONFLICT", "/runId")
    ]
    assert "only one project execution" in refused.refusal[0].message
    also_refused = service.launch_next_run()
    assert also_refused is not None and not also_refused.launched

    # Cancelling the executing run frees the slot for the queued one.
    cancel = service.dispatch(_cancel_envelope("07-cancel", first))
    assert cancel.ok, cancel.errors
    outcome = service.wait_for_execution(timeout=GATE_TIMEOUT_SECONDS)
    assert outcome is not None and outcome.state == "cancelled"
    assert service.execution_run_id is None

    second_staging = service.store.layout.staging_directory(second)
    second_staging.mkdir(parents=True, exist_ok=True)
    (second_staging / RELEASE_NAME).write_text("", encoding="utf-8")
    retried = service.launch_admitted_run(second)
    assert retried.launched, retried.refusal
    assert retried.state == "succeeded"


def test_close_terminates_the_owned_process(service_adapter: PlatformAdapter):
    seeded = seed_race(service_adapter, script=pid_gated_script())
    service = seeded.service
    run_id = invoke(service, "05-invoke").result["run"]["runId"]
    staging = launch_parked(service, run_id, markers=(PID_NAME,))
    pid = read_pid(staging / PID_NAME)
    assert not pid_stopped(pid)

    # Quit stops scheduling and terminates the owned worker (SPEC.md
    # section 14): the process dies with the service, its run concludes
    # interrupted, and the reopened workspace holds no active job.
    service.close()
    wait_until_stopped(pid)

    reopened = WorkspaceService.open(service_adapter, WORKSPACE)
    try:
        assert reopened.runs[run_id].state == "interrupted"
        inspected = reopened.dispatch(_inspect_envelope("06-inspect"))
        assert inspected.ok, inspected.errors
        assert inspected.result["activeRuns"] == []
    finally:
        reopened.close()


def _inspect_envelope(request_id: str):
    from test_scheduling import envelope

    return envelope(request_id, method="workspace.inspect")
