from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from mod_web_toasts import MOD_WEB_TOAST_VERSION

BadgeTone = Literal["black", "purple", "red", "warn", "grey"]


def mod_web_tooltip_css() -> str:
    return """
                .q-tooltip,
                .leaflet-tooltip {
                    max-width: min(22rem, calc(100vw - 2rem));
                    padding: 0.4rem 0.55rem !important;
                    border: 1px solid var(--mod-accent-border-strong) !important;
                    border-radius: 0 !important;
                    color: var(--mod-text) !important;
                    background: #000000 !important;
                    box-shadow:
                        0 14px 30px rgba(0, 0, 0, 0.5),
                        inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
                    font-size: 0.88rem !important;
                    font-weight: 650;
                    line-height: 1.4;
                    letter-spacing: 0.01em;
                    overflow-wrap: anywhere;
                    backdrop-filter: blur(10px);
                }
                .q-tooltip .q-icon {
                    color: inherit !important;
                }
                .q-tooltip {
                    white-space: pre-line;
                }
                .leaflet-tooltip {
                    white-space: normal;
                }
                .leaflet-tooltip-top::before { border-top-color: rgba(10, 10, 14, 0.98); }
                .leaflet-tooltip-bottom::before { border-bottom-color: rgba(10, 10, 14, 0.98); }
                .leaflet-tooltip-left::before { border-left-color: rgba(10, 10, 14, 0.98); }
                .leaflet-tooltip-right::before { border-right-color: rgba(10, 10, 14, 0.98); }
            """


@dataclass(frozen=True, slots=True)
class NiceGuiPalette:
    primary: str
    secondary: str
    accent: str
    positive: str
    negative: str
    info: str
    warning: str


@dataclass(frozen=True, slots=True)
class ModWebPalette:
    background: str
    card: str
    card_raised: str
    border: str
    border_hot: str
    text: str
    muted: str
    dim: str
    purple: str
    purple_dark: str
    red: str
    red_dark: str
    warning: str
    warning_dark: str
    warning_text: str
    panel: str
    nicegui: NiceGuiPalette


