"""Artifact and script tokens (T010, SPEC.md section 12.6).

The token store mints the opaque, workspace-scoped, 15-minute handles
behind ``source.import`` and ``action.register``:

- A capture validates the bytes against the declared media type,
  stores them under their content hash, and answers a token that
  encodes no path, hash, or kind.
- A token redeems only inside its workspace and only before it expires.
- Expired tokens drop out of the store on redemption or purge.

These tests need no storage worker: the store only writes through the
content-addressed artifact directory.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from melt.schema.diagnostics import MeltSchemaError
from runtime.storage.artifacts import ArtifactStore
from runtime.workspace import (
    CAPTURE_KINDS,
    TOKEN_LIFETIME,
    TOKEN_PREFIX,
    MeltTokenError,
    TokenStore,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"opaque image payload"
PARQUET_BYTES = b"PAR1" + b"column data" + b"PAR1"
TEXT_BYTES = "revenue notes\n".encode("utf-8")
SCRIPT_BYTES = b"def run(ui):\n    pass\n"


class FixedClock:
    """A clock the tests advance by hand."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


@pytest.fixture
def artifacts_dir(service_root: Path) -> Path:
    case = service_root / f"tokens-{uuid.uuid4().hex[:12]}"
    case.mkdir(parents=True)
    return case


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc))


def store(workspace_id: str, artifacts_dir: Path, clock: FixedClock) -> TokenStore:
    return TokenStore(workspace_id, ArtifactStore(artifacts_dir), clock=clock)


def test_capture_validates_bytes_and_stores_them(
    artifacts_dir: Path, clock: FixedClock
):
    tokens = store("sales-demo", artifacts_dir, clock)
    for data, media_type, kind in (
        (PARQUET_BYTES, "application/vnd.apache.parquet", "table"),
        (PNG_BYTES, "image/png", "image"),
        (TEXT_BYTES, "text/plain", "text"),
        (TEXT_BYTES, "text/markdown", "text"),
        (SCRIPT_BYTES, "text/x-python", "script"),
    ):
        captured = tokens.capture(data, media_type)
        assert captured.kind == kind
        assert captured.mediaType == media_type
        assert captured.byteLength == len(data)
        assert captured.artifact == "sha256:" + hashlib.sha256(data).hexdigest()
        # The captured bytes live outside the database, under their hash.
        assert (artifacts_dir / captured.artifact[len("sha256:") :]).read_bytes() == data
        assert captured.capturedAt == "2026-10-18T12:00:00Z"
        assert captured.expiresAt == "2026-10-18T12:15:00Z"
        assert TOKEN_LIFETIME == timedelta(minutes=15)


def test_tokens_are_opaque_and_unguessable(artifacts_dir: Path, clock: FixedClock):
    tokens = store("sales-demo", artifacts_dir, clock)
    first = tokens.capture(PNG_BYTES, "image/png")
    second = tokens.capture(PNG_BYTES, "image/png")
    # Same bytes, same artifact, different opaque handles.
    assert first.artifact == second.artifact
    assert first.token != second.token
    for token in (first.token, second.token):
        assert token.startswith(TOKEN_PREFIX)
        remainder = token[len(TOKEN_PREFIX) :]
        assert len(remainder) >= 32, "at least 192 bits of random material"
        # The urlsafe alphabet carries no path or separator characters,
        # so a token can never be mistaken for a filesystem path.
        assert not (set(token) - set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"))
        assert hashlib.sha256(PNG_BYTES).hexdigest() not in token, (
            "a token encodes no derivable content"
        )


def test_tokens_expire_after_fifteen_minutes(artifacts_dir: Path, clock: FixedClock):
    tokens = store("sales-demo", artifacts_dir, clock)
    captured = tokens.capture(TEXT_BYTES, "text/plain")
    assert tokens.redeem(captured.token) == captured
    clock.advance(timedelta(minutes=14, seconds=59))
    assert tokens.redeem(captured.token) == captured
    clock.advance(timedelta(seconds=1))
    assert tokens.redeem(captured.token) is None, "the token expired"
    assert tokens.redeem(captured.token) is None, "expiry is permanent"


def test_expired_tokens_purge(artifacts_dir: Path, clock: FixedClock):
    tokens = store("sales-demo", artifacts_dir, clock)
    # Captured at 12:00, expires at 12:15.
    dying = tokens.capture(TEXT_BYTES, "text/plain")
    clock.advance(timedelta(minutes=10))
    # Captured at 12:10, expires at 12:25.
    living = tokens.capture(SCRIPT_BYTES, "text/x-python")
    clock.advance(timedelta(minutes=6))
    assert tokens.purge_expired() == 1
    assert tokens.redeem(dying.token) is None
    assert tokens.redeem(living.token) == living
    assert tokens.purge_expired() == 0


def test_tokens_are_workspace_scoped(artifacts_dir: Path, clock: FixedClock):
    home = store("sales-demo", artifacts_dir, clock)
    away = store("other-workspace", artifacts_dir, clock)
    captured = home.capture(PNG_BYTES, "image/png")
    assert away.redeem(captured.token) is None, (
        "a token of another workspace redeems nothing"
    )
    assert home.redeem(captured.token) == captured


def test_capture_rejects_bytes_that_do_not_match_the_media_type(
    artifacts_dir: Path, clock: FixedClock
):
    tokens = store("sales-demo", artifacts_dir, clock)

    def rejected(data: bytes, media_type: str) -> MeltSchemaError:
        with pytest.raises(MeltTokenError) as error:
            tokens.capture(data, media_type)
        return error.value

    not_png = rejected(b"PAR1not a pngPAR1", "image/png")
    assert not_png.diagnostics[0].code == "E_ARTIFACT"
    not_parquet = rejected(b"\x89PNG\r\n\x1a\n", "application/vnd.apache.parquet")
    assert not_parquet.diagnostics[0].code == "E_ARTIFACT"
    truncated_parquet = rejected(b"PAR1", "application/vnd.apache.parquet")
    assert truncated_parquet.diagnostics[0].code == "E_ARTIFACT"
    not_utf8 = rejected(b"revenue \xff notes", "text/plain")
    assert not_utf8.diagnostics[0].code == "E_ARTIFACT"
    not_graph = rejected(b"{not json", "application/vnd.melt.graph+json")
    assert not_graph.diagnostics[0].code == "E_ARTIFACT"
    not_graph_object = rejected(b"[1, 2]", "application/vnd.melt.graph+json")
    assert not_graph_object.diagnostics[0].code == "E_ARTIFACT"
    unknown_type = rejected(TEXT_BYTES, "application/octet-stream")
    assert unknown_type.diagnostics[0].code == "E_ARTIFACT"
    assert set(CAPTURE_KINDS) == {
        "application/vnd.apache.parquet",
        "application/vnd.melt.graph+json",
        "image/png",
        "text/plain",
        "text/markdown",
        "text/x-python",
    }
    # A failed capture mints no token and writes no artifact.
    assert tokens.redeem("melttok_anything") is None
    assert list(artifacts_dir.iterdir()) == []


def test_melt_token_error_is_a_schema_error():
    assert issubclass(MeltTokenError, MeltSchemaError)
