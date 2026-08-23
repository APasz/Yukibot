from __future__ import annotations

import unittest

from mod_web_theme import (
    DEFAULT_MOD_WEB_THEME,
    MOD_WEB_ACTION_BASE_CLASSES,
    MOD_WEB_THEME_STYLESHEET,
    apply_mod_web_theme,
    mod_web_badge_class,
)
from mod_web_toasts import MOD_WEB_TOAST_JAVASCRIPT, MOD_WEB_TOAST_VERSION


class _FakeUi:
    def __init__(self) -> None:
        self.colors_payload: dict[str, str] | None = None
        self.head_html: str | None = None

    def colors(self, **kwargs: str) -> None:
        self.colors_payload = dict(kwargs)

    def add_head_html(self, html: str) -> None:
        self.head_html = html


class ModWebThemeTests(unittest.TestCase):
    def test_default_theme_preserves_square_dark_visual_contract(self) -> None:
        css = DEFAULT_MOD_WEB_THEME.css()

        self.assertIn("--mod-bg: #000000", css)
        self.assertIn("--mod-purple: #8b5cf6", css)
        self.assertIn("--mod-accent: #8b5cf6", css)
        self.assertIn("--mod-red: #dc2626", css)
        self.assertIn("--mod-negative: var(--mod-red)", css)
        self.assertIn("--mod-info: #8b5cf6", css)
        self.assertIn("--mod-positive: #6b7280", css)
        self.assertIn("--mod-warning: #f59e0b", css)
        self.assertIn("--mod-warning-text: color-mix", css)
        self.assertNotIn("radial-gradient(circle at 14% -8%", css)
        self.assertIn("--mod-motion-medium: 260ms", css)
        self.assertIn("--mod-motion-tab-accent: 320ms", css)
        self.assertIn("--mod-motion-ease: cubic-bezier(0.22, 1, 0.36, 1)", css)
        self.assertIn(".mod-card", css)
        self.assertIn(".mod-skip-link", css)
        self.assertIn(".mod-skip-link:focus-visible", css)
        self.assertIn(".q-notification.bg-warning", css)
        self.assertIn(".q-notification.bg-negative", css)
        self.assertIn(".q-tooltip,", css)
        self.assertRegex(
            css,
            r"(?s)\.q-tooltip,.*?\.leaflet-tooltip \{.*?max-width: min\(22rem, calc\(100vw - 2rem\)\);.*?"
            r"border-radius: 0 !important;.*?background: #000000 !important;.*?"
            r"font-size: 0\.88rem !important;.*?overflow-wrap: anywhere;",
        )
        self.assertRegex(css, r"(?s)\.q-tooltip \{.*?white-space: pre-line;")
        self.assertIn(".nicegui-error-popup", css)
        self.assertIn("#popup.nicegui-error-popup", css)
        self.assertIn("#too_long_message_popup.nicegui-error-popup", css)
        self.assertIn(".mod-row", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-row-main \{.*?width: 100%;.*?max-width: 100%;.*?overflow: hidden;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-row-title,\s*\.mod-row-file \{.*?text-overflow: ellipsis;.*?white-space: nowrap;",
        )
        self.assertIn(".mod-setting-badge-rail", css)
        self.assertIn(".mod-setting-badge", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-setting-badge-rail \{.*?top: -1px;.*?right: -1px;.*?bottom: -1px;",
        )
        self.assertRegex(css, r"(?s)\.mod-setting-badge \{.*?writing-mode: vertical-rl;.*?transform: none;")
        self.assertIn(".mod-corner-badges", css)
        self.assertIn(".mod-corner-badges-wide", css)
        self.assertIn(".mod-corner-badge-row-fill", css)
        self.assertIn(".mod-app-node-badge", css)
        self.assertIn(".mod-app-node-badge-wrap", css)
        self.assertRegex(css, r"(?s)\.mod-app-corner-badge \{.*?pointer-events: auto;")
        self.assertIn(".mod-card-hero", css)
        self.assertIn(".mod-card-hero::after", css)
        self.assertIn("container-name: mod-app-hero", css)
        self.assertIn("@container mod-app-hero (max-width: 44rem)", css)
        self.assertRegex(
            css,
            r"(?s)@container mod-app-hero .*?\.mod-app-node-badge-wrap \{.*?position: relative;",
        )
        self.assertIn(".mod-hero-app-title-block", css)
        self.assertIn(".mod-section-tabs .q-tab:focus-visible", css)
        self.assertIn(".mod-app-hero-starting::after", css)
        self.assertIn(".mod-app-hero-running::after", css)
        self.assertIn(".mod-app-card-link::before", css)
        self.assertIn(".mod-app-card-disabled::before", css)
        self.assertIn(".mod-app-card-crashed::before", css)
        self.assertIn(".mod-app-card-open-corner", css)
        self.assertIn("clip-path: polygon(100% 0, 100% 100%, 0 0);", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card-tab-link \{.*?position: relative;.*?border-color: var\(--mod-accent\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card-tab-link::after \{.*?background: #050507;.*?"
            r"clip-path: polygon\(100% 0, 100% 100%, 0 0\);",
        )
        self.assertIn(".mod-app-card:hover", css)
        self.assertIn(".mod-app-card:focus-visible", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card \{.*?transition: border-color 150ms ease;.*?\}"
            r".*?\.mod-app-card:hover \{.*?border-color: var\(--mod-border-hot\) !important;\s*\}",
        )
        self.assertIn(".mod-node-card:hover", css)
        self.assertIn(".mod-card-link:not(.mod-app-card-link):hover", css)
        self.assertIn(".mod-card-link:focus-visible", css)
        self.assertIn(".mod-badge-link", css)
        self.assertIn(".mod-badge-avatar", css)
        self.assertRegex(css, r"(?s)\.mod-badge-avatar \{.*?padding: 0 !important;.*?overflow: hidden;")
        self.assertRegex(
            css,
            r"(?s)\.mod-badge-avatar-media > img \{.*?inset: 0;.*?width: 100%;.*?height: 100%;.*?object-fit: cover;",
        )
        self.assertIn(".mod-app-activity-alert", css)
        self.assertIn(".mod-home-section-grid", css)
        self.assertIn(".mod-home-section", css)
        self.assertIn(".mod-home-section-avatar", css)
        self.assertIn("container-name: mod-home-section", css)
        self.assertIn("container-name: mod-app-card", css)
        self.assertIn(".mod-home-app-count-badge", css)
        self.assertIn(".mod-home-node-badge-list", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-home-edge-badge-row \{.*?display: grid !important;.*?"
            r"grid-template-columns: max-content minmax\(0, 1fr\);",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-home-node-badge-list \{.*?column-gap: 0\.5rem;.*?row-gap: 0;",
        )
        self.assertIn(".mod-home-hero-header", css)
        self.assertIn(".mod-home-hero-title", css)
        self.assertIn(
            ".mod-home-hero-actionable:hover:not(:has(.mod-home-node-card:hover))",
            css,
        )
        self.assertIn(
            "html:not(.mod-pointer-navigation) :is(.mod-home-hero-actionable, .mod-home-node-card-actionable):focus-visible",
            css,
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-home-node-grid \{.*?display: grid !important;.*?"
            r"repeat\(auto-fit, minmax\(min\(19rem, 100%\), 1fr\)\);",
        )
        self.assertIn(".mod-stat-section", css)
        self.assertIn(".mod-stat-section-label", css)
        self.assertIn(".mod-stat-tone-purple", css)
        self.assertIn(".mod-stat-tone-red", css)
        self.assertIn(".mod-stat-line:has(.mod-stat-tone-purple) .mod-stat-line-label", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-system-hero-shell \.mod-stat-card \{.*?border-color: #2f2f37 !important;",
        )
        self.assertIn(".mod-system-schedule-field .q-field__native", css)
        self.assertIn(".mod-system-schedule-controls", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-system-schedule-controls \{.*?grid-template-columns: repeat\(6, minmax\(0, 1fr\)\);",
        )
        self.assertRegex(
            css,
            r"(?s)@media \(max-width: 36rem\) \{.*?\.mod-system-schedule-controls \{.*?grid-template-columns: 1fr;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-schedule-field \.q-field__control \{.*?height: 2\.75rem !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-schedule-time input \{.*?color-scheme: dark;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-edge-badge-wrap \{.*?position: absolute !important;.*?inset: -1px auto auto -1px !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-hero-header \{.*?display: grid;.*?grid-template-columns:",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-hero-shell \{.*?gap: 0\.5rem !important;.*?padding-top: 1\.9rem !important;",
        )
        self.assertIn(".mod-system-operational-signals", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-section-tabs-shell \{.*?flex: 0 0 auto;.*?\.mod-section-strip > \.mod-section-tabs-shell \{.*?flex: 1 1 100%;",
        )
        self.assertRegex(css, r"(?s)\.mod-section-layout > \.mod-section-tabs-shell \{.*?width: 100%;")
        self.assertRegex(
            css,
            r"(?s)\.mod-section-chrome \{.*?flex: 0 0 100%;.*?width: 100%;.*?margin-left: 0;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-section-chrome-badge-row \{.*?flex-wrap: wrap;.*?justify-content: flex-start;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-config-select \{.*?flex: 0 1 auto;.*?\.mod-tab-toolbar > \.mod-config-select \{.*?flex: 1 1 26rem;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-config-search \{.*?flex: 0 1 auto;.*?min-width: min\(15rem, 100%\);",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-native-tabs \{.*?display: inline-flex;.*?flex-wrap: wrap;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-hero-shell-visitor \{.*?padding-top: 0\.75rem !important;",
        )
        self.assertIn(".mod-system-native-tab-active", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-system-log-output \{.*?max-height: min\(58dvh, 44rem\);.*?overflow: auto;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-log-selectors \{.*?flex-wrap: wrap;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-log-event \{.*?grid-template-columns: minmax\(4\.75rem, auto\) minmax\(0, 1fr\);",
        )
        self.assertIn(".mod-system-log-event-error", css)
        self.assertIn(".mod-system-log-meta", css)
        self.assertIn(".mod-system-log-context", css)
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-system-log-event-error, \.mod-system-log-event-warn\)::before "
            r"\{.*?top: 0;.*?bottom: 0;.*?width: calc\(3px \+ 0\.85rem \+ 4\.75rem \+ 0\.75rem\);",
        )
        self.assertIn("@container mod-app-hero (max-width: 52rem)", css)
        self.assertIn("@container mod-app-hero (max-width: 34rem)", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-home-section-grid \{.*?repeat\(auto-fit, minmax\(min\(34rem, 100%\), 1fr\)\);",
        )
        self.assertIn("@container mod-app-card (max-width: 38rem)", css)
        self.assertIn(".mod-app-card-starting::before", css)
        self.assertIn(".mod-app-card-starting::after", css)
        self.assertIn(".mod-app-card-running::before", css)
        self.assertIn(".mod-app-card-stopping::before", css)
        self.assertIn(".mod-app-card-stopping::after", css)
        self.assertIn(".mod-section-tabs .q-tab__icon", css)
        self.assertIn("@keyframes mod-page-enter", css)
        self.assertIn("@keyframes mod-live-value-pulse-a", css)
        self.assertIn("@keyframes mod-live-value-pulse-b", css)
        live_value_pulse_css = css.split("@keyframes mod-live-value-pulse-a", maxsplit=1)[1].split(
            ".mod-card", maxsplit=1
        )[0]
        self.assertNotIn("translate:", live_value_pulse_css)
        self.assertIn("@keyframes mod-system-chart-draw", css)
        self.assertIn("@keyframes mod-console-output-pulse", css)
        self.assertIn("@keyframes mod-chat-entry-arrive", css)
        self.assertIn("@keyframes mod-chat-unread-arrive", css)
        self.assertIn("@keyframes mod-control-press", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-section-tabs \.q-tab::after \{.*?right: 0;.*?left: 0;.*?"
            r"linear-gradient\(90deg, var\(--mod-accent\) 0%, var\(--mod-accent-text\) 50%, "
            r"var\(--mod-accent\) 100%\);.*?"
            r"transform: scaleX\(0\);.*?"
            r"transform-origin: center;.*?"
            r"transition: transform var\(--mod-motion-tab-accent\)",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-chart-line-enter \{.*?stroke-dasharray: 1;.*?"
            r"animation: mod-system-chart-draw",
        )
        self.assertRegex(
            css,
            r"(?s)@media \(prefers-reduced-motion: reduce\) \{.*?"
            r"animation-duration: 0\.01ms !important;",
        )
        self.assertIn("font-variant-numeric: tabular-nums;", css)
        self.assertIn("mod-app-card-strip-starting 900ms linear infinite", css)
        self.assertIn("mod-app-card-strip-running 2.4s ease-in-out infinite", css)
        self.assertIn("mod-app-card-strip-stopping 900ms linear infinite", css)
        self.assertIn("mod-app-hero-border-starting 1.35s ease-in-out infinite", css)
        self.assertIn("mod-app-hero-border-running 2.4s ease-in-out infinite", css)
        self.assertIn("height: 3px;", css)
        self.assertIn("0 0 / 220% 100% no-repeat", css)
        self.assertIn("background-position: 100% 0;", css)
        self.assertIn("--mod-app-rail-width: 0.72rem;", css)
        self.assertIn("width: var(--mod-app-rail-width);", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card-crashed::before \{.*?var\(--mod-app-strip-color, var\(--mod-border-hot\)\);",
        )
        self.assertIn("var(--mod-card) 40% 56%", css)
        self.assertIn("transform: translateY(1.25rem);", css)
        self.assertIn(") left top / 1rem 1.25rem repeat-y", css)
        self.assertIn(") right top / 1rem 1.25rem repeat-y", css)
        self.assertIn("transform: translateY(1.25rem);", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card-starting::after \{.*?-45deg,.*?45deg,.*?"
            r"animation: mod-app-card-strip-starting 900ms linear infinite;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-app-card-stopping::after \{.*?45deg,.*?-45deg,.*?"
            r"animation: mod-app-card-strip-stopping 900ms linear infinite;",
        )
        self.assertRegex(
            css,
            r"(?s)@keyframes mod-app-card-strip-starting \{.*?0% \{.*?"
            r"translateY\(1\.25rem\).*?100% \{.*?translateY\(0\)",
        )
        self.assertRegex(
            css,
            r"(?s)@keyframes mod-app-card-strip-stopping \{.*?0% \{.*?"
            r"translateY\(0\).*?100% \{.*?translateY\(1\.25rem\)",
        )
        self.assertNotIn("data:image/svg+xml", css)
        self.assertNotIn(".mod-app-card-stopping.mod-app-card-live::before", css)
        self.assertNotIn(".mod-app-card-starting.mod-app-card-live::before", css)
        self.assertIn("min-height: 3.35rem;", css)
        self.assertIn("font-size: 1.34rem !important;", css)
        self.assertNotIn(".mod-app-card-subtitle", css)
        self.assertNotIn(".mod-app-card:hover, .mod-node-card:hover", css)
        self.assertNotIn("mod-app-card-strip-running 2.4s ease-in-out infinite alternate", css)
        self.assertIn(".mod-app-card-api-pill", css)
        self.assertIn(".mod-action-border-accent", css)
        self.assertIn(".mod-toolbar-chat-button", css)
        self.assertIn(".mod-user-avatar", css)
        self.assertIn("border: 3px solid transparent !important", css)
        self.assertIn(".mod-settings-search", css)
        self.assertIn(".mod-inline-toolbar", css)
        self.assertIn(".mod-inline-toolbar-actions", css)
        self.assertIn(".mod-mods-toolbar-search", css)
        self.assertIn(".mod-mods-toolbar-filters", css)
        self.assertIn(".mod-mods-toolbar-result-count", css)
        self.assertIn(".mod-list-button.mod-toolbar-primary", css)
        self.assertIn(".mod-virtual-mod-table tbody tr.selected > td::after", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-virtual-mod-table \.q-table__middle \{.*?"
            r"overflow-x: hidden;.*?overflow-y: auto;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-virtual-mod-table tbody tr\.selected > td::after,.*?content: none !important;",
        )
        self.assertNotRegex(css, r"(?s)\.mod-row:hover \{[^}]*border-color:")
        self.assertNotIn(".mod-row-disabled:hover", css)
        self.assertNotIn(".mod-row-client-only:hover", css)
        self.assertNotIn("mod-list-item-enter", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-mods-toolbar-actions \.mod-toolbar-selection-action \{.*?"
            r"min-width: 12rem;.*?width: 0;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-mods-toolbar-actions \.mod-toolbar-selection-utility \{.*?"
            r"min-width: 8\.25rem;.*?width: 8\.25rem;",
        )
        self.assertIn(".mod-row .mod-row-selection-checkbox", css)
        self.assertRegex(css, r"(?s)\.mod-row \{.*?border: 1px solid #25252c !important;")
        self.assertRegex(css, r"(?s)\.mod-card\.mod-mod-list-card \{.*?border: none !important;")
        self.assertRegex(
            css,
            r"(?s)\.mod-mods-toolbar-actions \.mod-toolbar-menu-button \{.*?"
            r"width: 2\.5rem;.*?min-width: 2\.5rem;",
        )
        self.assertRegex(
            css,
            r"(?s)@media \(max-width: 720px\).*?\.mod-mods-toolbar-filters \{.*?"
            r"display: flex !important;.*?\.mod-mods-toolbar-actions \{.*?flex-wrap: wrap;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-badge\.warn \{.*?background: var\(--mod-warning-dark\).*?"
            r"border-color: var\(--mod-warning\).*?color: var\(--mod-warning-text\)",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-system-schedule-field \.q-field__control::after \{.*?"
            r"border-bottom: 2px solid var\(--mod-accent\)",
        )
        self.assertIn(".mod-toolbar-menu-mobile-only", css)
        self.assertRegex(
            css,
            r"(?s)@media \(max-width: 30rem\).*?\.mod-mods-toolbar-filters \{.*?"
            r"display: grid !important;",
        )
        self.assertIn(".mod-toolbar-menu-item-danger", css)
        self.assertIn(".mod-modlist-dialog-card", css)
        self.assertIn(".mod-save-upload-panel", css)
        self.assertIn(".mod-file-upload-zone", css)
        self.assertIn(".mod-save-upload-zone", css)
        self.assertIn(".mod-save-upload-target-static", css)
        self.assertIn(".mod-save-upload-target-button", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-save-upload-target-static \{.*?color: var\(--mod-text\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-file-upload-zone, \.mod-save-upload-zone\) \.q-uploader__header \{.*?color: #fff !important;",
        )
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-file-upload-zone, \.mod-save-upload-zone\) \.q-uploader__list \{.*?color: var\(--mod-text\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-file-upload-zone, \.mod-save-upload-zone\) \.q-uploader__list:empty \{.*?display: none !important;",
        )
        self.assertNotIn(".mod-dialog-card::before", css)
        self.assertNotIn("@keyframes mod-dialog-accent-arrive", css)
        self.assertIn(".mod-dialog-card:focus-within", css)
        self.assertIn("overscroll-behavior: contain", css)
        self.assertIn("100dvh", css)
        self.assertIn("safe-area-inset-bottom", css)
        self.assertIn("-webkit-overflow-scrolling: touch", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(".mod-mod-details-dialog-card", css)
        self.assertIn(".mod-mod-details-header", css)
        self.assertIn(".mod-mod-details-footer", css)
        self.assertIn(".mod-mod-details-danger-zone", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-details-shell \{.*?max-height:.*?overflow-y: auto;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-details-header \{.*?position: sticky;.*?top: 0;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-details-footer \{.*?position: sticky;.*?bottom: 0;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-details-links \{.*?width: calc\(100% - 2\.5rem\) !important;.*?box-sizing: border-box;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-app-details-field\.q-field--labeled \.q-field__native,.*?"
            r"\.mod-app-details-field \.q-field--labeled \.q-field__input \{.*?"
            r"min-height: 3\.35rem;.*?padding-top: 1\.35rem !important;.*?"
            r"padding-bottom: 0\.25rem !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-app-details-field:is\(\.mod-config-select, \.mod-mod-details-select\) "
            r"\.q-field__control \{.*?"
            r"height: 3\.35rem !important;.*?min-height: 3\.35rem !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-app-details-field:is\(\.mod-config-select, \.mod-mod-details-select\) "
            r"\.q-field__native \{.*?"
            r"height: auto !important;.*?min-height: 0 !important;.*?"
            r"padding-top: 0\.875rem !important;.*?padding-bottom: 0\.125rem !important;",
        )
        self.assertIn(".mod-modlist-preview-frame", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-modlist-preview \{.*?max-height: min\(24rem, 50vh\);.*?"
            r"color: var\(--mod-text\) !important;.*?white-space: pre;",
        )
        self.assertRegex(css, r"(?s)\* \{.*?scrollbar-color: var\(--mod-scrollbar-thumb\) transparent;")
        self.assertIn("*::-webkit-scrollbar-thumb", css)
        self.assertIn("*::-webkit-scrollbar-corner", css)
        self.assertIn(":is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__native", css)
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-config-input, \.mod-config-select\) \.q-field__native,.*?"
            r"-webkit-text-fill-color: var\(--mod-text\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-settings-search \.q-field__label,.*?color: var\(--mod-text\) !important;"
            r".*?\.mod-settings-search \.q-field__native::placeholder,.*?"
            r"-webkit-text-fill-color: var\(--mod-text\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-config-input, \.mod-config-select\) input:-webkit-autofill,.*?"
            r"-webkit-text-fill-color: var\(--mod-text\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s):is\(\.mod-config-input, \.mod-config-select\) \.q-field__native::placeholder,.*?"
            r"-webkit-text-fill-color: var\(--mod-muted\) !important;",
        )
        self.assertIn(".mod-hero-support", css)
        self.assertIn(".mod-hero-actions", css)
        self.assertIn(".mod-status-card", css)
        self.assertIn(".mod-status-shell", css)
        self.assertIn(".mod-status-figure", css)
        self.assertIn(".mod-status-figure-svg", css)
        self.assertIn(".mod-status-context", css)
        self.assertIn(".mod-status-detail", css)
        self.assertIn(".mod-status-detail-label", css)
        self.assertIn(".mod-status-detail-text", css)
        self.assertIn(".mod-status-actions", css)
        self.assertIn(".mod-fake-chat-dialog-card", css)
        self.assertIn(".mod-fake-chat-field", css)
        self.assertIn(".mod-fake-chat-menu", css)
        self.assertIn(".mod-fake-chat-footer", css)
        self.assertIn(".mod-app-details-dialog-card", css)
        self.assertIn(".mod-timestamp-picker-dialog-card", css)
        self.assertIn(".mod-timestamp-picker-workspace", css)
        self.assertIn(".mod-metadata-review-card", css)
        self.assertIn(".mod-metadata-review-provider-title", css)
        self.assertIn(".mod-metadata-review-link", css)
        self.assertIn(".mod-bulk-metadata-dialog-card", css)
        self.assertIn(".mod-bulk-metadata-table", css)
        self.assertIn(".mod-bulk-metadata-selection-checkbox", css)
        self.assertIn(".mod-bulk-metadata-type-checkbox", css)
        self.assertIn(".mod-app-details-section", css)
        self.assertIn(".mod-app-details-field", css)
        self.assertIn(".mod-user-appearance-grid", css)
        self.assertIn("repeat(auto-fit, minmax(min(17rem, 100%), 1fr))", css)
        self.assertIn(".mod-user-accent-input .q-field__control", css)
        self.assertNotIn(".mod-user-accent-swatch", css)
        self.assertIn(".mod-page-editor-controls", css)
        self.assertIn(".mod-page-url-invalid", css)
        self.assertIn(".mod-mod-page-link", css)
        self.assertIn(".mod-details-tab-row", css)
        self.assertIn(".mod-details-tab-button", css)
        self.assertIn(".mod-list-button.secondary.mod-details-tab-active", css)
        self.assertIn(".mod-mod-override-field", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-override-field input::placeholder \{.*?opacity: 1 !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-mod-override-datetime input\[type=datetime-local\] \{.*?color-scheme: dark;",
        )
        self.assertIn(
            ".mod-mod-override-datetime input[type=datetime-local]::-webkit-calendar-picker-indicator",
            css,
        )
        self.assertIn(".mod-app-details-notes", css)
        self.assertIn(".mod-app-details-toggle", css)
        self.assertIn(".mod-app-properties-card", css)
        self.assertIn(".mod-client-pack-dialog-card", css)
        self.assertRegex(css, r"(?s)\.mod-section-layout \{.*?gap: 0\.35rem;")
        self.assertIn("scrollbar-color:", css)
        self.assertIn(".mod-client-pack-checkbox .q-checkbox__inner--truthy", css)
        self.assertIn(".mod-client-pack-select", css)
        self.assertIn(".mod-client-pack-select .q-field__native", css)
        self.assertIn(".mod-client-pack-config-layout", css)
        self.assertIn(".mod-client-pack-config-column", css)
        self.assertIn(".mod-client-pack-changelog textarea", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-client-pack-release-section \{.*?gap: 0\.65rem;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-client-pack-changelog-block \{.*?gap: 0\.4rem;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-client-pack-changelog-hint \{.*?margin-top: 0;",
        )
        self.assertIn(".mod-client-pack-publish-reasons", css)
        self.assertIn(".mod-client-pack-publish-reason", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-client-pack-changelog textarea.*?color: var\(--mod-text\) !important;",
        )
        self.assertNotIn("padding-top: 1.8rem !important;", css)
        self.assertIn(".mod-client-pack-changelog-content", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-client-pack-changelog-content \{.*?white-space: pre-wrap;",
        )
        self.assertIn(".mod-client-pack-config-search", css)
        self.assertIn(".mod-client-pack-config-option", css)
        self.assertIn(".mod-client-pack-config-control", css)
        self.assertIn(".mod-client-pack-config-group", css)
        self.assertIn(".mod-client-pack-config-policy", css)
        self.assertIn(".mod-client-pack-config-invalid .q-field__control", css)
        self.assertIn(".mod-app-details-state-button", css)
        self.assertIn(".mod-toolbar-button-fill", css)
        self.assertIn(".mod-toolbar-status-button", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-toolbar-status-button \.q-btn__content \{.*?color: var\(--mod-accent-text-strong\) !important;",
        )
        self.assertIn(".mod-setting-control-surface", css)
        self.assertIn(".mod-setting-field-secondary", css)
        self.assertIn(".mod-settings-group-divider", css)
        self.assertIn(".mod-setting-menu", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-setting-menu \.q-item\.mod-app-installer-policy-option \{.*?"
            r"color: var\(--mod-text\) !important;",
        )
        self.assertIn(".mod-notepad-menu", css)
        self.assertIn(".mod-card-notepad .mod-config-select .q-field__control", css)
        self.assertIn(".mod-recipe-field .q-field__control", css)
        self.assertIn(".mod-recipe-field .q-field__native", css)
        self.assertIn(".mod-recipe-editor-shell", css)
        self.assertIn(".mod-recipe-browser-shell", css)
        self.assertIn(".mod-recipe-subtabs", css)
        self.assertIn(".mod-recipe-subtab-panels", css)
        self.assertIn("height: auto !important", css)
        self.assertIn(".mod-recipe-workbench", css)
        self.assertIn(".mod-recipe-slot-grid", css)
        self.assertIn(".mod-recipe-slot", css)
        self.assertIn(".mod-recipe-slot-drop-active", css)
        self.assertIn(".mod-recipe-slot-selected", css)
        self.assertIn(".mod-recipe-icon-shell", css)
        self.assertIn(".mod-recipe-icon-stack", css)
        self.assertIn(".mod-recipe-icon-image", css)
        self.assertIn(".mod-recipe-browser-grid", css)
        self.assertIn(".mod-recipe-browser-filter", css)
        self.assertIn(".mod-recipe-browser-card", css)
        self.assertIn(".mod-recipe-browser-card-row", css)
        self.assertIn(".mod-recipe-subtabs > .mod-section-tabs-shell", css)
        self.assertIn("min-height: 3.75rem", css)
        self.assertIn("right: -1px", css)
        self.assertIn("width: 3.75rem", css)
        self.assertIn(".mod-recipe-manage-card", css)
        self.assertIn(".mod-recipe-entry", css)
        self.assertIn(".mod-recipe-operation-add", css)
        self.assertIn(".mod-recipe-operation-remove", css)
        self.assertIn(".mod-setting-meta-secret-main", css)
        self.assertIn(".mod-chat-source-badge", css)
        self.assertIn(".mod-chat-author-row", css)
        self.assertIn(".mod-chat-author-avatar", css)
        self.assertIn(".mod-chat-head-meta", css)
        self.assertIn(".mod-chat-badge-row", css)
        self.assertIn(".mod-chat-header-main", css)
        self.assertIn(".mod-chat-shell", css)
        self.assertIn(".mod-chat-shell-card", css)
        self.assertIn(".mod-chat-shell-header", css)
        self.assertIn(".mod-chat-shell-header-main", css)
        self.assertIn(".mod-chat-shell-header .mod-corner-badges", css)
        self.assertIn(".mod-chat-shell-header .mod-corner-badge-row", css)
        self.assertIn(".mod-chat-panel-embedded", css)
        self.assertIn(".mod-chat-subtitle", css)
        self.assertIn(".mod-chat-status-row", css)
        self.assertIn(".mod-chat-section-label", css)
        self.assertIn(".mod-chat-timeline-shell", css)
        self.assertIn(".mod-chat-unread-bar", css)
        self.assertIn(".mod-chat-media-grid", css)
        self.assertIn(".mod-chat-media-image", css)
        self.assertIn(".mod-chat-media-video", css)
        self.assertIn(".mod-chat-media-audio", css)
        self.assertRegex(css, r"(?s)\.mod-chat-media-card \{.*?flex: 0 1 auto;.*?width: fit-content;")
        self.assertRegex(css, r"(?s)\.mod-chat-media-link \{.*?display: inline-flex;.*?width: fit-content;")
        self.assertRegex(css, r"(?s)\.mod-chat-media-link \{.*?cursor: pointer;")
        self.assertRegex(css, r"(?s)\.mod-chat-media-image,\s*\.mod-chat-media-video \{.*?width: auto;")
        self.assertIn(".mod-chat-asset-row", css)
        self.assertIn(".mod-chat-entry-list", css)
        self.assertIn(".mod-chat-markup", css)
        self.assertIn(".mod-chat-markup-heading", css)
        self.assertIn(".mod-chat-markup-subtext", css)
        self.assertIn(".mod-chat-markup-list", css)
        self.assertIn(".mod-chat-markup a", css)
        self.assertIn(".mod-chat-inline-code", css)
        self.assertIn(".mod-chat-code-block", css)
        self.assertIn(".mod-chat-quote", css)
        self.assertIn(".mod-chat-spoiler", css)
        self.assertIn("overflow-wrap: anywhere;", css)
        self.assertIn("word-break: break-word;", css)
        self.assertIn(".mod-chat-entry-meta", css)
        self.assertIn(".mod-chat-entry-time", css)
        self.assertIn(".mod-chat-entry-menu", css)
        self.assertIn(".mod-chat-entry-menu-item", css)
        self.assertIn(".mod-timezone-options", css)
        self.assertIn(".mod-timezone-menu", css)
        self.assertIn(".mod-timezone-option .q-btn__content", css)
        self.assertIn(".mod-timezone-option-summary", css)
        self.assertIn(".mod-timezone-option-code", css)
        self.assertIn(".mod-timezone-option-location", css)
        self.assertIn(".mod-timestamp-mode-panels", css)
        self.assertIn(".mod-timestamp-dialog-card", css)
        self.assertIn(".mod-timestamp-input", css)
        self.assertIn(".mod-timestamp-format-option", css)
        self.assertIn(".mod-timestamp-format-pattern", css)
        self.assertIn(".mod-unit-conversion-option", css)
        self.assertRegex(css, r"(?s)\.mod-unit-conversion-system \{.*?text-align: right;")
        self.assertIn(".mod-unit-conversion-filter-heading:focus-within", css)
        self.assertIn(".mod-unit-conversion-amount-heading", css)
        self.assertIn(".mod-unit-conversion-source", css)
        self.assertIn(".mod-chat-reference", css)
        self.assertIn(".mod-chat-reference-label", css)
        self.assertIn(".mod-chat-reply-banner", css)
        self.assertIn(".mod-chat-composer-surface", css)
        self.assertIn(".mod-chat-send-stack", css)
        self.assertIn(".mod-chat-send-subtext", css)
        self.assertIn(".mod-chat-message::before", css)
        self.assertIn(".mod-chat-message:hover", css)
        self.assertIn(".mod-chat-message:first-child::before", css)
        self.assertIn("rgba(113, 113, 122, 0.08) 48%", css)
        self.assertIn(
            "linear-gradient(90deg, var(--mod-hero-border-glow, rgba(244, 244, 245, 0.05)), transparent 32%) padding-box",
            css,
        )
        self.assertIn("var(--mod-hero-border-fade, var(--mod-border)) 100%", css)
        self.assertIn("scroll-behavior: auto", css)
        self.assertIn("position: relative;", css)
        self.assertIn("--mod-chat-panel-inline-padding: 1rem;", css)
        self.assertIn("--mod-chat-shell-inline-padding: clamp(0.5rem, 2vw, 1.25rem);", css)
        self.assertIn(
            "width: calc(100% + ((var(--mod-chat-panel-inline-padding) + var(--mod-chat-shell-inline-padding)) * 2));",
            css,
        )
        self.assertIn(
            "margin-inline: calc((var(--mod-chat-panel-inline-padding) + var(--mod-chat-shell-inline-padding)) * -1);",
            css,
        )
        self.assertIn("padding: 0.58rem 0 0.62rem;", css)
        self.assertIn("border-top: 1px solid rgba(63, 63, 70, 0.74);", css)
        self.assertIn("border-left: 0;", css)
        self.assertIn("padding: 0.5rem 0.62rem 0.48rem", css)
        self.assertIn("padding: 0.08rem 0 0.18rem 0;", css)
        self.assertIn("max-height: min(60vh, 40rem)", css)
        self.assertIn("width: min(92rem, calc(100vw - 1.5rem))", css)
        self.assertIn("min-height: clamp(36rem, 78vh, 58rem)", css)
        self.assertIn("scrollbar-gutter: stable", css)
        self.assertIn("overscroll-behavior: contain", css)
        self.assertIn("position: absolute;", css)
        self.assertIn("min-height: 2rem;", css)
        self.assertIn("border-radius: 0;", css)
        self.assertIn("cursor: pointer;", css)
        self.assertIn("backdrop-filter: blur(12px);", css)
        self.assertIn("linear-gradient(135deg, var(--mod-negative-glow), transparent 58%)", css)
        self.assertIn("font-size: 0.72rem;", css)
        self.assertIn("filter: drop-shadow(0 12px 24px var(--mod-negative-glow));", css)
        self.assertIn("width: min(52rem, calc(100vw - 1.5rem)) !important;", css)
        self.assertIn("width: min(44rem, calc(100vw - 1.5rem)) !important;", css)
        self.assertIn("min-height: 11.5rem !important;", css)
        self.assertIn("min-height: 9.5rem !important;", css)
        self.assertIn("max-width: 15rem !important;", css)
        self.assertIn("border: 1px solid rgba(82, 82, 91, 0.82) !important;", css)
        self.assertIn("image-rendering: pixelated", css)
        self.assertIn(".mod-setting-meta-secret-cycle", css)
        self.assertIn(".mod-setting-meta-secret-cycle-token", css)
        self.assertIn(".mod-setting-meta-secret-revealable", css)
        self.assertIn(".mod-setting-meta-secret-reveal", css)
        self.assertIn(".mod-setting-meta-secret-reveal-token", css)
        self.assertIn(".mod-chat-head-meta", css)
        self.assertIn(".mod-chat-badge-row", css)
        self.assertIn("flex-wrap: nowrap !important", css)
        self.assertIn(".mod-transfer-overlay", css)
        self.assertRegex(
            css,
            r"(?s)\.mod-transfer-overlay-tracks \{.*?display: flex;.*?flex-direction: column;.*?width: 100%;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-transfer-overlay-track \{.*?position: relative;.*?width: 100%;.*?overflow: hidden;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-user-plate-actions \{.*?display: grid !important;.*?"
            r"grid-template-columns: repeat\(.*?auto-fill,.*?"
            r"minmax\(.*?var\(--mod-user-plate-action-height\),.*?"
            r"calc\(var\(--mod-user-plate-action-height\) \* 2\).*?\).*?\);.*?gap: 0\.5rem;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-user-plate-actions \{.*?--mod-user-plate-action-height: 2\.2rem;"
            r".*?\.mod-user-header-icon-button \{.*?min-width: var\(--mod-user-plate-action-height\) !important;"
            r".*?min-height: var\(--mod-user-plate-action-height\) !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-user-plate-actions > \.mod-user-header-icon-button \{.*?width: 100%;"
            r".*?max-width: calc\(var\(--mod-user-plate-action-height\) \* 2\);",
        )
        self.assertIn(".mod-user-utility-menu-item .q-item__section--avatar", css)
        self.assertIn("padding-left: 2.45rem", css)
        self.assertIn("transform: translateY(-50%)", css)
        self.assertIn(".mod-user-plate-menu .q-item[aria-selected=\"true\"]", css)
        self.assertIn(".mod-user-plate-option-content", css)
        self.assertIn("--mod-setting-secret-cycle-duration", css)
        self.assertIn("--mod-setting-secret-flicker-duration", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-main", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-shadow-a", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-shadow-b", css)
        self.assertIn("@keyframes mod-setting-secret-shift-a", css)
        self.assertIn("@keyframes mod-setting-secret-shift-b", css)
        self.assertIn("border-radius: 0 !important", css)
        self.assertIn("background: var(--mod-accent-dark)", css)
        self.assertNotIn(".mod-setting-meta-corner", css)
        self.assertNotIn("var(--mod-purple-dark), var(--mod-red-dark)", css)
        self.assertNotIn("<script", css.casefold())

    def test_badge_tone_mapping_keeps_status_palette_stable(self) -> None:
        self.assertEqual(mod_web_badge_class("black"), "mod-badge black")
        self.assertEqual(mod_web_badge_class("purple"), "mod-badge purple")
        self.assertEqual(mod_web_badge_class("red"), "mod-badge red")
        self.assertEqual(mod_web_badge_class("warn"), "mod-badge warn")
        self.assertEqual(mod_web_badge_class("grey"), "mod-badge grey")

    def test_factorio_generator_styles_preserve_compact_contrast_and_responsive_tables(self) -> None:
        css = DEFAULT_MOD_WEB_THEME.stylesheet()

        self.assertIn(".mod-factorio-generator", css)
        self.assertIn(".mod-factorio-titlebar", css)
        self.assertRegex(css, r"(?s)\.mod-factorio-generator \{.*?width: 100% !important;.*?max-width: none !important;")
        self.assertIn(".mod-factorio-header-actions", css)
        self.assertIn(".mod-factorio-tabs-shell", css)
        self.assertIn("overflow-x: auto;", css)
        self.assertRegex(css, r"(?s)\.mod-factorio-panel \{.*?width: 100% !important;.*?max-width: none !important;")
        self.assertRegex(
            css,
            r"(?s)\.mod-factorio-control-table,\s*\.mod-factorio-option-group \{.*?display: block !important;.*?"
            r"width: 100% !important;.*?max-width: none !important;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-factorio-advanced-grid \{.*?width: 100%;.*?grid-template-columns: minmax\(0, 1fr\);",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-factorio-control-table-cols-2 :is\(\.mod-factorio-control-header, "
            r"\.mod-factorio-control-row\) \{.*?grid-template-columns: minmax\(12rem, 1\.2fr\) repeat\(2, minmax\(10rem, 1fr\)\);",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-factorio-range-value \{.*?width: 6rem;.*?min-width: 6rem;",
        )
        self.assertRegex(
            css,
            r"(?s)\.mod-factorio-advanced-top-grid \{.*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);",
        )
        self.assertIn(".mod-factorio-control-row:has(.mod-factorio-control-enabled", css)
        self.assertIn("pointer-events: none;", css)
        self.assertIn(".mod-factorio-slider {", css)
        self.assertIn("min-width: 2.75rem;", css)
        self.assertIn(".mod-factorio-map-string-input textarea", css)
        self.assertIn("min-height: 4.75rem !important;", css)
        self.assertIn("resize: vertical !important;", css)
        self.assertIn(".mod-factorio-save", css)
        self.assertIn(".mod-factorio-running-world", css)
        self.assertNotIn(".mod-factorio-footer", css)
        self.assertNotIn(".mod-factorio-notice", css)
        self.assertIn("color: #251604 !important;", css)
        self.assertIn("background: #d97706 !important;", css)
        self.assertIn(".mod-factorio-generator input[type=\"number\"]", css)
        self.assertIn("::-webkit-inner-spin-button", css)
        self.assertIn("::-webkit-outer-spin-button", css)
        self.assertIn("-webkit-text-fill-color: #f4f4f5 !important;", css)

    def test_accent_chrome_uses_derived_accent_variables(self) -> None:
        stylesheet = MOD_WEB_THEME_STYLESHEET

        self.assertIn("--mod-accent-dark: color-mix", stylesheet)
        self.assertIn("--mod-accent-text: color-mix", stylesheet)
        self.assertIn("--mod-accent-border: color-mix", stylesheet)
        self.assertIn(".text-primary,\n                .text-accent", stylesheet)
        self.assertIn(".text-negative", stylesheet)
        self.assertIn(".q-btn.bg-primary,\n                .q-btn.bg-accent", stylesheet)
        self.assertIn(".q-btn.bg-negative", stylesheet)
        self.assertIn(".q-btn.mod-list-button.secondary", stylesheet)
        self.assertIn("var(--mod-accent-dark) !important;", stylesheet)
        self.assertIn(
            ".mod-badge.purple { background: var(--mod-accent-dark)",
            stylesheet,
        )
        self.assertIn("background: var(--mod-accent-dark) !important;", stylesheet)
        for forbidden_literal in (
            "rgba(139, 92, 246",
            "rgba(124, 58, 237",
            "rgba(196, 181, 253",
            "rgba(221, 214, 254",
            "rgba(237, 233, 254",
            "rgba(167, 139, 250",
            "rgba(216, 180, 254",
            "var(--mod-purple)",
            "var(--mod-purple-dark)",
        ):
            with self.subTest(forbidden_literal=forbidden_literal):
                self.assertNotIn(forbidden_literal, stylesheet)

    def test_interactive_control_content_uses_a_contrast_foreground(self) -> None:
        stylesheet = MOD_WEB_THEME_STYLESHEET

        self.assertIn("--mod-control-foreground: #ffffff;", stylesheet)
        self.assertRegex(
            stylesheet,
            r"(?s)\.mod-factorio-save \{.*?--mod-control-foreground: #251604;.*?"
            r"background: #d97706 !important;",
        )
        self.assertRegex(
            stylesheet,
            r"(?s):is\(\.q-btn, \.q-tab, \.mod-system-native-tab\) \{.*?"
            r"color: var\(--mod-control-foreground\) !important;",
        )
        self.assertIn(
            ":is(.q-btn, .q-tab) :is(.q-btn__content, .q-tab__content),",
            stylesheet,
        )

    def test_apply_theme_uses_palette_and_head_css(self) -> None:
        ui = _FakeUi()

        apply_mod_web_theme(ui=ui)

        self.assertEqual(
            ui.colors_payload,
            {
                "primary": "#8b5cf6",
                "secondary": "#52525b",
                "accent": "#8b5cf6",
                "positive": "#6b7280",
                "negative": "#dc2626",
                "info": "#8b5cf6",
                "warning": "#f59e0b",
            },
        )
        self.assertIsNotNone(ui.head_html)
        self.assertIn('/mod-web/assets/theme.css?v=', str(ui.head_html))
        self.assertIn(f'/mod-web/assets/toasts.js?v={MOD_WEB_TOAST_VERSION}', str(ui.head_html))
        self.assertIn("content-visibility: auto", MOD_WEB_THEME_STYLESHEET)

    def test_toast_client_queues_timers_and_pauses_on_hover(self) -> None:
        self.assertIn("const activeRecord", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("remainingMilliseconds", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("const attachProgress", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("const startProgressAnimation", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("window.cancelAnimationFrame", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("mod-toast-progress", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("document.addEventListener('mouseover'", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("document.addEventListener('mouseout'", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn("timeout: 0", MOD_WEB_TOAST_JAVASCRIPT)
        self.assertIn(".q-notification .mod-toast-progress", MOD_WEB_THEME_STYLESHEET)
        self.assertIn("--mod-toast-progress-scale", MOD_WEB_THEME_STYLESHEET)

    def test_action_base_class_stays_on_mod_action_system(self) -> None:
        self.assertIn("mod-action", MOD_WEB_ACTION_BASE_CLASSES)


if __name__ == "__main__":
    unittest.main()
