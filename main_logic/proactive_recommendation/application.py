"""Application-level coordination for recommendation adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
from typing import Any

from config import PROACTIVE_RECOMMENDATION_TUNING_MODE

from .domain_models import RecordFeedbackCommand
from .feedback.contracts import RecommendationFeedbackRecordResult
from .feedback.service import (
    FeedbackService,
    ProactiveRecommendationFeedbackTurnSink,
    configure_feedback_logged_hook,
)
from .runtime import (
    get_recommendation_runtime_status,
    rollback_recommendation_runtime,
)
from .state.preference import (
    get_recommendation_preference_state,
    reset_recommendation_preference_state,
)
from .tuning.service import (
    TuningService,
)
from .turn import RecommendationTurn


class RecommendationApplication:
    def __init__(self) -> None:
        self.feedback = FeedbackService()
        self.tuning = TuningService()
        configure_feedback_logged_hook(self._after_feedback_logged)

    async def create_turn(
        self,
        *,
        lanlan_name: str,
        configured_interval_seconds: Any = None,
        config_dir: Any = None,
        log: logging.Logger | None = None,
        recent_sources: Sequence[str] = (),
    ) -> RecommendationTurn:
        return await RecommendationTurn.create(
            lanlan_name=lanlan_name,
            configured_interval_seconds=configured_interval_seconds,
            config_dir=config_dir,
            log=log,
            recent_sources=recent_sources,
        )

    async def record_feedback(
        self,
        command: RecordFeedbackCommand,
    ) -> RecommendationFeedbackRecordResult:
        return await asyncio.to_thread(self.record_feedback_sync, command)

    def record_feedback_sync(
        self,
        command: RecordFeedbackCommand,
    ) -> RecommendationFeedbackRecordResult:
        return self.feedback.record_event(command)

    async def get_preference_state(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            get_recommendation_preference_state,
            config_dir=config_dir,
        )

    async def reset_preference_state(self, *, config_dir: Any) -> bool:
        return await asyncio.to_thread(
            reset_recommendation_preference_state,
            config_dir=config_dir,
        )

    async def get_tuning_status(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.tuning.status, config_dir=config_dir)

    async def reset_tuning(self, *, config_dir: Any) -> dict[str, Any]:
        await asyncio.to_thread(self.tuning.reset, config_dir=config_dir)
        return await self.get_tuning_status(config_dir=config_dir)

    async def pause_tuning(
        self,
        *,
        config_dir: Any,
        duration_seconds: int,
        reason: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.tuning.pause,
            config_dir=config_dir,
            duration_seconds=duration_seconds,
            reason=reason,
        )

    async def resume_tuning(self, *, config_dir: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.tuning.resume,
            config_dir=config_dir,
        )

    def get_runtime_status(self) -> dict[str, Any]:
        return get_recommendation_runtime_status()

    def rollback_runtime(self, *, reason: Any = None) -> dict[str, Any]:
        return rollback_recommendation_runtime(reason=reason)

    def feedback_turn_sink(self) -> ProactiveRecommendationFeedbackTurnSink:
        return ProactiveRecommendationFeedbackTurnSink()

    def _after_feedback_logged(self, config_dir: Any) -> None:
        if config_dir is None or PROACTIVE_RECOMMENDATION_TUNING_MODE != "auto_safe":
            return
        result = self.tuning.maybe_auto_apply_from_logs(
            mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
            config_dir=config_dir,
        )
        if not result.get("applied") and not result.get("rollback_applied"):
            self.tuning.update_health_from_logs(
                mode=PROACTIVE_RECOMMENDATION_TUNING_MODE,
                config_dir=config_dir,
            )


_application = RecommendationApplication()


def get_recommendation_application() -> RecommendationApplication:
    return _application
