"""Application service for shared platform credentials."""

from __future__ import annotations

from dataclasses import dataclass

from code_review_agent.ports.credentials import CredentialStorePort


@dataclass(frozen=True, slots=True)
class CredentialService:
    store: CredentialStorePort

    def set(self, *, provider_id: str, alias: str = "shared", secret: str) -> None:
        self.store.set(provider_id=provider_id, alias=alias, secret=secret)

    def get(self, *, provider_id: str, alias: str = "shared") -> str | None:
        return self.store.get(provider_id=provider_id, alias=alias)

    def clear(self, *, provider_id: str, alias: str = "shared") -> None:
        self.store.clear(provider_id=provider_id, alias=alias)

    def exists(self, *, provider_id: str, alias: str = "shared") -> bool:
        return self.store.exists(provider_id=provider_id, alias=alias)
