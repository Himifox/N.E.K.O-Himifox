# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Proactive Chat Router

Unified API for proactive-chat mode and frequency.

URL convention: routes are declared without a trailing slash (consistent with
``main_routers/config_router.py``; enforced by ``scripts/check_api_trailing_slash.py``).

Endpoints:

* ``GET  /api/proactive/mode``      — read the current mode (off / normal / focus / frequent / custom)
* ``POST /api/proactive/mode``      — apply a preset
* ``GET  /api/proactive/settings``  — read the current values of proactive-chat fields
* ``POST /api/proactive/settings``  — partially update proactive-chat fields (whitelisted)
* ``GET  /api/proactive/recommendation/summary`` — read shadow recommendation diagnostics
* ``GET  /api/proactive/recommendation/runtime`` — read active-source feature-flag state
* ``POST /api/proactive/recommendation/runtime/rollback`` — demote active-source to shadow

All writes go through ``utils.preferences.save_global_conversation_settings``
so the whitelist / type validation / atomic-write logic is maintained in one place.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any, Mapping

from fastapi import APIRouter, Request

from config import (
    PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
    PROACTIVE_RECOMMENDATION_OBSERVATION_LOG,
    PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
    PROACTIVE_RECOMMENDATION_TUNING_MODE,
)
from main_logic.proactive_recommendation.feedback.service import (
    record_recent_setting_feedback,
)
from main_logic.proactive_recommendation.feedback.events import (
    has_forbidden_feedback_fields,
)
from main_logic.proactive_recommendation.feedback.reports import (
    summarize_feedback_calibration,
    summarize_recommendation_feedback,
    summarize_reward_score_v2_preview,
)
from main_logic.proactive_recommendation.feedback.store import (
    FEEDBACK_LOG_FILENAME,
    load_recommendation_feedback_jsonl,
)
from main_logic.proactive_recommendation.application import (
    get_recommendation_application,
)
from main_logic.proactive_recommendation.domain_models import RecordFeedbackCommand
from main_logic.proactive_recommendation.observation.reports import (
    CALIBRATION_SAMPLE_LIMIT,
    CALIBRATION_WINDOW_SECONDS,
    DEFAULT_EXAMPLE_LIMIT,
    DEFAULT_HIGH_SCORE_THRESHOLD,
    MAX_EXAMPLE_LIMIT,
    get_recommendation_calibration_samples,
    select_recommendation_observation_examples,
    summarize_recommendation_calibration,
    summarize_recommendation_policy,
    summarize_recommendation_validation,
)
from main_logic.proactive_recommendation.observation.review import (
    summarize_recommendation_review_context,
)
from main_logic.proactive_recommendation.observation.store import (
    DEFAULT_ROTATE_BYTES,
    OBSERVATION_LOG_FILENAME,
    load_recommendation_observations_jsonl,
)
from main_logic.proactive_recommendation.state.bandit import (
    get_recommendation_bandit_state,
)
from main_logic.proactive_recommendation.tuning.store import (
    TUNING_FILENAME,
    load_recommendation_tuning,
)
from main_logic.proactive_recommendation.tuning.model import tuning_public_status
from utils.cloudsave_runtime import MaintenanceModeError
from utils.logger_config import get_module_logger
from utils.preferences import (
    aload_global_conversation_settings,
    save_global_conversation_settings,
)
from .shared_state import get_config_manager


router = APIRouter(prefix="/api/proactive", tags=["proactive"])
logger = get_module_logger(__name__, "Main")

# 用户绝对控制权 —— 插件和预设禁止越权修改的字段。
# ``proactiveVisionEnabled`` 是前端"隐私模式"开关的反面
# (``is_privacy_mode_enabled() == not proactiveVisionEnabled``)，
# 涉及屏幕内容采集，必须由用户本人在 UI 决定，任何 API 写入路径都要拒绝。
_USER_OWNED_FIELDS = frozenset({
    "proactiveVisionEnabled",
})

