"""Thread-safe process-local registry for delivered recommendation turns."""

from __future__ import annotations

from collections.abc import Iterable
import threading

from .contracts import PendingFeedbackClaim, PendingRecommendationFeedback


class PendingFeedbackRegistry:
    def __init__(self, *, reply_window_seconds: float) -> None:
        self._reply_window_seconds = float(reply_window_seconds)
        self._items: dict[tuple[str, str], PendingRecommendationFeedback] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def register(
        self, pending: PendingRecommendationFeedback
    ) -> PendingRecommendationFeedback:
        with self._lock:
            self._items[(pending.lanlan_name, pending.turn_id)] = pending
            self._prune_unlocked(now=pending.delivered_at)
            return pending

    def claim_event(
        self,
        lanlan_name: str,
        turn_id: str,
        *,
        event_type: str,
        state_group: str,
    ) -> PendingFeedbackClaim:
        with self._lock:
            pending = self._items.get((lanlan_name, turn_id))
            if pending is None:
                return PendingFeedbackClaim(pending=None)
            duplicate_event = event_type in pending.seen_event_types
            duplicate_group = state_group in pending.seen_groups
            pending.seen_event_types.add(event_type)
            pending.seen_groups.add(state_group)
            return PendingFeedbackClaim(
                pending=pending,
                duplicate_event=duplicate_event,
                duplicate_group=duplicate_group,
            )

    def get(
        self, lanlan_name: str, turn_id: str
    ) -> PendingRecommendationFeedback | None:
        with self._lock:
            return self._items.get((lanlan_name, turn_id))

    def add_reward_event(
        self,
        pending: PendingRecommendationFeedback,
        event_type: str,
        event: dict,
    ) -> tuple[dict, ...]:
        with self._lock:
            current = self._items.get((pending.lanlan_name, pending.turn_id))
            if current is None:
                return ()
            current.reward_events[event_type] = event
            return tuple(current.reward_events.values())

    def latest(
        self,
        lanlan_name: str,
        *,
        now: float,
        source_type: str | None = None,
        require_candidate: bool = False,
    ) -> PendingRecommendationFeedback | None:
        with self._lock:
            self._prune_unlocked(now=now)
            candidates = [
                pending
                for pending in self._items.values()
                if pending.lanlan_name == lanlan_name
                and (source_type is None or pending.source_type == source_type)
                and (not require_candidate or bool(pending.candidate_id))
                and 0 <= now - pending.delivered_at <= self._reply_window_seconds
            ]
            return max(candidates, key=lambda item: item.delivered_at, default=None)

    def claim_reply_action(self, pending: PendingRecommendationFeedback) -> str | None:
        with self._lock:
            current = self._items.get((pending.lanlan_name, pending.turn_id))
            if current is None:
                return None
            if current.reply_seen and not current.continue_seen:
                current.continue_seen = True
                return "continue"
            if not current.reply_seen:
                current.reply_seen = True
                return "reply"
            return None

    def mark_replied(
        self, pending_items: Iterable[PendingRecommendationFeedback]
    ) -> None:
        with self._lock:
            for pending in pending_items:
                current = self._items.get((pending.lanlan_name, pending.turn_id))
                if current is not None:
                    current.reply_seen = True

    def consecutive_unanswered(self, lanlan_name: str, *, now: float) -> int:
        with self._lock:
            self._prune_unlocked(now=now)
            rows = sorted(
                (
                    pending
                    for pending in self._items.values()
                    if pending.lanlan_name == lanlan_name
                    and pending.delivered_at <= now
                ),
                key=lambda pending: pending.delivered_at,
                reverse=True,
            )
            count = 0
            for pending in rows:
                if pending.reply_seen:
                    break
                count += 1
            return count

    def _prune_unlocked(self, *, now: float) -> None:
        retention = self._reply_window_seconds * 2
        expired = [
            key
            for key, pending in self._items.items()
            if now - pending.delivered_at > retention
        ]
        for key in expired:
            self._items.pop(key, None)
