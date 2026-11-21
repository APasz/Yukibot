"""Bound replaceable browser updates and suspend them while their surface is hidden."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import cast

from nicegui.element import Element

from .nicegui_protocols import ModWebUi, ModWebEventArgumentsContainer, _value_as_bool

log = logging.getLogger(__name__)


class CoalescedUiUpdate[T]:
    """Apply the latest value while visible, with a reset flag after reconnects or remounts."""

    def __init__(self, *, ui: ModWebUi, target: Element, apply: Callable[[T, bool], None], interval: float = 0.5) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("UI update interval must be finite and positive.")
        self._apply = apply
        self._interval = interval
        self._client = target.client
        self._loop = asyncio.get_running_loop()
        self._latest: T | None = None
        self._has_value = False
        self._has_pending = False
        self._reset = True
        self._visible = False
        self._closed = False
        self._sequence = 0
        self._in_flight: int | None = None
        self._received = 0
        self._sent = 0
        self._metrics_started_at = time.monotonic()
        self._timer: asyncio.TimerHandle | None = None
        # The bridge stays mounted when Quasar detaches an inactive tab panel.
        with self._client.layout:
            self._bridge = ui.element("span").classes("hidden")

        def visibility_changed(event: ModWebEventArgumentsContainer) -> None:
            self._visible = _value_as_bool(event)
            if self._visible:
                self._in_flight = None
                # A detached target may have missed queued deltas, even if they were acknowledged.
                self._reset = True
                self._has_pending = self._has_value
            self._schedule()

        def acknowledged(event: ModWebEventArgumentsContainer) -> None:
            if isinstance(event.args, bool) or not isinstance(event.args, int):
                raise ValueError("UI update acknowledgement must be an integer sequence.")
            if event.args == self._in_flight:
                self._in_flight = None
                self._schedule()

        self._bridge.on("mod-web-visibility", visibility_changed, js_handler="event => emit(event.detail)")
        self._bridge.on("mod-web-update-ack", acknowledged, js_handler="event => emit(event.detail)")
        ui.run_javascript(f"""(() => {{
          const bridge = getHtmlElement({self._bridge.id});
          if (!bridge) return;
          let previous = null;
          const notify = () => {{
            if (!bridge.isConnected) {{
              window.clearInterval(timer);
              document.removeEventListener('visibilitychange', notify);
              window.socket?.off('connect', reconnected);
              return;
            }}
            const target = getHtmlElement({target.id});
            const visible = document.visibilityState === 'visible' && Boolean(target?.isConnected && target.getClientRects().length > 0);
            if (visible === previous || !window.socket?.connected) return;
            previous = visible;
            bridge.dispatchEvent(new CustomEvent('mod-web-visibility', {{detail: visible}}));
          }};
          const reconnected = () => {{ previous = null; notify(); }};
          const timer = window.setInterval(notify, 1000);
          document.addEventListener('visibilitychange', notify);
          window.socket?.on('connect', reconnected);
          notify();
        }})()""")

    def submit(self, value: T) -> None:
        if self._closed:
            return
        self._latest = value
        self._has_value = True
        self._received += 1
        self._has_pending = True
        self._schedule()
        self._log_metrics()

    def _log_metrics(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self._received and (force or now - self._metrics_started_at >= 60):
            log.info("Browser updates summarized: callback=%s received=%s sent=%s pending=%s awaiting_ack=%s visible=%s interval_seconds=%.1f window_seconds=%.1f",
                     getattr(self._apply, "__qualname__", type(self._apply).__qualname__), self._received, self._sent, self._has_pending,
                     self._in_flight is not None, self._visible, self._interval, now - self._metrics_started_at)
            self._received = self._sent = 0
            self._metrics_started_at = now

    def _schedule(self) -> None:
        if (not self._closed and self._has_pending and self._visible
                and self._client.has_socket_connection and self._timer is None):
            if self._in_flight is not None:
                return
            self._timer = self._loop.call_later(self._interval, self._flush)

    def _flush(self) -> None:
        self._timer = None
        if self._closed or not self._visible or not self._client.has_socket_connection or not self._has_pending:
            return
        self._has_pending = False
        self._sequence += 1
        self._sent += 1
        self._in_flight = self._sequence
        try:
            self._apply(cast(T, self._latest), self._reset)
            self._reset = False
            self._client.run_javascript(
                f"getHtmlElement({self._bridge.id})?.dispatchEvent(new CustomEvent('mod-web-update-ack', {{detail: {self._sequence}}}));",
                timeout=0.1,
            )
        except BaseException:
            self._in_flight = None
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._log_metrics(force=True)
        self._closed = True
        self._latest = None
        self._has_value = False
        self._has_pending = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._bridge.delete()


SYSTEM_HISTORY_JAVASCRIPT = r"""
window.modWebSystemHistory ??= {
  update(id, samples, reset) {
    const svg = getHtmlElement(id)?.querySelector('svg');
    if (!svg) return;
    const interval = Number(svg.dataset.sampleInterval);
    const retention = Number(svg.dataset.retention);
    const history = reset ? [] : (svg.modWebSamples || []);
    for (const sample of samples) {
      const last = history[history.length - 1];
      if (last && Math.floor(last[0] / interval) === Math.floor(sample[0] / interval)) history.pop();
      history.push(sample);
    }
    const end = history.at(-1)?.[0] || 0;
    while (history.length && history[0][0] < end - retention) history.shift();
    svg.modWebSamples = history;
    const left = Number(svg.dataset.plotLeft), top = Number(svg.dataset.plotTop);
    const width = Number(svg.dataset.plotWidth), height = Number(svg.dataset.plotHeight);
    svg.querySelector('.mod-system-chart-empty')?.remove();
    for (const path of svg.querySelectorAll('[data-series]')) {
      const index = Number(path.dataset.series);
      let open = false;
      const commands = [];
      for (const sample of history) {
        const value = sample[index];
        if (value === null) { open = false; continue; }
        const x = left + (sample[0] - end + retention) / retention * width;
        const y = top + (100 - Math.min(100, Math.max(0, value))) / 100 * height;
        commands.push(`${open ? 'L' : 'M'} ${x.toFixed(1)} ${y.toFixed(1)}`);
        open = true;
      }
      path.setAttribute('d', commands.join(' '));
    }
  }
};
"""
