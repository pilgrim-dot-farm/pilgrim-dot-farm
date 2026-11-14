"""models.max_concurrency: the LLM client caps in-flight calls per model
(RADIO §6.1/§12, 2026-11-14). Fakes only — no real backends (AGENTS §2).

Concurrency is counted inside the production `LLM._hold_slot` (wrapped, not
replaced): the whole `chat_*` call runs in the held slot, and the mock SSE
body takes two chunks so each hold spans `hold_s`. Counting in the handler
body instead is unreliable — MockTransport abandons the async generator
after `[DONE]`, so a `finally` there runs at GC, not at stream end."""
from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from pilgrim.pipelines.llm import LLM, LLMError

_BODY_1 = b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
_BODY_2 = b'data: [DONE]\n'


def _streaming_client(hold_s: float) -> httpx.AsyncClient:
    """Two-chunk SSE body: the client must request the second chunk (behind
    the hold-sleep) before finishing, so each call keeps its slot for
    hold_s."""
    def handler(request: httpx.Request) -> httpx.Response:
        async def stream():
            yield _BODY_1
            await asyncio.sleep(hold_s)
            yield _BODY_2

        return httpx.Response(200, content=stream(),
                              headers={"Content-Type": "text/event-stream"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _count_holds(llm: LLM) -> dict:
    """Count concurrent holders of the REAL (production) slot hold."""
    count = {"n": 0, "max": 0}
    real = llm._hold_slot

    @contextlib.asynccontextmanager
    async def counting(model: str):
        async with real(model):
            count["n"] += 1
            count["max"] = max(count["max"], count["n"])
            try:
                yield
            finally:
                count["n"] -= 1

    llm._hold_slot = counting   # type: ignore[method-assign]
    return count


def test_llm_caps_in_flight_per_model(cfg):
    cfg.models.max_concurrency = {"m": 2}
    llm = LLM(cfg, "tok", client=_streaming_client(0.05))
    count = _count_holds(llm)

    async def main():
        await asyncio.gather(*(llm.chat_text("m", "sys", "user")
                               for _ in range(5)))
    asyncio.run(asyncio.wait_for(main(), 5.0))
    assert count["max"] == 2


def test_llm_unlisted_model_is_unbounded(cfg):
    cfg.models.max_concurrency = {"other": 1}   # "m" unlisted
    llm = LLM(cfg, "tok", client=_streaming_client(0.05))
    count = _count_holds(llm)

    async def main():
        await asyncio.gather(*(llm.chat_text("m", "sys", "user")
                               for _ in range(3)))
    asyncio.run(asyncio.wait_for(main(), 5.0))
    assert count["max"] == 3


def test_llm_cap_release_allows_next_call(cfg):
    """The slot is released when a call finishes (not stuck): four calls
    under a cap of 1 all complete, never two held at once."""
    cfg.models.max_concurrency = {"m": 1}
    llm = LLM(cfg, "tok", client=_streaming_client(0.01))
    count = _count_holds(llm)

    async def main():
        for _ in range(4):
            await llm.chat_text("m", "sys", "user")
    asyncio.run(asyncio.wait_for(main(), 5.0))
    assert count["max"] == 1


def test_llm_error_still_releases_the_slot(cfg):
    """A failed call must free its slot: the next call reaches the transport
    and succeeds (no stuck semaphore)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="nope")

        async def stream():
            yield _BODY_1
            yield _BODY_2
        return httpx.Response(200, content=stream(),
                              headers={"Content-Type": "text/event-stream"})

    cfg.models.max_concurrency = {"m": 1}
    llm = LLM(cfg, "tok",
              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError):
        asyncio.run(llm.chat_text("m", "sys", "user"))
    assert asyncio.run(asyncio.wait_for(llm.chat_text("m", "sys", "user"),
                                        5.0)) == "ok"
    assert calls["n"] == 2
