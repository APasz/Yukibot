from __future__ import annotations

import unittest

from mod_web_theme import (
    DEFAULT_MOD_WEB_THEME,
    MOD_WEB_ACTION_BASE_CLASSES,
    apply_mod_web_theme,
    mod_web_badge_class,
)


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

        self.assertIn("--mod-bg: #050507", css)
        self.assertIn("--mod-purple: #8b5cf6", css)
        self.assertIn("--mod-red: #dc2626", css)
        self.assertIn("--mod-warning: #f59e0b", css)
        self.assertIn(".mod-card", css)
        self.assertIn(".q-notification.bg-warning", css)
        self.assertIn(".q-notification.bg-negative", css)
        self.assertIn(".nicegui-error-popup", css)
        self.assertIn("#popup.nicegui-error-popup", css)
        self.assertIn("#too_long_message_popup.nicegui-error-popup", css)
        self.assertIn(".mod-row", css)
        self.assertIn(".mod-setting-badge-rail", css)
        self.assertIn(".mod-setting-badge", css)
        self.assertIn(".mod-corner-badges", css)
        self.assertIn(".mod-corner-badges-wide", css)
        self.assertIn(".mod-corner-badge-row-fill", css)
        self.assertIn(".mod-app-node-badge", css)
        self.assertIn(".mod-app-node-badge-wrap", css)
        self.assertIn(".mod-card-hero", css)
        self.assertIn(".mod-card-hero::after", css)
        self.assertIn(".mod-hero-app-title-block", css)
        self.assertIn(".mod-app-hero-starting::after", css)
        self.assertIn(".mod-app-hero-running::after", css)
        self.assertIn(".mod-app-card-link::before", css)
        self.assertIn(".mod-app-card-disabled::before", css)
        self.assertIn(".mod-app-card:hover", css)
        self.assertIn(".mod-node-card:hover", css)
        self.assertIn(".mod-card-link:not(.mod-app-card-link):hover", css)
        self.assertIn(".mod-badge-link", css)
        self.assertIn(".mod-app-card-starting::before", css)
        self.assertIn(".mod-app-card-running::before", css)
        self.assertIn(".mod-app-card-stopping::before", css)
        self.assertIn("font-variant-numeric: tabular-nums;", css)
        self.assertIn("mod-app-card-strip-starting", css)
        self.assertIn("mod-app-card-strip-running 2.4s ease-in-out infinite", css)
        self.assertIn("mod-app-card-strip-stopping 780ms linear infinite", css)
        self.assertIn("mod-app-hero-border-starting 1.35s ease-in-out infinite", css)
        self.assertIn("mod-app-hero-border-running 2.4s ease-in-out infinite", css)
        self.assertIn("top: calc(100% - 0.76rem);", css)
        self.assertIn("height: 3px;", css)
        self.assertIn("0 0 / 220% 100% no-repeat", css)
        self.assertIn("background-position: 100% 0;", css)
        self.assertIn("background-position: 0 0, 0 1rem, 0 1.5rem;", css)
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
        self.assertIn(".mod-fake-chat-send-target", css)
        self.assertIn(".mod-toolbar-button-fill", css)
        self.assertIn(".mod-setting-control-surface", css)
        self.assertIn(".mod-setting-field-secondary", css)
        self.assertIn(".mod-setting-menu", css)
        self.assertIn(".mod-notepad-menu", css)
        self.assertIn(".mod-card-notepad .mod-config-select .q-field__control", css)
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
        self.assertIn(".mod-chat-asset-row", css)
        self.assertIn(".mod-chat-entry-list", css)
        self.assertIn(".mod-chat-markup", css)
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
        self.assertIn("linear-gradient(90deg, rgba(139, 92, 246, 0.18), transparent 32%) padding-box", css)
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
        self.assertIn("linear-gradient(135deg, rgba(220, 38, 38, 0.13), transparent 58%)", css)
        self.assertIn("font-size: 0.72rem;", css)
        self.assertIn("filter: drop-shadow(0 12px 24px rgba(220, 38, 38, 0.24));", css)
        self.assertIn("width: min(52rem, calc(100vw - 1.5rem)) !important;", css)
        self.assertIn("border: 1px solid rgba(82, 82, 91, 0.82) !important;", css)
        self.assertIn("image-rendering: pixelated", css)
        self.assertIn(".mod-setting-meta-secret-cycle", css)
        self.assertIn(".mod-setting-meta-secret-cycle-token", css)
        self.assertIn(".mod-setting-meta-secret-revealable", css)
        self.assertIn(".mod-setting-meta-secret-reveal", css)
        self.assertIn(".mod-chat-head-meta", css)
        self.assertIn(".mod-chat-badge-row", css)
        self.assertIn("flex-wrap: nowrap !important", css)
        self.assertIn("--mod-setting-secret-cycle-duration", css)
        self.assertIn("--mod-setting-secret-flicker-duration", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-main", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-shadow-a", css)
        self.assertIn("@keyframes mod-setting-secret-cycle-shadow-b", css)
        self.assertIn("@keyframes mod-setting-secret-shift-a", css)
        self.assertIn("@keyframes mod-setting-secret-shift-b", css)
        self.assertIn("border-radius: 0 !important", css)
        self.assertIn("background: var(--mod-purple-dark)", css)
        self.assertNotIn(".mod-setting-meta-corner", css)
        self.assertNotIn("var(--mod-purple-dark), var(--mod-red-dark)", css)
        self.assertNotIn("<script", css.casefold())

    def test_badge_tone_mapping_keeps_status_palette_stable(self) -> None:
        self.assertEqual(mod_web_badge_class("black"), "mod-badge black")
        self.assertEqual(mod_web_badge_class("purple"), "mod-badge purple")
        self.assertEqual(mod_web_badge_class("red"), "mod-badge red")
        self.assertEqual(mod_web_badge_class("warn"), "mod-badge warn")
        self.assertEqual(mod_web_badge_class("grey"), "mod-badge grey")

    def test_apply_theme_uses_palette_and_head_css(self) -> None:
        ui = _FakeUi()

        apply_mod_web_theme(ui=ui)

        self.assertEqual(
            ui.colors_payload,
            {
                "primary": "#7c1d57",
                "secondary": "#b91c1c",
                "accent": "#8b5cf6",
                "positive": "#6b7280",
                "negative": "#dc2626",
                "info": "#8b5cf6",
                "warning": "#f59e0b",
            },
        )
        self.assertIsNotNone(ui.head_html)
        self.assertIn(".mod-card", str(ui.head_html))

    def test_action_base_class_stays_on_mod_action_system(self) -> None:
        self.assertIn("mod-action", MOD_WEB_ACTION_BASE_CLASSES)


if __name__ == "__main__":
    unittest.main()
