from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import MagicMock, Mock, patch

import pytest
from nicegui.props import Props

from mod_web_auth import ModWebUser

from web_dash.backend import ModWebDashboardBackend, _TRANSFER_DISPLAY_SECONDS
from web_dash.nicegui_protocols import ModWebUi
from web_dash.service import ModWebService
from web_dash.status_notifications import ModWebStatusNotificationsMixin
from web_dash.types import ModWebNotificationTrayItemKind, ModWebNotificationTrayItemState


@pytest.mark.parametrize("first_finished", ["upload", "download"])
@pytest.mark.parametrize("failed", [False, True])
def test_concurrent_transfer_statuses_and_track_order_remain_independent(
    first_finished: Literal["upload", "download"], failed: bool,
) -> None:
    backend = ModWebDashboardBackend()
    upload_id = backend.start_upload_transfers(user_id=42, filenames=("shared.zip",), detail_text="Sending mods…")[0]
    download_id = backend.start_download_transfers(user_id=42, filenames=("shared.zip",), detail_text="Preparing archive…")[0]
    assert backend.claim_download_transfer(transfer_id=download_id, user_id=42)
    try:
        backend.update_transfer_progress(transfer_id=upload_id, progress_percent=38)
        backend.update_transfer_progress(transfer_id=download_id, progress_percent=62)
        original_order = tuple(item.kind for item in backend.user_transfer_items(user_id=42))
        detail = ModWebStatusNotificationsMixin._transfer_overlay_detail(backend.user_transfer_items(user_id=42))
        assert detail == "Download · shared.zip · 62%\nUpload · shared.zip · 38%"

        finished_id = upload_id if first_finished == "upload" else download_id
        if failed:
            backend.fail_transfer(transfer_id=finished_id, detail_text="Connection interrupted.")
        else:
            backend.complete_transfer(transfer_id=finished_id)
        items = backend.user_transfer_items(user_id=42)
        assert tuple(item.kind for item in items) == original_order
        detail = ModWebStatusNotificationsMixin._transfer_overlay_detail(items)
        assert detail is not None and len(detail.splitlines()) == 2
        if first_finished == "upload":
            assert "Download · shared.zip · 62%" in detail
            assert "Upload · shared.zip · " + ("Failed: Connection interrupted." if failed else "Complete.") in detail
        else:
            assert "Upload · shared.zip · 38%" in detail
            assert "Download · shared.zip · " + ("Failed: Connection interrupted." if failed else "Transfer sent.") in detail
        assert backend.user_active_transfer_slots(user_id=42) == 1
        assert sum(item.state is ModWebNotificationTrayItemState.ACTIVE for item in items) == 1
        for item in items:
            props = Props.parse(ModWebStatusNotificationsMixin._transfer_overlay_track_props(item))
            assert props["aria-label"] == f"{item.kind.value.capitalize()} · shared.zip"
            if item.state is not ModWebNotificationTrayItemState.ACTIVE:
                assert props["aria-valuetext"] == ModWebStatusNotificationsMixin._transfer_overlay_status(item)

        if failed:
            backend.clear_user_transfers(user_id=42, failed_only=True)
        else:
            backend._expire_transfer(transfer_id=finished_id)
        remaining = backend.user_transfer_items(user_id=42)
        assert len(remaining) == 1
        assert remaining[0].state is ModWebNotificationTrayItemState.ACTIVE
        assert remaining[0].kind is (
            ModWebNotificationTrayItemKind.DOWNLOAD if first_finished == "upload" else ModWebNotificationTrayItemKind.UPLOAD
        )
    finally:
        backend.clear_user_transfers(user_id=42)


def test_processing_and_preview_status_stay_visible_alongside_download_progress() -> None:
    backend = ModWebDashboardBackend()
    upload_id = backend.start_upload_transfers(user_id=42, filenames=("mods.zip",), detail_text="Preparing mods…")[0]
    download_id = backend.start_download_transfers(user_id=42, filenames=("world.zip",), detail_text="Preparing archive…")[0]
    try:
        backend.update_transfer_progress(transfer_id=download_id, progress_percent=47)
        for status, blink in (("Preparing mods…", True), ("Select mods to install.", False), ("Installing mods…", True)):
            backend.update_transfer_progress(transfer_id=upload_id, progress_percent=None, detail_text=status, blink=blink)
            items = backend.user_transfer_items(user_id=42)
            assert ModWebStatusNotificationsMixin._transfer_overlay_detail(items) == (
                f"Download · world.zip · 47%\nUpload · mods.zip · {status}"
            )
            upload = next(item for item in items if item.kind is ModWebNotificationTrayItemKind.UPLOAD)
            assert ("mod-transfer-overlay-track-indeterminate" in ModWebStatusNotificationsMixin._transfer_overlay_track_classes(upload)) is blink
    finally:
        backend.clear_user_transfers(user_id=42)


