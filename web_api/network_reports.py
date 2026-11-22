"""Bounded, expiring storage for player-submitted diagnostics reports."""

from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from uuid import UUID

from diskcache import Cache
from pydantic import ConfigDict

from cache_safety import prepare_private_cache_directory

from .network_models import (
    MAX_REPORTS_PER_USER,
    MAX_STORED_REPORTS,
    NETWORK_LIMITS,
    NetworkIdentity,
    NetworkModel,
    NetworkReport,
    NetworkReportInput,
    NetworkReportSummary,
)


class _StoredReportSummary(NetworkModel):
    """Read library metadata without constructing the recording's sample objects."""

    model_config = ConfigDict(extra="ignore")
    summary: NetworkReportSummary


class NetworkReportStore:
    def __init__(self, directory: Path) -> None:
        self._cache = Cache(
            directory=prepare_private_cache_directory(directory),
            size_limit=64 * 1024 * 1024,
        )

    def close(self) -> None:
        self._cache.close()

    def _summaries(self) -> list[NetworkReportSummary]:
        self._cache.expire()
        summaries: list[NetworkReportSummary] = []
        for key in self._cache.iterkeys():
            raw = cast(object, self._cache.get(key))
            if isinstance(raw, str):
                summaries.append(_StoredReportSummary.model_validate_json(raw).summary)
        return sorted(summaries, key=lambda summary: summary.saved_at_ms, reverse=True)

    def list(self, identity: NetworkIdentity) -> list[NetworkReportSummary]:
        return [
            summary
            for summary in self._summaries()
            if identity.is_admin or summary.owner_id == identity.user_id
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
            previous_summaries = self._summaries()
            existing = next(
                (item for item in previous_summaries if item.id == recording.id), None
            )
            if str(recording.id) in self._cache and (
                existing is None or existing.owner_id != identity.user_id
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
                            (item.saved_at_ms for item in previous_summaries),
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
            summaries = self._summaries()
            owned = [item for item in summaries if item.owner_id == identity.user_id]
            for item in [
                *owned[MAX_REPORTS_PER_USER:],
                *summaries[MAX_STORED_REPORTS:],
            ]:
                self._cache.delete(str(item.id))
            return report