@dataclass(frozen=True, slots=True)
class ModWebTheme:
    name: str
    palette: ModWebPalette

    def root_variables_css(self) -> str:
        palette = self.palette
        return f"""
                :root {{
                    --mod-bg: {palette.background};
                    --mod-card: {palette.card};
                    --mod-card-2: {palette.card_raised};
                    --mod-border: {palette.border};
                    --mod-border-hot: {palette.border_hot};
                    --mod-text: {palette.text};
                    --mod-muted: {palette.muted};
                    --mod-dim: {palette.dim};
                    --mod-purple: {palette.purple};
                    --mod-purple-dark: {palette.purple_dark};
                    --mod-accent: {palette.purple};
                    --mod-accent-dark: color-mix(in srgb, var(--mod-accent) 38%, #050507);
                    --mod-accent-surface: color-mix(in srgb, var(--mod-accent) 22%, #050507);
                    --mod-accent-panel: color-mix(in srgb, var(--mod-accent) 16%, #111118);
                    --mod-accent-text: color-mix(in srgb, var(--mod-accent) 36%, #ffffff);
                    --mod-accent-text-strong: color-mix(in srgb, var(--mod-accent) 18%, #ffffff);
                    --mod-accent-border: color-mix(in srgb, var(--mod-accent) 58%, transparent);
                    --mod-accent-border-strong: color-mix(in srgb, var(--mod-accent) 74%, transparent);
                    --mod-accent-glow: color-mix(in srgb, var(--mod-accent) 24%, transparent);
                    --mod-accent-faint: color-mix(in srgb, var(--mod-accent) 12%, transparent);
                    --mod-accent-wash: color-mix(in srgb, var(--mod-accent) 8%, transparent);
                    --mod-info: {palette.nicegui.info};
                    --mod-info-dark: color-mix(in srgb, var(--mod-info) 38%, #050507);
                    --mod-info-surface: color-mix(in srgb, var(--mod-info) 22%, #050507);
                    --mod-info-text: color-mix(in srgb, var(--mod-info) 22%, #ffffff);
                    --mod-info-border: color-mix(in srgb, var(--mod-info) 58%, transparent);
                    --mod-info-border-strong: color-mix(in srgb, var(--mod-info) 74%, transparent);
                    --mod-info-glow: color-mix(in srgb, var(--mod-info) 24%, transparent);
                    --mod-positive: {palette.nicegui.positive};
                    --mod-positive-dark: color-mix(in srgb, var(--mod-positive) 38%, #050507);
                    --mod-positive-surface: color-mix(in srgb, var(--mod-positive) 22%, #050507);
                    --mod-positive-text: color-mix(in srgb, var(--mod-positive) 22%, #ffffff);
                    --mod-positive-border: color-mix(in srgb, var(--mod-positive) 58%, transparent);
                    --mod-positive-border-strong: color-mix(in srgb, var(--mod-positive) 74%, transparent);
                    --mod-positive-glow: color-mix(in srgb, var(--mod-positive) 24%, transparent);
                    --mod-red: {palette.red};
                    --mod-negative: var(--mod-red);
                    --mod-red-dark: color-mix(in srgb, var(--mod-negative) 42%, #050507);
                    --mod-negative-dark: var(--mod-red-dark);
                    --mod-negative-surface: color-mix(in srgb, var(--mod-negative) 22%, #050507);
                    --mod-negative-text: color-mix(in srgb, var(--mod-negative) 22%, #ffffff);
                    --mod-negative-border: color-mix(in srgb, var(--mod-negative) 58%, transparent);
                    --mod-negative-border-strong: color-mix(in srgb, var(--mod-negative) 74%, transparent);
                    --mod-negative-glow: color-mix(in srgb, var(--mod-negative) 24%, transparent);
                    --mod-warning: {palette.warning};
                    --mod-warning-dark: color-mix(in srgb, var(--mod-warning) 30%, #050507);
                    --mod-warning-surface: color-mix(in srgb, var(--mod-warning) 18%, #050507);
                    --mod-warning-text: color-mix(in srgb, var(--mod-warning) 28%, #ffffff);
                    --mod-warning-border: color-mix(in srgb, var(--mod-warning) 58%, transparent);
                    --mod-warning-border-strong: color-mix(in srgb, var(--mod-warning) 74%, transparent);
                    --mod-warning-glow: color-mix(in srgb, var(--mod-warning) 24%, transparent);
                    --mod-panel: {palette.panel};
                    --mod-control-foreground: #ffffff;
                    --mod-scrollbar-thumb: rgba(161, 161, 170, 0.34);
                    --mod-scrollbar-thumb-hover: rgba(161, 161, 170, 0.52);
                    --mod-motion-fast: 120ms;
                    --mod-motion-medium: 260ms;
                    --mod-motion-tab-accent: 320ms;
                    --mod-motion-slow: 420ms;
                    --mod-motion-ease: cubic-bezier(0.22, 1, 0.36, 1);
                }}"""

    def css(self) -> str:
        return f"""
            <style>
{self.root_variables_css()}
                html,
                body,
                body.body--light,
                #app,
                .q-layout,
                .q-page-container,
                .q-page {{
                    color: var(--mod-text);
                    background: var(--mod-bg) !important;
                }}
                html {{
                    overflow-y: scroll;
                    scrollbar-gutter: stable;
                }}
                * {{
                    scrollbar-color: var(--mod-scrollbar-thumb) transparent;
                    scrollbar-width: thin;
                }}
                *::-webkit-scrollbar {{
                    width: 0.5rem;
                    height: 0.5rem;
                }}
                *::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                *::-webkit-scrollbar-thumb {{
                    border: 1px solid transparent;
                    border-radius: 999px;
                    background: var(--mod-scrollbar-thumb);
                    background-clip: padding-box;
                }}
                *::-webkit-scrollbar-thumb:hover {{
                    background: var(--mod-scrollbar-thumb-hover);
                    background-clip: padding-box;
                }}
                *::-webkit-scrollbar-corner {{
                    background: transparent;
                }}
                .q-dialog__backdrop {{
                    background: rgba(2, 2, 4, 0.78) !important;
                    backdrop-filter: blur(7px);
                }}
                .q-dialog__inner {{
                    background: transparent !important;
                }}
                .q-notification {{
                    position: relative !important;
                    overflow: hidden !important;
                    border-radius: 0 !important;
                    border: 1px solid rgba(82, 82, 91, 0.88) !important;
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        rgba(10, 10, 14, 0.96) !important;
                    box-shadow:
                        0 20px 44px rgba(0, 0, 0, 0.42),
                        inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    color: var(--mod-text) !important;
                    backdrop-filter: blur(10px);
                }}
                .q-notification__message,
                .q-notification__caption,
                .q-notification .q-icon {{
                    color: inherit !important;
                }}
{mod_web_tooltip_css()}
                .q-notification .mod-toast-progress {{
                    --mod-toast-progress-scale: 1;
                    position: absolute;
                    inset: 0 auto 0 0;
                    width: 4px;
                    pointer-events: none;
                    transform: scaleY(var(--mod-toast-progress-scale));
                    transform-origin: bottom;
                    background: currentColor;
                    opacity: 0.78;
                }}
                .q-notification.bg-positive,
                .q-notification--standard.bg-positive {{
                    border-color: var(--mod-positive-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-positive-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-positive-dark) !important;
                }}
                .q-notification.bg-info,
                .q-notification--standard.bg-info {{
                    border-color: var(--mod-info-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-info-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-info-dark) !important;
                }}
                .q-notification.bg-warning,
                .q-notification--standard.bg-warning {{
                    border-color: var(--mod-warning-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-warning-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-warning-dark) !important;
                }}
                .q-notification.bg-negative,
                .q-notification--standard.bg-negative {{
                    border-color: var(--mod-negative-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-negative-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-negative-dark) !important;
                }}
                .nicegui-error-popup {{
                    color: var(--mod-text) !important;
                    border-radius: 0 !important;
                    border: 1px solid rgba(82, 82, 91, 0.88) !important;
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        rgba(10, 10, 14, 0.96) !important;
                    box-shadow:
                        0 20px 44px rgba(0, 0, 0, 0.42),
                        inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    backdrop-filter: blur(10px);
                }}
                .nicegui-error-popup > span:last-child {{
                    color: var(--mod-muted) !important;
                }}
                #popup.nicegui-error-popup {{
                    border-color: var(--mod-warning-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-warning-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-warning-dark) !important;
                }}
                #too_long_message_popup.nicegui-error-popup {{
                    border-color: var(--mod-negative-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-negative-glow), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-negative-dark) !important;
                }}
                .mod-page {{
                    max-width: 1180px;
                    margin: 0 auto;
                    animation: mod-page-enter var(--mod-motion-medium) var(--mod-motion-ease) both;
                }}
                .mod-page-app {{ max-width: 1380px; }}
                .text-primary,
                .text-accent {{
                    color: var(--mod-accent) !important;
                }}
                .text-info {{
                    color: var(--mod-info) !important;
                }}
                .text-positive {{
                    color: var(--mod-positive) !important;
                }}
                .text-warning {{
                    color: var(--mod-warning) !important;
                }}
                .text-negative {{
                    color: var(--mod-negative) !important;
                }}
                .bg-primary,
                .bg-accent {{
                    background: var(--mod-accent) !important;
                }}
                .bg-info {{
                    background: var(--mod-info) !important;
                }}
                .bg-positive {{
                    background: var(--mod-positive) !important;
                }}
                .bg-warning {{
                    background: var(--mod-warning) !important;
                }}
                .bg-negative {{
                    background: var(--mod-negative) !important;
                }}
                .border-primary,
                .border-accent {{
                    border-color: var(--mod-accent) !important;
                }}
                .border-info {{
                    border-color: var(--mod-info) !important;
                }}
                .border-positive {{
                    border-color: var(--mod-positive) !important;
                }}
                .border-warning {{
                    border-color: var(--mod-warning) !important;
                }}
                .border-negative {{
                    border-color: var(--mod-negative) !important;
                }}
                .q-btn.bg-primary,
                .q-btn.bg-accent {{
                    border: 1px solid var(--mod-accent-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-glow), transparent),
                        var(--mod-accent-dark) !important;
                    color: var(--mod-accent-text-strong) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.06),
                        inset 0 -1px 0 var(--mod-accent-glow) !important;
                }}
                .q-btn.bg-primary:hover,
                .q-btn.bg-accent:hover {{
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-border), var(--mod-accent-faint)),
                        var(--mod-accent-surface) !important;
                    color: #ffffff !important;
                }}
                .q-btn.bg-info {{
                    border: 1px solid var(--mod-info-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-info-glow), transparent),
                        var(--mod-info-dark) !important;
                    color: var(--mod-info-text) !important;
                }}
                .q-btn.bg-positive {{
                    border: 1px solid var(--mod-positive-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-positive-glow), transparent),
                        var(--mod-positive-dark) !important;
                    color: var(--mod-positive-text) !important;
                }}
                .q-btn.bg-warning {{
                    border: 1px solid var(--mod-warning-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-warning-glow), transparent),
                        var(--mod-warning-dark) !important;
                    color: var(--mod-warning-text) !important;
                }}
                .q-btn.bg-negative {{
                    border: 1px solid var(--mod-negative-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-negative-glow), transparent),
                        var(--mod-negative-dark) !important;
                    color: var(--mod-negative-text) !important;
                }}
                .mod-skip-link {{
                    position: fixed;
                    top: 0.75rem;
                    left: 0.75rem;
                    z-index: 2147483000;
                    padding: 0.6rem 0.85rem;
                    border: 1px solid var(--mod-accent-border-strong);
                    color: #ffffff !important;
                    background: var(--mod-accent-surface);
                    box-shadow: 0 12px 26px rgba(0, 0, 0, 0.36);
                    text-decoration: none !important;
                    transform: translateY(-160%);
                    transition: transform 140ms ease;
                }}
                .mod-skip-link:focus-visible {{
                    transform: translateY(0);
                    outline: 2px solid var(--mod-accent);
                    outline-offset: 3px;
                }}
                .mod-card-hero .mod-title {{
                    animation: mod-hero-title-enter var(--mod-motion-slow) var(--mod-motion-ease) 40ms both;
                }}
                .mod-card-hero .mod-app-node-badge-wrap,
                .mod-card-hero .mod-corner-badges,
                .mod-card-hero .mod-home-capability-badges {{
                    animation: mod-badge-rail-enter var(--mod-motion-medium) var(--mod-motion-ease) 110ms both;
                }}
                .mod-home-node-card,
                .mod-home-section,
                .mod-stat-card {{
                    animation: mod-card-enter var(--mod-motion-medium) var(--mod-motion-ease) both;
                }}
                .mod-home-node-grid > :nth-child(2),
                .mod-home-section-grid > :nth-child(2),
                .mod-stat-grid > :nth-child(2) {{ animation-delay: 45ms; }}
                .mod-home-node-grid > :nth-child(3),
                .mod-home-section-grid > :nth-child(3),
                .mod-stat-grid > :nth-child(3) {{ animation-delay: 90ms; }}
                .mod-home-node-grid > :nth-child(4),
                .mod-home-section-grid > :nth-child(4),
                .mod-stat-grid > :nth-child(4) {{ animation-delay: 135ms; }}
                .mod-live-value-pulse-a {{
                    animation: mod-live-value-pulse-a 520ms var(--mod-motion-ease);
                }}
                .mod-live-value-pulse-b {{
                    animation: mod-live-value-pulse-b 520ms var(--mod-motion-ease);
                }}
                @keyframes mod-page-enter {{
                    from {{ opacity: 0; translate: 0 0.4rem; }}
                    to {{ opacity: 1; translate: 0 0; }}
                }}
                @keyframes mod-hero-title-enter {{
                    from {{ opacity: 0; translate: 0 0.38rem; }}
                    to {{ opacity: 1; translate: 0 0; }}
                }}
                @keyframes mod-badge-rail-enter {{
                    from {{ opacity: 0; translate: 0 0.28rem; }}
                    to {{ opacity: 1; translate: 0 0; }}
                }}
                @keyframes mod-card-enter {{
                    from {{ opacity: 0; translate: 0 0.34rem; }}
                    to {{ opacity: 1; translate: 0 0; }}
                }}
                @keyframes mod-live-value-pulse-a {{
                    0% {{ color: #ffffff; filter: brightness(1.75); translate: 0 1px; }}
                    45% {{ text-shadow: 0 0 0.7rem var(--mod-accent-border); }}
                    100% {{ filter: brightness(1); translate: 0 0; text-shadow: none; }}
                }}
                @keyframes mod-live-value-pulse-b {{
                    0% {{ color: #ffffff; filter: brightness(1.75); translate: 0 1px; }}
                    45% {{ text-shadow: 0 0 0.7rem var(--mod-accent-border); }}
                    100% {{ filter: brightness(1); translate: 0 0; text-shadow: none; }}
                }}
                .mod-card {{
                    border-radius: 0 !important;
                    background: linear-gradient(135deg, rgba(9, 9, 13, 0.98), rgba(15, 15, 21, 0.98)) !important;
                    border: 1px solid var(--mod-border) !important;
                    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.48), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    color: var(--mod-text) !important;
                }}
                .mod-card-plain {{
                    background: transparent !important;
                    border-color: transparent !important;
                    border-width: 0 !important;
                    box-shadow: none !important;
                }}
                .mod-card-hero {{
                    border: 3px solid transparent !important;
                    background:
                        linear-gradient(90deg, var(--mod-hero-border-glow, rgba(244, 244, 245, 0.05)), transparent 32%) padding-box,
                        linear-gradient(135deg, rgba(9, 9, 13, 0.98), rgba(15, 15, 21, 0.98)) padding-box,
                        linear-gradient(
                            180deg,
                            var(--mod-hero-border, var(--mod-border-hot)) 0%,
                            var(--mod-hero-border-fade, var(--mod-border)) 100%
                        ) border-box !important;
                    position: relative;
                    container-name: mod-app-hero;
                    container-type: inline-size;
                    overflow: hidden;
                    isolation: isolate;
                }}
                .mod-card-hero::after {{
                    content: "";
                    position: absolute;
                    top: 0;
                    left: 0;
                    right: 0;
                    height: 3px;
                    pointer-events: none;
                    opacity: 0;
                    background:
                        linear-gradient(
                            90deg,
                            transparent 0%,
                            transparent 24%,
                            var(--mod-hero-border, var(--mod-border-hot)) 50%,
                            transparent 76%,
                            transparent 100%
                        ) 0 0 / 220% 100% no-repeat;
                }}
                .mod-app-hero-starting::after {{
                    opacity: 0.88;
                    animation: mod-app-hero-border-starting 1.35s ease-in-out infinite;
                    will-change: background-position, opacity, filter;
                }}
                .mod-app-hero-running::after {{
                    opacity: 0.62;
                    animation: mod-app-hero-border-running 2.4s ease-in-out infinite;
                    will-change: background-position, opacity, filter;
                }}
                .mod-hero-shell {{
                    width: 100%;
                    position: relative;
                }}
                .mod-hero-header {{
                    width: 100%;
                    flex-wrap: nowrap !important;
                }}
                .mod-hero-header-main {{
                    flex: 1 1 0;
                    min-width: 0;
                }}
                .mod-card-hero .mod-hero-app-title-block {{
                    padding-top: 2.05rem;
                    padding-left: 0.45rem;
                }}
                .mod-card-hero .mod-title {{
                    line-height: 0.92 !important;
                    text-wrap: balance;
                }}
                .mod-app-hero-status {{
                    min-width: 9.5rem;
                    align-items: flex-end;
                    text-align: right;
                }}
                .mod-app-hero-status-value {{
                    font-size: clamp(1.1rem, 2vw, 1.45rem);
                    font-weight: 900;
                    line-height: 1;
                    letter-spacing: 0.01em;
                }}
                .mod-app-hero-status-value-grey {{ color: rgba(228, 228, 231, 0.76) !important; }}
                .mod-app-hero-status-value-purple {{ color: var(--mod-accent-text) !important; }}
                .mod-app-hero-status-value-warn {{ color: #fbbf24 !important; }}
                .mod-app-hero-status-value-red {{ color: #f87171 !important; }}
                .mod-app-hero-join-addresses {{
                    max-width: min(32rem, 100%);
                    margin-top: 0.2rem;
                    align-items: flex-end;
                    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
                    overflow-wrap: anywhere;
                }}
                .mod-app-hero-join-address {{
                    color: rgba(244, 244, 245, 0.92) !important;
                    font-size: 0.9rem;
                    font-weight: 700;
                    line-height: 1.25;
                }}
                .mod-app-hero-join-address-direct {{
                    color: rgba(228, 228, 231, 0.58) !important;
                    font-size: 0.74rem;
                    font-weight: 600;
                    line-height: 1.3;
                }}
                .mod-hero-support {{
                    max-width: min(42rem, 100%);
                    line-height: 1.35 !important;
                    text-wrap: pretty;
                }}
                .mod-hero-actions {{
                    display: flex;
                    gap: 0.65rem;
                    flex-wrap: wrap;
                    align-items: center;
                }}
                .mod-user-avatar {{
                    width: 2.15rem;
                    height: 2.15rem;
                    min-width: 2.15rem;
                    display: block;
                    border: 1px solid rgba(113, 113, 122, 0.52);
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0)),
                        rgba(15, 15, 18, 0.96);
                    object-fit: cover;
                    box-shadow:
                        inset 0 0 0 1px rgba(5, 5, 7, 0.72),
                        0 10px 24px rgba(0, 0, 0, 0.28);
                }}
                .mod-status-card {{
                    overflow: hidden;
                }}
                .mod-status-shell {{
                    gap: 1rem;
                }}
                .mod-status-top {{
                    align-items: flex-start;
                }}
                .mod-status-content {{
                    flex: 1 1 28rem;
                    min-width: 0;
                }}
                .mod-status-figure {{
                    display: flex;
                    justify-content: flex-start;
                    margin: 0;
                    padding: 0;
                    line-height: 0;
                }}
                .mod-status-figure-inline {{
                    flex: 0 0 auto;
                    width: auto;
                    min-width: 0;
                    justify-content: flex-end;
                }}
                .mod-status-figure-svg {{
                    display: block;
                    width: clamp(4.75rem, 8vw, 6.5rem);
                    height: auto;
                    color: var(--mod-negative-text);
                    filter: drop-shadow(0 12px 24px var(--mod-negative-glow));
                }}
                .mod-status-header-main {{
                    gap: 0.7rem;
                }}
                .mod-status-kicker {{
                    display: flex;
                    align-items: center;
                    gap: 0.65rem;
                    flex-wrap: wrap;
                }}
                .mod-status-context {{
                    color: rgba(244, 244, 245, 0.78) !important;
                    font-size: 0.78rem;
                    font-weight: 700;
                    letter-spacing: 0.16em;
                    text-transform: uppercase;
                }}
                .mod-status-detail {{
                    width: 100%;
                    gap: 0.45rem;
                    padding: 0.95rem 1rem;
                    background:
                        linear-gradient(135deg, var(--mod-negative-glow), transparent 58%),
                        rgba(9, 9, 13, 0.82);
                    border: 1px solid var(--mod-negative-border);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
                }}
                .mod-status-detail-label {{
                    color: var(--mod-negative-text) !important;
                    font-size: 0.72rem;
                    font-weight: 800;
                    letter-spacing: 0.16em;
                    text-transform: uppercase;
                }}
                .mod-status-detail-text {{
                    color: var(--mod-text) !important;
                    line-height: 1.45 !important;
                    text-wrap: pretty;
                }}
                .mod-status-actions {{
                    display: flex;
                    align-items: center;
                    gap: 0.65rem;
                    flex-wrap: wrap;
                }}
                .mod-status-card .mod-hero-support {{
                    max-width: min(48rem, 100%);
                }}
                .mod-corner-badges {{
                    display: flex;
                    flex: 0 1 auto;
                    flex-direction: column;
                    align-items: flex-end;
                    gap: 0.45rem;
                    margin-left: auto;
                    min-width: 0;
                    max-width: min(100%, 30rem);
                }}
                .mod-corner-badges-wide {{
                    flex: 1 1 34rem;
                    min-width: min(100%, 26rem);
                    max-width: none;
                }}
                .mod-corner-badge-row {{
                    display: flex;
                    flex-wrap: wrap;
                    justify-content: flex-end;
                    gap: 0.5rem;
                    width: 100%;
                }}
                .mod-corner-badge-row-fill {{
                    justify-content: flex-start;
                    align-items: flex-start;
                }}
                .mod-app-node-badge-wrap {{
                    position: absolute;
                    top: 0;
                    left: 0;
                    z-index: 2;
                    pointer-events: none;
                }}
                .mod-app-node-badge-row {{
                    display: flex;
                    align-items: flex-start;
                    gap: 0.5rem;
                }}
                .mod-app-corner-badge {{
                    margin: 0 !important;
                    padding: 0.4rem 0.72rem !important;
                    border-top: 0 !important;
                    pointer-events: auto;
                }}
                .mod-app-node-badge {{
                    border-left: 0 !important;
                }}
                @container mod-app-hero (max-width: 44rem) {{
                    .mod-app-node-badge-wrap {{
                        position: relative;
                        top: auto;
                        left: auto;
                        width: 100%;
                        max-width: 100%;
                        flex: 0 0 auto;
                    }}
                    .mod-app-node-badge-row {{
                        width: 100%;
                        flex-wrap: wrap;
                        gap: 0.35rem;
                    }}
                    .mod-app-corner-badge {{
                        padding: 0.34rem 0.58rem !important;
                        font-size: 0.64rem !important;
                    }}
                    .mod-card-hero .mod-hero-app-title-block {{
                        padding-top: 0;
                    }}
                }}
                .mod-app-card {{
                    --mod-app-rail-width: 0.72rem;
                    --mod-app-chevron-width: 2rem;
                    transition: border-color 150ms ease;
                    container-name: mod-app-card;
                    container-type: inline-size;
                }}
                .mod-app-card:hover {{
                    border-color: var(--mod-border-hot) !important;
                }}
                .mod-app-card:focus-visible {{
                    border-color: var(--mod-accent) !important;
                    outline: 2px solid var(--mod-accent) !important;
                    outline-offset: 3px;
                }}
                .mod-node-card:hover {{
                    transform: translateY(-1px);
                    border-color: var(--mod-border-hot) !important;
                    background: #101014 !important;
                }}
                .mod-app-card-shell {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr) max-content;
                    width: 100%;
                    min-width: 0;
                    min-height: 3.35rem;
                    align-items: center !important;
                }}
                .mod-app-card-main {{
                    display: grid !important;
                    flex: 1 1 16rem;
                    min-width: 0;
                    align-content: center;
                    overflow: hidden;
                }}
                .mod-app-card-actions {{
                    display: flex !important;
                    flex: 0 0 auto;
                    flex-wrap: nowrap !important;
                    min-width: 0;
                    align-items: center !important;
                }}
                .mod-app-card-badges {{
                    display: flex !important;
                    flex-wrap: nowrap !important;
                    min-width: 0;
                    align-items: center !important;
                }}
                .mod-app-card > .nicegui-content {{
                    padding: 0 !important;
                    width: 100%;
                }}
                .mod-app-card .mod-app-card-shell {{
                    gap: 0.7rem !important;
                    padding: 0.26rem 0.78rem 0.26rem calc(var(--mod-app-rail-width) + 0.5rem) !important;
                }}
                .mod-app-card .mod-app-card-main {{
                    gap: 0 !important;
                }}
                .mod-app-card .mod-app-card-actions {{
                    gap: 0.42rem !important;
                    row-gap: 0 !important;
                }}
                .mod-app-card .mod-app-card-badges {{
                    gap: 0.34rem !important;
                    row-gap: 0 !important;
                }}
                .mod-app-card .mod-app-card-title {{
                    display: block;
                    width: 100%;
                    font-size: 1.34rem !important;
                    line-height: 0.94 !important;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    word-break: normal !important;
                    overflow-wrap: normal !important;
                }}
                .mod-app-card .mod-badge {{
                    padding: 0.22rem 0.5rem !important;
                }}
                .mod-app-card .mod-action {{
                    padding: 0.36rem 0.72rem !important;
                    min-height: 0 !important;
                }}
                .mod-app-card .mod-app-card-api-pill {{
                    gap: 0.34rem;
                    padding: 0.22rem 0.5rem;
                    min-height: 1.5rem;
                }}
                .mod-app-card .mod-app-card-api-link {{
                    font-size: 0.68rem;
                    line-height: 1;
                }}
                .mod-app-card .mod-app-card-api-separator {{
                    height: 0.72rem;
                }}
                .mod-app-card-open-corner {{
                    position: absolute;
                    top: -1px;
                    right: -1px;
                    z-index: 2;
                    width: 1.2rem;
                    height: 1.2rem;
                    pointer-events: none;
                    background: var(--mod-bg);
                    clip-path: polygon(100% 0, 100% 100%, 0 0);
                }}
                .mod-app-card-tab-link {{
                    position: relative;
                    border-color: var(--mod-accent) !important;
                }}
                .mod-app-card-tab-link::after {{
                    content: "";
                    position: absolute;
                    top: 1px;
                    right: 1px;
                    width: 0.52rem;
                    height: 0.52rem;
                    pointer-events: none;
                    background: #050507;
                    clip-path: polygon(100% 0, 100% 100%, 0 0);
                }}
                @container mod-app-card (max-width: 38rem) {{
                    .mod-app-card-shell {{
                        grid-template-columns: minmax(0, 1fr);
                        min-height: auto;
                        align-items: start !important;
                    }}
                    .mod-app-card-actions {{
                        width: 100%;
                        flex-wrap: wrap !important;
                        justify-content: flex-start !important;
                    }}
                    .mod-app-card-badges {{
                        flex-wrap: wrap !important;
                    }}
                }}
                .mod-app-runtime-chip {{
                    display: inline-flex !important;
                    align-items: center;
                    justify-content: center;
                    min-width: 4.85rem;
                    text-align: center;
                    font-variant-numeric: tabular-nums;
                }}
                .mod-app-card-live {{
                    animation: mod-app-card-live-pulse 760ms ease-out;
                }}
                .mod-app-card-live::before {{
                    animation: mod-app-card-strip-live 760ms ease-out;
                }}
                .mod-app-card-starting::before,
                .mod-app-card-stopping::before {{
                    width: var(--mod-app-rail-width);
                    opacity: 1;
                    filter: none;
                    background: var(--mod-app-strip-color, var(--mod-border-hot));
                    animation: none;
                }}
                .mod-app-card-starting::after,
                .mod-app-card-stopping::after {{
                    content: "";
                    position: absolute;
                    z-index: 1;
                    top: -1.25rem;
                    bottom: 0;
                    left: calc((var(--mod-app-rail-width) - var(--mod-app-chevron-width)) / 2);
                    width: var(--mod-app-chevron-width);
                    pointer-events: none;
                    clip-path: inset(
                        0 calc((var(--mod-app-chevron-width) - var(--mod-app-rail-width)) / 2)
                    );
                    will-change: transform;
                }}
                .mod-app-card-starting::after {{
                    background:
                        linear-gradient(
                            -45deg,
                            transparent 0 40%,
                            var(--mod-card) 40% 56%,
                            transparent 56% 100%
                        ) left top / 1rem 1.25rem repeat-y,
                        linear-gradient(
                            45deg,
                            transparent 0 40%,
                            var(--mod-card) 40% 56%,
                            transparent 56% 100%
                        ) right top / 1rem 1.25rem repeat-y;
                    animation: mod-app-card-strip-starting 900ms linear infinite;
                }}
                .mod-app-card-running::before {{
                    animation: mod-app-card-strip-running 2.4s ease-in-out infinite;
                    will-change: opacity, filter;
                }}
                .mod-app-card-running.mod-app-card-live::before {{
                    animation:
                        mod-app-card-strip-live 760ms ease-out,
                        mod-app-card-strip-running 2.4s ease-in-out 760ms infinite;
                }}
                .mod-app-card-stopping::after {{
                    background:
                        linear-gradient(
                            45deg,
                            transparent 0 40%,
                            var(--mod-card) 40% 56%,
                            transparent 56% 100%
                        ) left top / 1rem 1.25rem repeat-y,
                        linear-gradient(
                            -45deg,
                            transparent 0 40%,
                            var(--mod-card) 40% 56%,
                            transparent 56% 100%
                        ) right top / 1rem 1.25rem repeat-y;
                    animation: mod-app-card-strip-stopping 900ms linear infinite;
                }}
                .mod-app-runtime-chip-live {{
                    animation: mod-app-runtime-chip-live 820ms ease-out;
                    will-change: transform, opacity;
                }}
                @keyframes mod-app-card-live-pulse {{
                    0% {{
                        border-color: var(--mod-accent-border-strong) !important;
                        box-shadow:
                            0 0 0 1px var(--mod-accent-glow),
                            0 24px 70px rgba(0, 0, 0, 0.48),
                            inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    }}
                    55% {{
                        border-color: var(--mod-accent-border-strong) !important;
                    }}
                    100% {{
                        border-color: var(--mod-border) !important;
                        box-shadow: 0 24px 70px rgba(0, 0, 0, 0.48), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    }}
                }}
                @keyframes mod-app-hero-border-starting {{
                    0% {{
                        background-position: 0% 0;
                        filter: saturate(0.98) brightness(0.96);
                    }}
                    50% {{
                        background-position: 100% 0;
                        filter: saturate(1.08) brightness(1.22);
                    }}
                    100% {{
                        background-position: 0% 0;
                        filter: saturate(0.98) brightness(0.96);
                    }}
                }}
                @keyframes mod-app-hero-border-running {{
                    0% {{
                        background-position: 0% 0;
                        filter: saturate(0.98) brightness(0.98);
                    }}
                    50% {{
                        background-position: 100% 0;
                        filter: saturate(1.05) brightness(1.12);
                    }}
                    100% {{
                        background-position: 0% 0;
                        filter: saturate(0.98) brightness(0.98);
                    }}
                }}
                @keyframes mod-app-card-strip-live {{
                    0% {{
                        opacity: 0.76;
                        filter: saturate(0.9) brightness(1);
                    }}
                    42% {{
                        opacity: 1;
                        filter: saturate(1.2) brightness(1.32);
                    }}
                    100% {{
                        opacity: 1;
                        filter: saturate(1) brightness(1);
                    }}
                }}
                @keyframes mod-app-card-strip-starting {{
                    0% {{
                        transform: translateY(1.25rem);
                    }}
                    100% {{
                        transform: translateY(0);
                    }}
                }}
                @keyframes mod-app-card-strip-running {{
                    50% {{
                        opacity: 1;
                        filter: saturate(1.16) brightness(1.18);
                    }}
                    0%,
                    100% {{
                        opacity: 0.78;
                        filter: saturate(0.92) brightness(0.92);
                    }}
                }}
                @keyframes mod-app-card-strip-stopping {{
                    0% {{
                        transform: translateY(0);
                    }}
                    100% {{
                        transform: translateY(1.25rem);
                    }}
                }}
                @keyframes mod-app-runtime-chip-live {{
                    0% {{
                        transform: translateY(2px);
                        opacity: 0.72;
                    }}
                    40% {{
                        transform: translateY(0);
                        opacity: 1;
                    }}
                    58% {{
                        box-shadow: 0 0 0 0.18rem var(--mod-accent-faint);
                    }}
                    100% {{
                        transform: translateY(0);
                        opacity: 1;
                        box-shadow: none;
                    }}
                }}
                .mod-node-card {{
                    border-radius: 0 !important;
                    background: #08080c !important;
                    border-color: var(--mod-accent-border) !important;
                }}
                .mod-card-empty {{
                    border-style: dashed !important;
                    background: rgba(9, 9, 13, 0.74) !important;
                }}
                .mod-card-link {{
                    display: block;
                    border-radius: 0 !important;
                    text-decoration: none !important;
                    color: inherit !important;
                }}
                .mod-card-link > .nicegui-content,
                .mod-card-link .nicegui-content {{
                    width: 100%;
                }}
                .mod-card-link {{
                    background: linear-gradient(135deg, rgba(9, 9, 13, 0.98), rgba(15, 15, 21, 0.98)) !important;
                    border: 1px solid var(--mod-border) !important;
                    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.48), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    transition: transform 150ms ease, border-color 150ms ease, background 150ms ease;
                }}
                .mod-card-link:not(.mod-app-card-link):hover {{
                    transform: translateY(-1px);
                    border-color: var(--mod-border-hot) !important;
                    background: #101014 !important;
                }}
                .mod-card-link:focus-visible {{
                    border-color: var(--mod-accent) !important;
                    outline: 2px solid var(--mod-accent) !important;
                    outline-offset: 3px;
                }}
                .mod-app-card-link {{
                    cursor: pointer;
                    position: relative;
                    overflow: hidden;
                }}
                .mod-app-card-link::before {{
                    content: "";
                    position: absolute;
                    top: 0;
                    bottom: 0;
                    left: 0;
                    width: var(--mod-app-rail-width);
                    background: var(--mod-app-strip-color, var(--mod-border-hot));
                    border-radius: 0;
                    pointer-events: none;
                }}
                .mod-app-card-disabled::before {{
                    opacity: 0.8;
                    background:
                        repeating-linear-gradient(
                            180deg,
                            var(--mod-app-strip-color, var(--mod-border-hot)) 0 0.7rem,
                            transparent 0.7rem 1.08rem
                        );
                }}
                .mod-app-card-crashed::before {{
                    background:
                        repeating-linear-gradient(45deg, rgba(0, 0, 0, 0.76) 0 0.16rem, transparent 0.16rem 0.34rem),
                        repeating-linear-gradient(-45deg, rgba(0, 0, 0, 0.76) 0 0.16rem, transparent 0.16rem 0.34rem),
                        var(--mod-app-strip-color, var(--mod-border-hot));
                }}
                .mod-app-card-title-disabled {{
                    color: rgba(244, 244, 245, 0.74) !important;
                    text-shadow: none !important;
                }}
                .mod-app-card-api-pill {{
                    display: inline-flex;
                    align-items: center;
                    gap: 0.55rem;
                    padding: 0.42rem 0.72rem;
                    border: 1px solid rgba(63, 63, 70, 0.92);
                    background: rgba(8, 8, 12, 0.84);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                }}
                .mod-app-card-api-link {{
                    color: var(--mod-muted) !important;
                    text-decoration: none !important;
                    font-size: 0.78rem;
                    font-weight: 900;
                    letter-spacing: 0.06em;
                    text-transform: uppercase;
                }}
                .mod-app-card-api-link:hover {{
                    color: var(--mod-text) !important;
                }}
                .mod-app-card-api-separator {{
                    display: inline-block;
                    width: 1px;
                    height: 0.85rem;
                    background: rgba(113, 113, 122, 0.72);
                }}
                .mod-title {{ color: var(--mod-text) !important; text-shadow: none; }}
                .mod-title-small {{ color: var(--mod-text) !important; }}
                .mod-subtitle {{ color: var(--mod-muted) !important; }}
                .mod-error-text {{ color: #f87171 !important; }}
                .mod-select-form {{ display: flex; flex-direction: column; gap: 0.55rem; }}
                .mod-section-strip {{
                    align-items: flex-start;
                }}
                .mod-section-tabs-shell {{
                    flex: 0 0 auto;
                    min-width: 0;
                }}
                .mod-section-strip > .mod-section-tabs-shell {{
                    flex: 1 1 100%;
                }}
                .mod-section-tabs {{
                    display: inline-flex;
                    width: auto !important;
                    max-width: 100%;
                    min-width: 0;
                    align-self: flex-start;
                    border-bottom: 1px solid rgba(82, 82, 91, 0.44);
                    padding-bottom: 0.25rem;
                }}
                .mod-section-tabs .q-tabs__content {{
                    display: inline-flex;
                    width: auto;
                    max-width: 100%;
                    gap: 0.4rem;
                    flex-wrap: wrap;
                    justify-content: flex-start;
                }}
                .mod-section-tabs .q-tab {{
                    position: relative;
                    min-height: 2.65rem;
                    padding: 0.4rem 0.78rem;
                    color: var(--mod-muted) !important;
                    border: 1px solid rgba(63, 63, 70, 0.88);
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0)),
                        rgba(12, 12, 18, 0.88) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 10px 22px rgba(0, 0, 0, 0.24);
                    font-size: 0.78rem;
                    font-weight: 900;
                    letter-spacing: 0.1em;
                    text-transform: uppercase;
                    overflow: hidden;
                }}
                .mod-section-tabs .q-tab__content {{
                    flex-direction: row;
                    gap: 0.4rem;
                }}
                .mod-section-tabs .q-tab__icon {{
                    font-size: 1.05rem;
                }}
                .mod-section-tabs .q-tab::after {{
                    content: "";
                    position: absolute;
                    right: 0;
                    bottom: 0;
                    left: 0;
                    height: 2px;
                    background: linear-gradient(90deg, var(--mod-accent) 0%, var(--mod-accent-text) 50%, var(--mod-accent) 100%);
                    transform: scaleX(0);
                    transform-origin: center;
                    transition: transform var(--mod-motion-tab-accent) var(--mod-motion-ease);
                }}
                .mod-section-tabs .q-tab--active {{
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-accent-faint), transparent 65%),
                        var(--mod-accent-panel) !important;
                    box-shadow:
                        inset 0 0 0 1px var(--mod-accent-faint),
                        0 12px 26px rgba(0, 0, 0, 0.28);
                }}
                .mod-section-tabs .q-tab:hover,
                .mod-section-tabs .q-tab:focus-visible {{
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border-strong);
                    outline: none;
                }}
                .mod-section-tabs .q-tab:focus-visible {{
                    box-shadow:
                        inset 0 0 0 1px var(--mod-accent-border),
                        0 0 0 2px var(--mod-accent-border);
                }}
                .mod-section-tabs .q-tab--active::after {{
                    transform: scaleX(1);
                }}
                .mod-section-tabs .q-tabs__arrow {{
                    color: var(--mod-muted) !important;
                }}
                .mod-section-tabs .q-tab__indicator {{
                    display: none !important;
                }}
                .mod-section-chrome {{
                    flex: 0 0 100%;
                    width: 100%;
                    min-width: 0;
                    margin-left: 0;
                }}
                .mod-section-chrome-panel {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: flex-start;
                    gap: 0.75rem;
                    width: 100%;
                    min-width: 0;
                }}
                .mod-section-chrome-badge-row {{
                    display: flex;
                    flex-wrap: wrap;
                    align-items: center;
                    justify-content: flex-start;
                    gap: 0.5rem;
                    min-width: 0;
                }}
                .mod-section-chrome-actions {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: flex-start;
                    flex-wrap: wrap;
                    gap: 0.5rem;
                }}
                .mod-hero-toolbar {{
                    width: 100%;
                    margin-top: 0.35rem;
                    padding-top: 0.9rem;
                    border-top: 1px solid rgba(82, 82, 91, 0.44);
                }}
                .mod-hero-toolbar .mod-hero-toolbar-surface {{
                    position: static;
                    top: auto;
                    z-index: auto;
                }}
                .mod-section-panels {{
                    background: transparent !important;
                }}
                .mod-section-panel {{
                    padding: 0 !important;
                    background: transparent !important;
                }}
                .mod-system-native-tabs {{
                    display: inline-flex;
                    width: auto !important;
                    max-width: 100%;
                    flex-wrap: wrap;
                    gap: 0.4rem;
                    align-self: flex-start;
                    border-bottom: 1px solid rgba(82, 82, 91, 0.44);
                    padding-bottom: 0.25rem;
                }}
                .mod-system-native-tab {{
                    position: relative;
                    display: inline-flex;
                    min-height: 2.65rem;
                    align-items: center;
                    justify-content: center;
                    gap: 0.4rem;
                    padding: 0.4rem 0.78rem;
                    border: 1px solid rgba(63, 63, 70, 0.88);
                    color: var(--mod-muted) !important;
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0)),
                        rgba(12, 12, 18, 0.88) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 10px 22px rgba(0, 0, 0, 0.24);
                    cursor: pointer;
                    font: inherit;
                    font-size: 0.78rem;
                    font-weight: 900;
                    letter-spacing: 0.1em;
                    line-height: 1.2;
                    text-transform: uppercase;
                    overflow: hidden;
                }}
                .mod-system-native-tab-icon {{
                    font-size: 1.05rem;
                }}
                .mod-system-native-tab::after {{
                    content: "";
                    position: absolute;
                    right: 0;
                    bottom: 0;
                    left: 0;
                    height: 2px;
                    background: linear-gradient(90deg, var(--mod-accent) 0%, var(--mod-accent-text) 50%, var(--mod-accent) 100%);
                    transform: scaleX(0);
                    transform-origin: center;
                    transition: transform var(--mod-motion-tab-accent) var(--mod-motion-ease);
                }}
                .mod-system-native-tab:hover,
                .mod-system-native-tab:focus-visible {{
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border-strong);
                    outline: none;
                }}
                .mod-system-native-tab:focus-visible {{
                    box-shadow:
                        inset 0 0 0 1px var(--mod-accent-border),
                        0 0 0 2px var(--mod-accent-border);
                }}
                .mod-system-native-tab-active {{
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(135deg, var(--mod-accent-faint), transparent 65%),
                        var(--mod-accent-panel) !important;
                    box-shadow:
                        inset 0 0 0 1px var(--mod-accent-faint),
                        0 12px 26px rgba(0, 0, 0, 0.28);
                }}
                .mod-system-native-tab-active::after {{
                    transform: scaleX(1);
                }}
                .mod-system-native-tab-content,
                .mod-system-native-panel {{
                    min-height: 0;
                }}
                .mod-tab-header {{
                    display: flex;
                    flex-direction: column;
                    align-items: stretch;
                    justify-content: flex-start;
                    gap: 0.45rem;
                }}
                .mod-tab-header-main {{
                    display: flex;
                    width: 100%;
                    flex: 0 0 auto;
                    min-width: 0;
                    flex-direction: column;
                    gap: 0.18rem;
                }}
                .mod-tab-header-badges {{
                    display: flex;
                    flex: 0 1 auto;
                    align-items: center;
                    justify-content: flex-end;
                    width: 100%;
                    gap: 0.5rem;
                    flex-wrap: wrap;
                }}
                .mod-section-layout {{
                    gap: 0.35rem;
                }}
                .mod-section-layout > .mod-section-tabs-shell {{
                    width: 100%;
                }}
                .mod-tab-empty-detail {{
                    max-width: min(56rem, 100%);
                }}
                .mod-list-toolbar,
                .mod-mods-toolbar {{
                    position: sticky; top: 0; z-index: 4;
                    display: flex; align-items: center; justify-content: space-between;
                    gap: 0.65rem; flex-wrap: wrap;
                    padding: 0.72rem 0.85rem;
                    border-radius: 0 !important;
                    background: linear-gradient(135deg, rgba(7, 7, 10, 0.96), rgba(23, 12, 22, 0.96)) !important;
                    border: 1px solid var(--mod-border-hot) !important;
                    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.42);
                    backdrop-filter: blur(10px);
                }}
                .mod-list-actions,
                .mod-mods-toolbar-actions {{
                    display: flex;
                    gap: 0.5rem;
                    flex-wrap: wrap;
                    align-items: stretch;
                    width: 100%;
                }}
                .mod-mods-toolbar {{
                    flex-direction: column;
                    align-items: stretch;
                }}
                .mod-mods-toolbar-filters {{
                    display: flex;
                    align-items: center;
                    gap: 0.65rem;
                    flex-wrap: nowrap;
                    min-width: 0;
                }}
                .mod-mods-toolbar-actions {{
                    flex: 0 1 auto;
                    width: auto;
                    max-width: 100%;
                    align-self: flex-end;
                    justify-content: flex-end;
                    margin-left: auto;
                }}
                .mod-mods-toolbar .mod-mods-toolbar-search {{
                    flex: 999 1 24rem;
                    min-width: 0;
                }}
                .mod-config-select.mod-mods-toolbar-sort {{
                    flex: 0 0 9rem;
                    width: 9rem;
                    min-width: 9rem;
                    max-width: 100%;
                }}
                .mod-mods-toolbar-result-count {{
                    flex: 0 0 auto;
                    min-width: 5.5rem;
                    padding: 0.38rem 0.58rem;
                    color: #d4d4d8 !important;
                    background: rgba(24, 24, 31, 0.9) !important;
                    border: 1px solid rgba(82, 82, 91, 0.72);
                    font-size: 0.68rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.06em;
                    text-align: center;
                    text-transform: uppercase;
                    white-space: nowrap;
                }}
                .mod-mods-toolbar-actions .mod-toolbar-button {{
                    flex: 0 1 auto;
                    width: auto;
                    min-width: 8.5rem;
                }}
                .mod-mods-toolbar-actions .mod-list-button.danger {{
                    margin-left: 0.35rem;
                }}
                .mod-list-button.mod-toolbar-primary {{
                    background: linear-gradient(135deg, var(--mod-accent-dark), var(--mod-accent)) !important;
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.12),
                        0 10px 26px var(--mod-accent-glow) !important;
                }}
                .mod-toolbar-button {{
                    flex: 1 1 11rem;
                    min-width: 11rem;
                    width: 0;
                    min-height: 2.5rem !important;
                    height: 2.5rem !important;
                    justify-content: center;
                }}
                .mod-toolbar-button-fill {{
                    flex: 1 1 11rem;
                    min-width: 11rem;
                }}
                .mod-mods-toolbar-actions .mod-toolbar-selection-button {{
                    flex: 0 0 auto;
                    min-width: 4.75rem;
                    width: auto;
                    padding-inline: 0.62rem !important;
                }}
                .mod-mods-toolbar-actions .mod-toolbar-menu-button {{
                    flex: 0 0 2.5rem;
                    width: 2.5rem;
                    min-width: 2.5rem;
                    padding: 0 !important;
                }}
                .mod-mods-toolbar .mod-toolbar-status-button,
                .mod-mods-toolbar .mod-toolbar-status-button.q-btn--disabled {{
                    flex: 0 0 auto;
                    min-width: 9.5rem;
                    width: auto;
                    opacity: 1 !important;
                    color: var(--mod-accent-text-strong) !important;
                    border-color: var(--mod-accent-border) !important;
                    background: rgba(24, 16, 34, 0.78) !important;
                    cursor: default !important;
                }}
                .mod-mods-toolbar .mod-toolbar-status-button .q-btn__content {{
                    opacity: 1 !important;
                    color: var(--mod-accent-text-strong) !important;
                    text-shadow: 0 0 12px var(--mod-accent-glow);
                }}
                .mod-toolbar-menu {{
                    min-width: 12rem;
                }}
                .mod-toolbar-menu-item-danger,
                .mod-toolbar-menu-item-danger .q-item__label {{
                    color: #fca5a5 !important;
                }}
                .mod-toolbar-menu-item-danger:hover,
                .mod-toolbar-menu-item-danger.q-manual-focusable--focused {{
                    background: rgba(127, 29, 29, 0.42) !important;
                    color: #fecaca !important;
                }}
                .mod-modlist-dialog-card {{
                    width: min(46rem, calc(100vw - 1.5rem)) !important;
                    max-height: min(48rem, calc(100vh - 1.5rem)) !important;
                    height: auto !important;
                    overflow: hidden !important;
                }}
                .mod-modlist-dialog-card > .nicegui-content,
                .mod-modlist-dialog-card .nicegui-content {{
                    width: 100%;
                    height: auto !important;
                    min-height: 0 !important;
                    padding: 0 !important;
                }}
                .mod-modlist-body {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr);
                    align-content: start;
                    gap: 0.85rem;
                    padding: 1.1rem;
                    height: auto !important;
                    min-height: 0 !important;
                    overflow: auto;
                }}
                .mod-modlist-format {{
                    min-width: 0;
                }}
                .mod-modlist-options {{
                    gap: 0.5rem;
                }}
                .mod-modlist-toggle {{
                    min-width: 7rem;
                }}
                .mod-modlist-preview-section {{
                    gap: 0.4rem;
                }}
                .mod-modlist-preview-frame {{
                    width: calc(100% + 2.2rem);
                    margin-inline: -1.1rem;
                }}
                .mod-modlist-preview {{
                    min-height: 4.25rem;
                    max-height: min(24rem, 50vh);
                    margin: 0;
                    padding: 0.8rem;
                    overflow: auto;
                    border: 1px solid rgba(82, 82, 91, 0.78);
                    background: rgba(7, 7, 11, 0.96) !important;
                    color: var(--mod-text) !important;
                    font-family: "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
                    font-size: 0.78rem;
                    line-height: 1.5;
                    overflow-wrap: normal;
                    white-space: pre;
                }}
                .mod-save-upload-panel {{
                    padding: 0.85rem;
                    border: 1px solid rgba(82, 82, 91, 0.68);
                    background: rgba(8, 8, 10, 0.8) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 12px 28px rgba(0, 0, 0, 0.18) !important;
                }}
                .mod-save-upload-panel-title {{
                    letter-spacing: 0.01em;
                }}
                .mod-save-upload-panel-detail {{
                    line-height: 1.45;
                }}
                .mod-save-upload-field {{
                    flex: 0 1 auto;
                    min-width: 0;
                }}
                .mod-save-upload-target-static {{
                    width: 100%;
                    min-height: 2.35rem;
                    display: flex;
                    align-items: center;
                    padding: 0.45rem 0.7rem;
                    border: 1px solid rgba(63, 63, 70, 0.82);
                    background: rgba(13, 13, 18, 0.94) !important;
                    color: var(--mod-text) !important;
                    font-size: 0.88rem;
                    font-weight: 850 !important;
                    overflow-wrap: anywhere;
                }}
                .mod-save-upload-target-list {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr);
                }}
                .mod-save-upload-target-button {{
                    width: 100%;
                    justify-content: flex-start !important;
                    text-align: left !important;
                    min-height: 2.35rem !important;
                }}
                .mod-save-upload-target-button .q-btn__content {{
                    justify-content: flex-start !important;
                    min-width: 0;
                    overflow-wrap: anywhere;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone).q-uploader {{
                    width: 100%;
                    min-height: 0 !important;
                    max-height: none !important;
                    border-radius: 0 !important;
                    border: 1px solid var(--mod-accent-border) !important;
                    background: rgba(13, 13, 18, 0.96) !important;
                    color: var(--mod-text) !important;
                    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.24) !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__header {{
                    min-height: 2.5rem;
                    border-radius: 0 !important;
                    background: var(--mod-accent-dark) !important;
                    color: #fff !important;
                    border-bottom: 1px solid var(--mod-accent-border) !important;
                    box-shadow: none !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__title,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__subtitle,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__header .q-btn,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__header .q-icon {{
                    color: #fff !important;
                    opacity: 1 !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__title {{
                    font-weight: 900 !important;
                    letter-spacing: 0.02em;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__subtitle {{
                    color: var(--mod-accent-text) !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__list {{
                    min-height: 0 !important;
                    max-height: 8rem !important;
                    padding: 0.45rem !important;
                    background: rgba(8, 8, 12, 0.96) !important;
                    color: var(--mod-text) !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__list:empty {{
                    display: none !important;
                    padding: 0 !important;
                    border: 0 !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file-header,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file-name,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file-size,
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file-status {{
                    color: var(--mod-text) !important;
                }}
                :is(.mod-file-upload-zone, .mod-save-upload-zone) .q-uploader__file {{
                    border-radius: 0 !important;
                    background: rgba(24, 24, 31, 0.9) !important;
                    border: 1px solid rgba(63, 63, 70, 0.82) !important;
                }}
                .mod-list-button,
                .q-btn.mod-list-button {{
                    border-radius: 0 !important;
                    min-height: 2.25rem !important;
                    padding: 0.45rem 0.8rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.02em;
                    color: #fff !important;
                    background: var(--mod-accent-dark) !important;
                    border: 1px solid var(--mod-accent-border) !important;
                    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                :is(.mod-list-button, .mod-toolbar-button, .mod-action, .mod-badge-link, .mod-badge-action):focus-visible {{
                    outline: 2px solid var(--mod-accent) !important;
                    outline-offset: 3px;
                }}
                .mod-list-button.state-enabled,
                .mod-list-button.state-disabled,
                .mod-list-button.state-core-on,
                .mod-list-button.state-core-off,
                .mod-list-button.state-blocked,
                .mod-list-button.state-open {{
                    box-shadow:
                        inset 0 0 0 6px rgba(255, 255, 255, 0.08),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.secondary,
                .q-btn.mod-list-button.secondary {{
                    color: var(--mod-accent-text-strong) !important;
                    background: var(--mod-accent-panel) !important;
                    border-color: var(--mod-accent-border) !important;
                    box-shadow: none !important;
                }}
                .mod-list-button.state-enabled {{
                    background: var(--mod-accent-surface) !important;
                    border-color: var(--mod-accent) !important;
                    box-shadow:
                        inset 0 0 0 2px var(--mod-accent-glow),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.state-disabled {{
                    color: #d4d4d8 !important;
                    background: #18181f !important;
                    border-color: #52525b !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(212, 212, 216, 0.14),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.state-core-on {{
                    color: var(--mod-negative-text) !important;
                    background: var(--mod-negative-surface) !important;
                    border-color: var(--mod-negative) !important;
                    box-shadow:
                        inset 0 0 0 2px var(--mod-negative-glow),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.state-core-off {{
                    color: #e4e4e7 !important;
                    background: #16161d !important;
                    border-color: #3f3f46 !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(228, 228, 231, 0.12),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.state-blocked {{
                    color: var(--mod-negative-text) !important;
                    background: var(--mod-negative-surface) !important;
                    border-color: var(--mod-negative-border-strong) !important;
                    box-shadow:
                        inset 0 0 0 2px var(--mod-negative-glow),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.state-open {{
                    color: #e4e4e7 !important;
                    background: #16161d !important;
                    border-color: #52525b !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(228, 228, 231, 0.12),
                        0 10px 24px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-list-button.danger,
                .q-btn.mod-list-button.danger {{
                    color: var(--mod-negative-text) !important;
                    background: var(--mod-negative-dark) !important;
                    border-color: var(--mod-negative-border-strong) !important;
                }}
                .mod-list-count {{ color: var(--mod-muted) !important; font-size: 0.88rem; font-weight: 800; }}
                .mod-list {{ display: flex; flex-direction: column; gap: 0.4rem; }}
                @supports (content-visibility: auto) {{
                    .mod-list > .mod-card,
                    .mod-save-grid > .mod-save-card,
                    .mod-settings-grid > .mod-setting-card,
                    .mod-recipe-browser-grid > .mod-recipe-browser-card,
                    .mod-recipe-manage-list > .mod-recipe-manage-card,
                    .mod-chat-timeline > .mod-chat-message {{
                        content-visibility: auto;
                        contain-intrinsic-size: auto 7rem;
                    }}
                    .mod-settings-grid > .mod-setting-card {{
                        contain-intrinsic-size: auto 9rem;
                    }}
                    .mod-chat-timeline > .mod-chat-message {{
                        contain-intrinsic-size: auto 5rem;
                    }}
                }}
                .mod-tab-toolbar {{
                    display: flex;
                    gap: 0.6rem;
                    flex-wrap: wrap;
                    align-items: flex-end;
                }}
                .mod-tab-toolbar-surface {{
                    padding: 0.68rem 0.78rem;
                    border: 1px solid rgba(82, 82, 91, 0.48);
                    background: rgba(8, 8, 10, 0.9) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 12px 28px rgba(0, 0, 0, 0.24) !important;
                }}
                .mod-config-select {{
                    flex: 0 1 auto;
                    min-width: min(18rem, 100%);
                    max-width: 100%;
                }}
                .mod-tab-toolbar > .mod-config-select {{
                    flex: 1 1 26rem;
                }}
                :is(.mod-config-input, .mod-config-select) .q-field__control {{
                    color: var(--mod-text) !important;
                    background-color: rgba(17, 17, 24, 0.96) !important;
                    color-scheme: dark;
                }}
                :is(.mod-config-input, .mod-config-select) .q-field__native,
                :is(.mod-config-input, .mod-config-select) .q-field__input,
                :is(.mod-config-input, .mod-config-select) input,
                :is(.mod-config-input, .mod-config-select) textarea {{
                    color: var(--mod-text) !important;
                    -webkit-text-fill-color: var(--mod-text) !important;
                    caret-color: var(--mod-accent) !important;
                    opacity: 1 !important;
                }}
                :is(.mod-config-input, .mod-config-select) .q-field__label,
                :is(.mod-config-input, .mod-config-select) .q-field__marginal,
                :is(.mod-config-input, .mod-config-select) .q-icon {{
                    color: var(--mod-muted) !important;
                    opacity: 1 !important;
                }}
                :is(.mod-config-input, .mod-config-select) .q-field--focused .q-field__label,
                :is(.mod-config-input, .mod-config-select) .q-field--float .q-field__label {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                :is(.mod-config-input, .mod-config-select) .q-field__native::placeholder,
                :is(.mod-config-input, .mod-config-select) .q-field__input::placeholder,
                :is(.mod-config-input, .mod-config-select) input::placeholder,
                :is(.mod-config-input, .mod-config-select) textarea::placeholder {{
                    color: var(--mod-muted) !important;
                    -webkit-text-fill-color: var(--mod-muted) !important;
                    opacity: 1 !important;
                }}
                :is(.mod-config-input, .mod-config-select) input:-webkit-autofill,
                :is(.mod-config-input, .mod-config-select) input:-webkit-autofill:hover,
                :is(.mod-config-input, .mod-config-select) input:-webkit-autofill:focus {{
                    -webkit-text-fill-color: var(--mod-text) !important;
                    box-shadow: 0 0 0 1000px rgba(17, 17, 24, 0.98) inset !important;
                    caret-color: var(--mod-accent) !important;
                }}
                .mod-recipe-field .q-field__control {{
                    min-height: 3.05rem !important;
                    padding: 0 0.42rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                    border-radius: 0 !important;
                    background: rgba(12, 12, 15, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        0 10px 24px rgba(0, 0, 0, 0.24) !important;
                    transition:
                        border-color 120ms ease,
                        box-shadow 120ms ease,
                        background-color 120ms ease;
                }}
                .mod-recipe-field:hover .q-field__control,
                .mod-recipe-field .q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        var(--mod-accent-panel) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 var(--mod-accent-glow),
                        0 0 0 1px var(--mod-accent-glow),
                        0 12px 28px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-recipe-field .q-field__label {{
                    color: #b9b5c5 !important;
                    font-size: 0.72rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                }}
                .mod-recipe-field .q-field__label.q-field__label--focused,
                .mod-recipe-field .q-field--float .q-field__label {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                .mod-recipe-field .q-field__native,
                .mod-recipe-field .q-field__input,
                .mod-recipe-field .q-field__append,
                .mod-recipe-field .q-field__prepend,
                .mod-recipe-field .q-field__marginal,
                .mod-recipe-field .q-icon {{
                    color: #f4f4f5 !important;
                    opacity: 1 !important;
                }}
                .mod-recipe-field .q-field__native,
                .mod-recipe-field .q-field__input {{
                    font-size: 0.84rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.01em;
                }}
                .mod-recipe-field .q-field__native::placeholder,
                .mod-recipe-field input::placeholder {{
                    color: #d4d4d8 !important;
                    opacity: 1;
                }}
                .mod-recipe-editor-shell,
                .mod-recipe-browser-shell {{
                    background:
                        linear-gradient(135deg, rgba(12, 12, 18, 0.98), rgba(20, 16, 28, 0.96)) !important;
                    border: 1px solid rgba(82, 82, 91, 0.66) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 20px 42px rgba(0, 0, 0, 0.34) !important;
                }}
                .mod-recipe-subtabs {{
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    height: auto !important;
                    min-height: 0 !important;
                }}
                .mod-recipe-subtabs > .mod-section-tabs-shell {{
                    flex: 0 0 auto;
                    width: 100%;
                }}
                .mod-recipe-subtab-panels,
                .mod-recipe-subtab-panels .q-panel,
                .mod-recipe-subtab-panels .q-panel > div,
                .mod-recipe-subtab-panels .mod-section-panel {{
                    flex: 0 0 auto !important;
                    height: auto !important;
                    min-height: 0 !important;
                }}
                .mod-recipe-panel-heading {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.18rem;
                    padding-bottom: 0.15rem;
                    border-bottom: 1px solid rgba(63, 63, 70, 0.54);
                }}
                .mod-recipe-workbench {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) 18rem;
                    gap: 1rem;
                    align-items: start;
                    width: 100%;
                }}
                .mod-recipe-workbench-main,
                .mod-recipe-workbench-side {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.75rem;
                    min-width: 0;
                }}
                .mod-recipe-input-panel,
                .mod-recipe-selection-panel {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.55rem;
                    padding: 0.72rem 0.78rem;
                    border: 1px solid rgba(82, 82, 91, 0.62);
                    background: rgba(8, 8, 10, 0.92);
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 12px 28px rgba(0, 0, 0, 0.24);
                }}
                .mod-recipe-output-panel {{
                    background:
                        linear-gradient(180deg, rgba(34, 197, 94, 0.08), rgba(34, 197, 94, 0)),
                        rgba(9, 9, 13, 0.9);
                }}
                .mod-recipe-selection-actions {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 0.5rem;
                    padding-top: 0.2rem;
                }}
                .mod-recipe-selection-actions > * {{
                    flex: 1 1 100%;
                    min-width: 0;
                }}
                .mod-recipe-slot-grid {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    gap: 0.45rem;
                    width: 100%;
                    max-width: 28rem;
                }}
                .mod-recipe-slot {{
                    min-height: 5.9rem !important;
                    padding: 0.45rem !important;
                    border-radius: 0 !important;
                    border: 1px solid rgba(82, 82, 91, 0.82) !important;
                    background:
                        linear-gradient(180deg, rgba(244, 244, 245, 0.05), rgba(244, 244, 245, 0)),
                        rgba(16, 16, 22, 0.96) !important;
                    box-shadow:
                        inset 0 0 0 1px rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 var(--mod-accent-faint),
                        0 12px 24px rgba(0, 0, 0, 0.24) !important;
                }}
                .mod-recipe-slot:hover {{
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(22, 18, 30, 0.98) !important;
                }}
                .mod-recipe-slot-drop-active {{
                    border-color: rgba(96, 165, 250, 0.84) !important;
                    background:
                        linear-gradient(180deg, rgba(96, 165, 250, 0.14), rgba(96, 165, 250, 0)),
                        rgba(17, 24, 39, 0.98) !important;
                    box-shadow:
                        inset 0 0 0 1px rgba(191, 219, 254, 0.12),
                        0 0 0 1px rgba(96, 165, 250, 0.18),
                        0 14px 28px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-recipe-slot-selected {{
                    border-color: rgba(34, 197, 94, 0.82) !important;
                    background:
                        linear-gradient(180deg, rgba(34, 197, 94, 0.12), rgba(34, 197, 94, 0)),
                        rgba(18, 28, 22, 0.98) !important;
                    box-shadow:
                        inset 0 0 0 1px rgba(187, 247, 208, 0.12),
                        0 0 0 1px rgba(34, 197, 94, 0.18),
                        0 14px 28px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-recipe-slot .q-btn__content {{
                    width: 100%;
                    height: 100%;
                    align-items: stretch;
                    justify-content: flex-start;
                }}
                .mod-recipe-slot-head {{
                    width: 100%;
                    min-height: 100%;
                    align-items: flex-start;
                    justify-content: space-between;
                    gap: 0.55rem;
                    flex-wrap: nowrap;
                }}
                .mod-recipe-slot-copy {{
                    flex: 1 1 auto;
                    min-width: 0;
                    height: 100%;
                    justify-content: flex-start;
                    align-items: flex-start;
                    gap: 0.3rem;
                }}
                .mod-recipe-slot-visual,
                .mod-recipe-browser-visual {{
                    flex: 0 0 auto;
                    min-width: 0;
                }}
                .mod-recipe-slot-label {{
                    color: #f4f4f5 !important;
                    font-size: 0.78rem !important;
                    line-height: 1.15rem;
                    font-weight: 900 !important;
                    text-align: left;
                    word-break: break-word;
                }}
                .mod-recipe-slot-value {{
                    color: var(--mod-accent-text) !important;
                    font-size: 0.68rem !important;
                    line-height: 0.95rem;
                    font-weight: 800 !important;
                    text-align: left;
                    word-break: break-word;
                }}
                .mod-recipe-slot-value-tag {{
                    color: #86efac !important;
                }}
                .mod-recipe-slot-value-empty {{
                    color: #a1a1aa !important;
                }}
                .mod-recipe-icon-shell {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    width: 2.85rem;
                    height: 2.85rem;
                    border: 1px solid rgba(82, 82, 91, 0.82);
                    background:
                        linear-gradient(180deg, rgba(244, 244, 245, 0.05), rgba(244, 244, 245, 0)),
                        rgba(8, 8, 12, 0.92);
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        0 8px 18px rgba(0, 0, 0, 0.24);
                    overflow: hidden;
                }}
                .mod-recipe-icon-stack {{
                    position: relative;
                    display: inline-block;
                    width: 2.85rem;
                    height: 2.85rem;
                }}
                .mod-recipe-icon-stack .mod-recipe-icon-shell {{
                    position: absolute;
                    inset: 0;
                }}
                .mod-recipe-icon-image {{
                    display: block;
                    z-index: 1;
                    color: transparent;
                    font-size: 0;
                    object-fit: contain;
                    image-rendering: pixelated;
                    background: transparent;
                }}
                .mod-recipe-icon-tag,
                .mod-recipe-icon-empty,
                .mod-recipe-icon-fallback {{
                    color: var(--mod-accent-text-strong);
                    font-size: 1.1rem;
                    font-weight: 900;
                    letter-spacing: 0.04em;
                }}
                .mod-recipe-icon-tag {{
                    color: #86efac;
                    background:
                        linear-gradient(180deg, rgba(34, 197, 94, 0.12), rgba(34, 197, 94, 0)),
                        rgba(10, 18, 12, 0.94);
                }}
                .mod-recipe-icon-empty {{
                    color: #a1a1aa;
                }}
                .mod-recipe-icon-fallback {{
                    color: var(--mod-accent-text);
                }}
                .mod-recipe-browser-toolbar {{
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 0.75rem;
                    flex-wrap: wrap;
                    width: 100%;
                }}
                .mod-recipe-browser-filter {{
                    flex: 0 1 13rem;
                    min-width: 11rem;
                }}
                .mod-recipe-browser-status {{
                    display: flex;
                    align-items: center;
                    gap: 0.7rem;
                    flex-wrap: wrap;
                    min-height: 2.25rem;
                }}
                .mod-recipe-browser-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr));
                    gap: 0.75rem 0.9rem;
                    width: 100%;
                }}
                .mod-recipe-browser-card {{
                    position: relative;
                    overflow: hidden !important;
                    cursor: pointer;
                    min-height: 3.75rem;
                    padding: 0.5rem 4.25rem 0.5rem 0.65rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.66) !important;
                    background:
                        linear-gradient(135deg, rgba(12, 12, 18, 0.98), rgba(17, 14, 24, 0.96)) !important;
                    transition: border-color 120ms ease, transform 120ms ease, background-color 120ms ease;
                }}
                .mod-recipe-browser-card:hover {{
                    transform: translateY(-1px);
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(135deg, rgba(18, 16, 26, 0.98), rgba(26, 18, 36, 0.96)) !important;
                }}
                .mod-recipe-browser-card-row {{
                    width: 100%;
                    align-items: center;
                    justify-content: space-between;
                    gap: 0.7rem;
                    flex-wrap: nowrap;
                }}
                .mod-recipe-browser-visual {{
                    position: absolute;
                    z-index: 2;
                    top: -1px;
                    right: -1px;
                    bottom: -1px;
                    width: 3.75rem;
                    pointer-events: none;
                }}
                .mod-recipe-browser-card .mod-recipe-icon-shell {{
                    width: 100%;
                    height: 100%;
                    border: 0;
                    border-left: 1px solid var(--mod-accent-border);
                    box-shadow: none;
                }}
                .mod-recipe-browser-card .mod-recipe-icon-stack {{
                    width: 100%;
                    height: 100%;
                }}
                .mod-recipe-browser-copy {{
                    flex: 1 1 auto;
                    min-width: 0;
                    gap: 0.24rem;
                }}
                .mod-recipe-browser-name {{
                    color: var(--mod-text) !important;
                    font-size: 0.82rem !important;
                    line-height: 1.05rem;
                    font-weight: 900 !important;
                    word-break: break-word;
                }}
                .mod-recipe-browser-id {{
                    color: var(--mod-muted) !important;
                    font-size: 0.7rem !important;
                    line-height: 0.95rem;
                    font-weight: 800 !important;
                    word-break: break-word;
                }}
                .mod-recipe-manage-list,
                .mod-recipe-list {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.7rem;
                }}
                .mod-recipe-manage-card,
                .mod-recipe-entry {{
                    border: 1px solid rgba(82, 82, 91, 0.62) !important;
                    background:
                        linear-gradient(135deg, rgba(11, 11, 16, 0.98), rgba(18, 15, 24, 0.96)) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        0 16px 34px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-recipe-manage-header,
                .mod-recipe-entry-head {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: space-between;
                    gap: 0.75rem;
                    flex-wrap: wrap;
                    width: 100%;
                }}
                .mod-recipe-manage-badges {{
                    display: flex;
                    align-items: center;
                    gap: 0.45rem;
                    flex-wrap: wrap;
                }}
                .mod-recipe-operation,
                .mod-recipe-kind {{
                    display: inline-flex;
                    align-items: center;
                    min-height: 1.6rem;
                    padding: 0.2rem 0.52rem;
                    border: 1px solid rgba(82, 82, 91, 0.8);
                    font-size: 0.68rem;
                    font-weight: 900;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                }}
                .mod-recipe-operation {{
                    color: var(--mod-accent-text-strong);
                    background: var(--mod-accent-surface);
                }}
                .mod-recipe-operation-add {{
                    border-color: rgba(34, 197, 94, 0.56);
                    background: rgba(20, 83, 45, 0.84);
                    color: #dcfce7;
                }}
                .mod-recipe-operation-remove {{
                    border-color: rgba(220, 38, 38, 0.58);
                    background: rgba(91, 18, 18, 0.84);
                    color: #fecaca;
                }}
                .mod-recipe-kind {{
                    color: var(--mod-accent-text-strong);
                    background: rgba(18, 18, 24, 0.9);
                }}
                .mod-console-select {{
                    flex: 1 1 18rem;
                    min-width: 12rem;
                }}
                .mod-console-select-action {{
                    flex: 999 1 26rem;
                    min-width: 18rem;
                }}
                .mod-console-select-compact {{
                    flex: 0 0 10rem;
                    min-width: 10rem;
                }}
                .mod-console-select .q-field__control {{
                    min-height: 3rem !important;
                    padding: 0 0.42rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(16, 16, 22, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 var(--mod-accent-faint),
                        0 10px 24px rgba(0, 0, 0, 0.2) !important;
                }}
                .mod-console-select:hover .q-field__control,
                .mod-console-select .q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 var(--mod-accent-glow),
                        0 0 0 1px var(--mod-accent-faint),
                        0 12px 26px rgba(0, 0, 0, 0.26) !important;
                }}
                .mod-console-select .q-field--filled .q-field__control::before {{
                    border-bottom: 1px solid var(--mod-accent-glow) !important;
                }}
                .mod-console-select .q-field--filled .q-field__control::after {{
                    border-bottom: 2px solid var(--mod-accent-border-strong) !important;
                }}
                .mod-console-select .q-field__native,
                .mod-console-select .q-field__input,
                .mod-console-select .q-field__append,
                .mod-console-select .q-field__prepend,
                .mod-console-select .q-field__marginal,
                .mod-console-select .q-icon {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                .mod-console-select.mod-console-select-black .q-field__control,
                .mod-console-select.mod-console-select-black:hover .q-field__control,
                .mod-console-select.mod-console-select-black .q-field--focused .q-field__control {{
                    background: rgba(6, 6, 10, 0.98) !important;
                    background-image: none !important;
                }}
                .mod-console-select-menu {{
                    background: rgba(6, 6, 10, 0.98) !important;
                    color: var(--mod-accent-text-strong) !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                }}
                .mod-console-select-menu .q-item,
                .mod-console-select-menu .q-item__label,
                .mod-console-select-menu .q-icon {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                .mod-console-select-menu .q-item--active,
                .mod-console-select-menu .q-item.q-manual-focusable--focused,
                .mod-console-select-menu .q-item[aria-selected="true"] {{
                    background: var(--mod-accent-surface) !important;
                }}
                .mod-config-search {{
                    flex: 0 1 auto;
                    min-width: min(15rem, 100%);
                    max-width: 100%;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__control {{
                    background: rgba(12, 12, 15, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(255, 255, 255, 0.04) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field--filled .q-field__control::before {{
                    border-bottom: 1px solid var(--mod-accent-border) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field--filled .q-field__control::after {{
                    border-bottom: 2px solid var(--mod-accent) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__native,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__input,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__append,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__prepend,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-icon {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__native::placeholder,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) input::placeholder {{
                    color: var(--mod-accent-text) !important;
                    opacity: 1;
                }}
                .mod-tab-toolbar-actions {{
                    display: flex;
                    gap: 0.5rem;
                    flex-wrap: wrap;
                    margin-left: auto;
                }}
                .mod-inline-toolbar {{
                    flex-wrap: nowrap;
                    align-items: center;
                }}
                .mod-inline-toolbar > :is(.mod-settings-search, .mod-console-select-action) {{
                    flex: 1 1 0;
                    min-width: 0;
                }}
                .mod-inline-toolbar-actions {{
                    flex: 0 0 auto;
                    flex-wrap: nowrap;
                }}
                .mod-save-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr));
                    gap: 0.75rem;
                    align-items: stretch;
                }}
                .mod-save-card {{
                    border-radius: 0 !important;
                    min-height: 100%;
                    background: linear-gradient(135deg, rgba(11, 11, 16, 0.96), rgba(18, 15, 23, 0.96)) !important;
                    border: 1px solid #2b2b33 !important;
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03) !important;
                    color: var(--mod-text) !important;
                    transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
                }}
                .mod-save-card:hover {{
                    transform: translateY(-1px);
                    border-color: var(--mod-accent-border) !important;
                    background: linear-gradient(135deg, rgba(13, 13, 18, 0.98), rgba(22, 16, 28, 0.98)) !important;
                }}
                .mod-save-card > .nicegui-content,
                .mod-save-card .nicegui-content {{
                    padding: 0 !important;
                    width: 100%;
                    height: 100%;
                }}
                .mod-save-card-path {{
                    color: var(--mod-dim) !important;
                }}
                .mod-save-card-actions {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 0.5rem;
                    margin-top: auto;
                    width: min(100%, 11rem);
                }}
                .mod-save-card-actions-split {{
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }}
                .mod-save-card-button {{
                    width: 100%;
                    min-width: 0;
                }}
                .mod-settings-grid {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.55rem;
                }}
                .mod-setting-card {{
                    border-radius: 0 !important;
                    background: linear-gradient(135deg, rgba(11, 11, 16, 0.96), rgba(18, 15, 23, 0.96)) !important;
                    border: 1px solid #2b2b33 !important;
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03) !important;
                    color: var(--mod-text) !important;
                }}
                .mod-setting-card > .nicegui-content,
                .mod-setting-card .nicegui-content {{
                    padding: 0.34rem 0.42rem !important;
                }}
                .mod-setting-card {{
                    position: relative;
                }}
                .mod-setting-card.locked {{
                    border-color: #3f3f46 !important;
                    background: linear-gradient(135deg, rgba(10, 10, 14, 0.94), rgba(13, 13, 18, 0.94)) !important;
                }}
                .mod-setting-shell {{
                    display: grid;
                    grid-template-columns: minmax(9.5rem, 13.25rem) minmax(0, 1fr) minmax(8.5rem, 12rem);
                    gap: 0;
                    align-items: stretch;
                    min-height: 0;
                    padding-right: 3.05rem;
                    position: relative;
                }}
                .mod-setting-control {{
                    min-width: 0;
                    height: 100%;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    padding-right: 0.35rem;
                }}
                .mod-setting-control-paragraph {{
                    position: relative;
                    justify-content: flex-start;
                }}
                .mod-setting-control-surface {{
                    width: 100%;
                    min-width: 0;
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    overflow: hidden;
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.035), rgba(255, 255, 255, 0)),
                        rgba(9, 9, 13, 0.9) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.025),
                        inset 0 -1px 0 var(--mod-accent-faint) !important;
                }}
                .mod-setting-control-surface-paragraph {{
                    position: absolute;
                    top: -0.34rem;
                    bottom: -0.34rem;
                    left: 0;
                    right: -0.08rem;
                    overflow: hidden;
                    z-index: 1;
                }}
                .mod-setting-control-surface.locked {{
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0)),
                        rgba(7, 7, 10, 0.88) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.02),
                        inset 0 -1px 0 rgba(63, 63, 70, 0.3) !important;
                }}
                .mod-setting-main {{
                    min-width: 0;
                    align-self: stretch;
                    align-items: center;
                    justify-content: center;
                    text-align: center;
                }}
                .mod-setting-meta {{
                    min-width: 0;
                    align-self: stretch;
                    display: flex;
                    flex-direction: column;
                    justify-content: space-between;
                    align-items: flex-end;
                    gap: 0.16rem !important;
                    margin: 0;
                    padding: 0;
                }}
                .mod-setting-name {{
                    color: var(--mod-text) !important;
                    font-size: 0.94rem !important;
                    font-weight: 950 !important;
                    line-height: 1.2 !important;
                    text-align: center;
                }}
                .mod-setting-desc {{
                    color: var(--mod-muted) !important;
                    font-size: 0.81rem !important;
                    line-height: 1.3 !important;
                    text-align: center;
                }}
                .mod-setting-key {{
                    color: var(--mod-dim) !important;
                    font-size: 0.72rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.04em;
                    text-align: center;
                }}
                .mod-setting-field {{
                    width: 100%;
                    min-width: 0;
                    margin: 0 !important;
                }}
                .mod-setting-control-surface > .mod-setting-field + .mod-setting-field {{
                    border-top: 1px solid rgba(63, 63, 70, 0.38);
                }}
                .mod-setting-field .q-field {{
                    border-radius: 0 !important;
                }}
                .mod-setting-field .q-field__inner {{
                    row-gap: 0 !important;
                }}
                .mod-setting-field-primary {{
                    flex: 1 1 auto;
                    display: flex;
                    align-items: stretch;
                }}
                .mod-setting-field-primary .q-field,
                .mod-setting-field-primary .q-field__inner {{
                    height: 100%;
                }}
                .mod-setting-field .q-field__control {{
                    min-height: 2.18rem !important;
                    border: 0 !important;
                    border-radius: 0 !important;
                    background: transparent !important;
                    box-shadow: none !important;
                    transition:
                        border-color 120ms ease,
                        box-shadow 120ms ease,
                        background-color 120ms ease;
                }}
                .mod-setting-field-primary .q-field__control {{
                    min-height: 100% !important;
                    align-items: center !important;
                }}
                .mod-setting-field-secondary .q-field__control {{
                    min-height: 1.72rem !important;
                }}
                .mod-setting-field .q-field--filled .q-field__control {{
                    padding-left: 0.52rem;
                    padding-right: 0.46rem;
                }}
                .mod-setting-field .q-field--filled .q-field__control::before {{
                    left: 0;
                    right: 0;
                    border-bottom: 1px solid rgba(63, 63, 70, 0.2) !important;
                    opacity: 1 !important;
                    background: transparent !important;
                }}
                .mod-setting-control-surface > .mod-setting-field:last-child .q-field--filled .q-field__control::before {{
                    border-bottom-color: transparent !important;
                }}
                .mod-setting-field .q-field--filled .q-field__control::after {{
                    left: 0;
                    right: 0;
                    border-bottom: 2px solid var(--mod-accent-border-strong) !important;
                }}
                .mod-setting-field .q-field__native,
                .mod-setting-field .q-field__input,
                .mod-setting-field .q-field__marginal,
                .mod-setting-field .q-field__append,
                .mod-setting-field .q-field__prepend {{
                    color: var(--mod-text) !important;
                }}
                .mod-setting-field .q-field__native,
                .mod-setting-field .q-field__input {{
                    font-size: 0.82rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.02em;
                    line-height: 1.2 !important;
                }}
                .mod-setting-field-paragraph {{
                    width: 100%;
                    margin-right: 0 !important;
                    z-index: 1;
                }}
                .mod-setting-field-paragraph .q-field__control {{
                    height: 100% !important;
                    min-height: 100% !important;
                    max-height: 100% !important;
                    align-items: stretch !important;
                    padding-top: 0.08rem !important;
                    padding-bottom: 0 !important;
                    padding-right: 0 !important;
                }}
                .mod-setting-field-paragraph .q-field__control-container {{
                    height: 100% !important;
                    min-height: 100% !important;
                    display: flex !important;
                    align-items: stretch !important;
                }}
                .mod-setting-field-paragraph .q-field__native,
                .mod-setting-field-paragraph textarea.q-field__native {{
                    box-sizing: border-box !important;
                    height: 100% !important;
                    min-height: 100% !important;
                    max-height: 100% !important;
                    align-self: stretch !important;
                    line-height: 1.38 !important;
                    overflow-x: hidden !important;
                    overflow-y: auto !important;
                    padding-right: 0 !important;
                    padding-bottom: 0.04rem !important;
                    resize: none !important;
                    scrollbar-gutter: stable;
                }}
                .mod-setting-field-secondary .q-field__native,
                .mod-setting-field-secondary .q-field__input {{
                    font-size: 0.74rem !important;
                    font-weight: 800 !important;
                    color: var(--mod-muted) !important;
                }}
                .mod-setting-field-secondary .q-field__prefix {{
                    color: var(--mod-dim) !important;
                    font-size: 0.63rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                    padding-right: 0.38rem;
                }}
                .mod-setting-field .q-field__control-container {{
                    padding-top: 0 !important;
                }}
                .mod-setting-field .q-field__native::placeholder,
                .mod-setting-field input::placeholder {{
                    color: var(--mod-dim) !important;
                    opacity: 1;
                }}
                .mod-setting-field .q-field__append {{
                    padding-left: 0.12rem;
                }}
                .mod-setting-field .q-field__prepend {{
                    padding-right: 0.12rem;
                }}
                .mod-setting-field .q-icon {{
                    font-size: 0.95rem !important;
                    opacity: 0.68;
                }}
                .mod-setting-field:hover .q-field__control,
                .mod-setting-field .q-field--focused .q-field__control {{
                    border-color: transparent !important;
                    box-shadow: none !important;
                }}
                .mod-setting-field .q-field--disabled .q-field__control {{
                    background: transparent !important;
                    box-shadow: none !important;
                }}
                .mod-setting-field .q-field--disabled .q-field__native,
                .mod-setting-field .q-field--disabled .q-field__input,
                .mod-setting-field .q-field--disabled .q-field__marginal,
                .mod-setting-field .q-field--disabled .q-field__prefix {{
                    color: var(--mod-muted) !important;
                    opacity: 0.78 !important;
                }}
                .mod-setting-field input[type=number] {{
                    appearance: textfield;
                }}
                .mod-setting-field input[type=number]::-webkit-outer-spin-button,
                .mod-setting-field input[type=number]::-webkit-inner-spin-button {{
                    -webkit-appearance: none;
                    margin: 0;
                }}
                .mod-setting-menu,
                .mod-fake-chat-menu {{
                    border-radius: 0 !important;
                    border: 0 !important;
                    background: rgba(9, 9, 12, 0.98) !important;
                    box-shadow:
                        0 14px 34px rgba(0, 0, 0, 0.38),
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 var(--mod-accent-faint) !important;
                }}
                .mod-setting-menu .q-virtual-scroll__content,
                .mod-fake-chat-menu .q-virtual-scroll__content {{
                    padding: 0 !important;
                }}
                .mod-setting-menu .q-item,
                .mod-fake-chat-menu .q-item {{
                    min-height: 2rem;
                    padding: 0.35rem 0.7rem;
                    color: var(--mod-text) !important;
                    font-size: 0.76rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.02em;
                }}
                .mod-setting-menu .q-item:hover,
                .mod-fake-chat-menu .q-item:hover {{
                    background: rgba(63, 63, 70, 0.24) !important;
                }}
                .mod-setting-menu .q-item.q-manual-focusable--focused,
                .mod-setting-menu .q-item[aria-selected="true"],
                .mod-setting-menu .q-item--active,
                .mod-fake-chat-menu .q-item.q-manual-focusable--focused,
                .mod-fake-chat-menu .q-item[aria-selected="true"],
                .mod-fake-chat-menu .q-item--active {{
                    background: var(--mod-accent-glow) !important;
                    color: var(--mod-accent-text-strong) !important;
                }}
                .mod-setting-switch {{
                    padding: 0.1rem 0.25rem;
                    align-self: flex-start;
                    margin-left: -0.28rem;
                }}
                .mod-setting-switch .q-toggle__inner {{
                    color: var(--mod-accent) !important;
                }}
                .mod-setting-meta-value {{
                    color: var(--mod-text) !important;
                    font-size: 0.8rem !important;
                    font-weight: 900 !important;
                    line-height: 1.2 !important;
                    text-align: right;
                }}
                .mod-setting-meta-current {{
                    display: -webkit-box;
                    -webkit-box-orient: vertical;
                    -webkit-line-clamp: 3;
                    overflow: hidden;
                    word-break: break-word;
                    align-self: stretch;
                    margin: 0;
                }}
                .mod-setting-meta-default {{
                    color: var(--mod-dim) !important;
                    font-size: 0.72rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.04em;
                    line-height: 1.15 !important;
                    text-align: right;
                    text-transform: none;
                    white-space: nowrap;
                    margin: 0;
                }}
                .mod-setting-badge-rail {{
                    position: absolute;
                    top: -1px;
                    right: -1px;
                    bottom: -1px;
                    width: 2.4rem;
                    z-index: 2;
                    display: flex;
                    align-items: stretch;
                    justify-content: center;
                    margin: 0;
                    padding: 0;
                    overflow: hidden;
                    border-left: 1px solid var(--mod-accent-border);
                    background:
                        linear-gradient(180deg, rgba(127, 29, 29, 0.18), rgba(9, 9, 12, 0.08)),
                        rgba(13, 10, 18, 0.96);
                }}
                .mod-setting-badge {{
                    width: 100%;
                    height: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 0 !important;
                    margin: 0;
                    border-width: 0;
                    font-size: 0.62rem !important;
                    writing-mode: vertical-rl;
                    transform: none;
                }}
                .mod-setting-meta-secret,
                .mod-setting-meta-secret-cycle {{
                    position: relative;
                    align-self: stretch;
                    display: flex;
                    justify-content: flex-end;
                    min-height: 1rem;
                    margin: 0;
                    font-family: "IBM Plex Mono", "JetBrains Mono", "Fira Code", monospace;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                    isolation: isolate;
                    user-select: none;
                    -webkit-user-select: none;
                }}
                .mod-setting-meta-secret-cycle {{
                    overflow: hidden;
                }}
                .mod-setting-meta-secret-revealable {{
                    cursor: help;
                    outline: none;
                }}
                .mod-setting-meta-secret-revealable:focus-visible {{
                    box-shadow: 0 0 0 1px var(--mod-accent-border);
                }}
                .mod-setting-meta-secret-layer {{
                    display: block;
                    white-space: nowrap;
                    text-align: right;
                    will-change: transform, opacity, filter;
                }}
                .mod-setting-meta-secret-cycle-sizer {{
                    visibility: hidden;
                    white-space: nowrap;
                    text-align: right;
                }}
                .mod-setting-meta-secret-reveal {{
                    position: absolute;
                    inset: 0;
                    display: -webkit-box;
                    -webkit-box-orient: vertical;
                    -webkit-line-clamp: 3;
                    overflow: hidden;
                    opacity: 0;
                    z-index: 4;
                    color: var(--mod-text) !important;
                    text-align: right;
                    text-transform: none;
                    white-space: normal;
                    word-break: break-word;
                    transition: opacity 0.09s linear;
                    pointer-events: none;
                    user-select: none;
                    -webkit-user-select: none;
                }}
                .mod-setting-meta-secret-reveal-token {{
                    display: block;
                    -webkit-line-clamp: unset;
                    overflow: visible;
                    white-space: nowrap;
                    word-break: normal;
                    overflow-wrap: normal;
                    hyphens: manual;
                }}
                .mod-setting-meta-secret-cycle-token {{
                    position: absolute;
                    inset: 0;
                    display: block;
                    white-space: nowrap;
                    text-align: right;
                    color: #ece8ee !important;
                    opacity: 0;
                    z-index: 2;
                    will-change: transform, opacity, filter;
                    text-shadow:
                        -0.016em 0 0 rgba(138, 132, 137, 0.12),
                        0.016em 0 0 rgba(122, 120, 128, 0.12);
                    animation:
                        mod-setting-secret-cycle-main var(--mod-setting-secret-cycle-duration, 5.5s) linear infinite;
                    animation-delay: var(--mod-setting-secret-cycle-token-delay, var(--mod-setting-secret-cycle-delay, 0s));
                }}
                .mod-setting-meta-secret-cycle-token::before,
                .mod-setting-meta-secret-cycle-token::after {{
                    content: attr(data-text);
                    position: absolute;
                    inset: 0;
                    pointer-events: none;
                    opacity: 0;
                    will-change: transform, opacity, filter;
                    filter: blur(var(--mod-setting-secret-shadow-blur, 0.012rem));
                }}
                .mod-setting-meta-secret-cycle-token::before {{
                    color: rgba(147, 136, 139, var(--mod-setting-secret-shadow-opacity-a, 0.22)) !important;
                    animation:
                        mod-setting-secret-cycle-shadow-a var(--mod-setting-secret-cycle-duration, 5.5s) linear infinite;
                    animation-delay: var(--mod-setting-secret-cycle-token-delay, var(--mod-setting-secret-cycle-delay, 0s));
                }}
                .mod-setting-meta-secret-cycle-token::after {{
                    color: rgba(121, 118, 126, var(--mod-setting-secret-shadow-opacity-b, 0.2)) !important;
                    animation:
                        mod-setting-secret-cycle-shadow-b var(--mod-setting-secret-cycle-duration, 5.5s) linear infinite;
                    animation-delay: var(--mod-setting-secret-cycle-token-delay, var(--mod-setting-secret-cycle-delay, 0s));
                }}
                .mod-setting-meta-secret-main {{
                    position: relative;
                    color: #ece8ee !important;
                    z-index: 2;
                    text-shadow:
                        -0.022em 0 0 rgba(148, 118, 124, 0.18),
                        0.022em 0 0 rgba(123, 118, 143, 0.2);
                    animation:
                        mod-setting-secret-flicker var(--mod-setting-secret-flicker-duration, 2.25s) linear infinite;
                    animation-delay: var(--mod-setting-secret-flicker-delay, 0s);
                }}
                .mod-setting-meta-secret-shadow {{
                    position: absolute;
                    inset: 0;
                    pointer-events: none;
                    opacity: 0;
                    z-index: 1;
                    filter: blur(var(--mod-setting-secret-shadow-blur, 0.012rem));
                }}
                .mod-setting-meta-secret-shadow-a {{
                    color: rgba(150, 123, 129, var(--mod-setting-secret-shadow-opacity-a, 0.22)) !important;
                    animation:
                        mod-setting-secret-shift-a var(--mod-setting-secret-shift-duration-a, 1.9s) linear infinite;
                    animation-delay: var(--mod-setting-secret-shift-delay-a, 0s);
                }}
                .mod-setting-meta-secret-shadow-b {{
                    color: rgba(120, 116, 138, var(--mod-setting-secret-shadow-opacity-b, 0.2)) !important;
                    animation:
                        mod-setting-secret-shift-b var(--mod-setting-secret-shift-duration-b, 2.15s) linear infinite;
                    animation-delay: var(--mod-setting-secret-shift-delay-b, 0s);
                }}
                .mod-setting-meta-secret-revealable:hover .mod-setting-meta-secret-reveal,
                .mod-setting-meta-secret-revealable:focus-visible .mod-setting-meta-secret-reveal,
                .mod-setting-meta-secret-revealable:focus-within .mod-setting-meta-secret-reveal {{
                    opacity: 1;
                    pointer-events: auto;
                    user-select: text;
                    -webkit-user-select: text;
                }}
                .mod-setting-meta-secret-revealable:hover .mod-setting-meta-secret-layer,
                .mod-setting-meta-secret-revealable:focus-visible .mod-setting-meta-secret-layer,
                .mod-setting-meta-secret-revealable:focus-within .mod-setting-meta-secret-layer,
                .mod-setting-meta-secret-revealable:hover .mod-setting-meta-secret-cycle-token,
                .mod-setting-meta-secret-revealable:focus-visible .mod-setting-meta-secret-cycle-token,
                .mod-setting-meta-secret-revealable:focus-within .mod-setting-meta-secret-cycle-token {{
                    opacity: 0 !important;
                    animation-play-state: paused;
                }}
                .mod-setting-meta-secret-revealable:hover .mod-setting-meta-secret-cycle-token::before,
                .mod-setting-meta-secret-revealable:focus-visible .mod-setting-meta-secret-cycle-token::before,
                .mod-setting-meta-secret-revealable:focus-within .mod-setting-meta-secret-cycle-token::before,
                .mod-setting-meta-secret-revealable:hover .mod-setting-meta-secret-cycle-token::after,
                .mod-setting-meta-secret-revealable:focus-visible .mod-setting-meta-secret-cycle-token::after,
                .mod-setting-meta-secret-revealable:focus-within .mod-setting-meta-secret-cycle-token::after {{
                    opacity: 0 !important;
                    animation-play-state: paused;
                }}
                @keyframes mod-setting-secret-cycle-main {{
                    0%, 100% {{
                        opacity: 0;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                    0.8% {{
                        opacity: 0.78;
                        filter: blur(var(--mod-setting-secret-main-blur, 0.012rem));
                        transform: translate3d(
                            calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * -0.95),
                            calc(var(--mod-setting-secret-main-kick-y, 0.012rem) * -0.55),
                            0
                        );
                    }}
                    2.1% {{
                        opacity: 1;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                    21.2% {{
                        opacity: 1;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                    22.4% {{
                        opacity: 0.72;
                        filter: blur(var(--mod-setting-secret-main-blur, 0.012rem));
                        transform: translate3d(
                            var(--mod-setting-secret-main-kick-x, 0.036rem),
                            calc(var(--mod-setting-secret-main-kick-y, 0.012rem) * -1),
                            0
                        );
                    }}
                    23.2% {{
                        opacity: 0.94;
                        filter: none;
                        transform: translate3d(
                            calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * -0.52),
                            var(--mod-setting-secret-main-kick-y, 0.012rem),
                            0
                        );
                    }}
                    24.6% {{
                        opacity: 0;
                        filter: blur(var(--mod-setting-secret-main-blur, 0.012rem));
                        transform: translate3d(
                            calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * 0.38),
                            calc(var(--mod-setting-secret-main-kick-y, 0.012rem) * -0.35),
                            0
                        );
                    }}
                }}
                @keyframes mod-setting-secret-cycle-shadow-a {{
                    0%, 100% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    0.6% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-a, 0.22);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-a, 0.1rem) * -1),
                            var(--mod-setting-secret-shift-y-a, 0.014rem),
                            0
                        );
                    }}
                    1.8% {{
                        opacity: 0;
                        transform: translate3d(0.01rem, 0, 0);
                    }}
                    22.2% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-a, 0.22);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-a, 0.1rem) * 0.78),
                            calc(var(--mod-setting-secret-shift-y-a, 0.014rem) * -0.42),
                            0
                        );
                    }}
                    23.5% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                }}
                @keyframes mod-setting-secret-cycle-shadow-b {{
                    0%, 100% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    1.1% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-b, 0.2);
                        transform: translate3d(
                            var(--mod-setting-secret-shift-x-b, 0.11rem),
                            calc(var(--mod-setting-secret-shift-y-b, 0.016rem) * -1),
                            0
                        );
                    }}
                    2.3% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    21.8% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    22.9% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-b, 0.2);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-b, 0.11rem) * -0.72),
                            calc(var(--mod-setting-secret-shift-y-b, 0.016rem) * 0.72),
                            0
                        );
                    }}
                    24.1% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                }}
                @keyframes mod-setting-secret-flicker {{
                    0%, 11%, 100% {{
                        opacity: 0.96;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                    12% {{
                        opacity: 0.74;
                        filter: blur(var(--mod-setting-secret-main-blur, 0.012rem));
                        transform: translate3d(calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * -1), 0, 0);
                    }}
                    13% {{
                        opacity: 1;
                        filter: none;
                        transform: translate3d(
                            var(--mod-setting-secret-main-kick-x, 0.036rem),
                            calc(var(--mod-setting-secret-main-kick-y, 0.012rem) * -1),
                            0
                        );
                    }}
                    14% {{
                        opacity: 0.92;
                        transform: translate3d(0, 0, 0);
                    }}
                    43% {{
                        opacity: 0.96;
                    }}
                    44% {{
                        opacity: 0.7;
                        filter: blur(0.01rem);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * 0.72),
                            var(--mod-setting-secret-main-kick-y, 0.012rem),
                            0
                        );
                    }}
                    45% {{
                        opacity: 1;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                    71% {{
                        opacity: 0.96;
                    }}
                    72% {{
                        opacity: 0.68;
                        filter: blur(var(--mod-setting-secret-main-blur, 0.012rem));
                        transform: translate3d(
                            calc(var(--mod-setting-secret-main-kick-x, 0.036rem) * -0.58),
                            calc(var(--mod-setting-secret-main-kick-y, 0.012rem) * -1),
                            0
                        );
                    }}
                    73% {{
                        opacity: 1;
                        filter: none;
                        transform: translate3d(0, 0, 0);
                    }}
                }}
                @keyframes mod-setting-secret-shift-a {{
                    0%, 11%, 100% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    12% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-a, 0.22);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-a, 0.1rem) * -1),
                            var(--mod-setting-secret-shift-y-a, 0.014rem),
                            0
                        );
                    }}
                    13% {{
                        opacity: 0;
                        transform: translate3d(0.01rem, 0, 0);
                    }}
                    44% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-a, 0.22);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-a, 0.1rem) * 0.76),
                            calc(var(--mod-setting-secret-shift-y-a, 0.014rem) * -0.4),
                            0
                        );
                    }}
                    45% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    72% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-a, 0.22);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-a, 0.1rem) * -0.55),
                            calc(var(--mod-setting-secret-shift-y-a, 0.014rem) * -1),
                            0
                        );
                    }}
                    73% {{
                        opacity: 0;
                        transform: translate3d(0.01rem, 0, 0);
                    }}
                }}
                @keyframes mod-setting-secret-shift-b {{
                    0%, 11%, 100% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    12% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    13% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-b, 0.2);
                        transform: translate3d(
                            var(--mod-setting-secret-shift-x-b, 0.11rem),
                            calc(var(--mod-setting-secret-shift-y-b, 0.016rem) * -1),
                            0
                        );
                    }}
                    14% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    44% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    45% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-b, 0.2);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-b, 0.11rem) * -0.7),
                            calc(var(--mod-setting-secret-shift-y-b, 0.016rem) * 0.75),
                            0
                        );
                    }}
                    46% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    72% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                    73% {{
                        opacity: var(--mod-setting-secret-shadow-opacity-b, 0.2);
                        transform: translate3d(
                            calc(var(--mod-setting-secret-shift-x-b, 0.11rem) * 0.48),
                            var(--mod-setting-secret-shift-y-b, 0.016rem),
                            0
                        );
                    }}
                    74% {{
                        opacity: 0;
                        transform: translate3d(0, 0, 0);
                    }}
                }}
                .mod-setting-input-feedback {{
                    min-height: 0.95rem;
                    color: transparent !important;
                    font-size: 0.68rem !important;
                    font-weight: 800 !important;
                    line-height: 1.1 !important;
                    letter-spacing: 0.02em;
                    transition: color 120ms ease, transform 120ms ease;
                    transform: translateY(-1px);
                }}
                .mod-setting-input-feedback.active {{
                    color: var(--mod-negative-text) !important;
                    transform: translateY(0);
                }}
                .mod-setting-field-invalid .q-field__control {{
                    border-color: var(--mod-negative-border-strong) !important;
                    box-shadow:
                        0 0 0 1px var(--mod-negative-border),
                        0 0 18px var(--mod-negative-glow) !important;
                }}
                .mod-setting-field-shake .q-field__control {{
                    animation: mod-setting-invalid-shake 180ms ease;
                }}
                @keyframes mod-setting-invalid-shake {{
                    0% {{ transform: translateX(0); }}
                    25% {{ transform: translateX(-3px); }}
                    50% {{ transform: translateX(3px); }}
                    75% {{ transform: translateX(-2px); }}
                    100% {{ transform: translateX(0); }}
                }}
                .mod-config-editor,
                .mod-config-editor .cm-editor,
                .mod-config-editor .cm-scroller {{
                    width: 100% !important;
                    max-width: none !important;
                }}
                .mod-card-notepad {{
                    position: relative;
                    background:
                        linear-gradient(180deg, rgba(255, 255, 255, 0.035), rgba(255, 255, 255, 0)),
                        linear-gradient(135deg, rgba(10, 10, 14, 0.98), rgba(20, 13, 26, 0.98)) !important;
                    border-color: var(--mod-accent-border) !important;
                }}
                .mod-card-notepad.mod-card-plain {{
                    background: transparent !important;
                    border-color: transparent !important;
                    box-shadow: none !important;
                }}
                .mod-card-notepad .mod-tab-toolbar {{
                    align-items: stretch;
                    padding: 0.68rem 0.78rem;
                    border: 1px solid var(--mod-accent-glow);
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(8, 8, 12, 0.78) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 var(--mod-accent-faint) !important;
                }}
                .mod-card-notepad .mod-config-select .q-field__control {{
                    min-height: 3.05rem !important;
                    padding: 0 0.42rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(17, 17, 24, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 var(--mod-accent-faint),
                        0 10px 24px rgba(0, 0, 0, 0.24) !important;
                    transition:
                        border-color 120ms ease,
                        box-shadow 120ms ease,
                        background-color 120ms ease;
                }}
                .mod-card-notepad .mod-config-select:hover .q-field__control,
                .mod-card-notepad .mod-config-select .q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border-strong) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        var(--mod-accent-panel) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 var(--mod-accent-glow),
                        0 0 0 1px var(--mod-accent-glow),
                        0 12px 28px rgba(0, 0, 0, 0.28) !important;
                }}
                .mod-card-notepad .mod-config-select .q-field--outlined .q-field__control::before,
                .mod-card-notepad .mod-config-select .q-field--outlined .q-field__control::after {{
                    display: none !important;
                }}
                .mod-card-notepad .mod-config-select .q-field__label {{
                    color: #b9b5c5 !important;
                    font-size: 0.72rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                }}
                .mod-card-notepad .mod-config-select .q-field__label.q-field__label--focused,
                .mod-card-notepad .mod-config-select .q-field--float .q-field__label {{
                    color: var(--mod-accent-text-strong) !important;
                }}
                .mod-card-notepad .mod-config-select .q-field__native,
                .mod-card-notepad .mod-config-select .q-field__input,
                .mod-card-notepad .mod-config-select .q-field__append,
                .mod-card-notepad .mod-config-select .q-field__prepend,
                .mod-card-notepad .mod-config-select .q-field__marginal,
                .mod-card-notepad .mod-config-select .q-icon {{
                    color: #f4f4f5 !important;
                    opacity: 1 !important;
                }}
                .mod-card-notepad .mod-config-select .q-field__native,
                .mod-card-notepad .mod-config-select .q-field__input {{
                    font-size: 0.84rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.01em;
                }}
                .mod-card-notepad .mod-config-select input::placeholder {{
                    color: #d4d4d8 !important;
                    opacity: 1;
                }}
                .mod-notepad-menu {{
                    border-radius: 0 !important;
                    border: 1px solid var(--mod-accent-border) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(8, 8, 12, 0.985) !important;
                    box-shadow:
                        0 20px 42px rgba(0, 0, 0, 0.44),
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 var(--mod-accent-glow) !important;
                    backdrop-filter: blur(10px);
                }}
                .mod-notepad-menu .q-virtual-scroll__content {{
                    padding: 0.28rem 0 !important;
                }}
                .mod-notepad-menu .q-item {{
                    min-height: 2.3rem;
                    padding: 0.45rem 0.82rem;
                    color: #f4f4f5 !important;
                    font-size: 0.81rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.015em;
                }}
                .mod-notepad-menu .q-item + .q-item {{
                    border-top: 1px solid rgba(63, 63, 70, 0.36);
                }}
                .mod-notepad-menu .q-item__label,
                .mod-notepad-menu .q-item__section {{
                    color: inherit !important;
                }}
                .mod-notepad-menu .q-item:hover {{
                    background:
                        linear-gradient(90deg, var(--mod-accent-faint), transparent 78%),
                        rgba(39, 39, 42, 0.42) !important;
                }}
                .mod-notepad-menu .q-item.q-manual-focusable--focused,
                .mod-notepad-menu .q-item[aria-selected="true"],
                .mod-notepad-menu .q-item--active {{
                    background:
                        linear-gradient(90deg, var(--mod-accent-glow), transparent 88%),
                        rgba(91, 33, 182, 0.26) !important;
                    color: var(--mod-accent-text-strong) !important;
                    box-shadow: inset 3px 0 0 var(--mod-accent-border-strong);
                }}
                .mod-config-editor-shell {{
                    position: relative;
                }}
                .mod-config-editor {{
                    height: auto !important;
                    border: 1px solid #303038 !important;
                    background: #07070a !important;
                    overflow: visible !important;
                }}
                .mod-config-wrap-toggle {{
                    position: absolute;
                    top: 0.1rem;
                    right: 0.1rem;
                    z-index: 20;
                    padding: 0.25rem 0.45rem;
                    border: 1px solid rgba(63, 63, 70, 0.82);
                    background: rgba(9, 9, 13, 0.9);
                    color: var(--mod-text) !important;
                    backdrop-filter: blur(6px);
                    opacity: 0;
                    transform: translateY(-2px);
                    pointer-events: none;
                    transition: opacity 140ms ease, transform 140ms ease, border-color 140ms ease;
                }}
                .mod-config-editor-shell:hover .mod-config-wrap-toggle,
                .mod-config-editor-shell:focus-within .mod-config-wrap-toggle {{
                    opacity: 1;
                    transform: translateY(0);
                    pointer-events: auto;
                }}
                .mod-config-wrap-toggle:hover {{
                    border-color: var(--mod-accent-border);
                }}
                .mod-config-wrap-toggle .q-checkbox__label {{
                    font-size: 0.72rem !important;
                    font-weight: 800;
                    letter-spacing: 0.03em;
                    text-transform: uppercase;
                }}
                .mod-config-wrap-toggle .q-checkbox__inner {{
                    color: var(--mod-accent) !important;
                }}
                .mod-config-editor .cm-editor {{
                    min-height: 0 !important;
                    height: fit-content !important;
                    background: #07070a !important;
                    color: var(--mod-text) !important;
                }}
                .mod-config-editor .cm-scroller {{
                    height: auto !important;
                    max-height: none !important;
                    min-height: 0 !important;
                    overflow-x: auto !important;
                    overflow-y: visible !important;
                }}
                .mod-config-editor .cm-sizer,
                .mod-config-editor .cm-gutters {{
                    min-height: 0 !important;
                    height: auto !important;
                }}
                .mod-config-editor .cm-content,
                .mod-config-editor .cm-gutter {{
                    min-height: 0 !important;
                    font-family: "JetBrains Mono", "Fira Code", monospace !important;
                    font-size: 0.86rem !important;
                    line-height: 1.45 !important;
                }}
                .mod-config-editor .cm-content {{
                    caret-color: var(--mod-accent) !important;
                }}
                .mod-config-editor .cm-gutters {{
                    background: #050507 !important;
                    border-right: 1px solid #25252c !important;
                    color: #7c7f88 !important;
                }}
                .mod-config-editor .cm-activeLine,
                .mod-config-editor .cm-activeLineGutter {{
                    background: var(--mod-accent-faint) !important;
                }}
                .mod-config-editor .cm-focused {{
                    outline: 1px solid var(--mod-accent-border) !important;
                }}
                .mod-config-editor .cm-selectionBackground,
                .mod-config-editor .cm-focused .cm-selectionBackground,
                .mod-config-editor ::selection {{
                    background: var(--mod-accent-glow) !important;
                }}
                .mod-row {{
                    display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 0.75rem; align-items: center;
                    position: relative;
                    min-height: 4.25rem;
                    padding: 0.52rem 3.05rem 0.52rem 0.7rem;
                    border-radius: 0 !important;
                    background: #0b0b10 !important;
                    border: 1px solid #25252c !important;
                    box-shadow: inset 3px 0 0 var(--mod-accent-border);
                    transition: background 120ms ease, transform 120ms ease;
                }}
                .mod-row:hover {{
                    background: #101017 !important;
                    transform: translateX(1px);
                }}
                .mod-row.blocked {{
                    opacity: 0.72;
                    background: #08080b !important;
                    box-shadow: inset 3px 0 0 rgba(113, 113, 122, 0.5);
                }}
                .mod-row-disabled {{
                    border-color: rgba(113, 113, 122, 0.72) !important;
                    box-shadow:
                        inset 3px 0 0 rgba(113, 113, 122, 0.7),
                        inset 0 0 0 1px rgba(161, 161, 170, 0.12);
                }}
                .mod-row-clickable {{ cursor: pointer; }}
                .mod-virtual-mod-table {{
                    max-height: min(70vh, 54rem);
                    overflow: hidden;
                    border: none;
                    border-radius: 0;
                    background: transparent !important;
                }}
                .mod-virtual-mod-table .q-table__middle {{
                    overflow-x: hidden;
                    overflow-y: auto;
                }}
                .mod-virtual-mod-table .q-table {{
                    width: 100%;
                    max-width: 100%;
                    table-layout: fixed;
                }}
                .mod-virtual-mod-table .q-table,
                .mod-virtual-mod-table tbody {{
                    background: #050507 !important;
                }}
                .mod-virtual-mod-table .mod-virtual-row {{
                    background: transparent !important;
                    border: none !important;
                }}
                .mod-virtual-mod-table tbody tr.selected,
                .mod-virtual-mod-table tbody tr.selected > td,
                .mod-virtual-mod-table tbody tr:hover > td {{
                    background: transparent !important;
                }}
                .mod-virtual-mod-table tbody tr.selected > td::before,
                .mod-virtual-mod-table tbody tr.selected > td::after,
                .mod-virtual-mod-table tbody tr:hover > td::before {{
                    content: none !important;
                    background: transparent !important;
                    opacity: 0 !important;
                }}
                .mod-virtual-mod-table .mod-virtual-row-cell {{
                    height: auto !important;
                    padding: 0.2rem 0 !important;
                    border: none !important;
                    background: #050507 !important;
                    white-space: normal;
                }}
                .mod-virtual-mod-table .mod-row {{
                    width: 100%;
                    background: #0b0b10 !important;
                    background-image: none !important;
                }}
                .mod-virtual-mod-table .mod-row.blocked {{
                    background: #08080b !important;
                }}
                .mod-client-pack-required-table {{
                    max-height: 24rem;
                    overflow: hidden;
                    border: 1px solid var(--mod-border);
                    border-radius: 0.75rem;
                    background: rgba(9, 9, 11, 0.72) !important;
                }}
                .mod-client-pack-required-table .q-table__middle {{ overflow: auto; }}
                .mod-virtual-mod-table th {{
                    position: sticky;
                    top: 0;
                    z-index: 1;
                    background: #111114;
                    color: var(--mod-dim);
                }}
                .mod-list-pagination {{ margin: 0.75rem auto 0; color: var(--mod-text); }}
                .mod-row .q-checkbox__inner {{ color: var(--mod-accent) !important; }}
                .mod-row-main {{
                    width: 100%;
                    min-width: 0;
                    max-width: 100%;
                    overflow: hidden;
                }}
                .mod-row-title,
                .mod-row-file {{
                    display: block;
                    width: 100%;
                    min-width: 0;
                    max-width: 100%;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-row-title {{ color: var(--mod-text) !important; font-weight: 850; }}
                .mod-row-file {{ color: var(--mod-dim) !important; font-size: 0.78rem; }}
                .mod-row-meta {{ display: flex; justify-content: flex-end; gap: 0.35rem; flex-wrap: wrap; min-width: 12rem; }}
                .mod-pill {{
                    border-radius: 0 !important;
                    padding: 0.13rem 0.44rem;
                    font-size: 0.65rem;
                    font-weight: 900;
                    text-transform: uppercase;
                    letter-spacing: 0.055em;
                    border: 1px solid transparent;
                }}
                .mod-pill.enabled {{ background: #1f1f27 !important; color: #d4d4d8 !important; border-color: #3f3f46; }}
                .mod-pill.disabled {{ background: #24151a !important; color: #fca5a5 !important; border-color: #7f1d1d; }}
                .mod-pill.core {{ background: var(--mod-accent-surface) !important; color: var(--mod-accent-text) !important; border-color: var(--mod-accent); }}
                .mod-pill.blocked {{ background: #3a1117 !important; color: #fecaca !important; border-color: #b91c1c; }}
                .mod-pill.origin {{ background: #15151b !important; color: #a1a1aa !important; border-color: #303038; }}
                .mod-pill.size {{ background: #15151b !important; color: #d4d4d8 !important; border-color: #3f3f46; }}
                .mod-row-download {{
                    color: var(--mod-accent-text) !important;
                    font-weight: 950;
                    font-size: 0.82rem;
                    text-decoration: none !important;
                    text-transform: uppercase;
                    letter-spacing: 0.04em;
                }}
                .mod-row-download.q-btn {{
                    min-height: auto !important;
                    padding: 0 !important;
                    background: transparent !important;
                    box-shadow: none !important;
                }}
                .mod-row-download.q-btn .q-btn__content {{
                    color: inherit !important;
                }}
                .mod-row-download:hover {{ color: #fca5a5 !important; }}
                .mod-row-download.blocked {{ color: #52525b !important; pointer-events: none; }}
                .mod-badge {{
                    display: inline-flex;
                    align-items: center;
                    border-radius: 0 !important;
                    padding: 0.28rem 0.62rem !important;
                    font-size: 0.68rem !important;
                    font-weight: 950 !important;
                    text-transform: uppercase;
                    letter-spacing: 0.06em;
                    border: 1px solid #34343d;
                    background: #111118 !important;
                    color: var(--mod-text) !important;
                    text-decoration: none !important;
                }}
                .mod-badge-link {{
                    transition: filter 120ms ease, border-color 120ms ease;
                }}
                .mod-badge-link:hover {{
                    filter: brightness(1.08);
                }}
                .mod-badge.black {{ background: #050507 !important; border-color: #52525b; }}
                .mod-badge.purple {{ background: var(--mod-accent-dark) !important; border-color: var(--mod-accent); color: var(--mod-accent-text-strong) !important; }}
                .mod-badge.red {{ background: var(--mod-negative-dark) !important; border-color: var(--mod-negative); color: var(--mod-negative-text) !important; }}
                .mod-badge.warn {{ background: var(--mod-warning-dark) !important; border-color: var(--mod-warning); color: var(--mod-warning-text) !important; }}
                .mod-badge.grey {{ background: #18181f !important; border-color: #3f3f46; color: #d4d4d8 !important; }}
                .mod-badge .mod-app-activity-alert {{
                    color: #f87171 !important;
                }}
                .mod-badge-icon-label {{
                    gap: 0.34rem;
                }}
                .mod-badge-icon {{
                    font-size: 0.92rem !important;
                    line-height: 1 !important;
                }}
                .mod-badge-avatar {{
                    min-height: 1.8rem;
                    max-width: 100%;
                    padding: 0 !important;
                    overflow: hidden;
                }}
                .mod-badge-avatar-media {{
                    position: relative;
                    align-self: stretch;
                    flex: 0 0 1.8rem;
                    width: 1.8rem;
                    min-height: 1.8rem;
                    overflow: hidden;
                    border-right: 1px solid #3f3f46;
                }}
                .mod-badge-avatar-media > img {{
                    position: absolute;
                    inset: 0;
                    display: block;
                    width: 100%;
                    height: 100%;
                    object-fit: cover;
                }}
                .mod-badge-avatar-value {{
                    min-width: 0;
                    padding: 0.28rem 0.62rem;
                    white-space: normal;
                    overflow-wrap: anywhere;
                }}
                .mod-node-status-badge {{
                    justify-content: center;
                    min-width: 10.75rem;
                }}
                .mod-node-status-badge-actionable {{
                    cursor: pointer !important;
                    user-select: none;
                }}
                .mod-home-section-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(min(34rem, 100%), 1fr));
                    gap: 1.5rem;
                }}
                .mod-home-section {{
                    min-width: 0;
                    container-name: mod-home-section;
                    container-type: inline-size;
                }}
                .mod-home-section-header {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr) max-content;
                    align-items: center !important;
                    gap: 0.6rem 1rem !important;
                }}
                .mod-home-section-identity {{
                    min-width: 0;
                }}
                .mod-home-section-resource-badges {{
                    justify-content: flex-end;
                }}
                @container mod-home-section (max-width: 30rem) {{
                    .mod-home-section-header {{
                        grid-template-columns: minmax(0, 1fr);
                    }}
                    .mod-home-section-resource-badges {{
                        justify-content: flex-start;
                    }}
                }}
                .mod-home-section-avatar {{
                    width: 1.6rem;
                    height: 1.6rem;
                    min-width: 1.6rem;
                }}
                .mod-system-hero-avatar {{
                    width: 4rem;
                    height: 4rem;
                    min-width: 4rem;
                }}
                .mod-system-edge-badge-wrap {{
                    position: absolute !important;
                    inset: -1px auto auto -1px !important;
                    width: calc(100% + 2px) !important;
                    max-width: none !important;
                    margin: 0 !important;
                }}
                .mod-home-hero {{
                    padding: 0 !important;
                    gap: 0 !important;
                }}
                .mod-home-hero-actionable {{
                    cursor: pointer !important;
                    transition: border-color 140ms ease, box-shadow 140ms ease;
                }}
                .mod-home-hero-actionable:hover:not(:has(.mod-home-node-card:hover)) {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow: 0 0 0 1px var(--mod-accent-glow), 0 14px 34px rgba(0, 0, 0, 0.24) !important;
                }}
                :is(.mod-home-hero-actionable, .mod-home-node-card-actionable):focus {{
                    outline: none;
                }}
                html:not(.mod-pointer-navigation) :is(.mod-home-hero-actionable, .mod-home-node-card-actionable):focus-visible {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow: 0 0 0 1px var(--mod-accent-glow), 0 14px 34px rgba(0, 0, 0, 0.24) !important;
                    outline: 2px solid var(--mod-accent) !important;
                    outline-offset: 3px;
                }}
                .mod-home-edge-badge-wrap {{
                    position: relative !important;
                    inset: auto !important;
                    width: 100% !important;
                    max-width: 100% !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    box-sizing: border-box !important;
                    z-index: 2;
                    pointer-events: none;
                }}
                .mod-home-edge-badge-row {{
                    display: grid !important;
                    grid-template-columns: max-content minmax(0, 1fr);
                    width: 100%;
                    align-items: flex-start;
                    column-gap: 0.5rem;
                    row-gap: 0;
                    pointer-events: auto;
                }}
                .mod-home-app-count-badge {{
                    flex: 0 0 auto;
                    border-left: 0 !important;
                }}
                .mod-home-node-badge-list {{
                    display: flex !important;
                    width: 100%;
                    min-width: 0;
                    flex-wrap: wrap !important;
                    justify-content: flex-end;
                    column-gap: 0.5rem;
                    row-gap: 0;
                }}
                .mod-home-node-badge-list .mod-node-status-badge {{
                    min-width: min(10.75rem, 100%);
                    max-width: 100%;
                }}
                .mod-home-node-status-badge-text {{
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-home-hero-shell {{
                    padding-top: clamp(0.75rem, 2.5cqi, 1.5rem) !important;
                }}
                .mod-home-hero-header {{
                    display: flex !important;
                    justify-content: center !important;
                }}
                .mod-home-hero-header .mod-hero-header-main {{
                    flex: 0 1 auto;
                    width: 100%;
                    align-items: center;
                    text-align: center;
                }}
                .mod-home-hero-title {{
                    font-size: clamp(1.65rem, 5cqi, 3rem) !important;
                    line-height: 0.98 !important;
                    text-wrap: nowrap !important;
                    white-space: nowrap;
                }}
                .mod-home-capability-badges {{
                    display: flex !important;
                    width: 100% !important;
                    min-width: 0;
                    flex-direction: row !important;
                    flex-wrap: wrap !important;
                    align-items: center;
                    justify-content: center;
                    gap: 0.5rem;
                    padding-top: 0.5rem;
                }}
                .mod-system-edge-badge-row {{
                    width: 100%;
                    flex-wrap: wrap;
                    gap: 0.5rem;
                }}
                .mod-system-hero-shell {{
                    gap: 0.5rem !important;
                    padding-top: 1.9rem !important;
                    padding-bottom: 0.75rem !important;
                }}
                .mod-system-hero-header {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
                    align-items: start;
                    gap: 0.5rem 1.5rem;
                    width: 100%;
                }}
                .mod-system-hero-identity {{
                    display: flex !important;
                    min-width: 0;
                    flex-wrap: nowrap !important;
                }}
                .mod-system-scope-slot {{
                    min-width: 0;
                    max-width: 38rem;
                    align-items: flex-end;
                    justify-self: end;
                }}
                .mod-system-scope-badges {{
                    display: flex;
                    width: 100%;
                    flex-wrap: wrap;
                    justify-content: flex-end;
                    gap: 0.5rem;
                }}
                .mod-system-operational-signals {{
                    padding-top: 0.5rem;
                    border-top: 1px solid rgba(82, 82, 91, 0.56);
                }}
                @container mod-app-hero (max-width: 52rem) {{
                    .mod-system-hero-header {{
                        grid-template-columns: minmax(0, 1fr);
                    }}
                    .mod-system-scope-slot {{
                        max-width: 100%;
                        align-items: flex-start;
                        justify-self: start;
                    }}
                    .mod-system-scope-badges {{
                        justify-content: flex-start;
                    }}
                }}
                @container mod-app-hero (max-width: 34rem) {{
                    .mod-system-hero-shell {{
                        padding-top: 4.8rem !important;
                    }}
                }}
                .mod-home-node-grid {{
                    display: grid !important;
                    grid-template-columns: repeat(auto-fit, minmax(min(19rem, 100%), 1fr));
                    align-items: stretch;
                    gap: 0.75rem !important;
                }}
                .mod-home-node-card {{
                    width: 100%;
                    min-width: 0;
                    border-radius: 0 !important;
                    background: rgba(10, 10, 14, 0.86) !important;
                    border: 1px solid #2f2f37 !important;
                    box-shadow: none !important;
                }}
                .mod-home-node-card-black {{
                    border-color: #4b5563 !important;
                }}
                .mod-home-node-card-purple {{
                    border-color: var(--mod-accent-border-strong) !important;
                }}
                .mod-home-node-card-red {{
                    border-color: var(--mod-negative-border-strong) !important;
                }}
                .mod-home-node-card-warn {{
                    border-color: var(--mod-warning) !important;
                }}
                .mod-home-node-card-grey {{
                    border-color: #3f3f46 !important;
                }}
                .mod-home-node-card-actionable {{
                    cursor: pointer !important;
                    transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease, transform 140ms ease;
                }}
                .mod-home-node-card-actionable:hover {{
                    background: rgba(20, 18, 30, 0.94) !important;
                    border-color: var(--mod-accent-text) !important;
                    transform: translateY(-1px);
                }}
                .mod-home-node-title {{
                    color: var(--mod-text) !important;
                    font-size: 0.98rem !important;
                    font-weight: 900 !important;
                    line-height: 1.1 !important;
                }}
                .mod-home-node-subtitle {{
                    color: var(--mod-muted) !important;
                    font-size: 0.74rem !important;
                    font-weight: 700 !important;
                    letter-spacing: 0.04em;
                    text-transform: uppercase;
                }}
                .mod-home-node-metrics {{
                    display: flex;
                    flex-wrap: wrap;
                    align-items: stretch;
                    gap: 0.55rem 1rem;
                }}
                .mod-home-node-metric {{
                    min-width: 0;
                    max-width: 100%;
                    align-items: center;
                    gap: 0.55rem;
                    flex: 0 1 auto;
                    flex-wrap: nowrap !important;
                }}
                .mod-home-node-metric-icon,
                .mod-home-node-running-icon {{
                    font-size: 1rem !important;
                    flex: 0 0 auto;
                }}
                .mod-home-node-metric-icon.mod-tone-black,
                .mod-home-node-running-icon.mod-tone-black {{
                    color: #9ca3af !important;
                }}
                .mod-home-node-metric-icon.mod-tone-purple,
                .mod-home-node-running-icon.mod-tone-purple {{
                    color: var(--mod-accent-text) !important;
                }}
                .mod-home-node-metric-icon.mod-tone-red,
                .mod-home-node-running-icon.mod-tone-red {{
                    color: var(--mod-negative-text) !important;
                }}
                .mod-home-node-metric-icon.mod-tone-warn,
                .mod-home-node-running-icon.mod-tone-warn {{
                    color: var(--mod-warning-text) !important;
                }}
                .mod-home-node-metric-icon.mod-tone-grey,
                .mod-home-node-running-icon.mod-tone-grey {{
                    color: #a1a1aa !important;
                }}
                .mod-home-node-metric-value {{
                    color: var(--mod-text) !important;
                    font-size: 0.82rem !important;
                    font-weight: 850 !important;
                    line-height: 1.2 !important;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-home-node-running {{
                    min-width: 0;
                    align-items: center;
                    gap: 0.55rem;
                    padding-top: 0.3rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.45);
                    flex-wrap: nowrap !important;
                }}
                .mod-home-node-running-value {{
                    color: var(--mod-muted) !important;
                    font-size: 0.8rem !important;
                    font-weight: 750 !important;
                    line-height: 1.2 !important;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                @container mod-app-hero (max-width: 24rem) {{
                    .mod-home-edge-badge-row {{
                        column-gap: 0.35rem;
                    }}
                    .mod-home-node-badge-list {{
                        column-gap: 0.35rem;
                    }}
                }}
                .mod-stat-grid {{ align-items: stretch; }}
                .mod-stat-card {{
                    border-radius: 0 !important;
                    background: rgba(10, 10, 14, 0.86) !important;
                    border: 1px solid #2f2f37 !important;
                    box-shadow: none !important;
                }}
                .mod-stat-card.black {{ border-color: #4b5563 !important; }}
                .mod-stat-card.purple {{ border-color: var(--mod-accent-border-strong) !important; }}
                .mod-stat-card.red {{ border-color: var(--mod-negative-border-strong) !important; }}
                .mod-stat-card.warn {{ border-color: var(--mod-warning-border-strong) !important; }}
                .mod-stat-card.grey {{ border-color: #3f3f46 !important; }}
                .mod-system-hero-shell .mod-stat-card {{
                    border-color: #2f2f37 !important;
                }}
                .mod-stat-label {{
                    color: var(--mod-dim) !important;
                    font-size: 0.62rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                }}
                .mod-stat-value {{
                    color: var(--mod-text) !important;
                    font-size: 0.88rem !important;
                    font-weight: 900 !important;
                    line-height: 1.2 !important;
                }}
                .mod-stat-line {{
                    padding-top: 0.1rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.45);
                }}
                .mod-stat-line:first-child {{
                    padding-top: 0;
                    border-top: 0;
                }}
                .mod-stat-line-label {{
                    color: var(--mod-muted) !important;
                    font-size: 0.74rem !important;
                    font-weight: 850 !important;
                    text-transform: uppercase;
                    letter-spacing: 0.04em;
                    flex: 0 0 auto;
                }}
                .mod-stat-line-value {{
                    color: var(--mod-text) !important;
                    font-size: 0.84rem !important;
                    font-weight: 900 !important;
                    line-height: 1.2 !important;
                }}
                .mod-stat-tone-black,
                .mod-stat-line:has(.mod-stat-tone-black) .mod-stat-line-label,
                .mod-stat-section:has(.mod-stat-tone-black) .mod-stat-section-label {{
                    color: #d4d4d8 !important;
                }}
                .mod-stat-tone-grey,
                .mod-stat-line:has(.mod-stat-tone-grey) .mod-stat-line-label,
                .mod-stat-section:has(.mod-stat-tone-grey) .mod-stat-section-label {{
                    color: #a1a1aa !important;
                }}
                .mod-stat-tone-purple,
                .mod-stat-line:has(.mod-stat-tone-purple) .mod-stat-line-label,
                .mod-stat-section:has(.mod-stat-tone-purple) .mod-stat-section-label {{
                    color: var(--mod-accent-text) !important;
                }}
                .mod-stat-tone-warn,
                .mod-stat-line:has(.mod-stat-tone-warn) .mod-stat-line-label,
                .mod-stat-section:has(.mod-stat-tone-warn) .mod-stat-section-label {{
                    color: #fca5a5 !important;
                }}
                .mod-stat-tone-red,
                .mod-stat-line:has(.mod-stat-tone-red) .mod-stat-line-label,
                .mod-stat-section:has(.mod-stat-tone-red) .mod-stat-section-label {{
                    color: #fecaca !important;
                }}
                .mod-stat-section {{
                    padding-top: 0.42rem;
                    border-top: 1px solid rgba(113, 113, 122, 0.55);
                }}
                .mod-stat-section:first-child {{
                    padding-top: 0;
                    border-top: 0;
                }}
                .mod-stat-section-label {{
                    color: var(--mod-dim) !important;
                    font-size: 0.62rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                }}
                .mod-stat-section-value {{
                    color: var(--mod-text) !important;
                    font-size: 0.88rem !important;
                    font-weight: 900 !important;
                    line-height: 1.2 !important;
                }}
                .mod-system-chart-shell {{
                    min-height: 12rem;
                    overflow: hidden;
                    border: 1px solid rgba(63, 63, 70, 0.62);
                    background: rgba(5, 5, 8, 0.72);
                }}
                .mod-system-chart {{
                    display: block;
                    width: 100%;
                    height: auto;
                    min-height: 12rem;
                }}
                .mod-system-chart-grid line {{
                    stroke: rgba(113, 113, 122, 0.2);
                    stroke-width: 1;
                }}
                .mod-system-chart-grid text,
                .mod-system-chart-axis-labels text,
                .mod-system-chart-legend text {{
                    fill: var(--mod-muted);
                    font-size: 11px;
                    font-weight: 750;
                }}
                .mod-system-chart-line {{
                    fill: none;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                    vector-effect: non-scaling-stroke;
                }}
                .mod-system-chart-line-enter {{
                    stroke-dasharray: 1;
                    stroke-dashoffset: 1;
                    animation: mod-system-chart-draw 720ms var(--mod-motion-ease) forwards;
                }}
                @keyframes mod-system-chart-draw {{
                    to {{ stroke-dashoffset: 0; }}
                }}
                .mod-system-chart-empty {{
                    display: flex;
                    min-height: 12rem;
                    align-items: center;
                    justify-content: center;
                    padding: 2rem;
                    color: var(--mod-muted);
                    font-size: 0.82rem;
                    font-weight: 750;
                    text-align: center;
                }}
                .mod-system-log-output {{
                    min-height: 18rem;
                    max-height: min(58dvh, 44rem);
                    overflow: auto;
                    overscroll-behavior: contain;
                    border: 1px solid rgba(113, 113, 122, 0.42);
                    background: rgba(5, 5, 8, 0.72);
                }}
                .mod-system-log-selectors {{
                    align-items: stretch;
                    flex-wrap: wrap;
                }}
                .mod-system-log-selector {{
                    flex: 1 1 16rem;
                    min-width: min(16rem, 100%);
                }}
                .mod-system-log-load-button {{
                    flex: 0 0 auto;
                    align-self: end;
                    min-height: 2.5rem;
                }}
                .mod-system-log-empty {{
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 2rem;
                    color: var(--mod-muted);
                    font-size: 0.82rem;
                    font-weight: 750;
                    text-align: center;
                }}
                .mod-system-log-event {{
                    position: relative;
                    display: grid;
                    grid-template-columns: minmax(4.75rem, auto) minmax(0, 1fr);
                    gap: 0.75rem;
                    align-items: start;
                    padding: 0.72rem 0.85rem;
                    border-left: 3px solid rgba(161, 161, 170, 0.54);
                    border-bottom: 1px solid rgba(82, 82, 91, 0.3);
                    isolation: isolate;
                    transition: background-color 160ms var(--mod-motion-ease);
                }}
                .mod-system-log-event > * {{
                    position: relative;
                    z-index: 1;
                }}
                .mod-system-log-event:hover {{
                    background: rgba(255, 255, 255, 0.045);
                }}
                .mod-system-log-event-error {{
                    border-left-color: #f87171;
                }}
                .mod-system-log-event-warn {{
                    border-left-color: #fbbf24;
                }}
                .mod-system-log-event-info {{
                    border-left-color: #60a5fa;
                }}
                :is(.mod-system-log-event-error, .mod-system-log-event-warn)::before {{
                    content: "";
                    position: absolute;
                    top: 0;
                    bottom: 0;
                    left: 0;
                    width: calc(3px + 0.85rem + 4.75rem + 0.75rem);
                    pointer-events: none;
                }}
                .mod-system-log-event-error::before {{
                    background: linear-gradient(90deg, rgba(127, 29, 29, 0.16), rgba(127, 29, 29, 0.015));
                }}
                .mod-system-log-event-warn::before {{
                    background: linear-gradient(90deg, rgba(146, 64, 14, 0.14), rgba(146, 64, 14, 0.015));
                }}
                .mod-system-log-level {{
                    min-width: 4.55rem;
                    padding: 0.15rem 0.35rem;
                    border: 1px solid rgba(161, 161, 170, 0.48);
                    color: var(--mod-muted);
                    font-family: "IBM Plex Mono", "JetBrains Mono", "Fira Code", monospace;
                    font-size: 0.66rem;
                    font-weight: 850;
                    letter-spacing: 0.055em;
                    line-height: 1.2;
                    text-align: center;
                }}
                .mod-system-log-meta {{
                    display: flex;
                    flex-direction: column;
                    gap: 0.35rem;
                    align-items: stretch;
                    min-width: 4.75rem;
                    padding: 0.08rem 0.2rem 0.08rem 0;
                }}
                .mod-system-log-event-error .mod-system-log-level {{
                    border-color: rgba(248, 113, 113, 0.7);
                    color: #fecaca;
                }}
                .mod-system-log-event-warn .mod-system-log-level {{
                    border-color: rgba(251, 191, 36, 0.7);
                    color: #fde68a;
                }}
                .mod-system-log-time {{
                    color: var(--mod-muted);
                    font-family: "IBM Plex Mono", "JetBrains Mono", "Fira Code", monospace;
                    font-size: 0.68rem;
                    line-height: 1.35;
                    text-align: center;
                    white-space: nowrap;
                }}
                .mod-system-log-event-body {{
                    min-width: 0;
                }}
                .mod-system-log-message {{
                    color: var(--mod-text);
                    font-size: 0.81rem;
                    font-weight: 650;
                    line-height: 1.4;
                    overflow-wrap: anywhere;
                    white-space: pre-wrap;
                }}
                .mod-system-log-context {{
                    margin-top: 0.22rem;
                    color: var(--mod-muted);
                    font-family: "IBM Plex Mono", "JetBrains Mono", "Fira Code", monospace;
                    font-size: 0.67rem;
                    line-height: 1.3;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-system-danger-zone {{
                    border: 1px solid var(--mod-negative-border-strong);
                    background: linear-gradient(135deg, rgba(30, 18, 42, 0.88), rgba(11, 10, 15, 0.94));
                }}
                .mod-system-schedule-section-title {{
                    width: 100%;
                    margin-top: 0.25rem;
                    padding-top: 1rem;
                    border-top: 1px solid var(--mod-border);
                }}
                .mod-system-schedule-disable {{
                    color: var(--mod-negative-text) !important;
                    background: #1b1520 !important;
                    border-color: var(--mod-negative-border-strong) !important;
                    box-shadow: inset 0 0 0 1px var(--mod-negative-glow) !important;
                }}
                .mod-system-disk-select {{
                    flex: 1 1 18rem;
                    min-width: min(18rem, 100%);
                }}
                .mod-system-disk-property-row {{
                    display: grid;
                    grid-template-columns: minmax(10rem, 1fr) auto minmax(12rem, 0.8fr);
                    align-items: end;
                    gap: 0.75rem;
                    width: 100%;
                    padding: 0.75rem;
                    border: 1px solid rgba(82, 82, 91, 0.62);
                    background: rgba(8, 8, 12, 0.72);
                }}
                .mod-system-disk-property-identity {{
                    align-self: center;
                }}
                .mod-system-disk-mountpoint {{
                    overflow-wrap: anywhere;
                }}
                .mod-system-disk-label-field {{
                    min-width: 0;
                }}
                @media (max-width: 42rem) {{
                    .mod-system-disk-property-row {{
                        grid-template-columns: 1fr auto;
                    }}
                    .mod-system-disk-label-field {{
                        grid-column: 1 / -1;
                    }}
                }}
                .mod-system-schedule-row {{
                    padding: 1rem;
                    border: 1px solid var(--mod-border);
                    background: rgba(8, 8, 10, 0.72);
                }}
                .mod-system-schedule-row + .mod-system-schedule-row {{
                    margin-top: 0.25rem;
                }}
                .mod-system-schedule-controls {{
                    display: grid !important;
                    grid-template-columns: repeat(6, minmax(0, 1fr));
                    align-items: end;
                }}
                .mod-system-schedule-timezone {{
                    min-width: 0;
                }}
                .mod-system-schedule-field {{
                    height: 2.75rem;
                    min-height: 2.75rem;
                }}
                .mod-system-schedule-field .q-field__control {{
                    height: 2.75rem !important;
                    min-height: 2.75rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.72) !important;
                    border-radius: 0 !important;
                    background: rgba(12, 12, 17, 0.94) !important;
                    color-scheme: dark;
                }}
                .mod-system-schedule-field .q-field__control::before {{
                    border: 0 !important;
                }}
                .mod-system-schedule-field .q-field__control::after {{
                    border-bottom: 2px solid var(--mod-accent) !important;
                }}
                .mod-system-schedule-field .q-field__label {{
                    color: var(--mod-muted) !important;
                    font-size: 0.72rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.04em;
                }}
                .mod-system-schedule-field .q-field__native,
                .mod-system-schedule-field .q-field__input,
                .mod-system-schedule-field .q-field__marginal,
                .mod-system-schedule-field .q-icon {{
                    color: var(--mod-text) !important;
                    opacity: 1 !important;
                }}
                .mod-system-schedule-field .q-field__marginal {{
                    height: 2.75rem !important;
                }}
                .mod-system-schedule-field .q-field__native,
                .mod-system-schedule-field .q-field__input {{
                    font-size: 0.82rem !important;
                    font-weight: 800 !important;
                    color-scheme: dark;
                }}
                .mod-system-schedule-time input {{
                    color: var(--mod-text) !important;
                    background: transparent !important;
                    color-scheme: dark;
                }}
                @media (max-width: 56rem) {{
                    .mod-system-schedule-controls {{
                        grid-template-columns: repeat(3, minmax(0, 1fr));
                    }}
                }}
                @media (max-width: 36rem) {{
                    .mod-system-schedule-controls {{
                        grid-template-columns: 1fr;
                    }}
                }}
                .mod-system-action-row {{
                    padding-top: 0.9rem;
                    border-top: 1px solid rgba(127, 29, 29, 0.52);
                }}
                .mod-dialog-card {{
                    width: min(30rem, calc(100vw - 2rem)) !important;
                    max-width: none !important;
                    max-height: calc(100vh - 1.5rem);
                    max-height: calc(100dvh - 1.5rem - env(safe-area-inset-top) - env(safe-area-inset-bottom));
                    position: relative;
                    isolation: isolate;
                    overflow-x: hidden;
                    overflow-y: auto;
                    overscroll-behavior: contain;
                    scroll-padding-block: 0.75rem;
                    -webkit-overflow-scrolling: touch;
                    scrollbar-color: var(--mod-accent-border) rgba(9, 9, 13, 0.88);
                    transition: border-color 160ms ease, box-shadow 160ms ease;
                }}
                .mod-dialog-card:focus-within {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow:
                        0 24px 70px rgba(0, 0, 0, 0.52),
                        0 0 0 1px var(--mod-accent-faint),
                        inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
                }}
                .mod-dialog-card > .nicegui-content {{
                    width: 100%;
                    min-width: 0;
                    max-width: 100%;
                }}
                @media (max-width: 36rem) {{
                    .mod-dialog-card {{
                        width: calc(100vw - 0.75rem) !important;
                        max-height: calc(100vh - 0.75rem);
                        max-height: calc(
                            100dvh - 0.75rem - env(safe-area-inset-top) - env(safe-area-inset-bottom)
                        );
                    }}
                }}
                .mod-client-pack-dialog-card {{
                    width: min(72rem, calc(100vw - 2rem)) !important;
                    max-height: min(52rem, calc(100vh - 1.5rem));
                    overflow: hidden;
                }}
                .mod-client-pack-dialog-card > .nicegui-content,
                .mod-client-pack-dialog-card .nicegui-content {{
                    width: 100%;
                    padding: 0 !important;
                }}
                .mod-client-pack-body {{
                    gap: 0;
                    padding: 1.2rem 1.25rem 1rem;
                    max-height: min(52rem, calc(100vh - 1.5rem));
                    overflow-y: auto;
                }}
                .mod-client-pack-header {{
                    gap: 0.18rem;
                    padding-bottom: 0.95rem;
                }}
                .mod-client-pack-config-layout {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
                    align-items: stretch;
                    gap: 1rem;
                }}
                .mod-client-pack-config-column {{
                    display: flex;
                    flex-direction: column;
                    min-width: 0;
                    gap: 0;
                    align-self: stretch;
                    overflow: visible;
                }}
                .mod-client-pack-config-column-left {{
                    min-height: 0;
                }}
                .mod-client-pack-config-column-right {{
                    min-height: 0;
                }}
                .mod-client-pack-config-mods-section {{
                    display: grid !important;
                    flex: 1 1 auto;
                    grid-template-rows: auto auto minmax(0, 1fr);
                    align-content: start;
                    justify-content: stretch;
                    align-items: stretch;
                    gap: 0.32rem;
                    height: 100%;
                    min-height: 0;
                }}
                .mod-client-pack-config-mod-list {{
                    display: grid !important;
                    grid-template-rows: auto minmax(0, 1fr);
                    align-content: start;
                    justify-content: stretch;
                    align-items: stretch;
                    gap: 0.4rem;
                    flex: 1 1 auto;
                    min-height: 0;
                    overflow: hidden;
                }}
                .mod-client-pack-config-mod-list > .mod-client-pack-option-list {{
                    margin-top: 0;
                    display: flex !important;
                    flex: 1 1 auto;
                    flex-direction: column;
                    justify-content: flex-start;
                    align-items: stretch;
                    align-self: stretch;
                    min-height: 0;
                    max-height: none;
                    overflow-y: auto;
                    padding-right: 0.15rem;
                }}
                .mod-client-pack-config-column > .mod-client-pack-section:first-child {{
                    padding-top: 0;
                    border-top: 0;
                }}
                .mod-client-pack-section {{
                    gap: 0.42rem;
                    padding: 0.85rem 0;
                    border-top: 1px solid rgba(82, 82, 91, 0.52);
                }}
                .mod-client-pack-section-hint {{
                    margin-top: -0.2rem;
                    font-size: 0.78rem;
                    line-height: 1.35;
                }}
                .mod-client-pack-release-section {{
                    gap: 0.65rem;
                }}
                .mod-client-pack-release-versions {{
                    gap: 1.5rem;
                }}
                .mod-client-pack-release-version {{
                    gap: 0.15rem;
                }}
                .mod-client-pack-changelog-block {{
                    gap: 0.4rem;
                }}
                .mod-client-pack-changelog-hint {{
                    margin-top: 0;
                }}
                .mod-client-pack-publish-reasons {{
                    gap: 0.38rem;
                }}
                .mod-client-pack-publish-reason {{
                    padding-left: 0.9rem;
                    color: var(--mod-text) !important;
                    font-size: 0.82rem;
                    line-height: 1.45;
                    position: relative;
                    white-space: pre-wrap;
                }}
                .mod-client-pack-publish-reason::before {{
                    content: "•";
                    position: absolute;
                    left: 0;
                    color: var(--mod-accent);
                    font-weight: 900;
                }}
                .mod-client-pack-option-list,
                .mod-client-pack-choice-list {{
                    gap: 0.38rem;
                }}
                .mod-client-pack-option,
                .mod-client-pack-checkbox,
                .mod-client-pack-choice {{
                    min-height: 2.65rem;
                    margin: 0 !important;
                    border: 1px solid #2b2b33;
                    border-radius: 0 !important;
                    background: linear-gradient(135deg, rgba(11, 11, 16, 0.96), rgba(18, 15, 23, 0.96));
                    color: var(--mod-text) !important;
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                }}
                .mod-client-pack-option {{
                    justify-content: space-between;
                    gap: 0.75rem;
                    padding: 0.5rem 0.65rem;
                }}
                .mod-client-pack-option-label {{
                    min-width: 0;
                    color: var(--mod-text) !important;
                    font-size: 0.88rem;
                    font-weight: 800;
                    overflow-wrap: anywhere;
                }}
                .mod-client-pack-config-search {{
                    margin-bottom: 0.75rem;
                }}
                .mod-client-pack-config-search .q-field__native,
                .mod-client-pack-config-search .q-field__input,
                .mod-client-pack-config-search .q-field__label,
                .mod-client-pack-config-search .q-field__marginal,
                .mod-client-pack-config-search .q-icon {{
                    color: var(--mod-text) !important;
                }}
                .mod-client-pack-config-search .q-field__native::placeholder,
                .mod-client-pack-config-search .q-field__input::placeholder {{
                    color: var(--mod-muted) !important;
                    opacity: 1;
                }}
                .mod-client-pack-config-option {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr) minmax(0, 8.5rem);
                    align-items: center;
                    overflow: hidden;
                    box-sizing: border-box;
                }}
                .mod-client-pack-config-option-alt {{
                    grid-template-columns: minmax(0, 1fr) minmax(0, 7.5rem) minmax(0, 8rem);
                }}
                .mod-client-pack-config-control {{
                    width: 100%;
                    max-width: 100%;
                    min-width: 0;
                    flex: none;
                }}
                .mod-client-pack-config-control.mod-config-select,
                .mod-client-pack-config-control.mod-config-input {{
                    flex: 0 1 auto;
                    min-width: 0;
                    max-width: 100%;
                }}
                .mod-client-pack-config-group {{
                    grid-column: 2;
                }}
                .mod-client-pack-config-policy {{
                    grid-column: 2;
                }}
                .mod-client-pack-config-option-alt .mod-client-pack-config-policy {{
                    grid-column: 3;
                }}
                .mod-client-pack-config-control .q-field__control {{
                    max-width: 100%;
                    min-width: 0 !important;
                }}
                .mod-client-pack-config-invalid .q-field__control {{
                    border-color: rgba(248, 113, 113, 0.96) !important;
                    box-shadow:
                        0 0 0 1px rgba(248, 113, 113, 0.52),
                        0 0 0.8rem rgba(239, 68, 68, 0.24) !important;
                }}
                .mod-client-pack-config-invalid .q-field__native,
                .mod-client-pack-config-invalid .q-field__input,
                .mod-client-pack-config-invalid .q-field__label,
                .mod-client-pack-config-invalid .q-icon {{
                    color: #fca5a5 !important;
                }}
                @media (max-width: 70rem) {{
                    .mod-client-pack-config-layout {{
                        grid-template-columns: minmax(0, 1fr);
                    }}
                    .mod-client-pack-config-column-left,
                    .mod-client-pack-config-column-right {{
                        flex-basis: 100%;
                    }}
                    .mod-client-pack-config-mods-section,
                    .mod-client-pack-config-mod-list {{
                        height: auto;
                    }}
                    .mod-client-pack-config-mod-list > .mod-client-pack-option-list {{
                        max-height: min(34rem, calc(100vh - 14rem));
                    }}
                }}
                @media (max-width: 54rem) {{
                    .mod-client-pack-config-option {{
                        grid-template-columns: minmax(0, 1fr);
                    }}
                    .mod-client-pack-config-group,
                    .mod-client-pack-config-policy {{
                        grid-column: 1;
                    }}
                }}
                .mod-client-pack-checkbox {{
                    padding: 0.38rem 0.55rem;
                }}
                .mod-client-pack-checkbox .q-checkbox__label {{
                    color: var(--mod-text) !important;
                    font-size: 0.88rem;
                    font-weight: 800;
                    line-height: 1.3;
                }}
                .mod-client-pack-checkbox .q-checkbox__inner {{
                    color: rgba(228, 228, 231, 0.78) !important;
                }}
                .mod-client-pack-checkbox .q-checkbox__inner--truthy {{
                    color: var(--mod-accent) !important;
                }}
                .mod-client-pack-checkbox .q-checkbox__bg {{
                    border-radius: 0 !important;
                }}
                .mod-client-pack-choice {{
                    gap: 0;
                    padding: 0.45rem 0.55rem;
                }}
                .mod-client-pack-select {{
                    flex: 1 1 auto;
                    min-width: 0;
                }}
                .mod-client-pack-select .q-field__native,
                .mod-client-pack-select .q-field__input,
                .mod-client-pack-select .q-field__label,
                .mod-client-pack-select .q-field__marginal,
                .mod-client-pack-select .q-icon {{
                    color: var(--mod-text) !important;
                }}
                .mod-client-pack-changelog .q-field__control {{
                    background: rgba(9, 9, 13, 0.96) !important;
                    border: 1px solid rgba(82, 82, 91, 0.72) !important;
                }}
                .mod-client-pack-changelog .q-field__native,
                .mod-client-pack-changelog .q-field__input,
                .mod-client-pack-changelog textarea,
                .mod-client-pack-changelog .q-field__label,
                .mod-client-pack-changelog .q-field__marginal,
                .mod-client-pack-changelog .q-icon {{
                    color: var(--mod-text) !important;
                    caret-color: var(--mod-accent) !important;
                }}
                .mod-client-pack-changelog .q-field__native::placeholder,
                .mod-client-pack-changelog .q-field__input::placeholder,
                .mod-client-pack-changelog textarea::placeholder {{
                    color: var(--mod-muted) !important;
                    opacity: 1;
                }}
                .mod-client-pack-changelog-content {{
                    width: 100%;
                    color: var(--mod-text) !important;
                    font-size: 0.88rem;
                    line-height: 1.5;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                }}
                .mod-client-pack-actions {{
                    justify-content: flex-end;
                    gap: 0.5rem;
                    flex-wrap: wrap;
                    padding-top: 0.95rem;
                    border-top: 1px solid rgba(82, 82, 91, 0.52);
                }}
                .mod-client-pack-actions .mod-list-button {{
                    flex: 0 1 auto;
                }}
                .mod-node-settings-overlay {{
                    position: fixed;
                    inset: 0;
                    z-index: 4000;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 1rem;
                }}
                .mod-node-settings-backdrop {{
                    position: absolute;
                    inset: 0;
                    background: rgba(2, 2, 4, 0.78);
                    backdrop-filter: blur(7px);
                }}
                .mod-node-settings-shell {{
                    position: relative;
                    z-index: 1;
                    width: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .mod-app-details-dialog-card {{
                    width: min(44rem, calc(100vw - 1.5rem)) !important;
                }}
                .mod-metadata-review-card {{
                    width: min(34rem, calc(100vw - 1.5rem)) !important;
                }}
                .mod-metadata-review-summary {{
                    gap: 0.2rem;
                    padding: 0.75rem 0.85rem;
                    border: 1px solid rgba(82, 82, 91, 0.7);
                    background: rgba(15, 15, 20, 0.92) !important;
                }}
                .mod-metadata-review-suggestion {{
                    color: var(--mod-text) !important;
                    font-size: 1rem;
                    font-weight: 850;
                    line-height: 1.25;
                }}
                .mod-metadata-review-providers {{
                    gap: 0.65rem;
                }}
                .mod-metadata-review-provider {{
                    gap: 0.28rem;
                    padding: 0.8rem 0.85rem;
                    border: 1px solid rgba(63, 63, 70, 0.82);
                    background: rgba(10, 10, 14, 0.72) !important;
                }}
                .mod-metadata-review-provider-title {{
                    margin-bottom: 0.2rem;
                    color: var(--mod-text) !important;
                    font-size: 0.9rem;
                    font-weight: 900;
                    line-height: 1.25;
                }}
                .mod-metadata-review-field-label {{
                    color: var(--mod-muted) !important;
                    font-size: 0.68rem;
                    font-weight: 850;
                    letter-spacing: 0.07em;
                    line-height: 1.2;
                    text-transform: uppercase;
                }}
                .mod-metadata-review-link {{
                    min-width: 0;
                    color: var(--mod-accent-text) !important;
                    font-size: 0.78rem;
                    line-height: 1.35;
                    overflow-wrap: anywhere;
                    text-decoration-color: var(--mod-accent-border) !important;
                    text-underline-offset: 0.18rem;
                }}
                .mod-metadata-review-link:hover {{
                    color: var(--mod-accent-text-strong) !important;
                    text-decoration-color: rgba(243, 232, 255, 0.8) !important;
                }}
                .mod-metadata-review-reference {{
                    color: var(--mod-text) !important;
                    font-size: 0.8rem;
                    line-height: 1.35;
                }}
                .mod-metadata-review-actions {{
                    padding-top: 0.8rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.72);
                }}
                .mod-bulk-metadata-dialog-card {{
                    width: min(72rem, calc(100vw - 2rem)) !important;
                    max-width: 72rem !important;
                    padding: 1.25rem;
                }}
                .mod-bulk-metadata-table .q-table__middle {{
                    max-height: min(62vh, 42rem);
                    overflow-y: auto;
                }}
                .mod-bulk-metadata-table .q-table {{
                    table-layout: fixed;
                }}
                .mod-bulk-metadata-table .q-table th:first-child,
                .mod-bulk-metadata-table .q-table td:first-child {{
                    width: 3rem;
                    min-width: 3rem;
                    max-width: 3rem;
                    padding-right: 0.25rem !important;
                    padding-left: 0.25rem !important;
                    text-align: center;
                }}
                .mod-bulk-metadata-selection-checkbox {{
                    display: inline-flex !important;
                    width: 2.5rem;
                    height: 2.5rem;
                    margin: 0 auto;
                    align-items: center;
                    justify-content: center;
                    vertical-align: middle;
                }}
                .mod-bulk-metadata-selection-checkbox .q-checkbox__inner {{
                    margin: 0 !important;
                }}
                .mod-bulk-metadata-type-suggestion {{
                    display: flex;
                    min-height: 2.5rem;
                    align-items: center;
                    gap: 0.25rem;
                }}
                .mod-bulk-metadata-type-checkbox {{
                    flex: 0 0 2.5rem;
                    width: 2.5rem;
                    height: 2.5rem;
                    margin-left: -0.5rem;
                    align-items: center;
                    justify-content: center;
                }}
                .mod-app-details-layout {{
                    gap: 1rem;
                }}
                .mod-app-properties-card {{
                    max-width: min(56rem, 100%);
                }}
                .mod-app-details-section {{
                    width: 100%;
                    display: flex;
                    flex-direction: column;
                    gap: 0.8rem;
                    padding: 0.95rem 1rem;
                    border: 1px solid rgba(82, 82, 91, 0.62);
                    background: rgba(8, 8, 10, 0.9) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03);
                }}
                .mod-app-details-subsection {{
                    width: 100%;
                    display: flex;
                    flex-direction: column;
                    gap: 0.45rem;
                }}
                .mod-app-details-field {{
                    width: 100%;
                }}
                .mod-standard-drink-table,
                .mod-currency-table {{
                    background: transparent !important;
                }}
                .mod-standard-drink-table .q-table__container,
                .mod-currency-table .q-table__container,
                .mod-standard-drink-table .q-table__middle,
                .mod-currency-table .q-table__middle,
                .mod-standard-drink-table .q-table,
                .mod-currency-table .q-table {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                }}
                .mod-standard-drink-table .q-table thead tr,
                .mod-currency-table .q-table thead tr,
                .mod-standard-drink-table .q-table th,
                .mod-currency-table .q-table th {{
                    background: rgba(39, 39, 49, 0.7) !important;
                    color: var(--mod-muted) !important;
                    font-size: 0.68rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.07em;
                    text-transform: uppercase;
                }}
                .mod-standard-drink-table .q-table tbody tr,
                .mod-currency-table .q-table tbody tr,
                .mod-standard-drink-table .q-table td,
                .mod-currency-table .q-table td {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                    font-size: 0.78rem !important;
                    font-weight: 600 !important;
                }}
                .mod-standard-drink-table .q-table tbody tr:not(:last-child) > td,
                .mod-currency-table .q-table tbody tr:not(:last-child) > td {{
                    border-bottom-color: rgba(82, 82, 91, 0.28) !important;
                }}
                .mod-standard-drink-table .q-table tbody tr:hover > td,
                .mod-currency-table .q-table tbody tr:hover > td {{
                    background: var(--mod-accent-faint) !important;
                }}
                .mod-standard-drink-table .q-table th:first-child,
                .mod-currency-table .q-table th:first-child,
                .mod-standard-drink-table .q-table td:first-child,
                .mod-currency-table .q-table td:first-child {{
                    text-align: left !important;
                }}
                .mod-standard-drink-divider {{
                    width: 100%;
                    height: 1px;
                    background: rgba(113, 113, 122, 0.42);
                }}
                .mod-user-appearance-section {{
                    gap: 0.65rem;
                    padding: 0.85rem 0.95rem;
                }}
                .mod-user-appearance-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(min(17rem, 100%), 1fr));
                    gap: 0.55rem 0.8rem;
                    width: 100%;
                    min-width: 0;
                }}
                .mod-user-accent-input .q-field__control {{
                    min-height: 2.65rem !important;
                    height: 2.65rem !important;
                }}
                .mod-user-accent-input .q-field__native {{
                    min-height: 2.65rem !important;
                }}
                .mod-page-editor-controls {{
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr) max-content;
                    align-items: end;
                }}
                .mod-app-details-field.mod-page-url-invalid .q-field__control {{
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -2px 0 #ef4444 !important;
                }}
                .mod-details-tab-row {{
                    display: grid !important;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }}
                .mod-details-tab-button {{
                    width: 100%;
                    min-width: 0;
                }}
                .mod-list-button.secondary.mod-details-tab-active {{
                    color: var(--mod-accent-text-strong) !important;
                    background: var(--mod-accent-surface) !important;
                    border-color: var(--mod-accent) !important;
                    box-shadow: inset 0 -2px 0 var(--mod-accent-text) !important;
                }}
                .mod-app-details-point-field {{
                    flex: 0 1 11.5rem;
                    max-width: 11.5rem;
                }}
                .mod-app-details-field .q-field__control {{
                    min-height: 2.9rem;
                    padding: 0 0.55rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.82) !important;
                    border-radius: 0 !important;
                    background: rgba(8, 8, 10, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04);
                }}
                .mod-app-details-field .q-field--filled .q-field__control::before {{
                    border-bottom: 0 !important;
                    opacity: 0 !important;
                }}
                .mod-app-details-field .q-field--filled .q-field__control::after {{
                    border-bottom: 0 !important;
                }}
                .mod-app-details-field.q-field--focused .q-field__control,
                .mod-app-details-field .q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 var(--mod-accent-glow),
                        0 0 0 1px var(--mod-accent-faint);
                }}
                .mod-app-details-field .q-field__native,
                .mod-app-details-field .q-field__input,
                .mod-app-details-field .q-field__append,
                .mod-app-details-field .q-field__prepend,
                .mod-app-details-field .q-field__marginal,
                .mod-app-details-field .q-icon {{
                    color: var(--mod-text) !important;
                }}
                .mod-app-details-field .q-field__label {{
                    color: var(--mod-muted) !important;
                    font-size: 0.74rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.04em;
                }}
                .mod-app-details-field .q-field__native,
                .mod-app-details-field .q-field__input {{
                    font-size: 0.9rem !important;
                    font-weight: 850 !important;
                }}
                .mod-timestamp-mode-panels,
                .mod-timestamp-mode-panels .q-panel,
                .mod-timestamp-mode-panels .q-panel-parent {{
                    overflow: visible !important;
                }}
                .mod-app-details-field.mod-timestamp-input .q-field__control,
                .mod-app-details-field.mod-timestamp-input .q-field__marginal {{
                    height: 3.35rem !important;
                    min-height: 3.35rem !important;
                }}
                .mod-app-details-field.mod-timestamp-input .q-field__label {{
                    top: 0.42rem !important;
                }}
                .mod-app-details-field.mod-timestamp-input .q-field__native,
                .mod-app-details-field.mod-timestamp-input .q-field__input {{
                    padding-top: 1.32rem !important;
                    padding-bottom: 0.28rem !important;
                    line-height: 1.15 !important;
                }}
                .mod-app-details-field.mod-timestamp-input .q-field__append {{
                    align-self: flex-end;
                    height: 2rem !important;
                    min-height: 2rem !important;
                    padding-bottom: 0.12rem;
                }}
                .mod-timestamp-format-option {{
                    min-height: 2.2rem !important;
                    padding: 0.4rem 0.7rem !important;
                    color: var(--mod-text) !important;
                    font-size: 0.76rem !important;
                    font-weight: 850 !important;
                    letter-spacing: 0.02em;
                }}
                .mod-timestamp-format-pattern {{
                    min-width: 12rem;
                    color: var(--mod-muted) !important;
                    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
                    font-size: 0.68rem !important;
                    font-weight: 750 !important;
                    letter-spacing: 0.02em;
                    text-align: right;
                }}
                .mod-timestamp-picker-dialog-card {{
                    width: min(42rem, calc(100vw - 1.5rem)) !important;
                }}
                .mod-timestamp-picker-workspace {{
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    align-items: start;
                    gap: 0.75rem;
                    width: 100%;
                }}
                .mod-timestamp-picker-date,
                .mod-timestamp-picker-time {{
                    width: 100%;
                    min-width: 0;
                    border: 1px solid rgba(82, 82, 91, 0.82) !important;
                    border-radius: 0 !important;
                    background: rgba(8, 8, 10, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        0 10px 26px rgba(0, 0, 0, 0.2) !important;
                    color: var(--mod-text) !important;
                }}
                .mod-timestamp-picker-dialog-card :is(.q-date__header, .q-time__header) {{
                    border-radius: 0 !important;
                    border-bottom: 1px solid var(--mod-accent-faint);
                    background:
                        linear-gradient(135deg, var(--mod-accent-panel), rgba(17, 15, 26, 0.98)) !important;
                    color: var(--mod-text) !important;
                }}
                .mod-timestamp-picker-dialog-card :is(.q-date__header-subtitle, .q-date__header-title-label) {{
                    color: inherit !important;
                }}
                .mod-timestamp-picker-dialog-card :is(.q-date__view, .q-time__container, .q-time__content) {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                }}
                .mod-timestamp-picker-dialog-card :is(.q-date, .q-time) .q-btn {{
                    border-radius: 0 !important;
                    color: var(--mod-text) !important;
                }}
                .mod-timestamp-picker-dialog-card .q-date__calendar-weekdays > div {{
                    color: var(--mod-muted) !important;
                    font-size: 0.67rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.03em;
                }}
                .mod-timestamp-picker-dialog-card .q-date__calendar-item--out .q-btn {{
                    color: rgba(161, 161, 170, 0.4) !important;
                }}
                .mod-timestamp-picker-dialog-card .q-date__today .q-btn {{
                    box-shadow: inset 0 0 0 1px var(--mod-accent-border-strong);
                }}
                .mod-timestamp-picker-dialog-card :is(.q-date, .q-time) .bg-primary {{
                    background: var(--mod-accent) !important;
                    color: #ffffff !important;
                }}
                .mod-timestamp-picker-dialog-card .q-time__clock {{
                    background: rgba(39, 39, 49, 0.72) !important;
                    box-shadow: inset 0 0 0 1px rgba(113, 113, 122, 0.52);
                }}
                .mod-timestamp-picker-dialog-card .q-time__clock-position {{
                    color: var(--mod-text) !important;
                }}
                .mod-timestamp-picker-dialog-card .q-time__clock-position--active,
                .mod-timestamp-picker-dialog-card :is(.q-time__clock-pointer, .q-time__clock-pointer::before) {{
                    background: var(--mod-accent) !important;
                    color: #ffffff !important;
                }}
                @media (max-width: 42rem) {{
                    .mod-timestamp-picker-workspace {{
                        grid-template-columns: minmax(0, 1fr);
                        justify-items: center;
                    }}
                    .mod-timestamp-picker-date,
                    .mod-timestamp-picker-time {{
                        max-width: 19rem;
                    }}
                }}
                .mod-mod-override-field .q-field__control {{
                    border-color: rgba(113, 113, 122, 0.92) !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(15, 15, 22, 0.99) !important;
                }}
                .mod-mod-override-field .q-field__label {{
                    color: var(--mod-accent-text) !important;
                    opacity: 1 !important;
                }}
                .mod-mod-override-field .q-field__native,
                .mod-mod-override-field .q-field__input {{
                    color: var(--mod-text) !important;
                    -webkit-text-fill-color: var(--mod-text) !important;
                    opacity: 1 !important;
                }}
                .mod-mod-override-field input::placeholder {{
                    color: #b8b8c2 !important;
                    -webkit-text-fill-color: #b8b8c2 !important;
                    opacity: 1 !important;
                }}
                .mod-mod-override-datetime .q-field__control,
                .mod-mod-override-datetime input[type=datetime-local] {{
                    color-scheme: dark;
                }}
                .mod-mod-override-datetime input[type=datetime-local]::-webkit-calendar-picker-indicator {{
                    filter: invert(88%) sepia(8%) saturate(491%) hue-rotate(201deg) brightness(106%);
                    cursor: pointer;
                    opacity: 0.95;
                }}
                .mod-app-details-point-field .q-field__control {{
                    min-height: 2.55rem;
                    padding: 0 0.45rem !important;
                }}
                .mod-app-details-point-field .q-field__native,
                .mod-app-details-point-field .q-field__input {{
                    font-size: 0.82rem !important;
                }}
                .mod-app-details-point-field input[type=number] {{
                    appearance: textfield;
                    -moz-appearance: textfield;
                    padding-right: 0.2rem !important;
                }}
                .mod-app-details-point-field input[type=number]::-webkit-outer-spin-button,
                .mod-app-details-point-field input[type=number]::-webkit-inner-spin-button {{
                    opacity: 1;
                    margin: 0;
                    min-height: 2rem;
                    filter: invert(0.82) sepia(0.16) saturate(0.75) hue-rotate(196deg) brightness(0.92);
                }}
                .mod-app-details-notes .q-field__control {{
                    min-height: 11.5rem !important;
                    align-items: stretch;
                    padding-top: 0.65rem !important;
                    padding-bottom: 0.65rem !important;
                }}
                .mod-app-details-notes textarea,
                .mod-app-details-notes .q-field__native {{
                    min-height: 9.5rem !important;
                    line-height: 1.45 !important;
                    resize: vertical;
                }}
                .mod-app-details-toggle {{
                    color: var(--mod-text) !important;
                }}
                .mod-app-details-toggle .q-checkbox__label {{
                    font-weight: 800 !important;
                }}
                .mod-app-details-toggle .q-checkbox__inner {{
                    color: rgba(228, 228, 231, 0.78) !important;
                }}
                .mod-app-details-toggle .q-checkbox__inner--truthy {{
                    color: var(--mod-accent) !important;
                }}
                .mod-app-details-toggle .q-checkbox__bg {{
                    border-radius: 0 !important;
                }}
                .mod-app-details-state-button {{
                    flex: 0 1 auto !important;
                    width: auto !important;
                    min-width: 11rem !important;
                    max-width: 15rem !important;
                    min-height: 2.7rem !important;
                    height: 2.7rem !important;
                    justify-content: center;
                    align-self: flex-start;
                }}
                .mod-app-details-actions {{
                    margin-top: 0.15rem;
                }}
                .mod-mod-details-dialog-card {{
                    width: min(48rem, calc(100vw - 1.5rem)) !important;
                    max-height: min(56rem, calc(100vh - 1rem));
                    padding: 0 !important;
                    overflow: hidden;
                }}
                .mod-mod-details-shell {{
                    max-height: min(56rem, calc(100vh - 1rem));
                    gap: 0 !important;
                    padding: 0 !important;
                    overflow-x: hidden;
                    overflow-y: auto;
                    scrollbar-color: var(--mod-accent-border) rgba(9, 9, 13, 0.88);
                }}
                .mod-mod-details-header {{
                    position: sticky;
                    top: 0;
                    z-index: 3;
                    padding: 1.05rem 1.25rem 0.95rem;
                    border-bottom: 1px solid rgba(113, 113, 122, 0.64);
                    background: rgba(7, 7, 11, 0.98) !important;
                    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.3);
                    backdrop-filter: blur(14px);
                }}
                .mod-mod-details-summary {{
                    gap: 0.55rem !important;
                    padding: 1rem 1.25rem 0.8rem;
                }}
                .mod-mod-details-summary .mod-detail-item {{
                    min-height: 3.4rem;
                    padding: 0.65rem 0.75rem;
                    border: 1px solid rgba(63, 63, 70, 0.7);
                    background: rgba(8, 8, 12, 0.68);
                }}
                .mod-mod-details-links {{
                    width: calc(100% - 2.5rem) !important;
                    max-width: calc(100% - 2.5rem);
                    min-width: 0;
                    align-self: center;
                    box-sizing: border-box;
                    margin: 0 1.25rem;
                    padding: 0.75rem 0.85rem;
                    border: 1px solid rgba(63, 63, 70, 0.78);
                    background: rgba(10, 10, 14, 0.72) !important;
                }}
                .mod-mod-details-description,
                .mod-mod-details-notes,
                .mod-mod-details-update-check,
                .mod-mod-details-classification-section {{
                    width: calc(100% - 2.5rem) !important;
                    max-width: calc(100% - 2.5rem);
                    min-width: 0;
                    align-self: center;
                    box-sizing: border-box;
                    margin-inline: 1.25rem;
                }}
                .mod-mod-details-editor {{
                    width: calc(100% - 2.5rem) !important;
                    max-width: calc(100% - 2.5rem);
                    min-width: 0;
                    align-self: center;
                    box-sizing: border-box;
                    margin: 1rem 1.25rem;
                    padding: 1rem;
                    background: rgba(10, 10, 14, 0.78) !important;
                    box-shadow: none !important;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field .q-field__control {{
                    min-height: 3.35rem;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field.q-field--labeled .q-field__native,
                .mod-mod-details-dialog-card .mod-app-details-field.q-field--labeled .q-field__input {{
                    min-height: 3.35rem;
                    padding-top: 1.35rem !important;
                    padding-bottom: 0.25rem !important;
                    line-height: 1.15 !important;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field.mod-mod-details-select .q-field__control {{
                    height: 3.35rem !important;
                    min-height: 3.35rem !important;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field.mod-mod-details-select .q-field__native {{
                    height: auto !important;
                    min-height: 0 !important;
                    padding-top: 0.875rem !important;
                    padding-bottom: 0.125rem !important;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field.mod-mod-details-select .q-field__marginal {{
                    height: 3.35rem !important;
                }}
                .mod-mod-details-dialog-card .mod-app-details-field .q-field__label {{
                    line-height: 1 !important;
                }}
                .mod-mod-details-classification {{
                    padding-bottom: 0.15rem;
                }}
                .mod-mod-details-subsection {{
                    gap: 0.55rem !important;
                    padding: 0.8rem 0.85rem;
                    border: 1px solid rgba(63, 63, 70, 0.76);
                    background: rgba(7, 7, 11, 0.58) !important;
                }}
                .mod-mod-details-metadata-label {{
                    padding-top: 0.9rem;
                    border-top: 1px solid rgba(82, 82, 91, 0.62);
                }}
                .mod-mod-details-metadata-label {{
                    margin-top: 0.1rem;
                }}
                .mod-mod-details-metadata-tabs {{
                    gap: 0.55rem !important;
                }}
                .mod-mod-details-metadata-panel {{
                    padding: 0.85rem;
                    border: 1px solid rgba(82, 82, 91, 0.62);
                    background: rgba(5, 5, 8, 0.54);
                }}
                .mod-mod-details-inline-actions {{
                    justify-content: flex-end;
                }}
                .mod-mod-details-discovery-button {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow: inset 0 -2px 0 var(--mod-accent-glow) !important;
                }}
                .mod-mod-details-danger-zone {{
                    width: calc(100% - 2.5rem) !important;
                    max-width: calc(100% - 2.5rem);
                    min-width: 0;
                    align-self: center;
                    box-sizing: border-box;
                    margin: 0 1.25rem 1rem;
                    padding: 0.85rem 1rem;
                    border: 1px solid var(--mod-negative-border);
                    background:
                        linear-gradient(135deg, var(--mod-negative-glow), transparent 62%),
                        rgba(10, 7, 9, 0.72);
                }}
                .mod-mod-details-danger-zone .mod-stat-label {{
                    color: #fca5a5 !important;
                }}
                .mod-mod-details-footer {{
                    position: sticky;
                    bottom: 0;
                    z-index: 3;
                    padding: 0.85rem 1.25rem;
                    border-top: 1px solid rgba(113, 113, 122, 0.64);
                    background: rgba(7, 7, 11, 0.97);
                    box-shadow: 0 -12px 30px rgba(0, 0, 0, 0.3);
                    backdrop-filter: blur(14px);
                }}
                @media (max-width: 640px) {{
                    .mod-mod-details-dialog-card {{
                        width: calc(100vw - 0.75rem) !important;
                        max-height: calc(100vh - 0.75rem);
                    }}
                    .mod-mod-details-shell {{
                        width: 100% !important;
                        max-height: calc(100vh - 0.75rem);
                    }}
                    .mod-mod-details-header,
                    .mod-mod-details-footer {{
                        padding-inline: 0.9rem;
                    }}
                    .mod-mod-details-summary {{
                        padding: 0.85rem 0.9rem 0.7rem;
                    }}
                    .mod-mod-details-links,
                    .mod-mod-details-description,
                    .mod-mod-details-notes,
                    .mod-mod-details-update-check,
                    .mod-mod-details-classification-section,
                    .mod-mod-details-editor,
                    .mod-mod-details-danger-zone {{
                        width: calc(100% - 1.8rem) !important;
                        max-width: calc(100% - 1.8rem);
                        margin-inline: 0.9rem;
                    }}
                    .mod-mod-details-inline-actions .mod-list-button {{
                        flex: 1 1 10rem;
                    }}
                }}
                .mod-fake-chat-dialog-card {{
                    width: min(52rem, calc(100vw - 1.5rem)) !important;
                    max-width: none !important;
                }}
                .mod-fake-chat-field {{
                    width: 100%;
                }}
                .mod-fake-chat-field .q-field__control {{
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(8, 8, 12, 0.94) !important;
                    border: 1px solid rgba(82, 82, 91, 0.82) !important;
                    min-height: 2.6rem;
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
                }}
                .mod-fake-chat-field .q-field--filled .q-field__control::before {{
                    border-bottom: 0 !important;
                    opacity: 0 !important;
                }}
                .mod-fake-chat-field .q-field--filled .q-field__control::after {{
                    border-bottom: 0 !important;
                }}
                .mod-fake-chat-field.q-field--focused .q-field__control,
                .mod-fake-chat-field .q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border-strong) !important;
                    box-shadow: 0 0 0 1px var(--mod-accent-glow);
                }}
                .mod-fake-chat-field .q-field__native,
                .mod-fake-chat-field .q-field__input,
                .mod-fake-chat-field .q-field__marginal,
                .mod-fake-chat-field .q-field__append,
                .mod-fake-chat-field .q-field__prepend,
                .mod-fake-chat-field .q-icon {{
                    color: var(--mod-text) !important;
                }}
                .mod-fake-chat-field .q-field__label {{
                    color: var(--mod-muted) !important;
                    font-size: 0.74rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.04em;
                }}
                .mod-fake-chat-field .q-field__native,
                .mod-fake-chat-field .q-field__input {{
                    font-size: 0.84rem !important;
                    font-weight: 850 !important;
                }}
                .mod-fake-chat-field .q-icon {{
                    opacity: 0.78;
                }}
                .mod-fake-chat-footer {{
                    display: flex;
                    align-items: flex-end;
                    justify-content: space-between;
                    gap: 0.9rem;
                    flex-wrap: wrap;
                }}
                .mod-chat-dialog-card {{
                    width: min(64rem, calc(100vw - 1.5rem)) !important;
                    max-width: none !important;
                    max-height: calc(100vh - 2rem) !important;
                    overflow: hidden;
                }}
                .mod-chat-panel-card > .nicegui-content,
                .mod-chat-dialog-card > .nicegui-content {{
                    width: 100%;
                }}
                .mod-chat-panel {{
                    --mod-chat-panel-inline-padding: 1rem;
                    gap: 0.88rem;
                    padding: 0.92rem var(--mod-chat-panel-inline-padding) 0.98rem;
                    min-height: 0;
                }}
                .mod-chat-shell {{
                    --mod-chat-shell-inline-padding: clamp(0.5rem, 2vw, 1.25rem);
                    gap: 0.64rem;
                    min-height: 100%;
                }}
                .mod-chat-shell-card {{
                    width: min(92rem, calc(100vw - 1.5rem)) !important;
                    max-width: 100% !important;
                    min-height: clamp(36rem, 78vh, 58rem);
                    margin-inline: auto;
                    overflow: hidden;
                }}
                .mod-chat-shell-header {{
                    align-items: center;
                }}
                .mod-chat-shell-header .mod-corner-badges {{
                    flex: 1 1 34rem;
                    min-width: 0;
                    max-width: none;
                }}
                .mod-chat-shell-header .mod-corner-badge-row {{
                    align-items: center;
                    justify-content: flex-end;
                }}
                .mod-chat-shell-header-main {{
                    flex: 1 1 0;
                    min-width: 0;
                }}
                .mod-chat-panel-embedded {{
                    --mod-chat-panel-inline-padding: 0rem;
                    --mod-chat-shell-inline-padding: 0rem;
                    padding: 0.08rem 0 0 !important;
                    flex: 1 1 auto;
                    min-height: 0;
                }}
                .mod-chat-header {{
                    gap: 0.7rem;
                    padding: 0.1rem 0.15rem 0.95rem;
                    border-bottom: 1px solid var(--mod-accent-glow);
                }}
                .mod-chat-header-top {{
                    align-items: flex-end;
                }}
                .mod-chat-header-main {{
                    flex: 0 1 auto;
                    min-width: max-content !important;
                    max-width: min(100%, 42rem);
                }}
                .mod-chat-title {{
                    color: var(--mod-text) !important;
                    font-size: clamp(1.35rem, 2vw, 2rem) !important;
                    font-weight: 950 !important;
                    line-height: 1.05 !important;
                    hyphens: none !important;
                    overflow-wrap: normal !important;
                    white-space: nowrap !important;
                    word-break: normal !important;
                }}
                .mod-chat-subtitle {{
                    max-width: 34rem;
                    font-size: 0.78rem !important;
                    line-height: 1.3 !important;
                    letter-spacing: 0.01em;
                }}
                .mod-chat-status-row {{
                    min-height: 2.25rem;
                    margin-left: auto;
                }}
                .mod-chat-close {{
                    min-width: 5rem;
                }}
                .mod-chat-section-head {{
                    min-height: 1.4rem;
                }}
                .mod-chat-section-label {{
                    color: var(--mod-text) !important;
                    font-size: 0.68rem !important;
                    font-weight: 950 !important;
                    letter-spacing: 0.12em;
                    line-height: 1 !important;
                    text-transform: uppercase;
                }}
                .mod-chat-section-hint {{
                    font-size: 0.72rem !important;
                    line-height: 1.2 !important;
                    text-align: right;
                }}
                .mod-chat-timeline-shell {{
                    gap: 0.38rem;
                    position: relative;
                    width: calc(100% + ((var(--mod-chat-panel-inline-padding) + var(--mod-chat-shell-inline-padding)) * 2));
                    margin-inline: calc((var(--mod-chat-panel-inline-padding) + var(--mod-chat-shell-inline-padding)) * -1);
                    padding: 0.58rem 0 0.62rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.74);
                    border-bottom: 1px solid rgba(63, 63, 70, 0.74);
                    border-left: 0;
                    border-right: 0;
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent 22%),
                        rgba(7, 7, 10, 0.92);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                    flex: 1 1 auto;
                    min-height: clamp(20rem, 46vh, 34rem);
                    overflow: hidden;
                }}
                .mod-chat-scroll-area {{
                    height: min(60vh, 40rem);
                    min-height: clamp(18rem, 40vh, 28rem);
                    max-height: min(60vh, 40rem);
                    width: 100%;
                    flex: 1 1 auto;
                    scroll-behavior: auto;
                    scrollbar-color: #52525b transparent;
                    scrollbar-width: thin;
                    scrollbar-gutter: stable;
                    overscroll-behavior: contain;
                }}
                .mod-chat-scroll-area .q-scrollarea__content {{
                    width: 100%;
                    padding: 0 !important;
                }}
                .mod-chat-timeline {{
                    gap: 0;
                    min-height: 100%;
                    overflow: visible;
                    overflow-anchor: none;
                    padding: 0.08rem 0 0.18rem 0;
                }}
                .mod-chat-empty {{
                    min-height: 16rem;
                    justify-content: center;
                    padding: 2.2rem 1.2rem;
                    border: 1px dashed rgba(82, 82, 91, 0.78);
                    background:
                        radial-gradient(circle at 14% 18%, var(--mod-accent-faint), transparent 19rem),
                        linear-gradient(180deg, rgba(10, 10, 14, 0.94), rgba(10, 10, 14, 0.72));
                }}
                .mod-chat-message {{
                    --mod-chat-source-rail: #52525b;
                    --mod-chat-source-glow: rgba(82, 82, 91, 0.12);
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    position: relative;
                    padding: 0.5rem 0.62rem 0.48rem !important;
                    border-radius: 0 !important;
                    background: rgba(0, 0, 0, 0) !important;
                    border: 0 !important;
                    border-left: 3px solid var(--mod-chat-source-rail) !important;
                    box-shadow: none !important;
                    color: var(--mod-text) !important;
                    transition: background 140ms ease;
                }}
                .mod-chat-message:hover {{
                    background: linear-gradient(
                        90deg,
                        var(--mod-chat-source-glow),
                        transparent 44%
                    ) !important;
                }}
                .mod-chat-message::before {{
                    content: "";
                    position: absolute;
                    top: 0;
                    left: 0.7rem;
                    right: 0;
                    height: 1px;
                    background: linear-gradient(
                        90deg,
                        rgba(113, 113, 122, 0.82) 0%,
                        rgba(113, 113, 122, 0.08) 48%,
                        rgba(113, 113, 122, 0.82) 100%
                    );
                    pointer-events: none;
                }}
                .mod-chat-message:first-child::before {{
                    display: none;
                }}
                .mod-chat-message > .nicegui-content {{
                    padding: 0 !important;
                }}
                .mod-chat-message-inner {{
                    gap: 0.06rem !important;
                    padding: 0 !important;
                }}
                .mod-chat-message-head {{
                    min-height: 0 !important;
                    align-items: flex-start !important;
                    gap: 0.16rem !important;
                }}
                .mod-chat-head-meta {{
                    margin-left: auto;
                    justify-content: flex-end;
                    align-items: flex-start;
                    min-width: 0;
                    gap: 0.14rem !important;
                    margin-top: -0.03rem;
                }}
                .mod-chat-badge-row {{
                    justify-content: flex-end;
                    gap: 0.12rem !important;
                }}
                .mod-chat-message.game {{
                    --mod-chat-source-rail: var(--mod-accent);
                    --mod-chat-source-glow: var(--mod-accent-faint);
                }}
                .mod-chat-message.discord {{
                    --mod-chat-source-rail: #52525b;
                    --mod-chat-source-glow: rgba(82, 82, 91, 0.16);
                }}
                .mod-chat-message.web {{
                    --mod-chat-source-rail: #3f3f46;
                    --mod-chat-source-glow: rgba(63, 63, 70, 0.14);
                }}
                .mod-chat-message.system,
                .mod-chat-message.unknown {{
                    --mod-chat-source-rail: #7f1d1d;
                    --mod-chat-source-glow: rgba(127, 29, 29, 0.16);
                }}
                .mod-chat-author {{
                    color: var(--mod-text) !important;
                    font-size: 0.86rem !important;
                    font-weight: 950 !important;
                    line-height: 1 !important;
                }}
                .mod-chat-author-row {{
                    min-width: 0;
                    gap: 0.34rem !important;
                }}
                .mod-chat-author-avatar {{
                    width: 1.35rem;
                    height: 1.35rem;
                    min-width: 1.35rem;
                    display: block;
                    border: 1px solid rgba(113, 113, 122, 0.46);
                    background: rgba(15, 15, 18, 0.9);
                    image-rendering: pixelated;
                    object-fit: cover;
                    box-shadow: inset 0 0 0 1px rgba(5, 5, 7, 0.75);
                }}
                .mod-chat-source-badge {{
                    padding: 0.12rem 0.34rem !important;
                    font-size: 0.56rem !important;
                    line-height: 1 !important;
                    letter-spacing: 0.07em;
                }}
                .mod-chat-content {{
                    color: #e4e4e7 !important;
                    font-size: 0.93rem !important;
                    line-height: 1.34 !important;
                }}
                .mod-chat-markup,
                .mod-chat-markup > .nicegui-content {{
                    color: inherit !important;
                    font: inherit !important;
                    line-height: inherit !important;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-markup {{
                    min-width: 0;
                }}
                .mod-chat-markup-block {{
                    margin: 0;
                    max-width: 100%;
                }}
                .mod-chat-markup-heading {{
                    margin: 0;
                    max-width: 100%;
                    color: var(--mod-accent-text-strong);
                    font-weight: 950;
                    letter-spacing: -0.02em;
                    line-height: 1.08;
                    text-wrap: balance;
                }}
                .mod-chat-markup-heading-1 {{
                    font-size: 1.26rem;
                }}
                .mod-chat-markup-heading-2 {{
                    font-size: 1.08rem;
                }}
                .mod-chat-markup-heading-3 {{
                    font-size: 0.98rem;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                }}
                .mod-chat-markup-subtext {{
                    margin: 0;
                    max-width: 100%;
                    color: rgba(161, 161, 170, 0.9);
                    font-size: 0.78rem;
                    font-weight: 700;
                    line-height: 1.28;
                }}
                .mod-chat-markup-list {{
                    margin: 0;
                    max-width: 100%;
                    padding-left: 1.15rem;
                }}
                .mod-chat-markup-list + .mod-chat-markup-list {{
                    margin-top: 0.2rem;
                }}
                .mod-chat-markup-list .mod-chat-markup-list {{
                    margin-top: 0.18rem;
                    padding-left: 1.1rem;
                }}
                .mod-chat-markup-list li {{
                    margin: 0.08rem 0;
                    padding-left: 0.08rem;
                }}
                .mod-chat-markup strong,
                .mod-chat-markup em,
                .mod-chat-markup u,
                .mod-chat-markup s {{
                    color: inherit;
                }}
                .mod-chat-markup a {{
                    color: var(--mod-accent-text);
                    text-decoration: underline;
                    text-decoration-color: var(--mod-accent-border);
                    text-underline-offset: 0.12rem;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-markup a:hover {{
                    color: var(--mod-accent-text-strong);
                    text-decoration-color: var(--mod-accent-border-strong);
                }}
                .mod-chat-inline-code {{
                    display: inline-block;
                    max-width: 100%;
                    padding: 0.06rem 0.26rem;
                    border: 1px solid rgba(82, 82, 91, 0.86);
                    background: rgba(10, 10, 14, 0.92);
                    color: var(--mod-accent-text-strong);
                    font-family: "IBM Plex Mono", "Fira Code", monospace;
                    font-size: 0.84em;
                    line-height: 1.2;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-code-block {{
                    margin: 0.18rem 0 0;
                    max-width: 100%;
                    padding: 0.48rem 0.58rem;
                    border: 1px solid rgba(82, 82, 91, 0.82);
                    background: rgba(6, 6, 10, 0.94);
                    color: var(--mod-accent-text-strong);
                    font-family: "IBM Plex Mono", "Fira Code", monospace;
                    font-size: 0.81em;
                    line-height: 1.42;
                    overflow-anchor: none;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-code-block code {{
                    font: inherit;
                    color: inherit;
                    white-space: inherit;
                    overflow-wrap: inherit;
                    word-break: inherit;
                }}
                .mod-console-stdout {{
                    position: relative;
                    transition: border-color var(--mod-motion-fast) ease, box-shadow var(--mod-motion-fast) ease;
                }}
                .mod-console-stdout-update {{
                    animation: mod-console-output-pulse 480ms var(--mod-motion-ease);
                }}
                @keyframes mod-console-output-pulse {{
                    0% {{
                        border-left-color: var(--mod-accent-text);
                        box-shadow: inset 3px 0 0 var(--mod-accent-border-strong);
                    }}
                    100% {{
                        border-left-color: rgba(82, 82, 91, 0.82);
                        box-shadow: inset 3px 0 0 transparent;
                    }}
                }}
                .mod-chat-quote {{
                    margin: 0.18rem 0 0;
                    padding: 0.22rem 0 0.22rem 0.62rem;
                    border-left: 3px solid var(--mod-accent-border);
                    color: rgba(228, 228, 231, 0.88);
                    background: linear-gradient(90deg, var(--mod-accent-wash), transparent 58%);
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-spoiler {{
                    padding: 0 0.18rem;
                    border-radius: 0.24rem;
                    background: rgba(228, 228, 231, 0.18);
                    color: transparent;
                    transition: color 140ms ease, background 140ms ease;
                    cursor: help;
                }}
                .mod-chat-spoiler:hover,
                .mod-chat-spoiler:focus-visible {{
                    background: rgba(228, 228, 231, 0.12);
                    color: inherit;
                    outline: none;
                }}
                .mod-chat-entry-list {{
                    gap: 0 !important;
                    padding-left: 0 !important;
                }}
                .mod-chat-entry {{
                    align-items: flex-start;
                    gap: 0.32rem !important;
                    margin-top: -0.03rem;
                    min-width: 0;
                }}
                .mod-chat-entry + .mod-chat-entry {{
                    margin-top: -0.08rem;
                }}
                .mod-chat-entry-live {{
                    animation: mod-chat-entry-arrive 320ms var(--mod-motion-ease) both;
                }}
                @keyframes mod-chat-entry-arrive {{
                    from {{ opacity: 0; translate: 0 0.28rem; }}
                    to {{ opacity: 1; translate: 0 0; }}
                }}
                .mod-chat-entry-main {{
                    gap: 0.04rem !important;
                    min-width: 0;
                }}
                .mod-chat-entry-meta {{
                    flex: 0 0 auto;
                    align-items: flex-start;
                    margin-left: 0.18rem;
                    min-width: 0;
                }}
                .mod-chat-entry-time {{
                    color: rgba(161, 161, 170, 0.74) !important;
                    font-size: 0.56rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.04em;
                    line-height: 1.05 !important;
                    padding-top: 0;
                    text-align: right;
                    white-space: nowrap;
                    text-transform: uppercase;
                }}
                .mod-chat-entry-menu {{
                    min-width: 11rem;
                    border: 1px solid rgba(82, 82, 91, 0.86);
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, var(--mod-accent-panel), rgba(11, 10, 17, 0.98))
                        !important;
                    box-shadow:
                        0 16px 42px rgba(0, 0, 0, 0.5),
                        inset 0 1px 0 var(--mod-accent-faint) !important;
                    color: var(--mod-text) !important;
                    overflow: hidden;
                }}
                .mod-chat-entry-menu .q-list {{
                    padding: 0.26rem 0;
                }}
                .mod-timezone-options {{
                    max-height: min(16rem, calc(100vh - 12rem));
                    overflow-x: hidden;
                    overflow-y: auto;
                    overscroll-behavior: contain;
                }}
                .mod-timestamp-dialog-card {{
                    overflow: visible !important;
                }}
                .mod-app-details-dialog-card:has(.mod-timezone-picker),
                .q-dialog__inner:has(.mod-timezone-picker) {{
                    overflow: visible !important;
                }}
                .mod-timezone-picker {{
                    position: relative;
                    z-index: 1;
                }}
                .mod-timezone-menu {{
                    position: absolute !important;
                    top: calc(100% + 0.25rem);
                    right: 0;
                    left: auto;
                    z-index: 20;
                    width: 100%;
                    max-width: none;
                    border: 0 !important;
                    background: rgba(9, 9, 12, 0.98) !important;
                    box-shadow:
                        0 14px 34px rgba(0, 0, 0, 0.38),
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 var(--mod-accent-faint) !important;
                }}
                .mod-chat-entry-menu-item {{
                    min-height: 0 !important;
                    padding: 0.48rem 0.74rem !important;
                    color: var(--mod-accent-text-strong) !important;
                    font-size: 0.8rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.03em;
                    text-transform: uppercase;
                    transition: background 120ms ease, color 120ms ease;
                }}
                .mod-timezone-option .q-btn__content {{
                    width: 100%;
                    align-items: flex-start;
                    flex-direction: column;
                    gap: 0.12rem;
                    white-space: normal;
                }}
                .mod-timezone-option {{
                    min-height: 2.16rem !important;
                    padding: 0.42rem 0.62rem !important;
                    color: #ffffff !important;
                    text-align: left;
                    transition: background 120ms ease, color 120ms ease;
                }}
                .mod-timezone-option-summary {{
                    align-items: baseline;
                    column-gap: 0.7rem;
                    color: inherit;
                    font-size: 0.8rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.03em;
                    line-height: 1.1 !important;
                }}
                .mod-timezone-option-code {{
                    flex: 0 0 4.5rem;
                    text-align: left;
                }}
                .mod-timezone-option-offset {{
                    flex: 0 0 auto;
                    text-align: left;
                }}
                .mod-timezone-option-location {{
                    color: rgba(228, 225, 231, 0.7) !important;
                    font-size: 0.67rem !important;
                    font-weight: 700 !important;
                    letter-spacing: 0.01em;
                    line-height: 1.15 !important;
                    text-align: left;
                    text-transform: none;
                }}
                .mod-timezone-option:hover,
                .mod-timezone-option.q-manual-focusable--focused,
                .mod-timezone-option[aria-selected="true"] {{
                    background: rgba(63, 63, 70, 0.24) !important;
                    color: #ffffff !important;
                }}
                .mod-chat-entry-menu-item:hover,
                .mod-chat-entry-menu-item.q-manual-focusable--focused,
                .mod-chat-entry-menu-item[aria-selected="true"] {{
                    background: var(--mod-accent-glow) !important;
                    color: #ffffff !important;
                }}
                .mod-chat-reference {{
                    gap: 0.16rem !important;
                    padding: 0.34rem 0.46rem 0.38rem;
                    border-left: 2px solid var(--mod-accent-border-strong);
                    background: rgba(32, 19, 53, 0.34);
                }}
                .mod-chat-reference-label {{
                    color: var(--mod-accent-border-strong) !important;
                    font-size: 0.63rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.05em;
                    line-height: 1 !important;
                    text-transform: uppercase;
                }}
                .mod-chat-reference-content {{
                    color: rgba(228, 228, 231, 0.84) !important;
                    font-size: 0.8rem !important;
                    line-height: 1.3 !important;
                }}
                .mod-chat-asset-row {{
                    gap: 0.42rem;
                    align-items: flex-start;
                    flex-wrap: wrap;
                    padding-top: 0.1rem;
                }}
                .mod-chat-asset {{
                    display: inline-flex;
                    max-width: min(100%, 30rem);
                    padding: 0.28rem 0.52rem;
                    border: 1px solid rgba(82, 82, 91, 0.78);
                    background: rgba(14, 14, 19, 0.88);
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-chat-media-grid {{
                    gap: 0.55rem;
                    align-items: flex-start;
                    flex-wrap: wrap;
                    padding-top: 0.2rem;
                }}
                .mod-chat-media-card {{
                    flex: 0 1 auto;
                    width: fit-content;
                    max-width: min(28rem, 100%);
                    border: 1px solid rgba(63, 63, 70, 0.82);
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(5, 5, 7, 0.92);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                    overflow: hidden;
                }}
                .mod-chat-media-card > .nicegui-content {{
                    display: block;
                    width: fit-content;
                    max-width: 100%;
                    padding: 0 !important;
                }}
                .mod-chat-media-link {{
                    display: inline-flex;
                    flex-direction: column;
                    gap: 0;
                    width: fit-content;
                    max-width: 100%;
                    color: #e4e4e7 !important;
                    cursor: pointer;
                    text-decoration: none !important;
                }}
                .mod-chat-media-image,
                .mod-chat-media-video {{
                    display: block;
                    width: auto;
                    max-width: 100%;
                    max-height: 19rem;
                    object-fit: contain;
                    background: linear-gradient(180deg, #020204, #08080c);
                }}
                .mod-chat-media-audio {{
                    display: block;
                    width: min(20rem, 100%);
                    min-height: 2.2rem;
                    background: #020204;
                }}
                .mod-chat-media-caption {{
                    display: block;
                    max-width: 100%;
                    padding: 0.38rem 0.5rem 0.42rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.72);
                    color: var(--mod-dim);
                    font-size: 0.68rem;
                    font-weight: 800;
                    line-height: 1.1;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mod-chat-composer {{
                    gap: 0.3rem;
                }}
                .mod-chat-reply-banner {{
                    padding: 0.38rem 0.42rem;
                    border: 1px solid var(--mod-accent-border);
                    background: linear-gradient(90deg, rgba(61, 37, 97, 0.58), rgba(16, 16, 22, 0.84));
                }}
                .mod-chat-reply-copy {{
                    flex: 1 1 0;
                }}
                .mod-chat-reply-label {{
                    color: var(--mod-accent-border-strong) !important;
                    font-size: 0.63rem !important;
                    font-weight: 950 !important;
                    letter-spacing: 0.07em;
                    line-height: 1 !important;
                    text-transform: uppercase;
                }}
                .mod-chat-reply-text {{
                    color: #e4e4e7 !important;
                    font-size: 0.78rem !important;
                    line-height: 1.32 !important;
                }}
                .mod-chat-reply-clear {{
                    color: rgba(228, 228, 231, 0.86) !important;
                    padding: 0.14rem 0.26rem !important;
                }}
                .mod-chat-composer-surface {{
                    gap: 0.4rem;
                    padding: 0.56rem;
                    border: 1px solid rgba(63, 63, 70, 0.74);
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(8, 8, 12, 0.92);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                }}
                .mod-chat-composer-row {{
                    align-items: stretch;
                }}
                .mod-chat-input {{
                    flex: 1 1 24rem;
                    min-width: 18rem;
                }}
                .mod-chat-input .q-field__control {{
                    background:
                        linear-gradient(180deg, var(--mod-accent-wash), transparent),
                        rgba(8, 8, 12, 0.94) !important;
                    border: 1px solid rgba(82, 82, 91, 0.78);
                    min-height: 2.7rem;
                }}
                .mod-chat-input.q-field--focused .q-field__control {{
                    border-color: var(--mod-accent-border);
                }}
                .mod-chat-input .q-field__native,
                .mod-chat-input input {{
                    color: var(--mod-text) !important;
                }}
                .mod-chat-send {{
                    min-width: 7rem;
                    padding: 0.36rem 0.72rem !important;
                    flex: 0 0 auto;
                }}
                .mod-chat-send .q-btn__content {{
                    width: 100%;
                }}
                .mod-chat-send-stack {{
                    align-items: center;
                    gap: 0.08rem !important;
                }}
                .mod-chat-send-label {{
                    color: #ffffff !important;
                    font-size: 0.84rem !important;
                    font-weight: 950 !important;
                    line-height: 1 !important;
                    letter-spacing: 0.02em;
                }}
                .mod-chat-send-subtext {{
                    color: rgba(228, 228, 231, 0.74) !important;
                    font-size: 0.57rem !important;
                    font-weight: 900 !important;
                    line-height: 1 !important;
                    letter-spacing: 0.08em;
                }}
                .mod-chat-unread-bar {{
                    position: absolute;
                    right: 0.82rem;
                    bottom: 0.72rem;
                    z-index: 3;
                    align-items: center;
                    justify-content: center;
                    max-width: min(16rem, calc(100% - 1.44rem));
                    min-height: 2rem;
                    padding: 0.28rem 0.62rem;
                    border: 1px solid var(--mod-accent-border);
                    border-radius: 0;
                    background: rgba(16, 16, 22, 0.92);
                    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.32);
                    backdrop-filter: blur(12px);
                    cursor: pointer;
                    transition: background 140ms ease, border-color 140ms ease;
                }}
                .mod-chat-unread-bar:hover {{
                    background: rgba(28, 28, 38, 0.96);
                    border-color: var(--mod-accent-border);
                }}
                .mod-chat-unread-live {{
                    animation: mod-chat-unread-arrive 260ms var(--mod-motion-ease) both;
                }}
                @keyframes mod-chat-unread-arrive {{
                    0% {{ opacity: 0; scale: 0.96; translate: 0 0.25rem; }}
                    70% {{ opacity: 1; scale: 1.015; translate: 0 0; }}
                    100% {{ opacity: 1; scale: 1; translate: 0 0; }}
                }}
                .mod-chat-unread-count {{
                    color: rgba(228, 228, 231, 0.86) !important;
                    font-size: 0.65rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.04em;
                    line-height: 1.1 !important;
                    text-align: center;
                    white-space: nowrap;
                }}
                .mod-chat-composer-warning {{
                    padding-inline: 0.1rem;
                }}
                .mod-detail-grid {{
                    width: 100%;
                    gap: 0.85rem;
                }}
                .mod-detail-item {{
                    min-width: 0;
                    padding-top: 0.4rem;
                    border-top: 1px solid rgba(63, 63, 70, 0.45);
                }}
                .mod-mod-page-links {{
                    width: 100%;
                }}
                .mod-mod-page-link {{
                    color: var(--mod-accent-text) !important;
                    font-size: 0.88rem !important;
                    font-weight: 900 !important;
                    text-decoration: underline !important;
                    text-decoration-color: var(--mod-accent-border-strong) !important;
                    text-underline-offset: 0.2rem;
                }}
                .mod-mod-page-link:hover {{
                    color: var(--mod-accent-text-strong) !important;
                    text-decoration-color: var(--mod-accent-text) !important;
                }}
                .mod-action {{
                    border-radius: 0 !important;
                    background: var(--mod-accent-dark) !important;
                    color: #fff !important;
                    border: 1px solid var(--mod-accent);
                    text-decoration: none !important;
                    font-weight: 950;
                    letter-spacing: 0.02em;
                    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
                }}
                .mod-action-border-accent {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border) !important;
                    box-shadow: none !important;
                }}
                .mod-toolbar-chat-button,
                .mod-toolbar-chat-button:hover,
                .mod-toolbar-chat-button:focus-visible {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                    border-color: var(--mod-accent-border) !important;
                    box-shadow: none !important;
                }}
                .mod-action:hover {{ filter: brightness(1.14); transform: translateY(-1px); }}
                .mod-toolbar-menu-mobile-only {{ display: none !important; }}
                .mod-list-button:active,
                .mod-action:active,
                .mod-toolbar-button:active,
                .mod-app-card-api-link:active {{
                    animation: mod-control-press 130ms ease-out;
                }}
                @keyframes mod-control-press {{
                    50% {{ scale: 0.985; translate: 0 1px; filter: brightness(0.94); }}
                    100% {{ scale: 1; translate: 0 0; }}
                }}
                .mod-user-header-tray-shell:not(:has(.mod-user-header-tray)) {{
                    display: none !important;
                    min-height: 0 !important;
                }}
                @media (min-width: 961px) and (max-width: 1023px) {{
                    .mod-user-header-row {{ flex-wrap: nowrap !important; }}
                }}
                @media (max-width: 960px) {{
                    .mod-user-header-tray-shell {{ min-height: 0 !important; }}
                    .mod-hero-header {{ flex-wrap: wrap !important; }}
                    .mod-hero-header-main {{ flex-basis: 100%; }}
                    .mod-app-hero-status {{
                        min-width: 0;
                        width: 100%;
                        align-items: flex-start;
                        text-align: left;
                    }}
                    .mod-app-hero-join-addresses {{ align-items: flex-start; }}
                    .mod-status-content {{
                        order: 2;
                        width: 100%;
                        flex-basis: 100%;
                    }}
                    .mod-status-figure-inline {{
                        order: 1;
                        width: 100%;
                        min-width: 0;
                        justify-content: flex-start;
                    }}
                    .mod-chat-shell-header {{ flex-wrap: wrap !important; }}
                    .mod-chat-shell-header-main {{ flex-basis: 100%; }}
                    .mod-chat-shell-card {{
                        width: min(100%, calc(100vw - 1rem)) !important;
                        min-height: 32rem;
                    }}
                    .mod-corner-badges {{
                        width: 100%;
                        max-width: none;
                    }}
                    .mod-corner-badges-wide {{ min-width: 0; }}
                }}
                @media (max-width: 720px) {{
                    .mod-recipe-workbench {{
                        grid-template-columns: minmax(0, 1fr);
                    }}
                    .mod-recipe-slot-grid {{
                        max-width: none;
                    }}
                    .mod-recipe-browser-toolbar {{
                        align-items: stretch;
                    }}
                    .mod-recipe-browser-filter {{
                        flex: 1 1 calc(50% - 0.5rem);
                        min-width: 10rem;
                    }}
                    .mod-recipe-browser-status {{
                        width: 100%;
                        justify-content: space-between;
                    }}
                    .mod-recipe-selection-actions > * {{
                        flex-basis: 100%;
                    }}
                    .mod-section-strip {{
                        align-items: stretch;
                    }}
                    .mod-section-strip > .mod-section-tabs-shell {{
                        flex: 1 1 100%;
                    }}
                    .mod-section-tabs {{
                        max-width: 100%;
                    }}
                    .mod-section-tabs .q-tab {{ flex: 1 1 calc(50% - 0.5rem); min-width: 0; }}
                    .mod-section-chrome {{
                        width: 100%;
                        justify-content: flex-start;
                    }}
                    .mod-section-chrome-panel {{
                        margin-left: 0;
                        width: 100%;
                        max-width: 100%;
                    }}
                    .mod-mods-toolbar-filters {{
                        display: flex !important;
                        align-items: center;
                        width: 100%;
                    }}
                    .mod-mods-toolbar-search {{
                        flex: 1 1 0;
                        width: auto;
                    }}
                    .mod-config-select.mod-mods-toolbar-sort {{
                        flex: 0 1 9rem;
                        width: 9rem;
                        min-width: 7.5rem;
                    }}
                    .mod-mods-toolbar-actions {{
                        display: flex !important;
                        flex-wrap: wrap;
                        width: 100%;
                        align-self: stretch;
                        margin-left: 0;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-button {{
                        flex: 1 1 0;
                        width: 0;
                        min-width: 0;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-selection-button {{
                        flex: 0 0 auto;
                        width: auto;
                        min-width: 4.75rem;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-menu-button {{
                        flex: 0 0 2.5rem;
                        width: 2.5rem;
                        min-width: 2.5rem;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-status-button {{
                        flex: 1 0 100%;
                        width: 100%;
                        min-width: 0;
                        order: -1;
                    }}
                    .mod-mods-toolbar-actions .mod-list-button.danger {{
                        margin-left: 0;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-mobile-secondary {{
                        display: none !important;
                    }}
                    .mod-toolbar-menu-mobile-only {{ display: flex !important; }}
                    .mod-tab-toolbar > :is(.mod-config-select, .mod-config-search) {{
                        flex-basis: 100%;
                        min-width: 0;
                    }}
                    .mod-app-details-dialog-card {{ width: calc(100vw - 1rem) !important; }}
                    .mod-app-details-section {{ padding: 0.85rem 0.85rem; }}
                    .mod-user-appearance-section {{ padding: 0.75rem 0.75rem; }}
                    .mod-app-details-point-field {{
                        flex-basis: calc(50% - 0.25rem);
                        max-width: none;
                        min-width: 0;
                    }}
                    .mod-app-details-state-button {{
                        width: 100% !important;
                        max-width: none !important;
                    }}
                    .mod-tab-toolbar-actions {{ width: 100%; margin-left: 0; }}
                    .mod-inline-toolbar-actions {{ width: auto; margin-left: auto; }}
                    .mod-save-card-button {{ flex-basis: 100%; min-width: 0; }}
                    .mod-chat-entry {{
                        flex-wrap: wrap;
                    }}
                    .mod-chat-entry-meta {{
                        flex-basis: 100%;
                        justify-content: space-between;
                        margin-left: 0;
                    }}
                    .mod-chat-entry-time {{
                        text-align: left;
                        padding-top: 0;
                    }}
                    .mod-chat-entry-reply {{
                        opacity: 1;
                    }}
                    .mod-chat-input {{
                        min-width: 0;
                    }}
                    .mod-chat-timeline-shell {{
                        min-height: 16rem;
                    }}
                    .mod-chat-scroll-area {{
                        height: min(54vh, 32rem);
                        min-height: 14rem;
                    }}
                    .mod-setting-shell {{
                        grid-template-columns: 1fr;
                        padding-right: 3.05rem;
                    }}
                    .mod-setting-field-paragraph {{
                        width: 100%;
                        margin-right: 0 !important;
                    }}
                    .mod-setting-meta {{
                        align-items: flex-start;
                    }}
                    .mod-setting-meta-value {{
                        text-align: left;
                    }}
                    .mod-setting-meta-default {{
                        text-align: left;
                    }}
                    .mod-setting-meta-secret {{
                        justify-content: flex-start;
                    }}
                    .mod-setting-meta-secret-reveal {{
                        text-align: left;
                    }}
                    .mod-setting-meta-secret-layer {{
                        text-align: left;
                    }}
                    .mod-row {{ grid-template-columns: auto minmax(0, 1fr); }}
                    .mod-row-main {{ grid-column: 2; }}
                    .mod-row-meta {{ grid-column: 2; justify-content: flex-start; min-width: 0; }}
                    .mod-row-download {{ grid-column: 2; }}
                    .mod-detail-grid {{ grid-template-columns: 1fr !important; }}
                    .mod-chat-panel {{
                        --mod-chat-panel-inline-padding: 0.8rem;
                        padding: 0.8rem;
                    }}
                    .mod-chat-panel-embedded {{
                        --mod-chat-panel-inline-padding: 0rem;
                        --mod-chat-shell-inline-padding: 0rem;
                        padding: 0.1rem 0 0 !important;
                    }}
                    .mod-chat-header-top {{ align-items: flex-start; }}
                    .mod-chat-status-row {{
                        width: 100%;
                        margin-left: 0;
                        justify-content: flex-start;
                    }}
                    .mod-chat-section-head {{
                        align-items: flex-start !important;
                    }}
                    .mod-chat-section-hint {{
                        text-align: left;
                    }}
                    .mod-chat-timeline-shell {{ padding: 0.7rem 0; }}
                    .mod-chat-composer-surface {{ padding: 0.7rem; }}
                    .mod-chat-unread-bar {{
                        left: 0.7rem;
                        right: 0.7rem;
                        bottom: 0.7rem;
                        max-width: none;
                        justify-content: center;
                    }}
                    .mod-chat-dialog-card {{
                        width: calc(100vw - 0.75rem) !important;
                        max-height: calc(100vh - 0.75rem) !important;
                    }}
                    .mod-fake-chat-dialog-card {{
                        width: calc(100vw - 0.75rem) !important;
                    }}
                    .mod-chat-timeline {{ max-height: none; }}
                    .mod-chat-input {{ flex-basis: 100%; min-width: 0; }}
                    .mod-chat-send {{ width: 100%; }}
                }}
                @media (max-width: 30rem) {{
                    .mod-section-tabs .q-tab {{
                        flex: 0 1 3rem;
                        min-width: 3rem;
                        padding-inline: 0.65rem;
                    }}
                    .mod-section-tabs .q-tab__label {{ display: none; }}
                    .mod-section-tabs .q-tab__content {{ gap: 0; }}
                    .mod-section-tabs .q-tab__icon {{ font-size: 1.2rem; }}
                    .mod-inline-toolbar {{
                        flex-wrap: wrap;
                    }}
                    .mod-inline-toolbar > :is(.mod-settings-search, .mod-console-select-action) {{
                        flex-basis: 100%;
                    }}
                    .mod-inline-toolbar-actions {{
                        width: 100%;
                        margin-left: 0;
                    }}
                    .mod-mods-toolbar-filters {{
                        display: grid !important;
                        grid-template-columns: minmax(0, 1fr) auto;
                    }}
                    .mod-mods-toolbar-search {{
                        grid-column: 1 / -1;
                        width: 100%;
                    }}
                    .mod-config-select.mod-mods-toolbar-sort {{
                        grid-column: 1;
                        width: 100%;
                        min-width: 0;
                    }}
                    .mod-mods-toolbar-result-count {{
                        grid-column: 2;
                    }}
                    .mod-mods-toolbar-actions {{
                        display: grid !important;
                        grid-template-columns: repeat(2, minmax(0, 1fr));
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-button {{
                        width: 100%;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-selection-button {{
                        width: auto;
                        justify-self: end;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-menu-button {{
                        grid-column: 2;
                        width: 2.5rem;
                        justify-self: end;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-status-button {{
                        grid-column: 1;
                        width: 100%;
                    }}
                    .mod-mods-toolbar-actions .mod-list-button.danger {{
                        grid-column: 2;
                        width: 100%;
                    }}
                }}
                .mod-factorio-generator {{
                    display: block !important;
                    width: 100% !important;
                    max-width: none !important;
                    box-sizing: border-box;
                    overflow: hidden;
                    border-color: #3f3d43 !important;
                    background: #18171b !important;
                    box-shadow:
                        inset 0 0 0 1px rgba(0, 0, 0, 0.9),
                        0 16px 36px rgba(0, 0, 0, 0.26) !important;
                }}
                .mod-factorio-titlebar {{
                    display: flex;
                    width: 100%;
                    box-sizing: border-box;
                    gap: 0.85rem;
                    align-items: center;
                    padding: 0.7rem 0.9rem;
                    border-bottom: 1px solid #454148;
                    background: linear-gradient(180deg, #27252a, #201f23) !important;
                }}
                .mod-factorio-title {{
                    color: #f8d79a !important;
                    font-size: 1.1rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.01em;
                    line-height: 1.1 !important;
                    text-shadow: 0 1px 0 rgba(0, 0, 0, 0.95);
                }}
                .mod-factorio-kicker {{
                    color: #aaa7ae !important;
                    font-size: 0.7rem !important;
                    font-weight: 750 !important;
                    letter-spacing: 0.03em;
                    text-transform: uppercase;
                }}
                .mod-factorio-header-actions {{ margin-left: auto; justify-content: flex-end; }}
                .mod-factorio-seed {{ min-width: min(14rem, 100%); justify-content: flex-end; }}
                .mod-factorio-seed .mod-factorio-plain-number {{ margin-top: 0; }}
                .mod-factorio-seed .mod-factorio-plain-value {{ width: 9.5rem; }}
                .mod-factorio-seed-label {{ color: #e7e3e8 !important; font-weight: 800 !important; }}
                .mod-factorio-tabs-shell {{
                    width: 100%;
                    box-sizing: border-box;
                    padding: 0.35rem 0.9rem 0;
                    overflow-x: auto;
                    border-bottom: 1px solid #3f3d43;
                    background: #1d1c20;
                    scrollbar-color: #555159 #1d1c20;
                }}
                .mod-factorio-tabs {{ gap: 0.18rem; min-width: max-content; min-height: 2.5rem; }}
                .mod-factorio-tabs .q-tab {{
                    flex: 0 0 auto !important;
                    min-height: 2.5rem !important;
                    padding: 0 0.82rem !important;
                    color: #bbb8c0 !important;
                    background: #29272c !important;
                    border: 1px solid #3c3940;
                    border-bottom: 0;
                    font-weight: 850 !important;
                    text-transform: none !important;
                }}
                .mod-factorio-tabs .q-tab--active {{
                    color: #f8d79a !important;
                    background: #242226 !important;
                    box-shadow: inset 0 2px 0 #d97706 !important;
                }}
                .mod-factorio-tabs .q-tab__indicator {{ display: none !important; }}
                .mod-factorio-panels {{ background: #242226 !important; }}
                .mod-factorio-panel {{
                    width: 100% !important;
                    max-width: none !important;
                    padding: 0.32rem 0.9rem 0.8rem !important;
                }}
                .mod-factorio-group-title {{
                    color: #f2d49a !important;
                    font-size: 0.92rem !important;
                    font-weight: 850 !important;
                }}
                .mod-factorio-group-hint {{
                    color: #c4c4cb !important;
                    font-size: 0.74rem !important;
                    font-weight: 800 !important;
                    white-space: nowrap;
                }}
                .mod-factorio-control-table,
                .mod-factorio-option-group {{
                    display: block !important;
                    width: 100% !important;
                    max-width: none !important;
                    box-sizing: border-box;
                    margin-top: 0.38rem;
                    padding: 0.56rem;
                    border: 1px solid #3a373e;
                    background: #2d2b30;
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035);
                }}
                .mod-factorio-control-table > .mod-factorio-group-title {{
                    display: block;
                    padding: 0.02rem 0.08rem 0.42rem;
                }}
                .mod-factorio-control-header,
                .mod-factorio-control-row {{
                    display: grid;
                    grid-template-columns: minmax(12rem, 1.2fr) repeat(3, minmax(10rem, 1fr));
                    gap: 0;
                }}
                .mod-factorio-control-table-cols-2 :is(.mod-factorio-control-header, .mod-factorio-control-row) {{
                    grid-template-columns: minmax(12rem, 1.2fr) repeat(2, minmax(10rem, 1fr));
                }}
                .mod-factorio-control-header {{
                    color: #d8d4dc;
                    background: #27252a;
                    border: 1px solid #3a373e;
                    font-size: 0.78rem;
                    font-weight: 900;
                }}
                .mod-factorio-control-header > * {{
                    padding: 0.48rem 0.55rem;
                    border-left: 1px solid #3a373e;
                    text-align: center;
                }}
                .mod-factorio-control-header > :first-child {{ border-left: 0; text-align: left; }}
                .mod-factorio-control-row {{
                    align-items: stretch;
                    background: #312f34;
                    border: 1px solid #3a373e;
                    border-top: 0;
                }}
                .mod-factorio-control-row > * {{
                    min-width: 0;
                    padding: 0.45rem 0.55rem;
                    border-left: 1px solid #403d44;
                }}
                .mod-factorio-control-row > :first-child {{ border-left: 0; }}
                .mod-factorio-control-row:nth-child(even) {{ background: #2d2b30; }}
                .mod-factorio-control-enabled .q-checkbox__inner {{ color: #d97706 !important; }}
                .mod-factorio-control-row:has(.mod-factorio-control-enabled .q-checkbox__inner:not(.q-checkbox__inner--truthy)) .mod-factorio-range-field {{
                    opacity: 0.46;
                    pointer-events: none;
                }}
                .mod-factorio-control-label {{
                    min-width: 0;
                    color: #f4f4f5 !important;
                    font-weight: 800 !important;
                    overflow-wrap: anywhere;
                }}
                .mod-factorio-range-field {{ min-width: 0; gap: 0.2rem !important; }}
                .mod-factorio-range-label {{
                    color: #d4d4d8 !important;
                    font-size: 0.72rem !important;
                    font-weight: 850 !important;
                    line-height: 1.1 !important;
                }}
                .mod-factorio-range-value {{ width: 6rem; min-width: 6rem; }}
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__control {{
                    min-height: 2rem !important;
                    background: #16161a !important;
                    border: 1px solid #111114 !important;
                    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.055) !important;
                }}
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__control::before,
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__control::after {{
                    border: 0 !important;
                }}
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__native,
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__input,
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) input,
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) textarea,
                .mod-factorio-map-type .q-field__label,
                .mod-factorio-map-type .q-icon {{
                    color: #f4f4f5 !important;
                    -webkit-text-fill-color: #f4f4f5 !important;
                    opacity: 1 !important;
                    font-variant-numeric: tabular-nums;
                }}
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field__label {{
                    color: #c4c4cb !important;
                    opacity: 1 !important;
                }}
                :is(.mod-factorio-range-value, .mod-factorio-plain-value, .mod-factorio-map-string-input, .mod-factorio-map-type) .q-field--focused .q-field__label {{
                    color: #f8c45b !important;
                }}
                .mod-factorio-generator input[type="number"] {{
                    appearance: textfield;
                    -moz-appearance: textfield;
                    color-scheme: dark;
                }}
                .mod-factorio-generator input[type="number"]::-webkit-inner-spin-button,
                .mod-factorio-generator input[type="number"]::-webkit-outer-spin-button {{
                    margin: 0;
                    appearance: none;
                    -webkit-appearance: none;
                }}
                .mod-factorio-map-type {{ width: min(28rem, 100%); margin-top: 0.5rem; }}
                .mod-factorio-slider {{ min-width: 2.75rem; }}
                .mod-factorio-slider .q-slider__track-container {{ height: 0.55rem !important; }}
                .mod-factorio-slider .q-slider__track {{ color: #141416 !important; opacity: 1 !important; }}
                .mod-factorio-slider .q-slider__track--active {{ color: #d97706 !important; }}
                .mod-factorio-slider .q-slider__thumb {{ color: #d4d4d8 !important; }}
                .mod-factorio-slider .q-slider__focus-ring {{ color: rgba(217, 119, 6, 0.25) !important; }}
                .mod-factorio-range-hint {{ color: var(--mod-muted) !important; font-size: 0.69rem !important; line-height: 1.15 !important; }}
                .mod-factorio-option-grid {{
                    width: 100%;
                    max-width: none;
                    box-sizing: border-box;
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 0.65rem;
                    margin-top: 0.62rem;
                }}
                .mod-factorio-option-grid > .mod-factorio-range-field {{
                    padding: 0.45rem;
                    background: rgba(0, 0, 0, 0.14);
                    border: 1px solid rgba(24, 24, 27, 0.85);
                }}
                .mod-factorio-toggle-row {{
                    justify-content: center;
                    gap: 0.35rem !important;
                    padding: 0.45rem;
                    background: rgba(0, 0, 0, 0.14);
                    border: 1px solid rgba(24, 24, 27, 0.85);
                }}
                .mod-factorio-toggle-row .q-toggle__inner,
                .mod-factorio-section-toggle .q-checkbox__inner {{ color: #d97706 !important; }}
                .mod-factorio-advanced-grid {{
                    width: 100%;
                    max-width: none;
                    box-sizing: border-box;
                    display: grid;
                    grid-template-columns: minmax(0, 1fr);
                    gap: 0.7rem;
                }}
                .mod-factorio-advanced-top-grid {{
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }}
                .mod-factorio-plain-number {{ margin-top: 0.5rem; }}
                .mod-factorio-map-string {{
                    display: grid;
                    grid-template-columns: minmax(12rem, 0.8fr) minmax(18rem, 1.7fr) auto;
                    align-items: center;
                    gap: 0.7rem;
                    padding: 0.8rem 0.9rem;
                    background: #1d1c20;
                    border-top: 1px solid #3a373e;
                }}
                .mod-factorio-map-string-input textarea {{
                    min-height: 4.75rem !important;
                    max-height: 11rem !important;
                    overflow: auto !important;
                    resize: vertical !important;
                }}
                .mod-factorio-save {{
                    --mod-control-foreground: #251604;
                    min-width: 6.5rem;
                    justify-content: center;
                    color: #251604 !important;
                    background: #d97706 !important;
                    border-color: #f8b34c !important;
                    box-shadow: inset 0 1px 0 rgba(255, 247, 232, 0.34), 0 5px 14px rgba(0, 0, 0, 0.24) !important;
                    text-transform: none !important;
                }}
                .mod-factorio-running-world,
                .mod-factorio-map-string-action {{
                    --mod-control-foreground: #ece8ed;
                    color: #ece8ed !important;
                    background: #2b2930 !important;
                    border-color: #4b4750 !important;
                    box-shadow: none !important;
                    text-transform: none !important;
                }}
                .mod-factorio-running-world {{
                    min-width: 10.5rem;
                }}
                .mod-factorio-running-world .q-icon {{ color: #f2b55b !important; }}
                .mod-factorio-running-world.q-btn--disabled,
                .mod-factorio-map-string-action.q-btn--disabled {{
                    color: #8a858e !important;
                    background: #242226 !important;
                    border-color: #37333a !important;
                }}
                @media (max-width: 64rem) {{
                    .mod-factorio-control-header,
                    .mod-factorio-control-row {{ grid-template-columns: minmax(10rem, 1fr) repeat(3, minmax(8rem, 1fr)); }}
                    .mod-factorio-control-table-cols-2 :is(.mod-factorio-control-header, .mod-factorio-control-row) {{
                        grid-template-columns: minmax(10rem, 1fr) repeat(2, minmax(8rem, 1fr));
                    }}
                    .mod-factorio-map-string {{ grid-template-columns: 1fr; align-items: stretch; }}
                }}
                @media (max-width: 48rem) {{
                    .mod-factorio-titlebar {{ align-items: flex-start; flex-direction: column; }}
                    .mod-factorio-header-actions {{ width: 100%; margin-left: 0; justify-content: flex-start; }}
                    .mod-factorio-seed {{ width: 100%; justify-content: flex-start; }}
                    .mod-factorio-control-header {{ display: none; }}
                    .mod-factorio-control-row {{
                        grid-template-columns: 1fr;
                        gap: 0;
                        border-top: 1px solid #18181b;
                    }}
                    .mod-factorio-control-row > * {{ border-left: 0; border-top: 1px solid #202024; }}
                    .mod-factorio-control-row > :first-child {{ border-top: 0; }}
                    .mod-factorio-option-grid,
                    .mod-factorio-advanced-grid {{ grid-template-columns: 1fr; }}
                    .mod-factorio-header-actions .mod-factorio-save {{ width: 100%; }}
                }}
                @media (prefers-reduced-motion: reduce) {{
                    *,
                    *::before,
                    *::after {{
                        scroll-behavior: auto !important;
                        animation-duration: 0.01ms !important;
                        animation-delay: 0ms !important;
                        animation-iteration-count: 1 !important;
                        transition-duration: 0.01ms !important;
                    }}
                }}
                /* Quasar can give a control's icon a semantic text colour independently of its label. */
                :is(.q-btn, .q-tab, .mod-system-native-tab) {{
                    color: var(--mod-control-foreground) !important;
                }}
                :is(.q-btn, .q-tab) :is(.q-btn__content, .q-tab__content),
                :is(.q-btn, .q-tab, .mod-system-native-tab) :is(.q-icon, .material-icons) {{
                    color: var(--mod-control-foreground) !important;
                }}
                :is(.q-btn, .q-tab, .mod-system-native-tab) svg {{
                    fill: currentColor !important;
                }}
            </style>
            """

    def stylesheet(self) -> str:
        html = self.css()
        style_start = html.find("<style>")
        style_end = html.rfind("</style>")
        if style_start < 0 or style_end <= style_start:
            raise RuntimeError("Mod web theme CSS wrapper is invalid.")
        return html[style_start + len("<style>") : style_end]


