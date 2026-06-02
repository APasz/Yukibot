from __future__ import annotations


def _is_executor_shutdown_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and "cannot schedule new futures after shutdown" in str(error)


def _http_exception(status_code: int, detail: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)


__all__: tuple[str, ...] = (
    "_http_exception",
    "_is_executor_shutdown_error",
)
