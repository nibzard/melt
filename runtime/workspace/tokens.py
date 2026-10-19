"""Service-issued artifact and script tokens (SPEC.md section 12.6).

Artifact and script tokens are opaque handles a host import operation
obtains from the service. A document command can carry the token, never
an arbitrary filesystem path: the service alone maps a token back to
bytes it captured and validated itself.

A token is:

- Opaque. It is random material with a fixed prefix; it encodes no
  path, hash, or kind a client could forge or reuse.
- Workspace-scoped. The store of one workspace cannot redeem a token
  another workspace minted.
- Short-lived. A token expires 15 minutes after capture. Tokens live in
  service memory only, so a restart withdraws them; the host repeats
  the import instead of trusting a stale handle.

Capturing writes the bytes into the workspace ``artifacts/`` directory
through the content-addressed store, so a later import or registration
references durable bytes, and the returned hash always matches them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from melt.schema.diagnostics import Diagnostic, DiagnosticCode, MeltSchemaError
from runtime.storage.artifacts import ArtifactStore

TOKEN_LIFETIME = timedelta(minutes=15)
"""How long a captured token stays redeemable (SPEC.md section 12.6)."""

TOKEN_PREFIX = "melttok_"
"""The fixed prefix of a token. The remainder is random material."""

TOKEN_ENTROPY_BYTES = 24
"""Random bytes per token: 192 bits, far beyond guessing range."""

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PARQUET_MAGIC = b"PAR1"

CAPTURE_KINDS = {
    "application/vnd.apache.parquet": "table",
    "application/vnd.melt.graph+json": "graph",
    "image/png": "image",
    "text/plain": "text",
    "text/markdown": "text",
    "text/x-python": "script",
}
"""The media types a capture accepts, with the source kind it yields.

The graph media type belongs to `melt/semantic-draft-1` (RFC 001
section 7.1); capturing graph bytes works in either language, but a
graph source enters only an extension document."""


class MeltTokenError(MeltSchemaError):
    """Raised when a capture cannot validate its bytes."""


@dataclass(frozen=True)
class CapturedArtifact:
    """One captured import and its opaque handle."""

    token: str
    kind: str
    artifact: str
    byteLength: int
    mediaType: str
    capturedAt: str
    expiresAt: str


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_bytes(payload: bytes, kind: str) -> None:
    """Check the captured bytes against their declared media type.

    The check is the capture-time guard of SPEC.md section 12.6: a
    token stands for bytes of the claimed kind. Full schema measurement
    happens at import and registration, not here.
    """

    def fail(message: str) -> None:
        raise MeltTokenError(
            (
                Diagnostic(
                    code=DiagnosticCode.ARTIFACT,
                    path="/payload/bytes",
                    message=message,
                ),
            )
        )

    if kind == "table":
        if len(payload) < 12 or not (
            payload.startswith(PARQUET_MAGIC) and payload.endswith(PARQUET_MAGIC)
        ):
            fail("parquet bytes must start and end with the PAR1 magic")
    elif kind == "image":
        if not payload.startswith(PNG_SIGNATURE):
            fail("png bytes must start with the PNG signature")
    elif kind == "graph":
        import json

        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(
                f"graph bytes must be one UTF-8 JSON object: {error}",
            )
        if not isinstance(value, dict):
            fail("graph bytes must encode one JSON object")
    else:
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            fail("text bytes must be valid UTF-8")


class TokenStore:
    """The token mint of one open workspace."""

    def __init__(
        self,
        workspace_id: str,
        artifacts: ArtifactStore,
        *,
        clock: "Callable[[], datetime] | None" = None,
    ):
        self._workspace_id = workspace_id
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tokens: dict[str, CapturedArtifact] = {}

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def capture(self, data: bytes, media_type: str) -> CapturedArtifact:
        """Validate bytes, store them, and mint one opaque token.

        Raises `MeltTokenError` with an `E_ARTIFACT` diagnostic when the
        media type is unknown or the bytes do not match it.
        """

        kind = CAPTURE_KINDS.get(media_type)
        if kind is None:
            raise MeltTokenError(
                (
                    Diagnostic(
                        code=DiagnosticCode.ARTIFACT,
                        path="/payload/mediaType",
                        message=f"unknown capture media type {media_type!r}",
                        hint="Use one of: "
                        + ", ".join(sorted(CAPTURE_KINDS))
                        + ".",
                    ),
                )
            )
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise MeltTokenError(
                (
                    Diagnostic(
                        code=DiagnosticCode.ARTIFACT,
                        path="/payload/bytes",
                        message="captured data must be bytes",
                    ),
                )
            )
        payload = bytes(data)
        _validate_bytes(payload, kind)
        artifact = self._artifacts.write(payload)
        now = self._clock()
        token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
        captured = CapturedArtifact(
            token=token,
            kind=kind,
            artifact=artifact,
            byteLength=len(payload),
            mediaType=media_type,
            capturedAt=_timestamp(now),
            expiresAt=_timestamp(now + TOKEN_LIFETIME),
        )
        self._tokens[token] = captured
        return captured

    def redeem(self, token: str) -> "CapturedArtifact | None":
        """Return the capture of one token, or None when unusable.

        None covers unknown tokens, tokens of another workspace, and
        expired tokens; the caller learns nothing beyond that.
        """

        captured = self._tokens.get(token)
        if captured is None:
            return None
        if self._clock() >= datetime.strptime(
            captured.expiresAt, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc):
            self._tokens.pop(token, None)
            return None
        return captured

    def purge_expired(self) -> int:
        """Drop every expired token and return how many went."""

        now = self._clock()
        expired = [
            token
            for token, captured in self._tokens.items()
            if now
            >= datetime.strptime(
                captured.expiresAt, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        ]
        for token in expired:
            del self._tokens[token]
        return len(expired)


__all__ = [
    "CAPTURE_KINDS",
    "CapturedArtifact",
    "MeltTokenError",
    "TOKEN_LIFETIME",
    "TOKEN_PREFIX",
    "TokenStore",
]
