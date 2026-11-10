"""In-memory stand-in for the Meta Graph API (INSTAGRAM.md Step 4; AGENTS §1.2).

Wired in as an `httpx.MockTransport` under the real `GraphClient`, so request
shapes, token placement and error handling are exercised for real. Shapes
copied from the 2026-11-08 probes (docs/backends.md §14). `statuses` is the
sequence a container reports on successive polls (the last value repeats);
`fail_next` answers the next N calls with HTTP 500; `error_next` answers the
next call with a Graph error object (HTTP 400).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx


class FakeGraph:
    def __init__(self, ig_user_id: str = "17841400000000000", version: str = "v27.0") -> None:
        self.ig = ig_user_id
        self.version = version
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (method, path, query+form)
        self.statuses: list[str] = ["IN_PROGRESS", "FINISHED"]
        self._status_i = 0
        self.quota_usage = 1
        self.quota_total = 100
        self.fail_next = 0
        self.error_next: dict[str, Any] | None = None
        self.containers: dict[str, dict[str, str]] = {}
        self.published: list[str] = []
        self.bad_auth: list[str] = []  # paths with no Bearer header / a token in the URL

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @staticmethod
    def _form(request: httpx.Request) -> dict[str, str]:
        body = request.content.decode() if request.content else ""
        return {k: v[0] for k, v in parse_qs(body).items()} if body else {}

    @staticmethod
    def _error(message: str, code: int = 100, subcode: int | None = 33) -> httpx.Response:
        err: dict[str, Any] = {"message": message, "type": "GraphMethodException",
                               "code": code, "fbtrace_id": "Afake"}
        if subcode is not None:
            err["error_subcode"] = subcode
        return httpx.Response(400, json={"error": err})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/{self.version}")
        query = dict(request.url.params.items())
        form = self._form(request)
        self.calls.append((request.method, path, {**query, **form}))
        if not request.headers.get("authorization", "").startswith("Bearer ") \
                or "access_token" in query or "access_token" in form:
            self.bad_auth.append(path)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500, text="<html>upstream error</html>")
        if self.error_next is not None:
            err, self.error_next = self.error_next, None
            return httpx.Response(400, json={"error": err})
        obj = path.lstrip("/")
        if request.method == "GET" and path == f"/{self.ig}":
            return httpx.Response(200, json={"id": self.ig, "username": "pilgrim.farm",
                                             "media_count": 4})
        if request.method == "GET" and path == f"/{self.ig}/content_publishing_limit":
            return httpx.Response(200, json={"data": [{
                "quota_usage": self.quota_usage,
                "config": {"quota_total": self.quota_total, "quota_duration": 86400}}]})
        if request.method == "POST" and path == f"/{self.ig}/media":
            cid = f"C{len(self.containers) + 1}"
            self.containers[cid] = form
            return httpx.Response(200, json={"id": cid})
        if request.method == "GET" and obj in self.containers:
            code = self.statuses[min(self._status_i, len(self.statuses) - 1)]
            self._status_i += 1
            body: dict[str, Any] = {"status_code": code, "id": obj}
            if code == "ERROR":
                body["status"] = "Error: The video file is corrupted"
            return httpx.Response(200, json=body)
        if request.method == "POST" and path == f"/{self.ig}/media_publish":
            if form.get("creation_id") not in self.containers:
                return self._error("Unsupported post request.")
            mid = f"M{len(self.published) + 1}"
            self.published.append(mid)
            return httpx.Response(200, json={"id": mid})
        if request.method == "GET" and obj in self.published:
            return httpx.Response(200, json={
                "id": obj, "media_type": "VIDEO", "media_product_type": "REELS",
                "permalink": f"https://www.instagram.com/reel/{obj}/",
                "timestamp": "2026-11-08T20:00:00+0000"})
        return self._error(f"Unsupported get request. Object with ID '{obj}' does not exist, "
                           "cannot be loaded due to missing permissions, or does not support "
                           "this operation.")
