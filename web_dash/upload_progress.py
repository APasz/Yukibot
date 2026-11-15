"""Browser byte progress for uploads sent directly to a game node."""

from __future__ import annotations

from collections.abc import Callable

from nicegui.elements.upload import Upload

from .backend import ModWebDashboardBackend
from .nicegui_protocols import ModWebEventArgumentsContainer, ModWebUi


_DIRECT_UPLOAD_LOADING_BAR_FILTER = r"""
    window.Quasar.LoadingBar.setDefaults({
        hijackFilter: url => !/\/apps\/[^/]+\/(?:mods\/upload(?:\/inspect)?|saves\/upload(?:-inferred)?)$/.test(
            new URL(url, window.location.href).pathname
        ),
    });
"""

_DIRECT_UPLOAD_PROGRESS_HANDLER = r"""({xhr}) => {
    let lastPercent = -1;
    let lastSentAt = -Infinity;
    xhr.upload.addEventListener('progress', event => {
        if (!event.lengthComputable || event.total <= 0) return;
        const percent = Math.floor(Math.min(100, 100 * event.loaded / event.total));
        const now = performance.now();
        if (percent === lastPercent || (percent < 100 && now - lastSentAt < 200)) return;
        lastPercent = percent;
        lastSentAt = now;
        emit(percent);
    });
    // Sending all bytes is distinct from the node finishing its work.
    xhr.upload.addEventListener('load', () => emit(null), {once: true});
}"""


def bind_direct_upload_progress(
    *,
    ui: ModWebUi,
    upload: Upload,
    backend: ModWebDashboardBackend,
    transfer_id: Callable[[], int | None],
    processing_text: str,
) -> None:
    """Report byte percentages, then indeterminate processing, for the active batch."""

    # Quasar applies defaults reactively, so configure the filter before the first request's loadstart.
    ui.run_javascript(_DIRECT_UPLOAD_LOADING_BAR_FILTER)

    def update(event: ModWebEventArgumentsContainer) -> None:
        progress: object = event.args
        if progress is not None and (
            isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 100
        ):
            raise ValueError("Upload progress must be a percentage or null while processing.")
        active_transfer_id: int | None = transfer_id()
        if active_transfer_id is None:
            return
        backend.update_transfer_progress(
            transfer_id=active_transfer_id,
            progress_percent=None if progress is None else float(progress),
            detail_text=processing_text if progress is None else None,
        )

    upload.on("uploading", update, js_handler=_DIRECT_UPLOAD_PROGRESS_HANDLER)
