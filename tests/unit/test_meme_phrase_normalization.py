from __future__ import annotations

from knowledge.moegirl_knowledge.filters import normalize_meme_phrase


def test_phrase_normalization_handles_cpu_pronoun_and_sentence_glue():
    assert normalize_meme_phrase("他在 CPU 你") == "人在cpu人"
    assert normalize_meme_phrase("他这是在 CPU 我吧？") == "人在cpu人"
