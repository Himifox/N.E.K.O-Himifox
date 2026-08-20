"""Run the fixed public-knowledge response-quality corpus against N.E.K.O.

The script can perform a local routing preflight only, or connect to an already
running Main Server with ``--live``. It never judges semantic quality with a
second model; the JSON output keeps the expected intent beside the captured
reply for human review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import websockets

from knowledge.api import KnowledgeService


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "knowledge_response_quality_cases.json"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("quality cases must be a non-empty JSON array")
    required = {"id", "category", "message", "expected_mode", "intent", "forbidden"}
    seen: set[str] = set()
    for case in payload:
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError("each quality case must use the documented fields")
        case_id = str(case["id"])
        if not case_id or case_id in seen:
            raise ValueError("quality case ids must be non-empty and unique")
        seen.add(case_id)
        if case["expected_mode"] not in {"strong", "none"}:
            raise ValueError(f"invalid expected_mode for {case_id}")
        if not isinstance(case["forbidden"], list):
            raise ValueError(f"forbidden must be a list for {case_id}")
    return payload


def _route_preflight(cases: list[dict[str, Any]], database: Path) -> list[dict[str, Any]]:
    service = KnowledgeService.for_database(database)
    results: list[dict[str, Any]] = []
    for case in cases:
        context = service.build_turn_context(str(case["message"]), limit=1)
        term_match = re.search(r"^Term: (.+)$", context.text, flags=re.MULTILINE)
        actual_mode = context.match_mode
        results.append({
            **case,
            "actual_mode": actual_mode,
            "matched_term": term_match.group(1) if term_match else "",
            "route_pass": actual_mode == case["expected_mode"],
            "card_chars": len(context.text),
        })
    return results


async def _receive_until_complete(websocket, *, startup: bool = False) -> dict[str, Any]:
    chunks: list[str] = []
    event_types: set[str] = set()
    started = time.perf_counter()
    first_text_at: float | None = None
    while time.perf_counter() - started < 90:
        timeout = 3.0 if first_text_at is not None else 30.0
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            if first_text_at is not None:
                break
            continue
        if isinstance(raw, bytes):
            event_types.add("binary_audio")
            continue
        payload = json.loads(raw)
        event_type = str(payload.get("type") or "unknown")
        event_types.add(event_type)
        if startup and event_type == "session_started":
            return {"ready": True, "events": sorted(event_types)}
        if event_type == "session_failed":
            return {"ready": False, "events": sorted(event_types), "failure": payload}
        if event_type == "gemini_response":
            if first_text_at is None:
                first_text_at = time.perf_counter()
            chunks.append(str(payload.get("text") or ""))
        if chunks and event_type == "system" and payload.get("data") == "turn end":
            break
    finished = time.perf_counter()
    return {
        "reply": "".join(chunks),
        "events": sorted(event_types),
        "ttft_ms": round((first_text_at - started) * 1000, 1) if first_text_at else None,
        "total_ms": round((finished - started) * 1000, 1),
    }


async def _run_live(
    routed: list[dict[str, Any]],
    *,
    websocket_url: str,
    language: str,
) -> list[dict[str, Any]]:
    async with websockets.connect(websocket_url, max_size=8 * 1024 * 1024) as websocket:
        await websocket.send(json.dumps({
            "action": "start_session",
            "input_type": "text",
            "new_session": False,
            "language": language,
        }, ensure_ascii=False))
        startup = await _receive_until_complete(websocket, startup=True)
        if not startup.get("ready"):
            raise RuntimeError(f"text session failed: {startup}")
        results: list[dict[str, Any]] = []
        for index, case in enumerate(routed, start=1):
            await websocket.send(json.dumps({
                "action": "stream_data",
                "input_type": "text",
                "data": case["message"],
                "request_id": f"knowledge-quality-{index}",
                "language": language,
            }, ensure_ascii=False))
            outcome = await _receive_until_complete(websocket)
            results.append({**case, **outcome, "manual_result": "pending"})
        await websocket.send(json.dumps({
            "action": "end_session",
            "reason": "knowledge_quality_complete",
        }))
        return results


async def _run_direct(
    routed: list[dict[str, Any]],
    *,
    database: Path,
    language: str,
) -> list[dict[str, Any]]:
    """Exercise the production text client without Main Server, TTS, or memory."""
    from config.prompts.prompts_sys import SESSION_INIT_PROMPT, _loc
    from main_logic.omni_offline_client import OmniOfflineClient
    from utils.config_manager import get_config_manager
    from utils.language_utils import normalize_language_code

    config_manager = get_config_manager()
    knowledge = KnowledgeService.for_database(database)
    conversation = config_manager.get_model_api_config("conversation")
    vision = config_manager.get_model_api_config("vision")
    master_name, character_name, _, _, _, prompts, *_ = (
        config_manager.get_character_data()
    )
    prompt = (
        _loc(SESSION_INIT_PROMPT, normalize_language_code(language, format="short"))
        .format(name=character_name)
        + prompts[character_name]
    ).replace("{LANLAN_NAME}", character_name).replace("{MASTER_NAME}", master_name)

    chunks: list[str] = []
    first_text_at: float | None = None
    turn_started = 0.0

    async def capture_text(delta: str, _is_first: bool, **delivery: Any) -> None:
        nonlocal first_text_at
        if delivery.get("ui_enabled", True) is False:
            return
        if first_text_at is None:
            first_text_at = time.perf_counter()
        chunks.append(delta)

    client = OmniOfflineClient(
        base_url=conversation["base_url"],
        api_key=conversation["api_key"],
        model=conversation["model"],
        vision_model=vision["model"],
        vision_base_url=vision["base_url"],
        vision_api_key=vision["api_key"],
        provider_type=conversation.get("provider_type"),
        vision_provider_type=vision.get("provider_type"),
        on_text_delta=capture_text,
        max_response_length=300,
        lanlan_name=character_name,
        master_name=master_name,
        user_language_provider=lambda: language,
    )
    results: list[dict[str, Any]] = []
    try:
        for case in routed:
            # Each corpus case is isolated: only the character SystemMessage,
            # current user message and this turn's optional card reach the model.
            await client.connect(prompt)
            chunks.clear()
            first_text_at = None
            turn_started = time.perf_counter()
            context = knowledge.build_turn_context(str(case["message"]), limit=1)
            await client.stream_text(
                str(case["message"]),
                ephemeral_response_instruction=context.text,
            )
            finished = time.perf_counter()
            results.append({
                **case,
                "reply": "".join(chunks),
                "ttft_ms": (
                    round((first_text_at - turn_started) * 1000, 1)
                    if first_text_at is not None
                    else None
                ),
                "total_ms": round((finished - turn_started) * 1000, 1),
                "manual_result": "pending",
            })
    finally:
        await client.close()
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--direct", action="store_true")
    parser.add_argument("--websocket-url", default="ws://127.0.0.1:48911/ws/YUI")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cases = _load_cases(args.cases)
    routed = _route_preflight(cases, args.database)
    if args.live:
        results = asyncio.run(_run_live(
            routed,
            websocket_url=args.websocket_url,
            language=args.language,
        ))
        run_mode = "main_websocket"
    elif args.direct:
        results = asyncio.run(_run_direct(
            routed,
            database=args.database,
            language=args.language,
        ))
        run_mode = "direct_text_model"
    else:
        results = routed
        run_mode = "routing_only"
    payload = {
        "route_pass": all(case["route_pass"] for case in routed),
        "run_mode": run_mode,
        "cases": results,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if payload["route_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
