"""Package bootstrap tests."""

import importlib


def test_package_is_importable_and_has_version() -> None:
    package = importlib.import_module("code_review_agent")

    version = getattr(package, "__version__", "")
    assert isinstance(version, str)
    assert version.strip()


def test_cli_module_import_has_no_output_or_side_effect(capsys) -> None:
    importlib.import_module("code_review_agent.cli.app")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
