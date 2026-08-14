from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any, Mapping

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    get_os_activity_snapshot,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
)

from .config import RecommendationConfig
from .feedback import apply_feedback_to_profile, settle_history
from .gate import evaluate_gate
from .llm_gateway import BackgroundLlm
from .profile import (
    active_interests,
    apply_profile_updates,
    heuristic_updates,
    message_from_memory_record,
)
from .prompting import build_delivery_prompt
from .ranking import rank_candidates
from .sources import discover_from_plugins
from .state import StateRepository


@neko_plugin
class ProactiveRecommenderPlugin(NekoPluginBase):
    """Personalized proactive content recommendations, entirely plugin-side."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._config = RecommendationConfig()
        self._state = StateRepository(self.store)
        self._llm = BackgroundLlm(self.logger)
        self._ready = False
        self._cycle_lock = threading.Lock()

    async def _load_config(self) -> None:
        raw = await self.config.dump(timeout=5.0)
        self._config = RecommendationConfig.from_mapping(
            raw if isinstance(raw, Mapping) else {}
        )

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        if not self.store.enabled:
            return Err(
                SdkError("PluginStore must be enabled for proactive_recommender")
            )
        await self._load_config()
        await self._state.load()
        self._ready = True
        self.logger.info(
            "proactive recommender ready (enabled={}, shadow={})",
            self._config.enabled,
            self._config.shadow_mode,
        )
        return Ok({"ready": True})

    @lifecycle(id="config_change")
    async def config_change(self, **_: Any):
        await self._load_config()
        return Ok({"reloaded": True})

    async def _new_messages(self) -> list[dict[str, Any]]:
        records = await self.bus.memory.get(
            bucket_id=self._config.memory_bucket,
            limit=100,
            timeout=5.0,
        )
        snapshot = self._state.snapshot()
        processed = set(
            str(value) for value in snapshot.get("processed_message_ids", [])
        )
        output = []
        for record in records:
            message = message_from_memory_record(record)
            if message is not None and message["id"] not in processed:
                output.append(message)
        return output

    async def _update_profile(self, messages: list[dict[str, Any]], now: float) -> None:
        if not messages:
            snapshot = self._state.snapshot()
            settled = settle_history(
                snapshot.get("history", []),
                [],
                now=now,
                reply_window_seconds=self._config.reply_window_minutes * 60,
                ignored_window_seconds=self._config.ignored_window_minutes * 60,
            )
            if settled != snapshot.get("history", []):

                def mutate(state: dict[str, Any]) -> None:
                    state["profile"] = apply_feedback_to_profile(
                        state.get("profile", {}),
                        state.get("history", []),
                        settled,
                        now=now,
                    )
                    state["history"] = settled

                await self._state.update(mutate)
            return

        updates: list[dict[str, Any]] = []
        if self._config.background_llm:
            updates = await self._llm.extract_interests(messages)
        if not updates:
            updates = heuristic_updates(messages)

        def mutate(state: dict[str, Any]) -> None:
            state["profile"] = apply_profile_updates(
                state.get("profile"), updates, now=now
            )
            seen = [str(value) for value in state.get("processed_message_ids", [])]
            seen.extend(str(message["id"]) for message in messages)
            state["processed_message_ids"] = list(dict.fromkeys(seen))[-500:]
            state["last_user_message_at"] = max(
                float(message.get("timestamp", 0.0)) for message in messages
            )
            before = state.get("history", [])
            settled = settle_history(
                state.get("history", []),
                messages,
                now=now,
                reply_window_seconds=self._config.reply_window_minutes * 60,
                ignored_window_seconds=self._config.ignored_window_minutes * 60,
            )
            state["profile"] = apply_feedback_to_profile(
                state["profile"], before, settled, now=now
            )
            state["history"] = settled

        await self._state.update(mutate)

    def _discovery_due(
        self, snapshot: Mapping[str, Any], now: float, force: bool
    ) -> bool:
        if force:
            return True
        candidates = (
            snapshot.get("candidates")
            if isinstance(snapshot.get("candidates"), list)
            else []
        )
        fresh = [
            item
            for item in candidates
            if isinstance(item, Mapping)
            and now - float(item.get("discovered_at", 0.0))
            < self._config.candidate_ttl_hours * 3600
        ]
        return (
            len(fresh) < 5
            and now - float(snapshot.get("last_discovery_at", 0.0)) >= 3600
        )

    async def _discover(self, now: float, *, force: bool = False) -> int:
        snapshot = self._state.snapshot()
        if not self._discovery_due(snapshot, now, force):
            return 0
        interests = active_interests(snapshot.get("profile"), include_trial=True)
        if not interests:
            return 0
        queries = [str(item.get("name") or "") for item in interests[:2]]
        try:
            candidates = await discover_from_plugins(
                self.plugins,
                queries,
                web_search=self._config.web_search,
                bilibili=self._config.bilibili,
            )
        except Exception as exc:
            self.logger.warning(
                "recommendation discovery failed: {}", type(exc).__name__
            )
            return 0
        if self._config.background_llm and candidates:
            scores = await self._llm.assess_candidates(interests, candidates)
            for candidate in candidates:
                score = scores.get(str(candidate.get("id")))
                if score:
                    candidate["llm_relevance"] = score["relevance"]
                    candidate["llm_quality"] = score["quality"]
        ranked = rank_candidates(candidates, interests, snapshot.get("history", []))
        delivered_ids = {
            str(item.get("candidate_id"))
            for item in snapshot.get("history", [])
            if isinstance(item, Mapping)
        }

        def mutate(state: dict[str, Any]) -> None:
            existing = {
                str(item.get("id")): dict(item)
                for item in state.get("candidates", [])
                if isinstance(item, Mapping)
            }
            existing.update(
                {
                    str(item["id"]): item
                    for item in ranked
                    if str(item["id"]) not in delivered_ids
                }
            )
            state["candidates"] = sorted(
                existing.values(),
                key=lambda item: float(item.get("score", 0.0)),
                reverse=True,
            )[:50]
            state["last_discovery_at"] = now

        await self._state.update(mutate)
        return len(ranked)

    async def _global_proactive_enabled(self) -> bool:
        try:
            from utils.preferences import load_global_conversation_settings

            settings = await asyncio.to_thread(load_global_conversation_settings)
            return bool(settings.get("proactiveChatEnabled", True))
        except Exception as exc:
            self.logger.warning(
                "global proactive setting unavailable: {}", type(exc).__name__
            )
            return False

    async def _deliver(self, now: float) -> dict[str, Any]:
        snapshot = self._state.snapshot()
        ttl = self._config.candidate_ttl_hours * 3600
        candidates = [
            dict(item)
            for item in snapshot.get("candidates", [])
            if isinstance(item, Mapping)
            and now - float(item.get("discovered_at", 0.0)) < ttl
        ]
        eligible = [
            item
            for item in candidates
            if float(item.get("score", 0.0)) >= self._config.score_threshold
        ]
        if not eligible:
            return {"submitted": False, "reason": "no_eligible_candidate"}

        proactive_enabled = await self._global_proactive_enabled()
        privacy_state, idle_seconds = "unavailable", None
        try:
            activity = await get_os_activity_snapshot("proactive_recommender")
            privacy_state = activity.privacy_state
            idle_seconds = activity.system_idle_seconds
        except Exception:
            pass
        decision = evaluate_gate(
            config=self._config,
            now=datetime.fromtimestamp(now).astimezone(),
            history=snapshot.get("history", []),
            proactive_enabled=proactive_enabled,
            privacy_state=privacy_state,
            idle_seconds=idle_seconds,
            last_user_message_at=float(snapshot.get("last_user_message_at", 0.0)),
        )
        if not decision.allowed:
            return {"submitted": False, "reason": decision.reason}

        candidate = eligible[0]
        mode = "shadow" if self._config.shadow_mode else "live"
        submitted = False
        reason = "shadow_mode"
        if mode == "live":
            receipt = self.push_message(
                source="proactive_recommender",
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": build_delivery_prompt(candidate)}],
                priority=3,
                coalesce_key="proactive_recommender:content",
            )
            submitted = (
                bool(receipt.get("submitted"))
                if isinstance(receipt, Mapping)
                else False
            )
            reason = (
                "submitted" if submitted else str(receipt.get("reason", "rejected"))
            )

        def mutate(state: dict[str, Any]) -> None:
            if mode == "shadow" or submitted:
                state["candidates"] = [
                    item
                    for item in state.get("candidates", [])
                    if isinstance(item, Mapping)
                    and str(item.get("id")) != str(candidate.get("id"))
                ]
                state.setdefault("history", []).append(
                    {
                        "candidate_id": candidate.get("id"),
                        "title": candidate.get("title"),
                        "source": candidate.get("source"),
                        "matched_interests": candidate.get("matched_interests", []),
                        "score": candidate.get("score"),
                        "timestamp": now,
                        "local_date": datetime.fromtimestamp(now)
                        .astimezone()
                        .date()
                        .isoformat(),
                        "mode": mode,
                        "submitted": submitted,
                        "outcome": "shadow" if mode == "shadow" else "pending",
                    }
                )
                state["history"] = state["history"][-200:]

        await self._state.update(mutate)
        return {
            "submitted": submitted,
            "reason": reason,
            "candidate_id": candidate.get("id"),
            "mode": mode,
        }

    async def _run_cycle(self, *, force_discovery: bool = False) -> dict[str, Any]:
        if not self._ready:
            await self.startup()
        if not self._config.enabled:
            return {"enabled": False, "reason": "plugin_disabled"}
        now = time.time()
        messages = await self._new_messages()
        await self._update_profile(messages, now)
        discovered = await self._discover(now, force=force_discovery)
        delivery = await self._deliver(now)
        return {
            "enabled": True,
            "messages_processed": len(messages),
            "discovered": discovered,
            "delivery": delivery,
        }

    @timer_interval(id="recommendation_cycle", seconds=60, auto_start=True)
    async def recommendation_cycle(self, **_: Any):
        # Timer callbacks can overlap when a network source is slow.
        if not self._cycle_lock.acquire(blocking=False):
            return Ok({"skipped": "cycle_running"})
        try:
            return Ok(await self._run_cycle())
        except Exception as exc:
            self.logger.exception("recommendation cycle failed")
            return Err(SdkError(f"recommendation cycle failed: {type(exc).__name__}"))
        finally:
            self._cycle_lock.release()

    @plugin_entry(
        id="recommendation_status",
        name=tr("entries.status.name", default="推荐状态"),
        description=tr(
            "entries.status.description",
            default="查看个性化主动推荐状态，不返回用户原始对话。",
        ),
    )
    async def recommendation_status(self, **_: Any):
        snapshot = self._state.snapshot()
        interests = active_interests(snapshot.get("profile"), include_trial=True)
        return Ok(
            {
                "enabled": self._config.enabled,
                "shadow_mode": self._config.shadow_mode,
                "interests": [
                    {
                        "name": item.get("name"),
                        "weight": item.get("weight"),
                        "status": item.get("status"),
                    }
                    for item in interests[:12]
                ],
                "candidate_count": len(snapshot.get("candidates", [])),
                "history": snapshot.get("history", [])[-10:],
            }
        )

    @plugin_entry(
        id="recommendation_run_once",
        name=tr("entries.run_once.name", default="运行一次推荐周期"),
        description=tr(
            "entries.run_once.description",
            default="立即执行一次画像、候选发现和门控；仍遵守 enabled、shadow_mode 与所有安全限制。",
        ),
    )
    async def recommendation_run_once(self, **_: Any):
        if not self._cycle_lock.acquire(blocking=False):
            return Ok({"skipped": "cycle_running"})
        try:
            return Ok(await self._run_cycle(force_discovery=True))
        finally:
            self._cycle_lock.release()

    @plugin_entry(
        id="recommendation_feedback",
        name=tr("entries.feedback.name", default="推荐反馈"),
        description=tr(
            "entries.feedback.description",
            default="将一次推荐标记为喜欢、不喜欢或忽略。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["engaged", "rejected", "ignored"],
                },
            },
            "required": ["outcome"],
        },
    )
    async def recommendation_feedback(
        self, outcome: str, candidate_id: str = "", **_: Any
    ):
        if outcome not in {"engaged", "rejected", "ignored"}:
            return Err(SdkError("outcome must be engaged, rejected, or ignored"))
        changed = False

        def mutate(state: dict[str, Any]) -> None:
            nonlocal changed
            before = [dict(item) for item in state.get("history", [])]
            for item in reversed(state.get("history", [])):
                if item.get("mode") != "live":
                    continue
                if candidate_id and str(item.get("candidate_id")) != candidate_id:
                    continue
                item["outcome"] = outcome
                item["settled_at"] = time.time()
                changed = True
                break
            if changed:
                state["profile"] = apply_feedback_to_profile(
                    state.get("profile", {}),
                    before,
                    state.get("history", []),
                    now=time.time(),
                )

        await self._state.update(mutate)
        return Ok({"updated": changed, "outcome": outcome})


__all__ = ["ProactiveRecommenderPlugin"]