DEFAULT_MOD_WEB_THEME = ModWebTheme(
    name="void_square",
    palette=ModWebPalette(
        background="#000000",
        card="#08080a",
        card_raised="#101014",
        border="#27272a",
        border_hot="#3f3f46",
        text="#f4f4f5",
        muted="#a1a1aa",
        dim="#71717a",
        purple="#8b5cf6",
        purple_dark="#3b164d",
        red="#dc2626",
        red_dark="#5f111b",
        warning="#f59e0b",
        warning_dark="#3a230b",
        warning_text="#fde68a",
        panel="rgba(0, 0, 0, 0.96)",
        nicegui=NiceGuiPalette(
            primary="#8b5cf6",
            secondary="#52525b",
            accent="#8b5cf6",
            positive="#6b7280",
            negative="#dc2626",
            info="#8b5cf6",
            warning="#f59e0b",
        ),
    ),
)

MOD_WEB_THEME_STYLESHEET = DEFAULT_MOD_WEB_THEME.stylesheet()
MOD_WEB_THEME_VERSION = hashlib.sha256(MOD_WEB_THEME_STYLESHEET.encode("utf-8")).hexdigest()[:12]

MOD_WEB_ACTION_BASE_CLASSES = "mod-action inline-flex items-center transition"


def mod_web_badge_class(tone: BadgeTone) -> str:
    return f"mod-badge {tone}"


def apply_mod_web_theme(*, ui: Any, theme: ModWebTheme = DEFAULT_MOD_WEB_THEME) -> None:
    palette = theme.palette.nicegui
    ui.colors(
        primary=palette.primary,
        secondary=palette.secondary,
        accent=palette.accent,
        positive=palette.positive,
        negative=palette.negative,
        info=palette.info,
        warning=palette.warning,
    )
    if theme is DEFAULT_MOD_WEB_THEME:
        ui.add_head_html(
            f'<link rel="stylesheet" href="/mod-web/assets/theme.css?v={MOD_WEB_THEME_VERSION}">'
            f'<script src="/mod-web/assets/toasts.js?v={MOD_WEB_TOAST_VERSION}"></script>'
        )
    else:
        ui.add_head_html(
            theme.css()
            + f'<script src="/mod-web/assets/toasts.js?v={MOD_WEB_TOAST_VERSION}"></script>'
        )
