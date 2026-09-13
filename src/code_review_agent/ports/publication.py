"""Port for side-effecting remote review publication."""

from __future__ import annotations

from typing import Protocol

from code_review_agent.domain.publication.models import (
    PublicationItem,
    PublicationTarget,
    RemoteComment,
    RemoteTargetState,
)


class RemotePublisherPort(Protocol):
    def verify_target(self, target: PublicationTarget) -> RemoteTargetState: ...

    def find_markers(
        self, target: PublicationTarget, markers: tuple[str, ...]
    ) -> dict[str, RemoteComment]: ...

    def create_line_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment: ...

    def create_summary_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment: ...
