from __future__ import annotations

from dataclasses import dataclass
from secrets import token_hex


@dataclass(frozen=True, slots=True)
class EditorSessionNamespace:
    token: str

    def prefix(self, base: str) -> str:
        if not base.endswith(":"):
            raise ValueError("Editor prefix base must end with `:`.")
        session_token = self.token.strip()
        if not session_token:
            raise ValueError("Editor session token must not be empty.")
        return f"{base}{session_token}:"


_STARTUP_EDITOR_SESSION_NAMESPACE = EditorSessionNamespace(token=token_hex(1))


def startup_editor_prefix(base: str) -> str:
    return _STARTUP_EDITOR_SESSION_NAMESPACE.prefix(base)