# 主动搭话所有可调字段（白名单子集；与 utils/preferences 的
# _ALLOWED_CONVERSATION_SETTINGS 保持同步，但只暴露搭话相关字段）。
# 注：``_PROACTIVE_FIELDS`` 仅用于**读路径**和模式反推，写路径会额外
# 过滤掉 ``_USER_OWNED_FIELDS``。
_PROACTIVE_BOOL_FIELDS = (
    "proactiveChatEnabled",
    "proactiveVisionEnabled",
    "proactiveVisionChatEnabled",
    "proactiveNewsChatEnabled",
    "proactiveVideoChatEnabled",
    "proactivePersonalChatEnabled",
    "proactiveMusicEnabled",
    "proactiveMemeEnabled",
    "proactiveMiniGameInviteEnabled",
)
_PROACTIVE_INT_FIELDS = (
    "proactiveChatInterval",
    "proactiveVisionInterval",
)
_PROACTIVE_FIELDS = _PROACTIVE_BOOL_FIELDS + _PROACTIVE_INT_FIELDS
# 写路径允许的字段：从全集里剔除用户专有字段。
_PROACTIVE_WRITABLE_FIELDS = frozenset(_PROACTIVE_FIELDS) - _USER_OWNED_FIELDS


# 预设模式：服务器端定义，避免每个调用方自己维护一份。
# interval 单位与前端 ``app-state.js`` 一致 —— 秒。
# 注：预设故意不包含 ``proactiveVisionEnabled``（隐私模式）；切换 mode
# 不会改变用户的隐私选择。
PROACTIVE_PRESETS: dict[str, dict[str, Any]] = {
    "off": {
        "proactiveChatEnabled": False,
        "proactiveVisionChatEnabled": False,
        "proactiveNewsChatEnabled": False,
        "proactiveVideoChatEnabled": False,
        "proactivePersonalChatEnabled": False,
        "proactiveMusicEnabled": False,
        "proactiveMemeEnabled": False,
        "proactiveMiniGameInviteEnabled": False,
    },
    "normal": {
        "proactiveChatEnabled": True,
        "proactiveVisionChatEnabled": True,
        "proactiveNewsChatEnabled": True,
        "proactiveVideoChatEnabled": True,
        "proactivePersonalChatEnabled": True,
        "proactiveMusicEnabled": True,
        "proactiveMemeEnabled": True,
        "proactiveMiniGameInviteEnabled": True,
        "proactiveChatInterval": 15,
        "proactiveVisionInterval": 10,
    },
    # 低打扰：保留搭话与个人动态，关掉新闻/视频/音乐等噪声源，间隔放长。
    # 不动 vision/隐私开关——是否允许看屏幕由用户自己决定。
    "focus": {
        "proactiveChatEnabled": True,
        "proactiveVisionChatEnabled": False,
        "proactiveNewsChatEnabled": False,
        "proactiveVideoChatEnabled": False,
        "proactivePersonalChatEnabled": True,
        "proactiveMusicEnabled": False,
        "proactiveMemeEnabled": False,
        "proactiveMiniGameInviteEnabled": False,
        "proactiveChatInterval": 60,
        "proactiveVisionInterval": 60,
    },
    # 高频：全开，间隔最短。
    "frequent": {
        "proactiveChatEnabled": True,
        "proactiveVisionChatEnabled": True,
        "proactiveNewsChatEnabled": True,
        "proactiveVideoChatEnabled": True,
        "proactivePersonalChatEnabled": True,
        "proactiveMusicEnabled": True,
        "proactiveMemeEnabled": True,
        "proactiveMiniGameInviteEnabled": True,
        "proactiveChatInterval": 5,
        "proactiveVisionInterval": 5,
    },
}

# Self-check：预设里不应混入用户绝对控制权字段，也不应有拼写错误/不可写字段。
# 每次模块加载时校验，把"加预设时忘了筛"和"键名打错被静默忽略"这两类回归
# 都挡在导入阶段，而不是用户调 set_mode 才暴露。
for _mode_name, _preset in PROACTIVE_PRESETS.items():
    _leaked = set(_preset.keys()) & _USER_OWNED_FIELDS
    if _leaked:
        raise RuntimeError(
            f"PROACTIVE_PRESETS[{_mode_name!r}] 不应包含用户专有字段: {sorted(_leaked)}"
        )
    _unknown = set(_preset.keys()) - _PROACTIVE_WRITABLE_FIELDS
    if _unknown:
        raise RuntimeError(
            f"PROACTIVE_PRESETS[{_mode_name!r}] 包含未知/不可写字段: {sorted(_unknown)}"
        )


def _filter_proactive_subset(settings: dict[str, Any]) -> dict[str, Any]:
    """Pick the proactive-chat-related fields out of the full conversation settings."""
    return {k: v for k, v in settings.items() if k in _PROACTIVE_FIELDS}


