"""Update operation feedback without leaving stale pending notifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mod_web_toasts import ModWebNotificationType, notification_policy

from .nicegui_protocols import ModWebUi

if TYPE_CHECKING:
    from nicegui.elements.notification import Notification


def start_operation_notification(*, ui: ModWebUi, message: str) -> Notification:
    return ui.notification(message, type="ongoing", spinner=True, timeout=0, close_button="Dismiss")


def finish_operation_notification(
    *,
    ui: ModWebUi,
    notification: Notification | None,
    message: str,
    tone: ModWebNotificationType,
) -> None:
    """Replace pending feedback, or emit the result if it was already dismissed."""
    if notification is None:
        ui.notify(message, type=tone)
        return
    try:
        client = notification.client
    except RuntimeError:
        # The page client can be collected while its operation is awaiting a response.
        return
    if client.is_deleted:
        return
    if notification.is_deleted:
        with client:
            ui.notify(message, type=tone)
        return
    policy = notification_policy(tone)
    with notification.props.suspend_updates():
        notification.message = message
        notification.type = tone
        notification.spinner = False
        notification.timeout = policy.timeout_milliseconds / 1000
        notification.close_button = "Dismiss" if policy.close_button else False
    notification.update()


def dismiss_operation_notification(notification: Notification | None) -> None:
    """Remove pending feedback when an operation is cancelled."""
    if notification is not None and not notification.is_deleted:
        notification.dismiss()
