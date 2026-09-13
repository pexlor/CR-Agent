from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1] / "fixtures" / "acceptance"


def test_acceptance_manifest_has_fourteen_predeclared_samples() -> None:
    data = tomllib.loads((ROOT / "manifest.toml").read_text(encoding="utf-8"))
    samples = data["sample"]

    assert data["schema_version"] == "1"
    assert [sample["id"] for sample in samples] == [
        f"AC-{index:02d}" for index in range(1, 15)
    ]
    for sample in samples:
        fixture = ROOT / sample["fixture"]
        assert fixture.is_file()
        assert sample["confidence"] in {"none", "high", "reference"}
        assert isinstance(sample["expected_findings"], list)
        assert isinstance(sample["forbidden_findings"], list)
        assert sample["requirements"]


def test_acceptance_hashes_are_stable_and_machine_readable() -> None:
    data = tomllib.loads((ROOT / "manifest.toml").read_text(encoding="utf-8"))
    hashes = {}
    for sample in data["sample"]:
        content = (ROOT / sample["fixture"]).read_bytes()
        hashes[sample["id"]] = hashlib.sha256(content).hexdigest()

    assert len(hashes) == 14
    assert all(len(digest) == 64 for digest in hashes.values())