def _clamp_float(value: Any, *, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _recommendation_observation_log_path() -> Path | None:
    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception as exc:
        logger.debug("proactive recommendation summary config dir unavailable: %s", exc)
        return None
    if not config_dir:
        return None
    return Path(config_dir) / OBSERVATION_LOG_FILENAME


def _recommendation_feedback_log_path() -> Path | None:
    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception as exc:
        logger.debug("proactive recommendation feedback config dir unavailable: %s", exc)
        return None
    if not config_dir:
        return None
    return Path(config_dir) / FEEDBACK_LOG_FILENAME


async def _current_lanlan_name_and_config_dir() -> tuple[str, Any]:
    try:
        config_manager = get_config_manager()
        _, her_name_default, _, _, _, _, _, _, _ = await config_manager.aget_character_data()
        return str(her_name_default or "").strip(), getattr(config_manager, "config_dir", None)
    except Exception:
        return "", None


def _disabled_applied_fields(applied: Mapping[str, Any]) -> list[str]:
    return [
        key
        for key, value in applied.items()
        if key in _PROACTIVE_BOOL_FIELDS and value is False
    ]


def _value_matches(actual: Any, expected: Any) -> bool:
    """type-aware equality: avoids Python's ``True == 1`` / ``False == 0`` trap.

    The bool-field validation in ``save_global_conversation_settings`` is
    ``isinstance(v, bool)`` and rejects integer ``0/1``; but with plain ``==``,
    ``True`` on disk would still compare equal to an incoming ``1`` and be
    reported as "applied" — the same class of issue Codex pointed out.
    Requiring an exact ``type()`` match cuts this off entirely.
    """
    return type(actual) is type(expected) and actual == expected


async def _readback_persisted(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Read back after saving; returns ``(applied, rejected)``.

    The check is a **strict by-value + by-type comparison**:
    - By value: when ``save_global_conversation_settings`` runs its second-pass
      filter, dropped fields keep their old on-disk values; if we only checked
      key existence, "old value already on disk + new value rejected" would be
      mislabeled as applied.
    - By type: in Python ``True == 1`` / ``False == 0``; passing int ``1`` for a
      bool field gets rejected by the saver, yet the on-disk ``True`` still
      compares ``==`` to the incoming ``1``. ``_value_matches`` enforces an exact
      ``type()`` match to cut off this trap.
    """
    latest = await aload_global_conversation_settings()
    applied: dict[str, Any] = {}
    rejected: list[str] = []
    for k, v in payload.items():
        if k in latest and _value_matches(latest[k], v):
            applied[k] = latest[k]
        else:
            rejected.append(k)
    return applied, rejected


def _infer_mode(settings: dict[str, Any]) -> str:
    """Infer which preset the currently persisted fields correspond to; returns ``custom`` if none match.

    Only fields explicitly listed by a preset are compared; missing fields count as a mismatch.
    """
    for mode_name, preset in PROACTIVE_PRESETS.items():
        if all(settings.get(k) == v for k, v in preset.items()):
            return mode_name
    return "custom"


@router.get("/mode")
async def get_proactive_mode():
    """Read the current mode + the current proactive-chat fields."""
    try:
        settings = await aload_global_conversation_settings()
        subset = _filter_proactive_subset(settings)
        return {
            "success": True,
            "mode": _infer_mode(subset),
            "available_modes": list(PROACTIVE_PRESETS.keys()),
            "settings": subset,
        }
    except Exception as e:
        logger.exception(f"获取主动搭话模式失败: {e}")
        return {"success": False, "error": "Internal server error", "mode": "custom", "settings": {}}


@router.post("/mode")
async def set_proactive_mode(request: Request):
    """Apply a preset mode.

    Request body: ``{"mode": "off" | "normal" | "focus" | "frequent"}``
    """
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return {"success": False, "error": "请求体必须为对象"}
        mode = data.get("mode")
        if not isinstance(mode, str) or mode not in PROACTIVE_PRESETS:
            return {
                "success": False,
                "error": f"未知模式: {mode!r}；可选值: {list(PROACTIVE_PRESETS.keys())}",
            }

        preset = PROACTIVE_PRESETS[mode]
        if not await asyncio.to_thread(save_global_conversation_settings, dict(preset)):
            return {"success": False, "error": "保存失败"}

        applied, rejected = await _readback_persisted(preset)
        disabled_fields = _disabled_applied_fields(applied)
        if disabled_fields:
            lanlan_name, config_dir = await _current_lanlan_name_and_config_dir()
            if lanlan_name:
                record_recent_setting_feedback(
                    lanlan_name=lanlan_name,
                    disabled_fields=disabled_fields,
                    log_mode=PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
                    config_dir=config_dir,
                )
        result: dict[str, Any] = {"success": True, "mode": mode, "applied": applied}
        if rejected:
            # 预设里所有字段都应是合法值；若仍出现 rejected，多半是
            # _ALLOWED_CONVERSATION_SETTINGS 漂移，需要 server 端跟进。
            result["rejected"] = rejected
        return result
    except MaintenanceModeError:
        raise
    except Exception as e:
        logger.exception(f"切换主动搭话模式失败: {e}")
        return {"success": False, "error": "Internal server error"}


@router.get("/settings")
async def get_proactive_settings():
    """Read the current proactive-chat fields (whitelisted)."""
    try:
        settings = await aload_global_conversation_settings()
        return {"success": True, "settings": _filter_proactive_subset(settings)}
    except Exception as e:
        logger.exception(f"获取主动搭话设置失败: {e}")
        return {"success": False, "error": "Internal server error", "settings": {}}


@router.get("/recommendation/summary")
async def get_proactive_recommendation_summary(
    limit: int | None = None,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    include_examples: bool = False,
):
    """Read-only calibration summary for proactive recommendation shadow observations."""
    safe_threshold = _clamp_float(
        high_score_threshold,
        default=DEFAULT_HIGH_SCORE_THRESHOLD,
        lower=0.0,
        upper=1.0,
    )
    log_path = _recommendation_observation_log_path()
    missing = log_path is None or not log_path.exists()
    feedback_path = _recommendation_feedback_log_path()
    feedback_missing = feedback_path is None or not feedback_path.exists()
    observations = []
    if not missing and log_path is not None:
        observations = await asyncio.to_thread(
            load_recommendation_observations_jsonl,
            log_path,
            limit=CALIBRATION_SAMPLE_LIMIT,
        )
    feedback_events = []
    if not feedback_missing and feedback_path is not None:
        feedback_events = await asyncio.to_thread(
            load_recommendation_feedback_jsonl,
            feedback_path,
            limit=CALIBRATION_SAMPLE_LIMIT * 4,
        )
    now = time.time()
    calibration_samples = get_recommendation_calibration_samples(
        observations,
        now=now,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    calibration = summarize_recommendation_calibration(
        observations,
        now=now,
        high_score_threshold=safe_threshold,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    validation = summarize_recommendation_validation(
        observations,
        now=now,
        high_score_threshold=safe_threshold,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    feedback = summarize_recommendation_feedback(
        calibration_samples,
        feedback_events,
        now=now,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    feedback_calibration = summarize_feedback_calibration(
        calibration_samples,
        feedback_events,
        now=now,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    reward_score_v2_preview = summarize_reward_score_v2_preview(
        calibration_samples,
        feedback_events,
        now=now,
        window_seconds=CALIBRATION_WINDOW_SECONDS,
        sample_limit=CALIBRATION_SAMPLE_LIMIT,
    )
    review_context_validation = summarize_recommendation_review_context(
        calibration_samples
    )
    policy_monitor = summarize_recommendation_policy(calibration_samples)
    try:
        tuning_config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        tuning_config_dir = None
    tuning = load_recommendation_tuning(config_dir=tuning_config_dir)
    bandit_learning = get_recommendation_bandit_state(
        config_dir=tuning_config_dir, now=now
    )

    payload: dict[str, Any] = {
        "ok": True,
        "missing": missing,
        "log_enabled": PROACTIVE_RECOMMENDATION_OBSERVATION_LOG == "jsonl",
        "summary": calibration["summary"],
        "calibration": calibration,
        "validation": validation,
        "feedback": feedback,
        "feedback_calibration": feedback_calibration,
        "reward_score_v2_preview": reward_score_v2_preview,
        "review_context_validation": review_context_validation,
        "policy_monitor": policy_monitor,
        "bandit_learning": bandit_learning,
        "manual_tuning_preview": feedback_calibration.get("manual_tuning_preview", {}),
        "runtime": get_recommendation_application().get_runtime_status(),
        "tuning": tuning_public_status(tuning),
        "sample_count": calibration["sample_count"],
        "retention": {
            "filename": OBSERVATION_LOG_FILENAME,
            "feedback_filename": FEEDBACK_LOG_FILENAME,
            "tuning_filename": TUNING_FILENAME,
            "sample_window_seconds": CALIBRATION_WINDOW_SECONDS,
            "sample_limit": CALIBRATION_SAMPLE_LIMIT,
            "requested_limit_ignored": limit is not None,
            "high_score_threshold": safe_threshold,
            "examples_default_limit": DEFAULT_EXAMPLE_LIMIT,
            "examples_max_limit": MAX_EXAMPLE_LIMIT,
            "rotate_bytes": DEFAULT_ROTATE_BYTES,
            "feedback_missing": feedback_missing,
            "feedback_log_enabled": PROACTIVE_RECOMMENDATION_FEEDBACK_LOG == "jsonl",
            "tuning_mode": PROACTIVE_RECOMMENDATION_TUNING_MODE,
            "review_context_mode": PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE,
        },
    }
    if include_examples:
        payload["examples"] = select_recommendation_observation_examples(
            calibration_samples,
            high_score_threshold=safe_threshold,
            limit=DEFAULT_EXAMPLE_LIMIT,
        )
    return payload


@router.get("/recommendation/runtime")
async def get_proactive_recommendation_runtime():
    """Return the non-sensitive startup flag and effective runtime mode."""
    return {
        "success": True,
        "runtime": get_recommendation_application().get_runtime_status(),
    }


@router.post("/recommendation/runtime/rollback")
async def rollback_proactive_recommendation_runtime(request: Request):
    """Emergency one-way demotion from active_source to shadow.

    There is deliberately no matching activation endpoint.  Active source can
    only be opted into by the developer at process startup.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error
    reason = str(data.get("reason") or "developer_runtime_rollback").strip()[:120]
    result = get_recommendation_application().rollback_runtime(reason=reason)
    logger.warning(
        "proactive recommendation runtime rollback requested: applied=%s previous=%s",
        result.get("applied"),
        result.get("previous_mode"),
    )
    return {
        "success": True,
        **result,
    }


@router.post("/recommendation/feedback")
async def record_proactive_recommendation_feedback(request: Request):
    """Append one sanitized proactive recommendation feedback event."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {"success": False, "error": "request body must be an object"}

    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error

    if has_forbidden_feedback_fields(data):
        return {
            "success": False,
            "error": "feedback payload contains forbidden sensitive fields",
        }

    turn_id = str(data.get("turn_id") or "").strip()
    event_type = str(data.get("event_type") or "").strip()
    if not turn_id or not event_type:
        return {"success": False, "error": "turn_id and event_type are required"}

    try:
        config_manager = get_config_manager()
        _, her_name_default, _, _, _, _, _, _, _ = await config_manager.aget_character_data()
        config_dir = getattr(config_manager, "config_dir", None)
    except Exception:
        her_name_default = ""
        config_dir = None
    lanlan_name = str(data.get("lanlan_name") or her_name_default or "").strip()
    if not lanlan_name:
        return {"success": False, "error": "lanlan_name missing"}

    result = await get_recommendation_application().record_feedback(
        RecordFeedbackCommand(
            lanlan_name=lanlan_name,
            turn_id=turn_id,
            event_type=event_type,
            source_type=data.get("source_type"),
            candidate_id=data.get("candidate_id"),
            metadata=data.get("metadata") or {},
            log_mode=PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
            config_dir=config_dir,
        )
    )
    return {
        "success": True,
        "logged": result.logged,
        "event": result.event,
        "state_updated": result.state_updated,
        "feedback_scope": result.feedback_scope,
        "state_reason": result.state_reason,
        "preference_state_updated": result.preference_state_updated,
        "bandit_state_updated": result.bandit_state_updated,
        "log_enabled": PROACTIVE_RECOMMENDATION_FEEDBACK_LOG == "jsonl",
    }


@router.get("/recommendation/preference")
async def get_proactive_recommendation_preference():
    """Return the local, non-sensitive preference aggregate."""
    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    state = await get_recommendation_application().get_preference_state(
        config_dir=config_dir
    )
    return {"success": True, "preference_state": state}


@router.post("/recommendation/preference/reset")
async def reset_proactive_recommendation_preference(request: Request):
    """Clear learned preference evidence without changing recommendation flags."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error
    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    application = get_recommendation_application()
    reset = await application.reset_preference_state(config_dir=config_dir)
    return {
        "success": bool(reset),
        "preference_state": await application.get_preference_state(
            config_dir=config_dir
        ),
    }


@router.get("/recommendation/tuning")
async def get_proactive_recommendation_tuning():
    """Read sanitized proactive recommendation tuning state."""
    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    tuning = await get_recommendation_application().get_tuning_status(
        config_dir=config_dir
    )
    return {
        "ok": True,
        "mode": PROACTIVE_RECOMMENDATION_TUNING_MODE,
        "tuning": tuning,
        "retention": {
            "filename": TUNING_FILENAME,
        },
    }


@router.post("/recommendation/tuning/reset")
async def reset_proactive_recommendation_tuning(request: Request):
    """Reset local proactive recommendation tuning state."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error

    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    tuning = await get_recommendation_application().reset_tuning(config_dir=config_dir)
    return {
        "success": True,
        "tuning": tuning,
    }


@router.post("/recommendation/tuning/pause")
async def pause_proactive_recommendation_tuning(request: Request):
    """Pause automatic proactive recommendation tuning updates."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error

    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    reason = str(data.get("reason") or "manual_pause")
    try:
        duration_seconds = int(data.get("duration_seconds") or 6 * 3600)
    except (TypeError, ValueError):
        duration_seconds = 6 * 3600
    tuning = await get_recommendation_application().pause_tuning(
        config_dir=config_dir,
        duration_seconds=duration_seconds,
        reason=reason,
    )
    return {"success": True, "tuning": tuning}


@router.post("/recommendation/tuning/resume")
async def resume_proactive_recommendation_tuning(request: Request):
    """Resume automatic proactive recommendation tuning updates."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    from .system_router import _validate_local_mutation_request

    validation_error = _validate_local_mutation_request(request, payload=data)
    if validation_error is not None:
        return validation_error

    try:
        config_dir = getattr(get_config_manager(), "config_dir", None)
    except Exception:
        config_dir = None
    tuning = await get_recommendation_application().resume_tuning(
        config_dir=config_dir
    )
    return {"success": True, "tuning": tuning}


@router.post("/settings")
async def update_proactive_settings(request: Request):
    """Partially update proactive-chat fields. The request body only accepts fields
    in ``_PROACTIVE_WRITABLE_FIELDS``; user-owned fields (``proactiveVisionEnabled``
    privacy mode) are explicitly rejected and reported via ``rejected_user_owned``,
    while other unrecognized fields are silently ignored. The underlying
    ``save_global_conversation_settings`` performs another round of type + range validation."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return {"success": False, "error": "请求体必须为对象"}

        rejected_user_owned = sorted(set(data.keys()) & _USER_OWNED_FIELDS)
        payload = {k: v for k, v in data.items() if k in _PROACTIVE_WRITABLE_FIELDS}
        if not payload:
            err: dict[str, Any] = {"success": False, "error": "没有可识别的主动搭话字段"}
            if rejected_user_owned:
                err["rejected_user_owned"] = rejected_user_owned
            return err

        if not await asyncio.to_thread(save_global_conversation_settings, payload):
            return {"success": False, "error": "保存失败"}

        applied, rejected = await _readback_persisted(payload)
        disabled_fields = _disabled_applied_fields(applied)
        if disabled_fields:
            lanlan_name, config_dir = await _current_lanlan_name_and_config_dir()
            if lanlan_name:
                record_recent_setting_feedback(
                    lanlan_name=lanlan_name,
                    disabled_fields=disabled_fields,
                    log_mode=PROACTIVE_RECOMMENDATION_FEEDBACK_LOG,
                    config_dir=config_dir,
                )
        result: dict[str, Any] = {"success": True, "applied": applied}
        if rejected:
            # 字段类型/范围不合法被底层丢弃，或磁盘旧值与传入值不符。
            # 明确告知调用方避免误判为生效。
            result["rejected"] = rejected
        if rejected_user_owned:
            # 用户绝对控制权字段被拒：调用方应通过 UI 引导用户自行设置。
            result["rejected_user_owned"] = rejected_user_owned
        return result
    except MaintenanceModeError:
        raise
    except Exception as e:
        logger.exception(f"更新主动搭话设置失败: {e}")
        return {"success": False, "error": "Internal server error"}
