"""Scenario assets for the deterministic proactive recommendation testbench."""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from tests.testbench import config as tb_config
from tests.testbench.pipeline.atomic_io import atomic_write_json

SCHEMA_VERSIONS = {1, 2}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STAGES = {"source", "material"}
_SENSITIVE_KEYS = {"cookie", "cookies", "token", "api_key", "authorization", "screen_text", "user_text"}


class RecommendationScenarioError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, errors: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.errors = errors or []


def validate_scenario_dict(raw: Any) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return {"ok": False, "errors": [{"path": "$", "message": "scenario must be an object"}]}
    sid = str(raw.get("id") or "")
    if not _ID_RE.fullmatch(sid):
        errors.append({"path": "id", "message": "must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}"})
    version = raw.get("schema_version", 1)
    if version not in SCHEMA_VERSIONS:
        errors.append({"path": "schema_version", "message": "only schema_version 1 or 2 is supported"})
    if raw.get("stage") not in _STAGES:
        errors.append({"path": "stage", "message": "must be source or material"})
    kind = raw.get("kind", "single")
    if version == 2 and kind not in {"single", "sequence"}:
        errors.append({"path": "kind", "message": "must be single or sequence"})
    required_objects = ("base_context", "base_inputs") if kind == "sequence" else ("context", "inputs")
    for key in (*required_objects, "oracle"):
        if not isinstance(raw.get(key, {}), dict):
            errors.append({"path": key, "message": "must be an object"})
    oracle = raw.get("oracle") if isinstance(raw.get("oracle"), dict) else {}
    relevance = oracle.get("relevance", {})
    if not isinstance(relevance, dict):
        errors.append({"path": "oracle.relevance", "message": "must be an object"})
    else:
        for cid, value in relevance.items():
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
                errors.append({"path": f"oracle.relevance.{cid}", "message": "must be an integer from 0 to 3"})
    for key in ("must_filter_candidate_ids", "acceptable_top1_sources"):
        value = oracle.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            errors.append({"path": f"oracle.{key}", "message": "must be an array of non-empty strings"})
    reasons = oracle.get("expected_filter_reasons", {})
    if not isinstance(reasons, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in reasons.items()):
        errors.append({"path": "oracle.expected_filter_reasons", "message": "must be a string map"})
    expected_empty = oracle.get("expected_empty")
    if expected_empty is not None and not isinstance(expected_empty, bool):
        errors.append({"path": "oracle.expected_empty", "message": "must be boolean or null"})
    must_filter = set(oracle.get("must_filter_candidate_ids") or [])
    if isinstance(reasons, dict) and not set(reasons).issubset(must_filter):
        errors.append({"path": "oracle.expected_filter_reasons",
                       "message": "every reason key must also appear in must_filter_candidate_ids"})
    if version == 2:
        factor = raw.get("factor_under_test")
        if not isinstance(factor, str) or not factor.strip():
            errors.append({"path": "factor_under_test", "message": "must be a non-empty string"})
        controlled = raw.get("controlled_fields", [])
        if not isinstance(controlled, list) or any(not isinstance(item, str) or not item for item in controlled):
            errors.append({"path": "controlled_fields", "message": "must be an array of strings"})
        if kind == "sequence":
            _validate_sequence(raw.get("steps"), errors)
    _scan_sensitive(raw, "$", errors)
    _scan_non_finite(raw, "$", errors)
    normalized = copy.deepcopy(raw)
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("name", sid)
    normalized.setdefault("description", "")
    normalized.setdefault("tags", [])
    normalized.setdefault("context", {})
    normalized.setdefault("inputs", {})
    normalized.setdefault("oracle", {})
    normalized.setdefault("kind", "single")
    return {"ok": not errors, "errors": errors, "normalized": normalized if not errors else None}


def _validate_sequence(steps: Any, errors: list[dict[str, str]]) -> None:
    if not isinstance(steps, list) or len(steps) < 2:
        errors.append({"path": "steps", "message": "sequence requires at least two steps"})
        return
    seen: set[str] = set()
    allowed_patch_keys = {"recent_sources", "recent_shadow_sources", "recent_candidate_ids",
                          "activity_state", "privacy_state", "source_weights", "source_type_adjustments"}
    for index, step in enumerate(steps):
        path = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append({"path": path, "message": "must be an object"}); continue
        step_id = str(step.get("id") or "")
        if not step_id or step_id in seen:
            errors.append({"path": f"{path}.id", "message": "must be non-empty and unique"})
        seen.add(step_id)
        patch = step.get("context_patch", {})
        if not isinstance(patch, dict):
            errors.append({"path": f"{path}.context_patch", "message": "must be an object"})
        elif set(patch) - allowed_patch_keys:
            errors.append({"path": f"{path}.context_patch", "message": "contains forbidden patch fields"})
        if not isinstance(step.get("variant_patch", {}), dict):
            errors.append({"path": f"{path}.variant_patch", "message": "must be an object"})
        if not isinstance(step.get("oracle", {}), dict):
            errors.append({"path": f"{path}.oracle", "message": "must be an object"})


