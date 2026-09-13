"""Credential storage boundary."""

from __future__ import annotations

from typing import Protocol


class CredentialStorePort(Protocol):
    def set(self, *, provider_id: str, alias: str, secret: str) -> None: ...

    def get(self, *, provider_id: str, alias: str) -> str | None: ...

    def clear(self, *, provider_id: str, alias: str) -> None: ...

    def exists(self, *, provider_id: str, alias: str) -> bool: ...
