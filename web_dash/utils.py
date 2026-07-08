from __future__ import annotations

from _utils import format_player_capacity as _format_player_capacity


def _format_uptime_seconds(total_seconds: int) -> str:
    remaining = max(0, int(total_seconds))
    days, remaining = divmod(remaining, 24 * 60 * 60)
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, _seconds = divmod(remaining, 60)
    if days == 0 and hours == 0 and minutes == 0:
        return "<1m"
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or parts:
        parts.append(f"{hours}h")
    if minutes > 0 or parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _is_executor_shutdown_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and "cannot schedule new futures after shutdown" in str(error)


def _http_exception(status_code: int, detail: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)


__all__: tuple[str, ...] = (
    "_format_player_capacity",
    "_format_uptime_seconds",
    "_http_exception",
    "_is_executor_shutdown_error",
)