def _scan_sensitive(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).lower() in _SENSITIVE_KEYS:
                errors.append({"path": child_path, "message": "sensitive/raw user field is forbidden"})
            _scan_sensitive(child, child_path, errors)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _scan_sensitive(child, f"{path}[{i}]", errors)


def _scan_non_finite(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        errors.append({"path": path, "message": "non-finite numbers are forbidden"})
    elif isinstance(value, dict):
        for key, child in value.items():
            _scan_non_finite(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_non_finite(child, f"{path}[{index}]", errors)


def _scan_dir(path: Path, source: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    for file in sorted(path.glob("*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                result = validate_scenario_dict(item)
                if result["ok"]:
                    data = result["normalized"]
                    rows[data["id"]] = {"scenario": data, "source": source, "path": str(file)}
        except (OSError, ValueError):
            continue
    return rows


def list_scenarios() -> list[dict[str, Any]]:
    builtin = _scan_dir(tb_config.BUILTIN_RECOMMENDATION_SCENARIOS_DIR, "builtin")
    user = _scan_dir(tb_config.USER_RECOMMENDATION_SCENARIOS_DIR, "user")
    result = []
    for sid in sorted(set(builtin) | set(user)):
        active = user.get(sid) or builtin[sid]
        scenario = active["scenario"]
        result.append({
            "id": sid, "name": scenario.get("name", sid), "description": scenario.get("description", ""),
            "stage": scenario.get("stage"), "tags": scenario.get("tags", []), "source": active["source"],
            "kind": scenario.get("kind", "single"), "factor_under_test": scenario.get("factor_under_test"),
            "has_builtin": sid in builtin, "has_user": sid in user, "overriding_builtin": sid in builtin and sid in user,
        })
    return result


def read_scenario(scenario_id: str) -> dict[str, Any]:
    builtin = _scan_dir(tb_config.BUILTIN_RECOMMENDATION_SCENARIOS_DIR, "builtin")
    user = _scan_dir(tb_config.USER_RECOMMENDATION_SCENARIOS_DIR, "user")
    active = user.get(scenario_id) or builtin.get(scenario_id)
    if not active:
        raise RecommendationScenarioError("RecommendationScenarioNotFound", f"unknown scenario: {scenario_id}", 404)
    return {**copy.deepcopy(active["scenario"]), "_source": active["source"],
            "has_builtin": scenario_id in builtin, "has_user": scenario_id in user,
            "overriding_builtin": scenario_id in builtin and scenario_id in user}


def read_builtin_scenario(scenario_id: str) -> dict[str, Any]:
    """Read the immutable builtin layer, deliberately bypassing user overlay."""
    builtin = _scan_dir(tb_config.BUILTIN_RECOMMENDATION_SCENARIOS_DIR, "builtin")
    active = builtin.get(scenario_id)
    if not active:
        raise RecommendationScenarioError("RecommendationBuiltinScenarioNotFound",
                                          f"unknown builtin scenario: {scenario_id}", 404)
    return {**copy.deepcopy(active["scenario"]), "_source": "builtin",
            "has_builtin": True, "has_user": False, "overriding_builtin": False}


def save_user_scenario(raw: dict[str, Any]) -> dict[str, Any]:
    result = validate_scenario_dict(raw)
    if not result["ok"]:
        raise RecommendationScenarioError("RecommendationScenarioInvalid", "scenario validation failed", 422, result["errors"])
    data = result["normalized"]
    tb_config.USER_RECOMMENDATION_SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(tb_config.USER_RECOMMENDATION_SCENARIOS_DIR / f"{data['id']}.json", data)
    return read_scenario(data["id"])


def delete_user_scenario(scenario_id: str) -> dict[str, Any]:
    target = tb_config.USER_RECOMMENDATION_SCENARIOS_DIR / f"{scenario_id}.json"
    if not target.exists():
        raise RecommendationScenarioError("RecommendationScenarioBuiltinProtected", "only user scenarios can be deleted", 403)
    target.unlink()
    return {"deleted": scenario_id}


def duplicate_scenario(source_id: str, target_id: str, overwrite: bool = False) -> dict[str, Any]:
    if not _ID_RE.fullmatch(target_id):
        raise RecommendationScenarioError("RecommendationScenarioInvalid", "invalid target id", 422)
    target = tb_config.USER_RECOMMENDATION_SCENARIOS_DIR / f"{target_id}.json"
    if target.exists() and not overwrite:
        raise RecommendationScenarioError("RecommendationScenarioTargetExists", "target scenario exists", 409)
    data = read_scenario(source_id)
    for key in ("_source", "has_builtin", "has_user", "overriding_builtin"):
        data.pop(key, None)
    data["id"] = target_id
    data["name"] = f"{data.get('name', source_id)} (copy)"
    return save_user_scenario(data)


__all__ = ["RecommendationScenarioError", "delete_user_scenario", "duplicate_scenario", "list_scenarios",
           "read_builtin_scenario", "read_scenario", "save_user_scenario", "validate_scenario_dict"]