def test_compact_long_names_preserve_full_accessibility_labels_and_file_extensions() -> None:
    backend = ModWebDashboardBackend()
    filename = "VeryLong_mod_name_" * 12 + "ファイル🧟.zip"
    transfer_id = backend.start_download_transfers(user_id=42, filenames=(filename,), detail_text="Preparing archive…")[0]
    try:
        backend.update_transfer_progress(transfer_id=transfer_id, progress_percent=62)
        item = backend.user_transfer_items(user_id=42)[0]
        detail = ModWebStatusNotificationsMixin._transfer_overlay_detail((item,))
        assert detail is not None and len(detail) < len(filename)
        assert detail.startswith("Download · VeryLong_mod_name_")
        assert "…" in detail and detail.endswith("ファイル🧟.zip · 62%")
        assert Props.parse(ModWebStatusNotificationsMixin._transfer_overlay_track_props(item))["aria-label"] == f"Download · {filename}"
    finally:
        backend.clear_user_transfers(user_id=42)


def test_failed_transfers_persist_without_reserving_slots_and_dismissal_is_scoped() -> None:
    backend = ModWebDashboardBackend()
    failed_id, _active_id, complete_id = backend.start_upload_transfers(
        user_id=42, filenames=("failed.zip", "active.zip", "complete.zip"), detail_text="Sending…",
    )
    other_failed_id = backend.start_upload_transfers(user_id=43, filenames=("other.zip",), detail_text="Sending…")[0]
    try:
        backend.fail_transfer(transfer_id=failed_id, detail_text="Connection lost.")
        backend.fail_transfer(transfer_id=other_failed_id, detail_text="Connection lost.")
        backend.complete_transfer(transfer_id=complete_id)
        assert backend.user_active_transfer_slots(user_id=42) == 1
        backend.update_transfer_progress(transfer_id=failed_id, progress_percent=50)
        backend._expire_transfer(transfer_id=failed_id)
        assert len(backend.user_transfer_items(user_id=42)) == 3
        after_expiry = time.monotonic() + _TRANSFER_DISPLAY_SECONDS + 1
        with patch("web_dash.backend.time.monotonic", return_value=after_expiry):
            remaining = backend.user_transfer_items(user_id=42)
            assert {item.label for item in remaining} == {"failed.zip", "active.zip"}
            assert next(item for item in remaining if item.label == "failed.zip").progress_percent == 100
            backend.clear_user_transfers(user_id=42, failed_only=True)
            assert backend.user_transfer_items(user_id=42)[0].label == "active.zip"
            assert backend.user_transfer_items(user_id=43)[0].label == "other.zip"
        assert backend.user_active_transfer_slots(user_id=42) == 1
    finally:
        backend.clear_user_transfers(user_id=42)
        backend.clear_user_transfers(user_id=43)


def test_failed_transfer_dismiss_control_preserves_active_transfers() -> None:
    service = ModWebService()
    user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
    failed_id, _active_id = service._backend.start_upload_transfers(
        user_id=42, filenames=("failed.zip", "active.zip"), detail_text="Sending…",
    )
    service._backend.fail_transfer(transfer_id=failed_id, detail_text="Connection lost.")
    ui = SimpleNamespace(element=MagicMock(), label=MagicMock(), button=Mock(), teleport=MagicMock())
    try:
        service._render_user_transfer_overlay(ui=cast(ModWebUi, ui), user=user)
        assert ui.button.call_args.args == ("Dismiss failed transfers",)
        ui.button.call_args.kwargs["on_click"]()
        remaining = service._backend.user_transfer_items(user_id=42)
        assert len(remaining) == 1 and remaining[0].label == "active.zip"
    finally:
        service._backend.clear_user_transfers(user_id=42)


@pytest.mark.parametrize("failed_first", [False, True])
def test_transfer_results_cannot_be_overwritten_by_late_callbacks(failed_first: bool) -> None:
    backend = ModWebDashboardBackend()
    transfer_id = backend.start_upload_transfers(user_id=42, filenames=("mod.zip",), detail_text="Sending…")[0]
    try:
        if failed_first:
            backend.fail_transfer(transfer_id=transfer_id, detail_text="Original failure.")
        else:
            backend.complete_transfer(transfer_id=transfer_id, detail_text="Original success.")
        original = backend.user_transfer_items(user_id=42)
        backend.complete_transfer(transfer_id=transfer_id, detail_text="Late completion.")
        backend.fail_transfer(transfer_id=transfer_id, detail_text="Late failure.")
        assert backend.user_transfer_items(user_id=42) == original
    finally:
        backend.clear_user_transfers(user_id=42)
