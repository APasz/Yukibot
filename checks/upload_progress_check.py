from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from nicegui.elements.upload import Upload
from nicegui.props import Props

from web_dash.backend import ModWebDashboardBackend
from web_dash.nicegui_protocols import ModWebUi
from web_dash.status_notifications import ModWebStatusNotificationsMixin
from web_dash.types import ModWebNotificationTrayItemKind, ModWebNotificationTrayItemState, _ModWebNotificationTrayItem
from web_dash.upload_progress import bind_direct_upload_progress


def test_direct_upload_reports_bytes_then_processing_and_ignores_finished_batches() -> None:
    backend = ModWebDashboardBackend()
    active_id: int | None = backend.start_upload_transfers(
        user_id=42, filenames=("mod.zip",), detail_text="Sending mod.zip.",
    )[0]
    assert backend.user_transfer_items(user_id=42)[0].progress_percent == 0
    upload = Mock(spec=Upload)
    bind_direct_upload_progress(
        ui=cast(ModWebUi, Mock(spec=ModWebUi)),
        upload=cast(Upload, upload), backend=backend, transfer_id=lambda: active_id,
        processing_text="Preparing mods…",
    )
    update = upload.on.call_args.args[1]
    update(SimpleNamespace(args=43))
    sending = backend.user_transfer_items(user_id=42)[0]
    assert sending.progress_percent == 43
    assert sending.detail_text == "Sending mod.zip."

    update(SimpleNamespace(args=None))
    processing = backend.user_transfer_items(user_id=42)[0]
    assert processing.progress_percent is None
    assert processing.detail_text == "Preparing mods…"
    assert processing.state is ModWebNotificationTrayItemState.ACTIVE

    backend.complete_transfer(transfer_id=active_id)
    update(SimpleNamespace(args=99))
    assert backend.user_transfer_items(user_id=42)[0].progress_percent == 100
    active_id = None
    update(SimpleNamespace(args=None))
    assert backend.user_transfer_items(user_id=42)[0].state is ModWebNotificationTrayItemState.SUCCESS
    backend.clear_user_transfers(user_id=42)


@pytest.mark.parametrize("progress", [-1, 101, True, "50", float("nan"), float("inf"), {}, []])
def test_direct_upload_rejects_invalid_progress(progress: object) -> None:
    upload = Mock(spec=Upload)
    bind_direct_upload_progress(
        ui=cast(ModWebUi, Mock(spec=ModWebUi)),
        upload=cast(Upload, upload), backend=ModWebDashboardBackend(), transfer_id=lambda: None,
        processing_text="Processing…",
    )
    with pytest.raises(ValueError, match="percentage or null"):
        upload.on.call_args.args[1](SimpleNamespace(args=progress))


def test_progress_accessibility_labels_preserve_unicode_and_quoted_names() -> None:
    item = _ModWebNotificationTrayItem(
        kind=ModWebNotificationTrayItemKind.UPLOAD,
        state=ModWebNotificationTrayItemState.ACTIVE,
        label='💾 "mod".zip',
        detail_text='Préparation des mods 🧟…',
    )
    props = Props.parse(ModWebStatusNotificationsMixin._transfer_overlay_track_props(item))
    assert props["aria-label"] == item.label
    assert props["aria-valuetext"] == item.detail_text
    assert "aria-valuenow" not in props
