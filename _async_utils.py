from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

_BLOCKING_WORKER_COUNT = min(32, (os.cpu_count() or 1) + 4)
_BLOCKING_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BLOCKING_WORKER_COUNT,
    thread_name_prefix="yukibot-blocking",
)
_BLOCKING_RESULT_POLL_SECONDS = 0.005


async def run_blocking(func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    future = _BLOCKING_EXECUTOR.submit(call)
    try:
        while not future.done():
            await asyncio.sleep(_BLOCKING_RESULT_POLL_SECONDS)
    except BaseException:
        future.cancel()
        raise
    return future.result()


def shutdown_blocking_executor(*, wait: bool = False, cancel_futures: bool = True) -> None:
    _BLOCKING_EXECUTOR.shutdown(wait=wait, cancel_futures=cancel_futures)
