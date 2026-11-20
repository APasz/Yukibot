from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, cast
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest
from nicegui import core, ui as nicegui_ui
from nicegui.api_router import APIRouter
from nicegui.client import Client
from nicegui.elements.notification import Notification
from nicegui.page import page

from checks import mod_web_check
from mod_web_auth import ModWebUser
from mod_web_toasts import MOD_WEB_TOAST_JAVASCRIPT, ModWebNotificationType, notification_policy
from node_api.app_state import NodeAppMutationAction, NodeAppMutationResult
from web_dash.nicegui_protocols import ModWebUi
from web_dash.notifications import finish_operation_notification, start_operation_notification
from web_dash.service import ModWebService


@pytest.fixture
def notification() -> MagicMock:
    return MagicMock(spec=Notification, is_deleted=False, client=MagicMock(is_deleted=False))


def test_toast_browser_behaviour() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for toast client behaviour checks.")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "fixtures" / "toasts_check.cjs")],
        input=MOD_WEB_TOAST_JAVASCRIPT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("tone", ["positive", "negative", "warning", "info"])
def test_operation_result_updates_the_original_notification(tone: ModWebNotificationType, notification: MagicMock) -> None:
    ui = Mock(spec=ModWebUi)
    ui.notification.return_value = notification
    started = start_operation_notification(ui=cast(ModWebUi, ui), message="Starting…")
    assert started is notification
    ui.notification.assert_called_once_with(
        "Starting…", type="ongoing", spinner=True, timeout=0, close_button="Dismiss",
    )
    finish_operation_notification(
        ui=cast(ModWebUi, ui), notification=started, message="Result", tone=tone,
    )
    policy = notification_policy(tone)
    assert notification.message == "Result"
    assert notification.type == tone
    assert notification.spinner is False
    assert notification.timeout == policy.timeout_milliseconds / 1000
    assert notification.close_button == ("Dismiss" if policy.close_button else False)
    notification.update.assert_called_once_with()
    notification.props.suspend_updates.assert_called_once_with()
    ui.notify.assert_not_called()


def test_dismissed_progress_does_not_hide_the_operation_result(notification: MagicMock) -> None:
    ui = Mock(spec=ModWebUi)
    notification.is_deleted = True
    ui.notify.side_effect = lambda *_args, **_kwargs: notification.client.__enter__.assert_called_once()
    finish_operation_notification(
        ui=cast(ModWebUi, ui), notification=cast(Notification, notification), message="Failed", tone="negative",
    )
    ui.notify.assert_called_once_with("Failed", type="negative")
    notification.update.assert_not_called()


def test_result_is_not_sent_after_its_page_client_is_deleted(notification: MagicMock) -> None:
    ui = Mock(spec=ModWebUi)
    notification.is_deleted = True
    notification.client.is_deleted = True
    finish_operation_notification(
        ui=cast(ModWebUi, ui), notification=cast(Notification, notification), message="Complete", tone="positive",
    )
    ui.notify.assert_not_called()
    notification.update.assert_not_called()


def test_result_is_not_sent_after_its_page_client_is_collected(notification: MagicMock) -> None:
    ui = Mock(spec=ModWebUi)
    with patch.object(type(notification), "client", new_callable=PropertyMock, create=True) as client_property:
        client_property.side_effect = RuntimeError("The client of this element has been deleted.")
        finish_operation_notification(
            ui=cast(ModWebUi, ui), notification=cast(Notification, notification), message="Complete", tone="positive",
        )
    ui.notify.assert_not_called()
    notification.update.assert_not_called()


def test_operation_feedback_uses_the_real_nicegui_client_and_batches_updates() -> None:
    async def exercise() -> None:
        with patch.object(core, "loop", asyncio.get_running_loop()):
            client = Client(page("/notification-check", api_router=APIRouter()))
            try:
                with client:
                    pending = start_operation_notification(ui=cast(ModWebUi, nicegui_ui), message="Starting")
                with patch.object(client.outbox, "enqueue_update", wraps=client.outbox.enqueue_update) as update:
                    finish_operation_notification(
                        ui=cast(ModWebUi, nicegui_ui), notification=pending, message="Complete", tone="positive",
                    )
                    update.assert_called_once_with(pending)
                assert pending.props["options"]["timeout"] == notification_policy("positive").timeout_milliseconds
                assert pending.spinner is False
                pending.delete()
                finish_operation_notification(
                    ui=cast(ModWebUi, nicegui_ui), notification=pending, message="Failure", tone="negative",
                )
                target, message_type, payload = client.outbox.messages[-1]
                assert target == client.id and message_type == "notify" and payload["message"] == "Failure"
                client.delete()
                message_count = len(client.outbox.messages)
                finish_operation_notification(
                    ui=cast(ModWebUi, nicegui_ui), notification=pending, message="Ignored", tone="positive",
                )
                assert len(client.outbox.messages) == message_count
            finally:
                if not client.is_deleted:
                    client.delete()
                await asyncio.sleep(0)

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["completed", "accepted", "failed", "cancelled"])
def test_runtime_toolbar_replaces_its_pending_feedback(
    outcome: Literal["completed", "accepted", "failed", "cancelled"],
    notification: MagicMock,
) -> None:
    model = mod_web_check.ModWebTests._overview_model_with_config_and_chat()
    service = ModWebService()
    user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
    ui = MagicMock(spec=ModWebUi)
    ui.notification.return_value = notification

    async def exercise() -> None:
        release = asyncio.Event()

        async def mutate(**_: object) -> NodeAppMutationResult:
            await release.wait()
            if outcome == "failed":
                raise RuntimeError("Connection lost.")
            message = (
                service._app_action_pending_message(NodeAppMutationAction.START, model.app_friendly)
                if outcome == "accepted"
                else "App started."
            )
            assert message is not None
            return NodeAppMutationResult(
                app_name=model.app_name, app_friendly=model.app_friendly, node=model.node_name,
                action=NodeAppMutationAction.START, message=message, app_stats=None,
            )

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_mutate_app", side_effect=mutate),
            patch.object(service, "_guarded_reload"),
            patch("web_dash.app_page._DOWNLOAD_FEEDBACK_DELAY_SECONDS", 0),
        ):
            service._render_global_app_toolbar(ui=cast(ModWebUi, ui), model=model, user=user, poll_runtime_model=False)
            handler = cast(Callable[[], Awaitable[None]], ui.button.call_args_list[0].kwargs["on_click"])
            task = asyncio.create_task(handler())
            await asyncio.sleep(0)
            ui.notification.assert_called_once()
            notification.update.assert_not_called()
            if outcome == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                notification.dismiss.assert_called_once_with()
                notification.update.assert_not_called()
                ui.notify.assert_not_called()
                return
            release.set()
            await task
        expected_tone: ModWebNotificationType = (
            "positive" if outcome == "completed" else "info" if outcome == "accepted" else "negative"
        )
        assert notification.type == expected_tone
        assert notification.timeout == notification_policy(expected_tone).timeout_milliseconds / 1000
        assert notification.spinner is False
        notification.update.assert_called_once_with()
        ui.notify.assert_not_called()

    asyncio.run(exercise())
