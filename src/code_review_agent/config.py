"""Configuration values owned by the CLI composition root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CliConfig:
    reports_dir: Path = Path("reports")
    default_budget_tokens: int = 50_000
    max_budget_tokens: int = 1_000_000

    @classmethod
    def from_environment(cls) -> CliConfig:
        reports_dir = os.environ.get("CR_AGENT_REPORT_DIR")
        return cls(reports_dir=Path(reports_dir) if reports_dir else Path("reports"))
