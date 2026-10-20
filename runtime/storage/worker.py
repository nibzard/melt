"""Client for the Node storage worker (SPEC.md sections 12.1 and 14).

The pinned Pi Durable adapter is a Node library over ``node:sqlite``,
so the owning connection of a workspace ``state.sqlite`` lives in a
Node helper process: ``pi_adapter/workspace_worker.mjs``. This client
drives that helper across the framed channel of SPEC.md section 12.1,
reusing the frame codec of the ``melt`` schema package so both sides
agree on the four-byte big-endian length prefix and the frame limit.

One helper process owns exactly one connection. The helper exits when
its stdin pipe closes, so a service that dies leaves no orphan holding
the database. Ownership across processes is the workspace ``owner.lock``
in ``runtime.storage.ownership``.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from melt.schema.commands import encode_frame, read_frame
from melt.schema.limits import DEFAULT_LIMITS

from runtime.storage.errors import StorageWorkerError

PI_DURABLE_PIN = "1.3.0"
"""The pinned Pi Durable adapter version (pi_adapter/package.json)."""

SYNCHRONOUS_FULL = 2
"""`PRAGMA synchronous = FULL`, verified on the owning connection."""

WORKER_SCRIPT = Path(__file__).resolve().parent / "pi_adapter" / "workspace_worker.mjs"

ADAPTER_PACKAGE = Path(__file__).resolve().parent / "pi_adapter" / "node_modules" / "@earendil-works" / "pi-durable"

DEFAULT_TIMEOUT_SECONDS = 30.0
STDERR_TAIL_CHARACTERS = 2000


def _installed_adapter_version() -> str | None:
    """The installed adapter version, or None when the pin is missing."""

    marker = ADAPTER_PACKAGE / "package.json"
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def storage_worker_available(node: str | None = None) -> tuple[bool, str]:
    """Report whether the helper can run, with an actionable reason."""

    executable = node or shutil.which("node")
    if executable is None:
        return False, "node >= 22.19 is required to run the storage worker"
    installed = _installed_adapter_version()
    if installed != PI_DURABLE_PIN:
        return False, (
            f"pinned Pi Durable adapter {PI_DURABLE_PIN} is not installed"
            f" (found {installed!r}); run `make probe-setup`"
        )
    return True, ""


def _default_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise StorageWorkerError(
            "node >= 22.19 is required to run the storage worker;"
            " install it or pass node=<executable>"
        )
    return node


class StorageWorker:
    """One spawned helper process with its one owning connection."""

    def __init__(
        self,
        process: subprocess.Popen,
        stderr_file,
        facts: Mapping[str, Any],
    ):
        self._process = process
        self._stderr_file = stderr_file
        self._open_facts = dict(facts)
        self._root_committed = False
        self._closed = False

    # ------------------------------------------------------------------
    # Startup and teardown
    # ------------------------------------------------------------------

    @classmethod
    def start(
        cls,
        database_path: Path,
        *,
        node: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> "StorageWorker":
        """Spawn the helper and open the owning connection.

        The durability policy is verified on every open: the adapter
        ships ``synchronous = NORMAL`` per connection (probe finding
        F1), so the helper raises it to FULL and this client refuses an
        owning connection that does not report it.
        """

        if not WORKER_SCRIPT.is_file():
            raise StorageWorkerError(f"storage worker script is missing: {WORKER_SCRIPT}")
        executable = node or _default_node()
        installed = _installed_adapter_version()
        if installed != PI_DURABLE_PIN:
            raise StorageWorkerError(
                f"pinned Pi Durable adapter {PI_DURABLE_PIN} is not installed"
                f" (found {installed!r}); run `make probe-setup`"
            )
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [executable, str(WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            cwd=str(WORKER_SCRIPT.parent),
        )
        worker = cls(process, stderr_file, {})
        try:
            response = worker.call(
                "open",
                {"path": str(database_path)},
                timeout=timeout,
            )
            worker._root_committed = response.get("rootConversationCommitted") is True
            facts = response.get("facts") or {}
            problems = []
            if facts.get("adapterVersion") != PI_DURABLE_PIN:
                problems.append(
                    f"adapter version {facts.get('adapterVersion')!r} is not"
                    f" the pin {PI_DURABLE_PIN}"
                )
            if facts.get("synchronous") != SYNCHRONOUS_FULL:
                problems.append(
                    f"synchronous={facts.get('synchronous')!r}, Melt requires"
                    f" FULL ({SYNCHRONOUS_FULL})"
                )
            if facts.get("journalMode") != "wal":
                problems.append(f"journal_mode={facts.get('journalMode')!r}")
            if sys.platform == "darwin":
                for setting in ("fullfsync", "checkpointFullfsync"):
                    if facts.get(setting) != 1:
                        problems.append(f"{setting}={facts.get(setting)!r}, requires ON (1)")
            if problems:
                raise StorageWorkerError(
                    "owning connection rejected: " + "; ".join(problems)
                )
            worker._open_facts = facts
            return worker
        except BaseException:
            worker.terminate()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def facts(self) -> dict:
        """The durability facts verified when the connection opened."""

        return dict(self._open_facts)

    @property
    def root_conversation_committed(self) -> bool:
        """Whether the database already holds its root conversation.

        The adapter rejects a scan of a conversation that was never
        created, so callers check this before their first scan of a
        fresh workspace.
        """

        return self._root_committed

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def stderr_tail(self) -> str:
        """The tail of the helper's stderr, for error messages."""

        try:
            self._stderr_file.seek(0)
            raw = self._stderr_file.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""
        return raw[-STDERR_TAIL_CHARACTERS:]

    def terminate(self) -> None:
        """Stop the helper, closing its connection. Idempotent."""

        if self._process.poll() is None:
            try:
                self.call("shutdown", {}, timeout=10.0)
            except (StorageWorkerError, OSError, ValueError):
                self._process.kill()
        self._process.wait(timeout=10.0)
        self._stderr_file.close()
        self._closed = True

    def kill(self) -> None:
        """SIGKILL the helper, as the crash tests do."""

        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=10.0)
        self._stderr_file.close()
        self._closed = True

    def close_input(self) -> None:
        """Close the input pipe; the helper then exits on its own."""

        if self._process.stdin is not None:
            self._process.stdin.close()

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the helper to exit and return its exit code."""

        return self._process.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Frame transport
    # ------------------------------------------------------------------

    def _write_all(self, data: bytes) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise StorageWorkerError("the storage worker has no input pipe")
        view = memoryview(data)
        while view:
            try:
                view = view[stdin.write(view) :]
            except BrokenPipeError as error:
                raise StorageWorkerError(
                    "the storage worker closed its input:"
                    f" {self._failure_detail()}"
                ) from error
        stdin.flush()

    def _read_exact(self, count: int, timeout: float) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            raise StorageWorkerError("the storage worker has no output pipe")
        parts = []
        remaining = count
        deadline = time.monotonic() + timeout
        while remaining:
            timeout_left = deadline - time.monotonic()
            if timeout_left <= 0:
                raise StorageWorkerError(
                    f"the storage worker did not answer within {timeout} s"
                )
            readable, _, _ = select.select([stdout], [], [], timeout_left)
            if not readable:
                raise StorageWorkerError(
                    f"the storage worker did not answer within {timeout} s:"
                    f" {self._failure_detail()}"
                )
            chunk = os.read(stdout.fileno(), remaining)
            if not chunk:
                raise StorageWorkerError(
                    "the storage worker exited before answering:"
                    f" {self._failure_detail()}"
                )
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def _failure_detail(self) -> str:
        code = self._process.poll()
        tail = self.stderr_tail().strip()
        detail = f"exit code {code}" if code is not None else "still running"
        if tail:
            detail = f"{detail}; stderr: {tail}"
        return detail

    def call(
        self,
        op: str,
        fields: Mapping[str, Any],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Send one operation frame and return its successful response.

        Raises `StorageWorkerError` when the helper reports a failure.
        The frame limit is enforced before any byte reaches the wire
        (SPEC.md section 12.1); an oversized request raises the schema
        `E_LIMIT` error from `encode_frame`.
        """

        if self._closed:
            raise StorageWorkerError("the storage worker is stopped")
        payload = json.dumps(
            {"op": op, **fields}, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        # encode_frame raises E_LIMIT before any byte reaches the wire
        # when the request exceeds the control frame limit.
        self._write_all(encode_frame(payload))
        raw = read_frame(
            lambda count: self._read_exact(count, timeout),
            limit=DEFAULT_LIMITS.control_frame_bytes,
        )
        try:
            response = json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise StorageWorkerError(
                "the storage worker sent a frame that is not JSON"
            ) from error
        if not isinstance(response, dict):
            raise StorageWorkerError(
                "the storage worker sent a frame that is not an object"
            )
        if response.get("ok") is not True:
            code = response.get("code", "E_PARSE")
            message = response.get("message", "unknown storage worker failure")
            raise StorageWorkerError(f"storage worker {op} failed [{code}]: {message}")
        return response

    def send_raw(self, data: bytes) -> None:
        """Write raw bytes to the helper, for frame-protocol tests."""

        self._write_all(data)

    def read_response(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
        """Read and decode one response frame, for frame-protocol tests."""

        raw = read_frame(
            lambda count: self._read_exact(count, timeout),
            limit=DEFAULT_LIMITS.control_frame_bytes,
        )
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def mint_id(self) -> int:
        """Mint one identifier on the owning connection (finding F5)."""

        return int(self.call("mintId", {})["id"])

    def commit_entries(
        self, records: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> tuple[int, list[dict]]:
        """Commit records atomically; return the sequence and entries."""

        response = self.call(
            "commit",
            {
                "records": [
                    {"kind": kind, "data": dict(data)} for kind, data in records
                ]
            },
        )
        # The first commit establishes the root conversation, after
        # which the record stream is scannable.
        self._root_committed = True
        return int(response["commitSequence"]), list(response["entries"])

    def get_entry(self, entry_id: int) -> dict | None:
        """Read one record by its minted identifier."""

        entry = self.call("entry", {"id": int(entry_id)}).get("entry")
        return None if entry is None else dict(entry)

    def scan_entries(
        self, *, limit: int = 200, cursor: Mapping[str, Any] | None = None
    ) -> tuple[list[dict], dict | None]:
        """Scan the record stream oldest first."""

        fields: dict[str, Any] = {"limit": int(limit)}
        if cursor is not None:
            fields["cursor"] = dict(cursor)
        response = self.call("scan", fields)
        return list(response["entries"]), response.get("next")

    def durability_facts(self) -> dict:
        """Re-read the durability settings on the owning connection."""

        return dict(self.call("facts", {})["facts"])


__all__ = [
    "ADAPTER_PACKAGE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PI_DURABLE_PIN",
    "SYNCHRONOUS_FULL",
    "StorageWorker",
    "WORKER_SCRIPT",
    "storage_worker_available",
]
