from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

_BLOCKING_WORKER_COUNT = min(32, (os.cpu_count() or 1) + 4)
_BLOCKING_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BLOCKING_WORKER_COUNT,
    thread_name_prefix="yukibot-blocking",
)
_BLOCKING_RESULT_POLL_SECONDS = 0.005


def _submit_blocking(func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    return _BLOCKING_EXECUTOR.submit(call)


async def _blocking_result(future: Future[_T]) -> _T:
    while not future.done():
        await asyncio.sleep(_BLOCKING_RESULT_POLL_SECONDS)
    return future.result()


async def run_blocking(func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    future = _submit_blocking(func, *args, **kwargs)
    try:
        return await _blocking_result(future)
    except BaseException:
        future.cancel()
        raise


async def run_blocking_cancellable(
    cancelled: Event, func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _T:
    """Signal cancellation and retain ownership until the worker has stopped."""
    future = _submit_blocking(func, *args, **kwargs)
    try:
        return await _blocking_result(future)
    except asyncio.CancelledError:
        cancelled.set()
        future.cancel()
        while not future.done():
            try:
                await asyncio.sleep(_BLOCKING_RESULT_POLL_SECONDS)
            except asyncio.CancelledError:
                continue
        # The original cancellation takes precedence over the worker's result.
        raise


def shutdown_blocking_executor(*, wait: bool = False, cancel_futures: bool = True) -> None:
    _BLOCKING_EXECUTOR.shutdown(wait=wait, cancel_futures=cancel_futures)
