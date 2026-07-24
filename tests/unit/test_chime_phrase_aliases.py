from __future__ import annotations

from knowledge.moegirl_knowledge.filters import normalize_meme_phrase
from knowledge.moegirl_knowledge.sources.chime import _entry_from_record


def test_phrase_normalization_handles_cpu_pronoun_and_sentence_glue():
    assert normalize_meme_phrase("他在 CPU 你") == "人在cpu人"
    assert normalize_meme_phrase("他这是在 CPU 我吧？") == "人在cpu人"


def test_chime_entry_stores_a_phrase_alias_and_hashes_it_for_reimport():
    record = {
        "meme": "他在 CPU 你",
        "meaning": "being manipulated through language",
        "examples": [],
    }

    entry = _entry_from_record(record, record_index=1)

    assert entry.aliases == ("人在cpu人",)
    assert entry.content_hash
