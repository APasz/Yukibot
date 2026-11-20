"""Reusable synchronous HTTP sessions owned by individual worker threads."""

from __future__ import annotations

import threading
from typing import cast

import requests


class ThreadLocalHttpSessions:
    """Pool connections without sharing mutable Requests sessions between threads."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    def session(self) -> requests.Session:
        with self._lock:
            session = cast(requests.Session | None, getattr(self._local, "session", None))
            if session is None:
                session = requests.Session()
                self._local.session = session
                self._sessions.append(session)
            return session

    def close(self) -> None:
        """Close all connections and invalidate every thread's cached session."""
        with self._lock:
            sessions = tuple(self._sessions)
            self._sessions.clear()
            self._local = threading.local()
        for session in sessions:
            session.close()
