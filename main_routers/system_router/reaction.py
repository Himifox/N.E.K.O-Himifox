"""Optional reactions to user messages; independent of reply and avatar emotion."""

import asyncio
import json
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, Field, ValidationError

from config import (
    MESSAGE_REACTION_INPUT_MAX_TOKENS,
    MESSAGE_REACTION_OUTPUT_MAX_TOKENS,
    MESSAGE_REACTION_TIMEOUT_SECONDS,
)
from config.prompts.prompts_reaction import MESSAGE_REACTION_PROMPT
from utils.file_utils import robust_json_loads
from utils.icebreaker_free_text import strip_json_fence
from utils.llm_client import create_chat_llm_async
from utils.tokenize import acount_tokens, atruncate_to_tokens
from utils.token_tracker import set_call_type
from ..shared_state import get_config_manager
from ._shared import _validate_local_mutation_request, router


REACTION_EMOJIS = frozenset(("😊", "😄", "😃", "🙂", "😌", "🤔", "🧐", "💭", "❓", "👍", "✅", "🙌", "💪", "🎉", "🙏", "🤝", "😮", "👀", "⚠️", "💡", "😔", "😢", "😅", "🙇", "🥳", "✨", "🌟", "💻", "🤖", "📚", "🔧", "❤️", "⭐", "🔥", "🚀", "📌", "😂", "🤗"))


class ReactionContext(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=2000)


class MessageReactionRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    lanlan_name: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=6000)
    context: list[ReactionContext] = Field(default_factory=list, max_length=3)


async def _choose_message_reaction(payload: MessageReactionRequest):
    cm = get_config_manager()
    character_data = await cm.aget_character_data()
    personas = character_data[5]
    if payload.lanlan_name not in personas:
        return None
    persona = str(personas[payload.lanlan_name] or "").replace(
        "{LANLAN_NAME}", payload.lanlan_name
    ).replace("{MASTER_NAME}", str(character_data[0] or ""))
    model_config = await cm.aget_model_api_config("emotion")
    if not model_config.get("model") or not model_config.get("base_url"):
        return None

    # Reserve the complete contract and chat framing before dividing the data
    # budget. Check the serialized JSON too: escaping can expand bounded fields.
    user_budget = MESSAGE_REACTION_INPUT_MAX_TOKENS - await acount_tokens(MESSAGE_REACTION_PROMPT) - 32
    if user_budget <= 0:
        return None
    name = await atruncate_to_tokens(payload.lanlan_name, 64)
    data = {
        "companion": name, "persona": "", "latest_user_message": "",
        "context": [{"role": item.role, "text": ""} for item in payload.context],
    }
    source_budget = user_budget - await acount_tokens(json.dumps(data, ensure_ascii=False))
    while source_budget > 0:
        data["persona"] = await atruncate_to_tokens(persona, source_budget // 4)
        data["context"] = [
            {"role": item.role, "text": await atruncate_to_tokens(item.text, source_budget // 12)}
            for item in payload.context
        ]
        data["latest_user_message"] = await atruncate_to_tokens(payload.text, source_budget // 2)
        serialized = json.dumps(data, ensure_ascii=False)
        if await acount_tokens(serialized) <= user_budget and data["latest_user_message"]:
            break
        source_budget //= 2
    else:
        return None
    messages = [
        {"role": "system", "content": MESSAGE_REACTION_PROMPT},
        {"role": "user", "content": serialized},
    ]
    set_call_type("emotion")
    llm = await create_chat_llm_async(
        model_config["model"], model_config["base_url"], model_config.get("api_key", ""),
        provider_type=model_config.get("provider_type"),
        max_completion_tokens=MESSAGE_REACTION_OUTPUT_MAX_TOKENS,
        timeout=MESSAGE_REACTION_TIMEOUT_SECONDS,
    )
    async with llm:
        result = await llm.ainvoke(messages)
    parsed = robust_json_loads(strip_json_fence(result.content))
    if not isinstance(parsed, dict):
        return None
    emoji = parsed.get("emoji")
    # Providers may omit the optional emoji presentation selector. Always emit
    # the canonical allowlisted sequence so all frontend schemas agree.
    if emoji in ("❤", "⚠"):
        emoji += "\ufe0f"
    if not isinstance(emoji, str) or emoji not in REACTION_EMOJIS:
        return None
    return {"emoji": emoji, "author": payload.lanlan_name}


@router.post("/chat/reaction")
async def message_reaction(request: Request):
    validation_error = _validate_local_mutation_request(request)
    if validation_error is not None:
        return validation_error
    try:
        payload = MessageReactionRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return {"reaction": None}
    if not payload.text.strip():
        return {"reaction": None}
    try:
        # Covers configuration, tokenization, client construction and invocation.
        async with asyncio.timeout(MESSAGE_REACTION_TIMEOUT_SECONDS):
            reaction = await _choose_message_reaction(payload)
        return {"message_id": payload.message_id, "reaction": reaction}
    except Exception as exc:
        # No raw conversations, provider errors, or credentials in logs/results.
        print(f"[message_reaction] failed: {type(exc).__name__}")
        return {"message_id": payload.message_id, "reaction": None}
