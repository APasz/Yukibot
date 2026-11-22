from __future__ import annotations

import logging
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import psutil
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps._app import App
from node_api.process import NodeAppProcessSummary
from node_api.service import NodeApiService
from node_api.system_service import NodeSystemMonitoringService
from node_auth import NodeApiScope


def monitoring() -> NodeSystemMonitoringService:
    return NodeSystemMonitoringService(
        node_name=lambda: "erin",
        http_exception=lambda status, detail: HTTPException(status, detail),
        logger=logging.getLogger(__name__),
        summary_cache_ttl_seconds=1,
        history_retention_seconds=60,
        history_interval_seconds=1,
        max_log_lines=100,
    )


def process(
    pid: int, *, started: float = 100, cpu: float = 2, memory: int = 1024
) -> Mock:
    value = Mock(spec=psutil.Process)
    value.pid = pid
    value.create_time.return_value = started
    value.children.return_value = []
    value.is_running.return_value = True
    value.oneshot.side_effect = nullcontext
    value.cpu_times.return_value = SimpleNamespace(user=cpu, system=0)
    value.memory_info.return_value = SimpleNamespace(rss=memory)
    value.num_threads.return_value = 3
    value.io_counters.return_value = SimpleNamespace(read_bytes=100, write_bytes=50)
    return value


def app() -> App:
    return cast(
        App,
        SimpleNamespace(
            name="ul",
            process=SimpleNamespace(pid=10),
            check_running=Mock(return_value=True),
        ),
    )


def test_process_tree_sampling_shares_intervals_and_preserves_multicore_cpu() -> None:
    service = monitoring()
    root, child = process(10), process(11, memory=2048)
    root.children.return_value = [child]
    clock = [0.0]
    with (
        patch("node_api.system_service.psutil.Process", return_value=root),
        patch("node_api.system_service.time.monotonic", side_effect=lambda: clock[0]),
    ):
        target = app()
        first = service.build_app_process_summary(target)
        assert first.cpu_percent is None and first.read_bytes_per_second is None
        assert first.memory_rss_bytes == 3072
        assert first.process_count == 2 and first.thread_count == 6
        clock[0] = 0.2
        assert service.build_app_process_summary(target) is first
        clock[0] = 2.0
        root.cpu_times.return_value = SimpleNamespace(user=5, system=0)
        child.cpu_times.return_value = SimpleNamespace(user=4, system=0)
        root.io_counters.return_value = SimpleNamespace(read_bytes=300, write_bytes=150)
        second = service.build_app_process_summary(target)
    assert second.cpu_percent == 250
    assert second.read_bytes_per_second == 100
    assert second.write_bytes_per_second == 50


def test_restart_pid_reuse_and_tree_changes_reset_rate_baselines() -> None:
    service = monitoring()
    root = process(10)
    child = process(20)
    clock = [0.0]
    with (
        patch("node_api.system_service.psutil.Process", return_value=root),
        patch("node_api.system_service.time.monotonic", side_effect=lambda: clock[0]),
    ):
        target = app()
        service.build_app_process_summary(target)
        root.create_time.return_value = 200
        root.cpu_times.return_value = SimpleNamespace(user=99, system=0)
        clock[0] = 0.2
        reused = service.build_app_process_summary(target)
        assert reused.started_at_ms == 200000
        assert reused.cpu_percent is None
        clock[0] = 2
        root.children.return_value = [child]
        changed = service.build_app_process_summary(target)
        assert changed.process_count == 2
        assert changed.cpu_percent is None and changed.read_bytes_per_second is None
        clock[0] = 62
        after_gap = service.build_app_process_summary(target)
        assert (
            after_gap.cpu_percent is None and after_gap.write_bytes_per_second is None
        )


def test_missing_process_data_is_not_reported_as_zero() -> None:
    service = monitoring()
    root = process(10)
    root.io_counters.side_effect = psutil.AccessDenied(10)
    clock = [0.0]
    with (
        patch("node_api.system_service.psutil.Process", return_value=root),
        patch("node_api.system_service.time.monotonic", side_effect=lambda: clock[0]),
    ):
        target = app()
        service.build_app_process_summary(target)
        clock[0] = 2
        sample = service.build_app_process_summary(target)
        assert sample.state == "running" and sample.cpu_percent == 0
        assert sample.read_bytes_per_second is None
        root.memory_info.side_effect = psutil.AccessDenied(10)
        clock[0] = 4
        sample = service.build_app_process_summary(target)
        assert sample.state == "unavailable" and sample.memory_rss_bytes is None
        target.process = None
        assert service.build_app_process_summary(target).state == "unavailable"
        with patch.object(target, "check_running", return_value=False):
            stopped = service.build_app_process_summary(target)
            assert stopped.state == "stopped" and stopped.cpu_percent is None


def test_process_route_requires_scoped_access_before_reading_processes() -> None:
    service = NodeApiService()
    application = FastAPI()
    sample = NodeAppProcessSummary(
        state="running", captured_at_ms=100, pid=10, cpu_percent=250
    )
    with (
        patch.object(service.request_auth, "require_access") as access,
        patch.object(service, "_resolve_app", return_value=app()),
        patch.object(
            service.system_monitoring, "build_app_process_summary", return_value=sample
        ) as build,
    ):
        service.register_routes(application)
        with TestClient(application) as client:
            response = client.get("/api/node/apps/ul/process")
            assert response.status_code == 200, response.text
            assert response.json()["cpu_percent"] == 250
            assert response.headers["cache-control"] == "no-store"
            assert access.call_args.kwargs["app_name"] == "ul"
            assert access.call_args.kwargs["scopes"] == (NodeApiScope.APPS_READ,)
            build.reset_mock()
            access.side_effect = HTTPException(401, "Access required")
            assert client.get("/api/node/apps/ul/process").status_code == 401
            build.assert_not_called()


def test_real_owned_process_can_be_observed_and_stopped() -> None:
    service = monitoring()
    target = app()
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(10)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            assert (
                child.stdout is not None and child.stdout.readline().strip() == "ready"
            )
            target.process = child
            target.check_running = lambda: child.poll() is None  # type: ignore[method-assign]
            first = service.build_app_process_summary(target)
            assert first.state == "running" and first.pid == child.pid
            assert first.memory_rss_bytes is not None and first.memory_rss_bytes > 0
            time.sleep(1.05)
            second = service.build_app_process_summary(target)
            assert second.cpu_percent is not None and second.thread_count is not None
            child.terminate()
            child.wait(timeout=5)
            assert service.build_app_process_summary(target).state == "stopped"
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.parametrize("state", ["stopped", "unavailable"])
def test_empty_process_states_round_trip(state: str) -> None:
    sample = NodeAppProcessSummary.model_validate(
        {"state": state, "captured_at_ms": 100}
    )
    assert NodeAppProcessSummary.model_validate_json(sample.model_dump_json()) == sample


def test_exited_or_reused_child_is_excluded_from_process_totals() -> None:
    service = monitoring()
    root, child = process(10), process(11, memory=2048)
    root.children.return_value = [child]
    child.is_running.return_value = False
    with patch("node_api.system_service.psutil.Process", return_value=root):
        sample = service.build_app_process_summary(app())
    assert sample.state == "running"
    assert sample.memory_rss_bytes == 1024 and sample.thread_count == 3
    assert sample.process_count == 1
