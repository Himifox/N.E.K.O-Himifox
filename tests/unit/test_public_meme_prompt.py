from __future__ import annotations

from config.prompts.prompts_chara import get_lanlan_prompt


def test_public_knowledge_prompt_covers_lookup_sampling_and_local_boundary():
    prompt = get_lanlan_prompt("en")
    assert "query_public_knowledge" in prompt
    assert "mode=lookup" in prompt
    assert "mode=sample" in prompt
    assert "dataset:tarot-interpretations" in prompt
    assert "guess a homophone" in prompt
    assert "local-only and never performs a web or encyclopedia lookup" in prompt
    assert "continue naturally without waiting" in prompt
