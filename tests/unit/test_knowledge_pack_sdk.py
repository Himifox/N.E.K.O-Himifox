from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from knowledge.packs import canonical_pack_bytes, load_pack, validate_pack


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "knowledge" / "schemas" / "knowledge-pack-v1.schema.json"
EXAMPLE_PATH = (
    REPO_ROOT / "examples" / "knowledge-packs" / "minimal.neko-knowledge.json"
)
CLI_PATH = REPO_ROOT / "scripts" / "validate_knowledge_pack.py"


def _example_payload() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _run_cli(path: Path, *, strict: bool = False) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLI_PATH)]
    if strict:
        command.append("--strict")
    command.append(str(path))
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_is_draft_2020_12_and_accepts_example() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = _example_payload()

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(payload, schema, cls=jsonschema.Draft202012Validator)
    assert load_pack(EXAMPLE_PATH).collection_id == "example-colors"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"entrypoint": "unsafe.module:main"}),
        lambda payload: payload["entries"][0].update({"content": ""}),
        lambda payload: payload["entries"][0].update({"tags": ["source:forged"]}),
        lambda payload: payload["source"].update({"homepage": "javascript:alert(1)"}),
    ],
)
def test_schema_and_runtime_reject_shared_invalid_fixtures(mutate) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = _example_payload()
    mutate(payload)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema, cls=jsonschema.Draft202012Validator)
    with pytest.raises(ValueError):
        validate_pack(payload)


def test_normal_validation_accepts_formatted_json() -> None:
    result = _run_cli(EXAMPLE_PATH)

    assert result.returncode == 0
    assert "[PASS] pack_id: example-colors" in result.stdout
    assert "[PASS] collection_id: example-colors" in result.stdout
    assert "[PASS] entries: 1" in result.stdout


def test_strict_validation_requires_canonical_bytes(tmp_path: Path) -> None:
    formatted = _run_cli(EXAMPLE_PATH, strict=True)
    assert formatted.returncode == 2
    assert "[WARN] canonical_json" in formatted.stderr

    payload = _example_payload()
    canonical_path = tmp_path / "canonical.neko-knowledge.json"
    canonical_bytes = canonical_pack_bytes(payload)
    assert canonical_bytes.endswith(b"\n")
    assert b"\r" not in canonical_bytes
    canonical_path.write_bytes(canonical_bytes)
    canonical = _run_cli(canonical_path, strict=True)

    assert canonical.returncode == 0
    assert "[PASS] canonical_json" in canonical.stdout


def test_invalid_pack_reports_field_without_content(tmp_path: Path) -> None:
    payload = _example_payload()
    secret_content = payload["entries"][0]["content"]
    payload["entries"][0]["title"] = ""
    invalid_path = tmp_path / "invalid.neko-knowledge.json"
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run_cli(invalid_path)

    assert result.returncode == 2
    assert "entries[0].title" in result.stderr
    assert secret_content not in result.stdout
    assert secret_content not in result.stderr


def test_missing_pack_is_an_operational_error(tmp_path: Path) -> None:
    result = _run_cli(tmp_path / "missing.neko-knowledge.json")

    assert result.returncode == 1
    assert "[FAIL] file:" in result.stderr
