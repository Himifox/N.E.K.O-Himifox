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
from utils.llm_client import create_chat_llm_async
from utils.tokenize import atruncate_to_tokens
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
    model_config = await cm.aget_model_api_config("emotion")
    if not model_config.get("model") or not model_config.get("base_url"):
        return None

    # Budget each source before serialization so a long persona/context cannot
    # crowd the user's actual message out of the classifier input.
    max_tokens = MESSAGE_REACTION_INPUT_MAX_TOKENS
    name = await atruncate_to_tokens(payload.lanlan_name, 64)
    persona = await atruncate_to_tokens(str(personas[payload.lanlan_name] or ""), max_tokens // 4)
    context = [
        {"role": item.role, "text": await atruncate_to_tokens(item.text, max_tokens // 12)}
        for item in payload.context
    ]
    text = await atruncate_to_tokens(payload.text, max_tokens // 2)
    messages = [
        {"role": "system", "content": MESSAGE_REACTION_PROMPT},
        {"role": "user", "content": json.dumps({
            "companion": name, "persona": persona, "context": context,
            "latest_user_message": text,
        }, ensure_ascii=False)},
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
    parsed = robust_json_loads(result.content)
    if not isinstance(parsed, dict):
        return None
    emoji = parsed.get("emoji")
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
    except Exception:
        # No raw conversations, provider errors, or credentials in logs/results.
        return {"message_id": payload.message_id, "reaction": None}
