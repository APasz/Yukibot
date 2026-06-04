from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from _discord import RelayEmbedPayload

if TYPE_CHECKING:
    from apps._app import App


def format_uptime(duration: timedelta) -> str:
    total_seconds = max(0, round(duration.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    components: list[str] = []
    if hours:
        components.append(f"{hours}h")
    if minutes:
        components.append(f"{minutes}m")
    if seconds or not components:
        components.append(f"{seconds}s")
    return " ".join(components)


def build_app_relay_embed(app: App, *, title: str, description: str) -> RelayEmbedPayload:
    return RelayEmbedPayload(
        title=title,
        description=description,
        color=app.manage_embed_color,
    )


def build_app_lifecycle_embed(
    app: App,
    *,
    started: bool,
    uptime: timedelta | None = None,
) -> RelayEmbedPayload:
    action = "Started" if started else "Ended"
    title = f"{app.friendly} {action}"
    description_lines: list[str] = []
    if started:
        if app.cfg.join_display_address is not None:
            description_lines.append(f"Join: `{app.cfg.join_display_address}`")
        description_lines.extend(app.lifecycle_relay_description_lines(started=True))
    else:
        if uptime is not None:
            description_lines.append(f"Uptime: `{format_uptime(uptime)}`")
    return build_app_relay_embed(
        app,
        title=title,
        description="\n".join(description_lines),
    )


def build_app_crash_embed(
    app: App,
    *,
    summary: str | None = None,
    uptime: timedelta | None = None,
) -> RelayEmbedPayload:
    description_lines: list[str] = []
    if summary is not None:
        description_lines.append(summary)
    if uptime is not None:
        description_lines.append(f"Uptime: `{format_uptime(uptime)}`")
    return build_app_relay_embed(
        app,
        title=f"{app.friendly} Crashed",
        description="\n".join(description_lines),
    )
