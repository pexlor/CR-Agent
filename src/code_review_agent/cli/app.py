"""CLI entry point."""

from __future__ import annotations

from collections.abc import Callable

import typer

from code_review_agent.bootstrap import CliRuntime, build_runtime
from code_review_agent.cli.commands import build_commands


def create_app(
    runtime_factory: Callable[[], CliRuntime] = build_runtime,
) -> typer.Typer:
    return build_commands(runtime_factory)


app = create_app()


def main() -> None:
    app()
