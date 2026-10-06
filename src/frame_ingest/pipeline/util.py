from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def run_units(items: Sequence[T], fn: Callable[[T], Awaitable[R]], limit: int) -> list[R]:
    """Run fn over items with bounded concurrency, preserving order.

    The first exception cancels the remaining work and propagates (so fatal provider errors
    stop a stage promptly; fail-soft behaviour is implemented inside fn).
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def guarded(item: T) -> R:
        async with sem:
            return await fn(item)

    tasks = [asyncio.ensure_future(guarded(i)) for i in items]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return [t.result() for t in tasks]


def approx_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token for English, a bit denser here)."""
    return max(1, int(len(text) / 3.6))
