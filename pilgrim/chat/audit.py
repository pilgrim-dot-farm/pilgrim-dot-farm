"""CloudWatch audit log writer for chat events with message text / IP hashes.

SHEEP_PEN.md §0.5.5, §7. Ordinary structured events go to stdout (and the
container log group). Message text and IP hashes go **only** here, to the
dedicated audit log group, via boto3 `PutLogEvents`. The client is built from
`media.aws_profile` on the Mac (IAM user `mac-studio` holds `logs:*` since
2026-11-07) and from the task role on Fargate. A missing or denied credential
is a logged degradation: chat never crashes because auditing failed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from pilgrim.config import Config

log = logging.getLogger("radio.chat.audit")


class AuditLog:
    def __init__(self, cfg: Config, client: Any | None = None,
                 stream: str = "chat") -> None:
        self.cfg = cfg
        self.group = cfg.chat.audit_log_group
        self.stream = stream
        self._client = client
        self._token: str | None = None
        self._ready = client is not None
        self._warned = False

    def _build(self) -> Any:
        import boto3

        return boto3.Session(profile_name=self.cfg.media.aws_profile).client(
            "logs", region_name=self.cfg.media.region)

    def _ensure(self) -> bool:
        """Create the stream if needed; False when the client is unavailable."""
        if self._ready:
            return True
        try:
            self._client = self._build()
            self._ready = True
            return True
        except Exception as e:  # noqa: BLE001 - audit failure never breaks chat
            if not self._warned:
                self._warned = True
                log.warning("chat.audit.unavailable",
                            extra={"error": f"{type(e).__name__}: {e}"[:200]})
            return False

    def _describe(self) -> None:
        assert self._client is not None
        resp = self._client.describe_log_streams(
            logGroupName=self.group, logStreamNamePrefix=self.stream)
        for s in resp.get("logStreams", []):
            if s.get("logStreamName") == self.stream:
                self._token = s.get("uploadSequenceToken")
                return
        with contextlib.suppress(Exception):
            # ResourceAlreadyExists is fine.
            self._client.create_log_stream(logGroupName=self.group,
                                           logStreamName=self.stream)
        self._token = None

    def _put(self, event: dict[str, Any]) -> None:
        assert self._client is not None
        message = json.dumps(event, default=str, separators=(",", ":"))
        params: dict[str, Any] = {
            "logGroupName": self.group,
            "logStreamName": self.stream,
            "logEvents": [{"timestamp": int(time.time() * 1000), "message": message}],
        }
        if self._token:
            params["sequenceToken"] = self._token
        try:
            resp = self._client.put_log_events(**params)
            self._token = resp.get("nextSequenceToken")
        except Exception as e:  # noqa: BLE001
            # An invalid sequence token is recoverable once by re-describing.
            if "sequence" in str(e).lower():
                self._describe()
                params["sequenceToken"] = self._token
                resp = self._client.put_log_events(**params)
                self._token = resp.get("nextSequenceToken")
            else:
                raise

    def write(self, event: str, **fields: Any) -> bool:
        """Synchronous write (boto3). Returns False when auditing is degraded."""
        if not self._ensure():
            return False
        try:
            if self._token is None:
                self._describe()
            self._put({"event": event, "ts": time.time(), **fields})
            return True
        except Exception as e:  # noqa: BLE001
            if not self._warned:
                self._warned = True
                log.warning("chat.audit.failed",
                            extra={"event": event, "error": f"{type(e).__name__}: {e}"[:200]})
            return False

    async def awrite(self, event: str, **fields: Any) -> bool:
        return await asyncio.to_thread(self.write, event, **fields)


class NullAudit:
    """Test double: records events in memory, never touches AWS."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: str, **fields: Any) -> bool:
        self.events.append({"event": event, **fields})
        return True

    async def awrite(self, event: str, **fields: Any) -> bool:
        return self.write(event, **fields)
