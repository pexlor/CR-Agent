"""OS keyring backed credential storage."""

from __future__ import annotations

from typing import Any, cast

from code_review_agent.domain.common.errors import StableError

SERVICE_NAME = "code-review-agent"


class KeyringCredentialStore:
    def __init__(self, backend: Any | None = None) -> None:
        if backend is None:
            import keyring

            backend = keyring
        self._backend = backend

    @staticmethod
    def _username(provider_id: str, alias: str) -> str:
        if not provider_id or not alias or ":" in provider_id or ":" in alias:
            raise ValueError("invalid credential identity")
        return f"{provider_id}:{alias}"

    def set(self, *, provider_id: str, alias: str, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be non-empty")
        try:
            self._backend.set_password(
                SERVICE_NAME, self._username(provider_id, alias), secret
            )
        except Exception:
            raise StableError(
                code="credential_unavailable",
                category="credential",
                stage="credential_write",
                recoverable=True,
                next_actions=("configure_secure_keyring",),
            ) from None

    def get(self, *, provider_id: str, alias: str) -> str | None:
        try:
            return cast(
                str | None,
                self._backend.get_password(
                    SERVICE_NAME, self._username(provider_id, alias)
                ),
            )
        except Exception:
            raise StableError(
                code="credential_unavailable",
                category="credential",
                stage="credential_read",
                recoverable=True,
                next_actions=("configure_secure_keyring",),
            ) from None

    def clear(self, *, provider_id: str, alias: str) -> None:
        try:
            self._backend.delete_password(
                SERVICE_NAME, self._username(provider_id, alias)
            )
        except Exception:
            raise StableError(
                code="credential_unavailable",
                category="credential",
                stage="credential_clear",
                recoverable=True,
                next_actions=("configure_secure_keyring",),
            ) from None

    def exists(self, *, provider_id: str, alias: str) -> bool:
        return self.get(provider_id=provider_id, alias=alias) is not None
