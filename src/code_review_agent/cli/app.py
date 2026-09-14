"""CLI entry point."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from code_review_agent.bootstrap import CliRuntime, build_runtime
from code_review_agent.cli.commands import build_commands
from code_review_agent.config import CliConfig


def create_app(
    runtime_factory: Callable[[], CliRuntime] | None = None,
    config_path: Path = Path("code-review-agent.toml"),
) -> typer.Typer:
    if runtime_factory is None:
        config = CliConfig.from_file(config_path)

        def configured_runtime() -> CliRuntime:
            return build_runtime(config)

        runtime_factory = configured_runtime
    else:
        config = CliConfig()
    return build_commands(runtime_factory, config=config)


def main() -> None:
    create_app()()
