from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from _discord import RelayEmbedPayload
from relay_notices import AppLifecycleNotice, AppLifecycleState, RelayNoticeSeverity, RelayNoticeSource, notice_embed_spec

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
    notice = AppLifecycleNotice(
        state=AppLifecycleState.STARTED if started else AppLifecycleState.STOPPED,
        source=RelayNoticeSource.APP_MANAGER,
        join_address=app.cfg.join_display_address if started else None,
        detail_lines=app.lifecycle_relay_description_lines(started=started, uptime=uptime),
        uptime_seconds=None if uptime is None else max(0, round(uptime.total_seconds())),
    )
    embed_spec = notice_embed_spec(notice, app_name=app.friendly, author_name="System")
    if embed_spec is None:
        raise ValueError("App lifecycle notice did not produce an embed.")
    return build_app_relay_embed(app, title=embed_spec.title, description=embed_spec.description)


def build_app_crash_embed(
    app: App,
    *,
    summary: str | None = None,
    uptime: timedelta | None = None,
) -> RelayEmbedPayload:
    notice = AppLifecycleNotice(
        state=AppLifecycleState.CRASHED,
        source=RelayNoticeSource.APP_MANAGER,
        severity=RelayNoticeSeverity.ERROR,
        uptime_seconds=None if uptime is None else max(0, round(uptime.total_seconds())),
        summary=summary,
    )
    embed_spec = notice_embed_spec(notice, app_name=app.friendly, author_name="System")
    if embed_spec is None:
        raise ValueError("App crash notice did not produce an embed.")
    return build_app_relay_embed(app, title=embed_spec.title, description=embed_spec.description)
