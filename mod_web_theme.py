from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

BadgeTone = Literal["black", "purple", "red", "warn", "grey"]


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
                    --mod-red: {palette.red};
                    --mod-red-dark: {palette.red_dark};
                    --mod-warning: {palette.warning};
                    --mod-warning-dark: {palette.warning_dark};
                    --mod-panel: {palette.panel};
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
                    background:
                        radial-gradient(circle at 14% -8%, rgba(139, 92, 246, 0.24), transparent 30rem),
                        radial-gradient(circle at 88% 6%, rgba(220, 38, 38, 0.18), transparent 34rem),
                        linear-gradient(180deg, #020204 0%, #07070a 46%, #101012 100%) !important;
                }}
                html {{
                    overflow-y: scroll;
                    scrollbar-gutter: stable;
                }}
                .q-dialog__backdrop {{
                    background: rgba(2, 2, 4, 0.78) !important;
                    backdrop-filter: blur(7px);
                }}
                .q-dialog__inner {{
                    background: transparent !important;
                }}
                .q-notification {{
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
                .q-notification.bg-positive,
                .q-notification--standard.bg-positive {{
                    border-color: rgba(107, 114, 128, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(148, 163, 184, 0.14), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        rgba(20, 24, 31, 0.97) !important;
                }}
                .q-notification.bg-info,
                .q-notification--standard.bg-info {{
                    border-color: rgba(139, 92, 246, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(139, 92, 246, 0.2), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-purple-dark) !important;
                }}
                .q-notification.bg-warning,
                .q-notification--standard.bg-warning {{
                    border-color: rgba(245, 158, 11, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(245, 158, 11, 0.2), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-warning-dark) !important;
                }}
                .q-notification.bg-negative,
                .q-notification--standard.bg-negative {{
                    border-color: rgba(220, 38, 38, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(220, 38, 38, 0.2), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-red-dark) !important;
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
                    border-color: rgba(245, 158, 11, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(245, 158, 11, 0.2), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-warning-dark) !important;
                }}
                #too_long_message_popup.nicegui-error-popup {{
                    border-color: rgba(220, 38, 38, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(220, 38, 38, 0.2), transparent 45%),
                        linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0)),
                        var(--mod-red-dark) !important;
                }}
                .mod-page {{ max-width: 1180px; margin: 0 auto; }}
                .mod-page-app {{ max-width: 1380px; }}
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
                        linear-gradient(90deg, var(--mod-hero-border-glow, rgba(139, 92, 246, 0.18)), transparent 32%) padding-box,
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
                .mod-app-hero-status-value-purple {{ color: #d8b4fe !important; }}
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
                    color: rgba(248, 113, 113, 0.88);
                    filter: drop-shadow(0 12px 24px rgba(220, 38, 38, 0.24));
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
                        linear-gradient(135deg, rgba(220, 38, 38, 0.13), transparent 58%),
                        rgba(9, 9, 13, 0.82);
                    border: 1px solid rgba(248, 113, 113, 0.3);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
                }}
                .mod-status-detail-label {{
                    color: rgba(248, 113, 113, 0.82) !important;
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
                .mod-app-card {{ transition: border-color 150ms ease, background 150ms ease; }}
                .mod-app-card:hover {{
                    border-color: rgba(139, 92, 246, 0.58) !important;
                    background: linear-gradient(135deg, #0d0d12, #151018) !important;
                }}
                .mod-node-card:hover {{
                    transform: translateY(-1px);
                    border-color: rgba(139, 92, 246, 0.58) !important;
                    background: linear-gradient(135deg, #0d0d12, #151018) !important;
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
                    padding: 0.26rem 0.78rem 0.26rem 0.92rem !important;
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
                .mod-app-card-starting::before {{
                    top: 0.42rem;
                    bottom: auto;
                    left: 0.08rem;
                    width: 0.34rem;
                    height: 0.34rem;
                    border-radius: 999px;
                    animation: mod-app-card-strip-starting 1.35s cubic-bezier(0.56, 0.04, 0.44, 0.96) infinite;
                    will-change: top, opacity, filter;
                }}
                .mod-app-card-starting.mod-app-card-live::before {{
                    animation:
                        mod-app-card-strip-live 760ms ease-out,
                        mod-app-card-strip-starting 1.35s cubic-bezier(0.56, 0.04, 0.44, 0.96) 760ms infinite;
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
                .mod-app-card-stopping::before {{
                    width: 0.62rem;
                    background:
                        linear-gradient(
                            180deg,
                            rgba(0, 0, 0, 0.14),
                            rgba(0, 0, 0, 0.14)
                        ) 0 0 / 100% 100% no-repeat,
                        linear-gradient(
                            150deg,
                            transparent 0 26%,
                            rgba(255, 255, 255, 0.92) 26% 38%,
                            var(--mod-app-strip-color, var(--mod-border-hot)) 38% 52%,
                            transparent 52% 100%
                        ) 0 0 / 100% 1rem repeat-y,
                        linear-gradient(
                            30deg,
                            transparent 0 26%,
                            rgba(255, 255, 255, 0.92) 26% 38%,
                            var(--mod-app-strip-color, var(--mod-border-hot)) 38% 52%,
                            transparent 52% 100%
                        ) 0 0.5rem / 100% 1rem repeat-y;
                    animation: mod-app-card-strip-stopping 780ms linear infinite;
                    will-change: background-position, opacity, filter;
                }}
                .mod-app-card-stopping.mod-app-card-live::before {{
                    animation:
                        mod-app-card-strip-live 760ms ease-out,
                        mod-app-card-strip-stopping 780ms linear 760ms infinite;
                }}
                .mod-app-runtime-chip-live {{
                    animation: mod-app-runtime-chip-live 820ms ease-out;
                    will-change: transform, opacity;
                }}
                @keyframes mod-app-card-live-pulse {{
                    0% {{
                        border-color: rgba(139, 92, 246, 0.9) !important;
                        box-shadow:
                            0 0 0 1px rgba(196, 181, 253, 0.24),
                            0 24px 70px rgba(0, 0, 0, 0.48),
                            inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
                    }}
                    55% {{
                        border-color: rgba(196, 181, 253, 0.72) !important;
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
                        top: 0.42rem;
                        opacity: 0.86;
                        filter: saturate(0.96) brightness(0.98);
                    }}
                    50% {{
                        top: calc(100% - 0.76rem);
                        opacity: 1;
                        filter: saturate(1.12) brightness(1.22);
                    }}
                    100% {{
                        top: 0.42rem;
                        opacity: 0.86;
                        filter: saturate(0.96) brightness(0.98);
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
                        background-position: 0 0, 0 0, 0 0.5rem;
                        opacity: 0.92;
                        filter: saturate(1) brightness(1);
                    }}
                    100% {{
                        background-position: 0 0, 0 1rem, 0 1.5rem;
                        opacity: 1;
                        filter: saturate(1.02) brightness(1.04);
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
                        box-shadow: 0 0 0 0.18rem rgba(196, 181, 253, 0.16);
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
                    border-color: #30203a !important;
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
                    border-color: rgba(139, 92, 246, 0.58) !important;
                    background: linear-gradient(135deg, #0d0d12, #151018) !important;
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
                    width: 0.42rem;
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
                .mod-title {{ color: var(--mod-text) !important; text-shadow: 0 0 26px rgba(139, 92, 246, 0.26); }}
                .mod-title-small {{ color: var(--mod-text) !important; }}
                .mod-subtitle {{ color: var(--mod-muted) !important; }}
                .mod-error-text {{ color: #f87171 !important; }}
                .mod-select-form {{ display: flex; flex-direction: column; gap: 0.55rem; }}
                .mod-section-strip {{
                    align-items: flex-start;
                }}
                .mod-section-tabs-shell {{
                    flex: 1 1 24rem;
                    min-width: 0;
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
                }}
                .mod-section-tabs .q-tab--active {{
                    color: var(--mod-text) !important;
                    border-color: rgba(139, 92, 246, 0.72) !important;
                    background:
                        linear-gradient(135deg, rgba(139, 92, 246, 0.16), transparent 65%),
                        rgba(24, 16, 36, 0.96) !important;
                    box-shadow:
                        inset 0 0 0 1px rgba(196, 181, 253, 0.14),
                        0 12px 26px rgba(0, 0, 0, 0.28);
                }}
                .mod-section-tabs .q-tabs__arrow {{
                    color: var(--mod-muted) !important;
                }}
                .mod-section-tabs .q-tab__indicator {{
                    display: none !important;
                }}
                .mod-section-chrome {{
                    flex: 0 1 auto;
                    min-width: 0;
                    margin-left: auto;
                }}
                .mod-section-chrome-panel {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: flex-end;
                    gap: 0.75rem;
                    min-width: 0;
                }}
                .mod-section-chrome-badge-stack {{
                    display: flex;
                    flex-direction: column;
                    align-items: flex-end;
                    justify-content: flex-start;
                    gap: 0.35rem;
                    min-width: 0;
                }}
                .mod-section-chrome-badge-row {{
                    display: flex;
                    flex-direction: row-reverse;
                    align-items: flex-start;
                    justify-content: flex-end;
                    gap: 0.5rem;
                    min-width: 0;
                }}
                .mod-section-chrome-badge-column {{
                    display: flex;
                    flex-direction: column;
                    align-items: flex-end;
                    justify-content: flex-start;
                    gap: 0.35rem;
                    min-width: 0;
                }}
                .mod-section-chrome-actions {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: flex-end;
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
                .mod-mods-toolbar .mod-mods-toolbar-sort {{
                    flex: 0 0 12rem;
                    min-width: 12rem;
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
                    background: linear-gradient(135deg, #6d28d9, #8b5cf6) !important;
                    border-color: rgba(196, 181, 253, 0.82) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.12),
                        0 10px 26px rgba(76, 29, 149, 0.36) !important;
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
                .mod-list-button {{
                    border-radius: 0 !important;
                    min-height: 2.25rem !important;
                    padding: 0.45rem 0.8rem !important;
                    font-weight: 900 !important;
                    letter-spacing: 0.02em;
                    color: #fff !important;
                    background: var(--mod-purple-dark) !important;
                    border: 1px solid rgba(139, 92, 246, 0.52) !important;
                    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.28) !important;
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
                .mod-list-button.secondary {{
                    color: var(--mod-text) !important;
                    background: #15151b !important;
                    border-color: #34343d !important;
                    box-shadow: none !important;
                }}
                .mod-list-button.state-enabled {{
                    background: #24113a !important;
                    border-color: #7c3aed !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(196, 181, 253, 0.22),
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
                    color: #fef2f2 !important;
                    background: #3a1117 !important;
                    border-color: #dc2626 !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(254, 202, 202, 0.2),
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
                    color: #fecaca !important;
                    background: #311218 !important;
                    border-color: #b91c1c !important;
                    box-shadow:
                        inset 0 0 0 2px rgba(254, 202, 202, 0.18),
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
                .mod-list-button.danger {{
                    background: #5f111b !important;
                    border-color: rgba(220, 38, 38, 0.7) !important;
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
                    background:
                        linear-gradient(180deg, rgba(196, 181, 253, 0.04), rgba(196, 181, 253, 0)),
                        rgba(10, 10, 14, 0.78) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.08),
                        0 12px 28px rgba(0, 0, 0, 0.24) !important;
                }}
                .mod-config-select {{
                    flex: 1 1 26rem;
                    min-width: 18rem;
                }}
                .mod-recipe-field .q-field__control {{
                    min-height: 3.05rem !important;
                    padding: 0 0.42rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, rgba(237, 233, 254, 0.06), rgba(237, 233, 254, 0)),
                        rgba(17, 17, 24, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(124, 58, 237, 0.16),
                        0 10px 24px rgba(0, 0, 0, 0.24) !important;
                    transition:
                        border-color 120ms ease,
                        box-shadow 120ms ease,
                        background-color 120ms ease;
                }}
                .mod-recipe-field:hover .q-field__control,
                .mod-recipe-field .q-field--focused .q-field__control {{
                    border-color: rgba(139, 92, 246, 0.72) !important;
                    background:
                        linear-gradient(180deg, rgba(221, 214, 254, 0.08), rgba(221, 214, 254, 0)),
                        rgba(20, 14, 31, 0.98) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.22),
                        0 0 0 1px rgba(139, 92, 246, 0.24),
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
                    color: #ddd6fe !important;
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
                    background:
                        linear-gradient(180deg, rgba(196, 181, 253, 0.06), rgba(196, 181, 253, 0)),
                        rgba(9, 9, 13, 0.88);
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
                        inset 0 -1px 0 rgba(139, 92, 246, 0.12),
                        0 12px 24px rgba(0, 0, 0, 0.24) !important;
                }}
                .mod-recipe-slot:hover {{
                    border-color: rgba(139, 92, 246, 0.64) !important;
                    background:
                        linear-gradient(180deg, rgba(221, 214, 254, 0.08), rgba(221, 214, 254, 0)),
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
                    color: #c4b5fd !important;
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
                    color: #ede9fe;
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
                    color: #c4b5fd;
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
                    border-color: rgba(139, 92, 246, 0.68) !important;
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
                    border-left: 1px solid rgba(139, 92, 246, 0.58);
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
                    color: #f5f3ff;
                    background: rgba(36, 17, 58, 0.9);
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
                    color: #ddd6fe;
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
                        linear-gradient(180deg, rgba(237, 233, 254, 0.05), rgba(237, 233, 254, 0)),
                        rgba(16, 16, 22, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(124, 58, 237, 0.14),
                        0 10px 24px rgba(0, 0, 0, 0.2) !important;
                }}
                .mod-console-select:hover .q-field__control,
                .mod-console-select .q-field--focused .q-field__control {{
                    border-color: rgba(139, 92, 246, 0.62) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.22),
                        0 0 0 1px rgba(139, 92, 246, 0.14),
                        0 12px 26px rgba(0, 0, 0, 0.26) !important;
                }}
                .mod-console-select .q-field--filled .q-field__control::before {{
                    border-bottom: 1px solid rgba(139, 92, 246, 0.26) !important;
                }}
                .mod-console-select .q-field--filled .q-field__control::after {{
                    border-bottom: 2px solid rgba(196, 181, 253, 0.88) !important;
                }}
                .mod-console-select .q-field__native,
                .mod-console-select .q-field__input,
                .mod-console-select .q-field__append,
                .mod-console-select .q-field__prepend,
                .mod-console-select .q-field__marginal,
                .mod-console-select .q-icon {{
                    color: #f5f3ff !important;
                }}
                .mod-console-select.mod-console-select-black .q-field__control,
                .mod-console-select.mod-console-select-black:hover .q-field__control,
                .mod-console-select.mod-console-select-black .q-field--focused .q-field__control {{
                    background: rgba(6, 6, 10, 0.98) !important;
                    background-image: none !important;
                }}
                .mod-console-select-menu {{
                    background: rgba(6, 6, 10, 0.98) !important;
                    color: #f5f3ff !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                }}
                .mod-console-select-menu .q-item,
                .mod-console-select-menu .q-item__label,
                .mod-console-select-menu .q-icon {{
                    color: #f5f3ff !important;
                }}
                .mod-console-select-menu .q-item--active,
                .mod-console-select-menu .q-item.q-manual-focusable--focused,
                .mod-console-select-menu .q-item[aria-selected="true"] {{
                    background: rgba(36, 17, 58, 0.92) !important;
                }}
                .mod-config-search {{
                    flex: 0 1 18rem;
                    min-width: 15rem;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__control {{
                    background:
                        linear-gradient(180deg, rgba(196, 181, 253, 0.08), rgba(196, 181, 253, 0)),
                        rgba(36, 17, 58, 0.72) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.3) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field--filled .q-field__control::before {{
                    border-bottom: 1px solid rgba(139, 92, 246, 0.34) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field--filled .q-field__control::after {{
                    border-bottom: 2px solid var(--mod-purple) !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__native,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__input,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__append,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__prepend,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-icon {{
                    color: #ede9fe !important;
                }}
                :is(.mod-settings-search, .mod-mods-toolbar-sort) .q-field__native::placeholder,
                :is(.mod-settings-search, .mod-mods-toolbar-sort) input::placeholder {{
                    color: #c4b5fd !important;
                    opacity: 1;
                }}
                .mod-tab-toolbar-actions {{
                    display: flex;
                    gap: 0.5rem;
                    flex-wrap: wrap;
                    margin-left: auto;
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
                    border-color: rgba(139, 92, 246, 0.48) !important;
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
                        inset 0 -1px 0 rgba(139, 92, 246, 0.14) !important;
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
                    border-bottom: 2px solid rgba(139, 92, 246, 0.8) !important;
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
                        inset 0 -1px 0 rgba(139, 92, 246, 0.14) !important;
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
                    background: rgba(124, 58, 237, 0.18) !important;
                    color: #ddd6fe !important;
                }}
                .mod-setting-switch {{
                    padding: 0.1rem 0.25rem;
                    align-self: flex-start;
                    margin-left: -0.28rem;
                }}
                .mod-setting-switch .q-toggle__inner {{
                    color: var(--mod-purple) !important;
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
                    border-left: 1px solid rgba(124, 58, 237, 0.46);
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
                    box-shadow: 0 0 0 1px rgba(139, 92, 246, 0.5);
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
                    color: #fca5a5 !important;
                    transform: translateY(0);
                }}
                .mod-setting-field-invalid .q-field__control {{
                    border-color: rgba(220, 38, 38, 0.88) !important;
                    box-shadow:
                        0 0 0 1px rgba(220, 38, 38, 0.42),
                        0 0 18px rgba(127, 29, 29, 0.22) !important;
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
                    border-color: rgba(124, 58, 237, 0.3) !important;
                }}
                .mod-card-notepad.mod-card-plain {{
                    background: transparent !important;
                    border-color: transparent !important;
                    box-shadow: none !important;
                }}
                .mod-card-notepad .mod-tab-toolbar {{
                    align-items: stretch;
                    padding: 0.68rem 0.78rem;
                    border: 1px solid rgba(124, 58, 237, 0.18);
                    background:
                        linear-gradient(180deg, rgba(196, 181, 253, 0.05), rgba(196, 181, 253, 0)),
                        rgba(8, 8, 12, 0.78) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 rgba(124, 58, 237, 0.14) !important;
                }}
                .mod-card-notepad .mod-config-select .q-field__control {{
                    min-height: 3.05rem !important;
                    padding: 0 0.42rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.9) !important;
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, rgba(237, 233, 254, 0.06), rgba(237, 233, 254, 0)),
                        rgba(17, 17, 24, 0.96) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(124, 58, 237, 0.16),
                        0 10px 24px rgba(0, 0, 0, 0.24) !important;
                    transition:
                        border-color 120ms ease,
                        box-shadow 120ms ease,
                        background-color 120ms ease;
                }}
                .mod-card-notepad .mod-config-select:hover .q-field__control,
                .mod-card-notepad .mod-config-select .q-field--focused .q-field__control {{
                    border-color: rgba(139, 92, 246, 0.72) !important;
                    background:
                        linear-gradient(180deg, rgba(221, 214, 254, 0.08), rgba(221, 214, 254, 0)),
                        rgba(20, 14, 31, 0.98) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.22),
                        0 0 0 1px rgba(139, 92, 246, 0.24),
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
                    color: #ddd6fe !important;
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
                    border: 1px solid rgba(124, 58, 237, 0.32) !important;
                    background:
                        linear-gradient(180deg, rgba(221, 214, 254, 0.05), rgba(221, 214, 254, 0)),
                        rgba(8, 8, 12, 0.985) !important;
                    box-shadow:
                        0 20px 42px rgba(0, 0, 0, 0.44),
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(124, 58, 237, 0.18) !important;
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
                        linear-gradient(90deg, rgba(139, 92, 246, 0.14), transparent 78%),
                        rgba(39, 39, 42, 0.42) !important;
                }}
                .mod-notepad-menu .q-item.q-manual-focusable--focused,
                .mod-notepad-menu .q-item[aria-selected="true"],
                .mod-notepad-menu .q-item--active {{
                    background:
                        linear-gradient(90deg, rgba(139, 92, 246, 0.24), transparent 88%),
                        rgba(91, 33, 182, 0.26) !important;
                    color: #ede9fe !important;
                    box-shadow: inset 3px 0 0 rgba(196, 181, 253, 0.84);
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
                    border-color: rgba(139, 92, 246, 0.55);
                }}
                .mod-config-wrap-toggle .q-checkbox__label {{
                    font-size: 0.72rem !important;
                    font-weight: 800;
                    letter-spacing: 0.03em;
                    text-transform: uppercase;
                }}
                .mod-config-wrap-toggle .q-checkbox__inner {{
                    color: var(--mod-purple) !important;
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
                    caret-color: var(--mod-purple) !important;
                }}
                .mod-config-editor .cm-gutters {{
                    background: #050507 !important;
                    border-right: 1px solid #25252c !important;
                    color: #7c7f88 !important;
                }}
                .mod-config-editor .cm-activeLine,
                .mod-config-editor .cm-activeLineGutter {{
                    background: rgba(139, 92, 246, 0.12) !important;
                }}
                .mod-config-editor .cm-focused {{
                    outline: 1px solid rgba(139, 92, 246, 0.42) !important;
                }}
                .mod-config-editor .cm-selectionBackground,
                .mod-config-editor .cm-focused .cm-selectionBackground,
                .mod-config-editor ::selection {{
                    background: rgba(139, 92, 246, 0.28) !important;
                }}
                .mod-row {{
                    display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 0.75rem; align-items: center;
                    position: relative;
                    min-height: 4.25rem;
                    padding: 0.52rem 3.05rem 0.52rem 0.7rem;
                    border-radius: 0 !important;
                    background: #0b0b10 !important;
                    border: 1px solid #25252c !important;
                    box-shadow: inset 3px 0 0 rgba(139, 92, 246, 0.5);
                    transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
                }}
                .mod-row:hover {{
                    background: #101017 !important;
                    border-color: rgba(139, 92, 246, 0.48) !important;
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
                .mod-row-client-only {{
                    border-color: rgba(139, 92, 246, 0.72) !important;
                    box-shadow:
                        inset 3px 0 0 rgba(139, 92, 246, 0.82),
                        inset 0 0 0 1px rgba(196, 181, 253, 0.14);
                }}
                .mod-row-clickable {{ cursor: pointer; }}
                .mod-row-disabled:hover {{ border-color: rgba(161, 161, 170, 0.82) !important; }}
                .mod-row-client-only:hover {{ border-color: rgba(196, 181, 253, 0.86) !important; }}
                .mod-row .q-checkbox__inner {{ color: var(--mod-purple) !important; }}
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
                .mod-pill.core {{ background: #2a1430 !important; color: #c4b5fd !important; border-color: #6d28d9; }}
                .mod-pill.blocked {{ background: #3a1117 !important; color: #fecaca !important; border-color: #b91c1c; }}
                .mod-pill.origin {{ background: #15151b !important; color: #a1a1aa !important; border-color: #303038; }}
                .mod-pill.size {{ background: #15151b !important; color: #d4d4d8 !important; border-color: #3f3f46; }}
                .mod-row-download {{
                    color: #c4b5fd !important;
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
                .mod-badge.purple {{ background: #24113a !important; border-color: #7c3aed; color: #ddd6fe !important; }}
                .mod-badge.red {{ background: #3a1117 !important; border-color: #dc2626; color: #fecaca !important; }}
                .mod-badge.warn {{ background: #22161a !important; border-color: #7f1d1d; color: #fca5a5 !important; }}
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
                    grid-template-columns: minmax(0, 1fr);
                    gap: 1.5rem;
                }}
                .mod-home-section {{
                    min-width: 0;
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
                .mod-system-edge-badge-row {{
                    width: 100%;
                    flex-wrap: wrap;
                    gap: 0.5rem;
                }}
                .mod-system-hero-shell {{
                    padding-top: 2.8rem !important;
                }}
                .mod-system-hero-header {{
                    display: grid;
                    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
                    align-items: start;
                    gap: 1rem 1.5rem;
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
                @media (min-width: 1280px) {{
                    .mod-home-section-grid {{
                        grid-template-columns: repeat(2, minmax(0, 1fr));
                    }}
                }}
                .mod-home-node-grid {{
                    align-items: stretch;
                }}
                .mod-home-node-card {{
                    min-width: min(19rem, 100%);
                    flex: 1 1 19rem;
                    border-radius: 0 !important;
                    background: rgba(10, 10, 14, 0.86) !important;
                    border: 1px solid #2f2f37 !important;
                    box-shadow: none !important;
                }}
                .mod-home-node-card-black {{
                    border-color: #4b5563 !important;
                }}
                .mod-home-node-card-purple {{
                    border-color: rgba(124, 58, 237, 0.72) !important;
                }}
                .mod-home-node-card-red {{
                    border-color: rgba(220, 38, 38, 0.78) !important;
                }}
                .mod-home-node-card-warn {{
                    border-color: rgba(127, 29, 29, 0.82) !important;
                }}
                .mod-home-node-card-grey {{
                    border-color: #3f3f46 !important;
                }}
                .mod-home-node-card-actionable {{
                    cursor: pointer !important;
                    transition: border-color 140ms ease, background 140ms ease, transform 140ms ease;
                }}
                .mod-home-node-card-actionable:hover,
                .mod-home-node-card-actionable:focus-visible {{
                    background: rgba(20, 18, 30, 0.94) !important;
                    border-color: #a78bfa !important;
                    outline: none !important;
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
                    color: #a78bfa !important;
                }}
                .mod-home-node-metric-icon.mod-tone-red,
                .mod-home-node-running-icon.mod-tone-red {{
                    color: #f87171 !important;
                }}
                .mod-home-node-metric-icon.mod-tone-warn,
                .mod-home-node-running-icon.mod-tone-warn {{
                    color: #fca5a5 !important;
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
                @media (max-width: 640px) {{
                    .mod-home-node-card {{
                        min-width: 100%;
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
                .mod-stat-card.purple {{ border-color: rgba(124, 58, 237, 0.72) !important; }}
                .mod-stat-card.red {{ border-color: rgba(220, 38, 38, 0.78) !important; }}
                .mod-stat-card.warn {{ border-color: rgba(127, 29, 29, 0.82) !important; }}
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
                    color: #c4b5fd !important;
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
                .mod-system-danger-card {{
                    border-color: rgba(220, 38, 38, 0.64) !important;
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
                    border: 1px solid rgba(127, 29, 29, 0.46);
                    background:
                        linear-gradient(135deg, rgba(127, 29, 29, 0.09), transparent 54%),
                        rgba(10, 10, 14, 0.52);
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
                .mod-system-action-row {{
                    padding-top: 0.9rem;
                    border-top: 1px solid rgba(127, 29, 29, 0.52);
                }}
                .mod-dialog-card {{
                    width: min(30rem, calc(100vw - 2rem)) !important;
                    max-width: none !important;
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
                .mod-app-details-layout {{
                    gap: 1rem;
                }}
                .mod-app-details-section {{
                    width: 100%;
                    display: flex;
                    flex-direction: column;
                    gap: 0.8rem;
                    padding: 0.95rem 1rem;
                    border: 1px solid rgba(82, 82, 91, 0.62);
                    background:
                        linear-gradient(180deg, rgba(196, 181, 253, 0.04), rgba(196, 181, 253, 0)),
                        rgba(10, 10, 14, 0.78) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.03),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.08);
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
                .mod-app-details-point-field {{
                    flex: 0 1 11.5rem;
                    max-width: 11.5rem;
                }}
                .mod-app-details-field .q-field__control {{
                    min-height: 2.9rem;
                    padding: 0 0.55rem !important;
                    border: 1px solid rgba(82, 82, 91, 0.82) !important;
                    border-radius: 0 !important;
                    background:
                        linear-gradient(180deg, rgba(139, 92, 246, 0.06), rgba(139, 92, 246, 0)),
                        rgba(8, 8, 12, 0.94) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.04),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.12);
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
                    border-color: rgba(139, 92, 246, 0.62) !important;
                    box-shadow:
                        inset 0 1px 0 rgba(255, 255, 255, 0.05),
                        inset 0 -1px 0 rgba(139, 92, 246, 0.2),
                        0 0 0 1px rgba(139, 92, 246, 0.16);
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
                .mod-mod-override-field .q-field__control {{
                    border-color: rgba(113, 113, 122, 0.92) !important;
                    background:
                        linear-gradient(180deg, rgba(139, 92, 246, 0.1), rgba(139, 92, 246, 0.015)),
                        rgba(15, 15, 22, 0.99) !important;
                }}
                .mod-mod-override-field .q-field__label {{
                    color: #d8b4fe !important;
                    opacity: 1 !important;
                }}
                .mod-mod-override-field .q-field__native,
                .mod-mod-override-field .q-field__input {{
                    color: #fafafa !important;
                    -webkit-text-fill-color: #fafafa !important;
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
                    color: var(--mod-purple) !important;
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
                .mod-fake-chat-dialog-card {{
                    width: min(52rem, calc(100vw - 1.5rem)) !important;
                    max-width: none !important;
                }}
                .mod-fake-chat-field {{
                    width: 100%;
                }}
                .mod-fake-chat-field .q-field__control {{
                    background:
                        linear-gradient(180deg, rgba(139, 92, 246, 0.08), rgba(139, 92, 246, 0)),
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
                    border-color: rgba(139, 92, 246, 0.62) !important;
                    box-shadow: 0 0 0 1px rgba(139, 92, 246, 0.18);
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
                .mod-fake-chat-send-target {{
                    min-width: min(20rem, 100%);
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
                    border-bottom: 1px solid rgba(124, 58, 237, 0.24);
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
                        linear-gradient(180deg, rgba(139, 92, 246, 0.08), rgba(139, 92, 246, 0) 22%),
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
                        radial-gradient(circle at 14% 18%, rgba(139, 92, 246, 0.14), transparent 19rem),
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
                        rgba(139, 92, 246, 0) 44%
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
                    --mod-chat-source-rail: #7c3aed;
                    --mod-chat-source-glow: rgba(124, 58, 237, 0.14);
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
                    color: #f5f3ff;
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
                    color: #c4b5fd;
                    text-decoration: underline;
                    text-decoration-color: rgba(196, 181, 253, 0.58);
                    text-underline-offset: 0.12rem;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}
                .mod-chat-markup a:hover {{
                    color: #ddd6fe;
                    text-decoration-color: rgba(221, 214, 254, 0.86);
                }}
                .mod-chat-inline-code {{
                    display: inline-block;
                    max-width: 100%;
                    padding: 0.06rem 0.26rem;
                    border: 1px solid rgba(82, 82, 91, 0.86);
                    background: rgba(10, 10, 14, 0.92);
                    color: #f5f3ff;
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
                    color: #f5f3ff;
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
                .mod-chat-quote {{
                    margin: 0.18rem 0 0;
                    padding: 0.22rem 0 0.22rem 0.62rem;
                    border-left: 3px solid rgba(139, 92, 246, 0.56);
                    color: rgba(228, 228, 231, 0.88);
                    background: linear-gradient(90deg, rgba(139, 92, 246, 0.08), rgba(139, 92, 246, 0) 58%);
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
                        linear-gradient(180deg, rgba(26, 13, 46, 0.96), rgba(11, 10, 17, 0.98))
                        !important;
                    box-shadow:
                        0 16px 42px rgba(0, 0, 0, 0.5),
                        inset 0 1px 0 rgba(196, 181, 253, 0.16) !important;
                    color: var(--mod-text) !important;
                    overflow: hidden;
                }}
                .mod-chat-entry-menu .q-list {{
                    padding: 0.26rem 0;
                }}
                .mod-chat-entry-menu-item {{
                    min-height: 0 !important;
                    padding: 0.48rem 0.74rem !important;
                    color: #f5f3ff !important;
                    font-size: 0.8rem !important;
                    font-weight: 800 !important;
                    letter-spacing: 0.03em;
                    text-transform: uppercase;
                    transition: background 120ms ease, color 120ms ease;
                }}
                .mod-chat-entry-menu-item:hover,
                .mod-chat-entry-menu-item.q-manual-focusable--focused,
                .mod-chat-entry-menu-item[aria-selected="true"] {{
                    background: rgba(139, 92, 246, 0.22) !important;
                    color: #ffffff !important;
                }}
                .mod-chat-reference {{
                    gap: 0.16rem !important;
                    padding: 0.34rem 0.46rem 0.38rem;
                    border-left: 2px solid rgba(139, 92, 246, 0.6);
                    background: rgba(32, 19, 53, 0.34);
                }}
                .mod-chat-reference-label {{
                    color: rgba(196, 181, 253, 0.9) !important;
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
                    flex: 1 1 min(20rem, 100%);
                    max-width: min(28rem, 100%);
                    border: 1px solid rgba(63, 63, 70, 0.82);
                    background:
                        linear-gradient(180deg, rgba(139, 92, 246, 0.06), rgba(139, 92, 246, 0)),
                        rgba(5, 5, 7, 0.92);
                    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
                    overflow: hidden;
                }}
                .mod-chat-media-card > .nicegui-content {{
                    padding: 0 !important;
                }}
                .mod-chat-media-link {{
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    color: #e4e4e7 !important;
                    text-decoration: none !important;
                }}
                .mod-chat-media-image,
                .mod-chat-media-video {{
                    display: block;
                    width: 100%;
                    max-height: 19rem;
                    object-fit: contain;
                    background: linear-gradient(180deg, #020204, #08080c);
                }}
                .mod-chat-media-audio {{
                    display: block;
                    width: 100%;
                    min-height: 2.2rem;
                    background: #020204;
                }}
                .mod-chat-media-caption {{
                    display: block;
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
                    border: 1px solid rgba(139, 92, 246, 0.34);
                    background: linear-gradient(90deg, rgba(61, 37, 97, 0.58), rgba(16, 16, 22, 0.84));
                }}
                .mod-chat-reply-copy {{
                    flex: 1 1 0;
                }}
                .mod-chat-reply-label {{
                    color: rgba(196, 181, 253, 0.92) !important;
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
                        linear-gradient(180deg, rgba(139, 92, 246, 0.06), rgba(139, 92, 246, 0)),
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
                        linear-gradient(180deg, rgba(139, 92, 246, 0.08), rgba(139, 92, 246, 0)),
                        rgba(8, 8, 12, 0.94) !important;
                    border: 1px solid rgba(82, 82, 91, 0.78);
                    min-height: 2.7rem;
                }}
                .mod-chat-input.q-field--focused .q-field__control {{
                    border-color: rgba(139, 92, 246, 0.56);
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
                    border: 1px solid rgba(139, 92, 246, 0.34);
                    border-radius: 0;
                    background: rgba(16, 16, 22, 0.92);
                    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.32);
                    backdrop-filter: blur(12px);
                    cursor: pointer;
                    transition: background 140ms ease, border-color 140ms ease;
                }}
                .mod-chat-unread-bar:hover {{
                    background: rgba(28, 28, 38, 0.96);
                    border-color: rgba(196, 181, 253, 0.52);
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
                .mod-action {{
                    border-radius: 0 !important;
                    background: #22113a !important;
                    color: #fff !important;
                    border: 1px solid rgba(139, 92, 246, 0.58);
                    text-decoration: none !important;
                    font-weight: 950;
                    letter-spacing: 0.02em;
                    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
                }}
                .mod-action-border-accent {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                    border-color: rgba(139, 92, 246, 0.58) !important;
                    box-shadow: none !important;
                }}
                .mod-toolbar-chat-button,
                .mod-toolbar-chat-button:hover,
                .mod-toolbar-chat-button:focus-visible {{
                    background: transparent !important;
                    color: var(--mod-text) !important;
                    border-color: rgba(139, 92, 246, 0.58) !important;
                    box-shadow: none !important;
                }}
                .mod-action:hover {{ filter: brightness(1.14); transform: translateY(-1px); }}
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
                    .mod-app-card-shell {{
                        grid-template-columns: minmax(0, 1fr);
                        min-height: auto;
                        align-items: start !important;
                    }}
                    .mod-app-card-actions {{
                        flex-wrap: wrap !important;
                        justify-content: flex-start !important;
                    }}
                    .mod-app-card-badges {{
                        flex-wrap: wrap !important;
                    }}
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
                    .mod-section-tabs-shell {{
                        flex: 1 1 100%;
                    }}
                    .mod-section-tabs {{
                        max-width: 100%;
                    }}
                    .mod-section-tabs .q-tab {{ flex: 1 1 calc(50% - 0.5rem); min-width: 0; }}
                    .mod-section-chrome {{
                        width: 100%;
                        justify-content: flex-end;
                    }}
                    .mod-section-chrome-panel {{
                        margin-left: auto;
                        max-width: 100%;
                    }}
                    .mod-mods-toolbar-filters {{
                        display: grid !important;
                        grid-template-columns: minmax(0, 1fr) auto;
                        align-items: center;
                        width: 100%;
                    }}
                    .mod-mods-toolbar-search {{
                        grid-column: 1 / -1;
                        width: 100%;
                    }}
                    .mod-mods-toolbar .mod-mods-toolbar-sort {{
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
                        width: 100%;
                        align-self: stretch;
                        margin-left: 0;
                    }}
                    .mod-mods-toolbar-actions .mod-toolbar-button {{
                        width: 100%;
                        min-width: 0;
                    }}
                    .mod-mods-toolbar-actions .mod-list-button.danger {{
                        grid-column: 2;
                        margin-left: 0;
                    }}
                    .mod-config-select,
                    .mod-config-search {{ flex-basis: 100%; min-width: 0; }}
                    .mod-app-details-dialog-card {{ width: calc(100vw - 1rem) !important; }}
                    .mod-app-details-section {{ padding: 0.85rem 0.85rem; }}
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
                    .mod-fake-chat-send-target {{
                        min-width: 100%;
                    }}
                    .mod-chat-timeline {{ max-height: none; }}
                    .mod-chat-input {{ flex-basis: 100%; min-width: 0; }}
                    .mod-chat-send {{ width: 100%; }}
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
        background="#050507",
        card="#09090d",
        card_raised="#101015",
        border="#2a202d",
        border_hot="#6d243f",
        text="#f4f4f5",
        muted="#a1a1aa",
        dim="#71717a",
        purple="#8b5cf6",
        purple_dark="#3b164d",
        red="#dc2626",
        red_dark="#5f111b",
        warning="#f59e0b",
        warning_dark="#3a230b",
        panel="rgba(10, 10, 14, 0.94)",
        nicegui=NiceGuiPalette(
            primary="#7c1d57",
            secondary="#b91c1c",
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
        )
    else:
        ui.add_head_html(theme.css())
