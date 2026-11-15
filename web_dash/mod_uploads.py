"""Component selection for staged 7D2D mod uploads."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import math
import json

from nicegui.elements.checkbox import Checkbox

from _utils import Utilities
from node_api.mod import NodeModUploadPreview
from .nicegui_protocols import ModWebUi


def mod_upload_error_detail(response: str) -> str:
    """Read the API's JSON or plain-text rejection without exposing HTML error pages."""
    try:
        payload: object = json.loads(response)
    except ValueError:
        detail = response.strip()
    else:
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
    if not isinstance(detail, str) or "<" in detail or len(detail) > 500:
        return ""
    return detail


def show_mod_upload_preview(
    *, ui: ModWebUi, preview: NodeModUploadPreview,
    apply: Callable[[tuple[str, ...]], Awaitable[None]], discard: Callable[[], Awaitable[None]],
) -> None:
    checkboxes: dict[str, Checkbox] = {}

    async def install_selected() -> None:
        names = tuple(name for name, checkbox in checkboxes.items() if checkbox.value)
        if not names:
            ui.notify("Select at least one new mod to install.", type="warning")
            return
        install_button.disable()
        cancel_button.disable()
        try:
            await apply(names)
        except Exception as xcp:
            ui.notify(f"Mod installation failed: {xcp}", type="negative")
        else:
            dialog.close()
        finally:
            install_button.enable()
            cancel_button.enable()

    async def cancel() -> None:
        cancel_button.disable()
        try:
            await discard()
        except Exception as xcp:
            ui.notify(f"Could not discard the upload: {xcp}", type="negative")
        finally:
            dialog.close()
            cancel_button.enable()

    with ui.dialog().props("persistent") as dialog:
        with ui.card().classes("mod-card mod-dialog-card"):
            with ui.column().classes("w-full gap-4 p-5"):
                ui.label("Review uploaded mods").classes("text-xl font-black mod-title-small")
                ui.label("Select new mods to install. Existing and protected components will be skipped.").classes("mod-subtitle text-sm")
                minutes = max(0, math.ceil((preview.expires_at - datetime.now(timezone.utc)).total_seconds() / 60))
                ui.label(f"This preview expires in {minutes} minutes.").classes("mod-subtitle text-sm")
                with ui.column().classes("w-full gap-2 max-h-96 overflow-y-auto"):
                    for component in preview.components:
                        with ui.row().classes("w-full items-center justify-between gap-2"):
                            checkbox = ui.checkbox(component.name, value=component.status == "new")
                            if component.status == "new":
                                checkboxes[component.name] = checkbox
                            else:
                                checkbox.disable()
                            detail = Utilities.humanise_bytes(component.size_bytes, separator=" ")
                            if component.status != "new":
                                detail += " · " + ("Protected component" if component.status == "protected" else "Already installed")
                            ui.label(detail).classes("mod-subtitle text-sm")
                with ui.row().classes("w-full justify-end gap-2"):
                    cancel_button = ui.button("Cancel", on_click=cancel).classes("mod-list-button secondary")
                    install_button = ui.button("Install selected", on_click=install_selected).classes("mod-list-button")
                    if not checkboxes:
                        install_button.disable()
    dialog.open()
