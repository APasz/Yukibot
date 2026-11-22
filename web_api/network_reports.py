"""Bounded, expiring storage for player-submitted diagnostics reports."""

from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from uuid import UUID

from diskcache import Cache

from cache_safety import prepare_private_cache_directory
from .network_models import (
    MAX_REPORTS_PER_USER,
    MAX_STORED_REPORTS,
    NETWORK_LIMITS,
    NetworkIdentity,
    NetworkReport,
    NetworkReportInput,
    NetworkReportSummary,
)


class NetworkReportStore:
    def __init__(self, directory: Path) -> None:
        self._cache = Cache(
            directory=prepare_private_cache_directory(directory),
            size_limit=64 * 1024 * 1024,
        )

    def close(self) -> None:
        self._cache.close()

    def _reports(self) -> list[NetworkReport]:
        self._cache.expire()
        reports: list[NetworkReport] = []
        for key in self._cache.iterkeys():
            raw = cast(object, self._cache.get(key))
            if isinstance(raw, str):
                reports.append(NetworkReport.model_validate_json(raw))
        return sorted(
            reports, key=lambda report: report.summary.saved_at_ms, reverse=True
        )

    def list(self, identity: NetworkIdentity) -> list[NetworkReportSummary]:
        return [
            report.summary
            for report in self._reports()
            if identity.is_admin or report.summary.owner_id == identity.user_id
        ]

    def get(self, report_id: UUID, identity: NetworkIdentity) -> NetworkReport | None:
        raw = cast(object, self._cache.get(str(report_id)))
        if not isinstance(raw, str):
            return None
        report = NetworkReport.model_validate_json(raw)
        if report.summary.owner_id != identity.user_id and not identity.is_admin:
            return None
        return report

    def save(
        self, recording: NetworkReportInput, identity: NetworkIdentity
    ) -> NetworkReport:
        with self._cache.transact():
            previous_reports = self._reports()
            existing = self.get(recording.id, identity)
            if str(recording.id) in self._cache and (
                existing is None or existing.summary.owner_id != identity.user_id
            ):
                raise PermissionError(
                    "This recording identifier belongs to another user."
                )
            report = NetworkReport(
                summary=NetworkReportSummary(
                    id=recording.id,
                    owner_id=identity.user_id,
                    owner_name=identity.display_name,
                    saved_at_ms=max(
                        round(time.time() * 1000),
                        max(
                            (item.summary.saved_at_ms for item in previous_reports),
                            default=0,
                        )
                        + 1,
                    ),
                    started_at_ms=recording.started_at_ms,
                    duration_ms=recording.duration_ms,
                    title=recording.title,
                    probe_count=len(recording.probes),
                ),
                recording=recording,
            )
            self._cache.set(
                str(recording.id),
                report.model_dump_json(),
                expire=NETWORK_LIMITS.retention_days * 86400,
            )
            reports = self._reports()
            owned = [
                item for item in reports if item.summary.owner_id == identity.user_id
            ]
            for item in [*owned[MAX_REPORTS_PER_USER:], *reports[MAX_STORED_REPORTS:]]:
                self._cache.delete(str(item.summary.id))
            return report
