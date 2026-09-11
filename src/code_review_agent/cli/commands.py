"""Typer command definitions for the MVP CLI."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import typer

from code_review_agent.application.dto import StartReviewCommand
from code_review_agent.bootstrap import CliRuntime
from code_review_agent.cli.exit_codes import INTERNAL_ERROR, NOT_FOUND, USAGE_ERROR
from code_review_agent.cli.presenters import human_result, human_trace, json_envelope
from code_review_agent.config import CliConfig


def _request_id(value: str | None) -> str:
    return value or str(uuid4())


def _valid_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    allowed = {
        ("github.com", "/pull/"),
        ("gitlab.com", "/merge_requests/"),
    }
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not any(
            parsed.netloc == host and marker in parsed.path
            for host, marker in allowed
        )
    ):
        raise typer.BadParameter("URL must be an HTTPS GitHub PR or GitLab MR")
    return value


def build_commands(
    runtime_factory: Callable[[], CliRuntime],
    config: CliConfig | None = None,
) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, add_completion=False)
    cli_config = config or CliConfig.from_environment()

    @app.command()
    def review(
        diff_file: Path | None = typer.Option(None, "--diff-file"),  # noqa: B008
        stdin: bool = typer.Option(False, "--stdin"),  # noqa: B008
        url: str | None = typer.Option(None, "--url"),  # noqa: B008
        provider: str = typer.Option(..., "--provider"),  # noqa: B008
        model: str = typer.Option(..., "--model"),  # noqa: B008
        budget_tokens: int = typer.Option(
            cli_config.default_budget_tokens, "--budget-tokens"
        ),  # noqa: B008
        json_output: bool = typer.Option(False, "--json"),  # noqa: B008
        no_color: bool = typer.Option(False, "--no-color"),  # noqa: B008
        verbose: bool = typer.Option(False, "--verbose"),  # noqa: B008
        request_id: str | None = typer.Option(None, "--request-id"),  # noqa: B008
    ) -> None:
        del no_color, verbose
        source_count = sum(value is not None for value in (diff_file, url)) + stdin
        if source_count == 0 and not sys.stdin.isatty():
            stdin = True
            source_count = 1
        if source_count != 1:
            typer.echo("exactly one input source is required", err=True)
            raise typer.Exit(USAGE_ERROR)
        if not 1 <= budget_tokens <= cli_config.max_budget_tokens:
            typer.echo("budget-tokens must be between 1 and 1000000", err=True)
            raise typer.Exit(USAGE_ERROR)
        source_url = _valid_url(url) if url is not None else None
        diff_text = sys.stdin.read() if stdin else None
        task_id = str(uuid4())
        command = StartReviewCommand(
            task_id=task_id,
            diff_text=diff_text,
            diff_file=diff_file,
            output_path=cli_config.reports_dir / f"{task_id}.md",
            source_url=source_url,
        )
        rid = _request_id(request_id)
        try:
            result = runtime_factory().review(
                command, provider, model, budget_tokens, rid
            )
        except Exception as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(INTERNAL_ERROR) from exc
        if json_output:
            typer.echo(json_envelope("review", rid, result))
        else:
            typer.echo(human_result(result))

    @app.command()
    def status(
        task_id: str,
        json_output: bool = typer.Option(False, "--json"),  # noqa: B008
        request_id: str | None = typer.Option(None, "--request-id"),  # noqa: B008
    ) -> None:
        rid = _request_id(request_id)
        try:
            result = runtime_factory().status(task_id)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(NOT_FOUND) from exc
        if json_output:
            typer.echo(json_envelope("status", rid, result))
        else:
            typer.echo(human_result(result))

    trace_app = typer.Typer(no_args_is_help=True, add_completion=False)
    app.add_typer(trace_app, name="trace")

    @trace_app.command("show")
    def trace_show(
        trace_id: str,
        json_output: bool = typer.Option(False, "--json"),  # noqa: B008
        request_id: str | None = typer.Option(None, "--request-id"),  # noqa: B008
    ) -> None:
        rid = _request_id(request_id)
        try:
            events = runtime_factory().trace(trace_id)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(NOT_FOUND) from exc
        data = {"trace_id": trace_id, "events": events}
        if json_output:
            typer.echo(json_envelope("trace show", rid, data))
        else:
            typer.echo(human_trace(trace_id, events))

    return app
