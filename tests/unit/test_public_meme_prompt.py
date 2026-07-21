from __future__ import annotations

from config.prompts.prompts_chara import get_lanlan_prompt


def test_public_meme_prompt_requires_lookup_before_wordplay_guessing():
    prompt = get_lanlan_prompt("en")
    assert "search_public_meme_knowledge" in prompt
    assert "guess a homophone" in prompt
    assert "Quotation marks and fixed sentence forms are never required" in prompt
    assert "Every ordinary user message is eligible" in prompt
    assert "before replying" in prompt
    assert "web_search plugin once only" in prompt
