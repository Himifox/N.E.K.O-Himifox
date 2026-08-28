"""Synthetic tests for the maintainer-only candidate miner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import natural_expression_candidate_miner as miner
from utils import natural_expression_candidates as candidate_core


def test_compatibility_script_runs_directly_without_pythonpath(tmp_path: Path):
    script = (
        Path(__file__).parents[2] / "scripts" / "natural_expression_candidate_miner.py"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: natural_expression_candidate_miner.py" in result.stdout
    assert "--input INPUT" in result.stdout


def _config(**overrides) -> miner.MiningConfig:
    values = {
        "threshold": 3,
        "word_ngram_min": 2,
        "word_ngram_max": 2,
        "cjk_ngram_min": 4,
        "cjk_ngram_max": 4,
        "min_length": 1,
        "exclude_covered": False,
    }
    values.update(overrides)
    return miner.MiningConfig(**values)


def _candidate(report, normalized_phrase: str):
    return next(
        candidate
        for candidate in report["candidates"]
        if candidate["normalized_phrase"] == normalized_phrase
    )


def test_assistant_only_counts_occurrences_and_distinct_messages(tmp_path: Path):
    input_path = tmp_path / "synthetic.jsonl"
    records = [
        {
            "role": "system",
            "content": "Soft silver rain Soft silver rain",
            "lang": "en",
        },
        {"role": "user", "content": "Soft silver rain Soft silver rain", "lang": "en"},
        {
            "role": "assistant",
            "content": "Soft silver rain. Soft silver rain.",
            "lang": "en",
        },
        {"role": "assistant", "content": "Soft silver rain.", "lang": "en"},
    ]
    input_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    messages, record_count = miner.read_jsonl(input_path)
    report = miner.build_report(
        messages,
        input_record_count=record_count,
        config=_config(word_ngram_min=3, word_ngram_max=3),
        rules_by_language={},
    )

    candidate = _candidate(report, "soft silver rain")
    assert candidate["occurrence_count"] == 3
    assert candidate["message_count"] == 2
    assert report["summary"]["assistant_message_count"] == 2
    assert report["summary"]["input_record_count"] == 4


def test_explicit_language_overrides_missing_or_unknown_record_language(tmp_path: Path):
    input_path = tmp_path / "override.jsonl"
    records = [
        {"role": "assistant", "content": "quiet lantern"},
        {"role": "assistant", "content": "quiet lantern", "lang": "unknown"},
        {"role": "assistant", "content": "quiet lantern", "lang": "ja"},
    ]
    input_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    messages, record_count = miner.read_jsonl(input_path, language_override="en-US")
    report = miner.build_report(
        messages,
        input_record_count=record_count,
        config=_config(),
        rules_by_language={},
    )

    assert [message.language for message in messages] == ["en", "en", "en"]
    assert _candidate(report, "quiet lantern")["occurrence_count"] == 3


@pytest.mark.parametrize(
    ("language", "phrase", "normalized"),
    [
        ("en", "Gentle Moonlight", "gentle moonlight"),
        ("es", "brisa cálida", "brisa cálida"),
        ("pt-BR", "Coração tranquilo", "coração tranquilo"),
        ("ru", "Тихий свет", "тихий свет"),
    ],
)
def test_unicode_word_ngrams_by_language(language, phrase, normalized):
    messages = [
        miner.SourceMessage(
            language=miner.normalize_language(language),
            content=phrase,
            source_line=index,
        )
        for index in range(1, 4)
    ]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )

    assert _candidate(report, normalized)["occurrence_count"] == 3


def test_word_tokenization_normalizes_decomposed_accents_before_counting():
    messages = [
        miner.SourceMessage("pt", "café tranquilo", 1),
        miner.SourceMessage("pt", "cafe\u0301 tranquilo", 2),
        miner.SourceMessage("pt", "cafe\u0301 tranquilo", 3),
    ]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )

    assert _candidate(report, "café tranquilo")["occurrence_count"] == 3


@pytest.mark.parametrize(
    ("language", "phrase"),
    [
        ("zh-CN", "嘴角微微上扬"),
        ("zh-TW", "嘴角微微上揚"),
        ("ja", "静かな月明かり"),
    ],
)
def test_cjk_character_ngrams_split_at_punctuation(language, phrase):
    size = len(phrase)
    config = _config(cjk_ngram_min=size, cjk_ngram_max=size)
    messages = [
        miner.SourceMessage(language, f"{phrase}。{phrase}！", 1),
        miner.SourceMessage(language, phrase, 2),
    ]

    report = miner.build_report(
        messages,
        input_record_count=2,
        config=config,
        rules_by_language={},
    )

    candidate = _candidate(report, phrase)
    assert candidate["occurrence_count"] == 3
    assert candidate["message_count"] == 2
    assert all(
        "。" not in item["phrase"] and "！" not in item["phrase"]
        for item in report["candidates"]
    )


def test_japanese_iteration_marks_remain_in_script_runs():
    phrase = "時々微笑む"
    messages = [miner.SourceMessage("ja", phrase, index) for index in range(1, 4)]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(cjk_ngram_min=len(phrase), cjk_ngram_max=len(phrase)),
        rules_by_language={},
    )

    assert _candidate(report, phrase)["occurrence_count"] == 3


def test_korean_uses_word_and_hangul_character_strategies():
    messages = [
        miner.SourceMessage("ko", "조용한 달빛. 두근두근.", index)
        for index in range(1, 4)
    ]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )

    assert _candidate(report, "조용한 달빛")["occurrence_count"] == 3
    assert _candidate(report, "두근두근")["occurrence_count"] == 3


def test_korean_single_token_is_not_double_counted_across_strategies():
    messages = [miner.SourceMessage("ko", "두근두근", index) for index in range(1, 3)]
    config = _config(
        threshold=1,
        word_ngram_min=1,
        word_ngram_max=1,
    )

    report = miner.build_report(
        messages,
        input_record_count=2,
        config=config,
        rules_by_language={},
    )

    candidate = _candidate(report, "두근두근")
    assert candidate["occurrence_count"] == 2
    assert candidate["message_count"] == 2

    below_threshold = miner.build_report(
        messages,
        input_record_count=2,
        config=_config(
            threshold=3,
            word_ngram_min=1,
            word_ngram_max=1,
        ),
        rules_by_language={},
    )
    assert below_threshold["candidates"] == []


def test_korean_word_candidates_stop_at_the_occurrence_cap(monkeypatch):
    generated = 0
    original_word_candidates = candidate_core._word_candidates

    def counted_word_candidates(*args, **kwargs):
        nonlocal generated
        for candidate in original_word_candidates(*args, **kwargs):
            generated += 1
            yield candidate

    monkeypatch.setattr(candidate_core, "_word_candidates", counted_word_candidates)
    message = candidate_core.SourceMessage(
        "ko",
        " ".join(["조용한"] * 20),
        1,
    )

    with pytest.raises(
        candidate_core.CandidateMinerError,
        match="assistant history exceeds local analysis limit",
    ):
        candidate_core.build_report(
            [message],
            input_record_count=1,
            config=_config(),
            rules_by_language={},
            max_occurrences=3,
        )

    assert generated == 4


def test_code_urls_and_template_noise_are_protected():
    text = (
        "`hidden phrase` https://example.test/hidden-phrase\n"
        "intranet.example/private-path\n"
        "```text\nhidden phrase\n```\n"
        "{{hidden phrase}} <HIDDEN_PHRASE>\n"
        "visible phrase"
    )
    messages = [miner.SourceMessage("en", text, index) for index in range(1, 4)]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )

    normalized = {candidate["normalized_phrase"] for candidate in report["candidates"]}
    assert "visible phrase" in normalized
    assert "hidden phrase" not in normalized
    assert all("intranet" not in phrase for phrase in normalized)
    assert all("example" not in phrase for phrase in normalized)


def test_indented_markdown_code_is_protected():
    text = "visible phrase\n\n    secret_key = value"
    messages = [miner.SourceMessage("en", text, index) for index in range(1, 4)]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )

    normalized = {candidate["normalized_phrase"] for candidate in report["candidates"]}
    assert "visible phrase" in normalized
    assert all("secret_key" not in phrase for phrase in normalized)


def test_threshold_filters_below_minimum_occurrence_count():
    messages = [
        miner.SourceMessage("en", "quiet lantern", 1),
        miner.SourceMessage("en", "quiet lantern", 2),
    ]

    report = miner.build_report(
        messages,
        input_record_count=2,
        config=_config(threshold=3),
        rules_by_language={},
    )

    assert report["candidates"] == []


def test_current_rule_coverage_is_read_only_and_can_be_excluded():
    messages = [
        miner.SourceMessage("en", "She smiled warmly", index) for index in range(1, 4)
    ]
    rules = {
        "en": [
            {
                "id": "EN_004",
                "find": r"\b(he|she|they|I|you)\s+smiled\s+(?:warmly|softly)\b",
                "flags": re.IGNORECASE,
            }
        ]
    }

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(word_ngram_min=3, word_ngram_max=3),
        rules_by_language=rules,
    )
    assert _candidate(report, "she smiled warmly")["covered_by_rule_ids"] == ["EN_004"]

    excluded = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(
            word_ngram_min=3,
            word_ngram_max=3,
            exclude_covered=True,
        ),
        rules_by_language=rules,
    )
    assert excluded["candidates"] == []


def test_partially_covered_candidate_is_annotated_but_not_excluded():
    messages = [
        miner.SourceMessage("en", "smiled warmly", index) for index in range(1, 4)
    ]
    messages.append(miner.SourceMessage("en", "She smiled warmly", 4))
    rules = {
        "en": [
            {
                "id": "EN_004",
                "find": r"\b(he|she|they|I|you)\s+smiled\s+(?:warmly|softly)\b",
                "flags": re.IGNORECASE,
            }
        ]
    }

    report = miner.build_report(
        messages,
        input_record_count=4,
        config=_config(exclude_covered=True),
        rules_by_language=rules,
    )

    candidate = _candidate(report, "smiled warmly")
    assert candidate["covered_by_rule_ids"] == ["EN_004"]
    assert candidate["occurrence_count"] == 4


def test_coverage_work_is_cached_across_candidates(monkeypatch):
    text = "quiet lantern silver morning"
    messages = [miner.SourceMessage("en", text, index) for index in range(1, 4)]
    rules = {"en": [{"id": "EN_CACHE", "find": r"\bquiet lantern\b"}]}
    original_compile = candidate_core.re.compile
    original_protected_spans = candidate_core._runtime_protected_spans
    calls = {"compile": 0, "finditer": 0, "protected": 0}

    class CountingPattern:
        def __init__(self, pattern):
            self.pattern = pattern

        def finditer(self, text):
            calls["finditer"] += 1
            return self.pattern.finditer(text)

    def counting_compile(pattern, flags=0):
        calls["compile"] += 1
        return CountingPattern(original_compile(pattern, flags))

    def counting_protected_spans(text):
        calls["protected"] += 1
        return original_protected_spans(text)

    monkeypatch.setattr(candidate_core.re, "compile", counting_compile)
    monkeypatch.setattr(
        candidate_core, "_runtime_protected_spans", counting_protected_spans
    )

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(word_ngram_min=2, word_ngram_max=2),
        rules_by_language=rules,
    )

    assert len(report["candidates"]) == 3
    # Candidate extraction scans each source message; coverage adds one shared scan.
    assert calls == {"compile": 1, "finditer": 1, "protected": 4}


def test_word_coverage_uses_original_sentence_delimiters():
    text = "Он смотрел. Словно само время замерло"
    messages = [miner.SourceMessage("ru", text, index) for index in range(1, 4)]
    rules = {
        "ru": [
            {
                "id": "RU_011",
                "find": (
                    r"(^|[.!?…]\s)(?:Словно|Будто)\s+(?:само\s+)?"
                    r"время\s+(?:замерло|остановилось|застыло)\b"
                ),
            }
        ]
    }

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(
            word_ngram_min=4,
            word_ngram_max=4,
            exclude_covered=True,
        ),
        rules_by_language=rules,
    )

    assert report["candidates"] == []


def test_cjk_coverage_uses_original_punctuation_context():
    text = "张了张嘴，欲言又止"
    messages = [miner.SourceMessage("zh-CN", text, index) for index in range(1, 4)]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
    )

    assert _candidate(report, "张了张嘴")["covered_by_rule_ids"] == ["ZH_026"]
    assert _candidate(report, "欲言又止")["covered_by_rule_ids"] == ["ZH_026"]

    excluded = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(exclude_covered=True),
    )
    assert excluded["candidates"] == []


def test_coverage_does_not_normalize_original_runtime_text():
    text = "aguanto\u0301 el aliento"
    messages = [miner.SourceMessage("es", text, index) for index in range(1, 4)]
    rules = {
        "es": [
            {
                "id": "ES_002",
                "find": r"\b(?:contuvo|aguant[oó]) el aliento\b",
            }
        ]
    }

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(
            word_ngram_min=3,
            word_ngram_max=3,
            exclude_covered=True,
        ),
        rules_by_language=rules,
    )

    assert _candidate(report, "aguantó el aliento")["covered_by_rule_ids"] == []


def test_coverage_preserves_protected_suffixes_in_original_text():
    text = "A beat of silence passed `token`"
    messages = [miner.SourceMessage("en", text, index) for index in range(1, 4)]
    rules = {
        "en": [
            {
                "id": "EN_023",
                "find": (
                    r"\b(?:[Aa]\s+)?(?:beat|moment)\s+of\s+silence\s+"
                    r"(?:passed|hung|stretched|fell|followed|settled)"
                    r"(?=\s*[.,;:!?]|\s*$)"
                ),
            }
        ]
    }

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(
            word_ngram_min=5,
            word_ngram_max=5,
            exclude_covered=True,
        ),
        rules_by_language=rules,
    )

    assert _candidate(report, "a beat of silence passed")["covered_by_rule_ids"] == []


def test_coverage_reads_the_real_curated_rule_table():
    messages = [
        miner.SourceMessage("en", "She smiled warmly", index) for index in range(1, 4)
    ]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(word_ngram_min=3, word_ngram_max=3),
    )

    assert _candidate(report, "she smiled warmly")["covered_by_rule_ids"] == ["EN_004"]


def test_traditional_chinese_is_not_covered_by_simplified_runtime_rules():
    phrase = "嘴角微微上揚"
    messages = [miner.SourceMessage("zh-TW", phrase, index) for index in range(1, 4)]
    rules = {"zh": [{"id": "ZH_TEST", "find": phrase}]}

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(
            cjk_ngram_min=len(phrase),
            cjk_ngram_max=len(phrase),
            exclude_covered=True,
        ),
        rules_by_language=rules,
    )

    assert _candidate(report, phrase)["covered_by_rule_ids"] == []


def test_cjk_coverage_checks_the_complete_matched_collocation():
    phrase = "嘴角微微勾起一抹笑意"
    messages = [miner.SourceMessage("zh-CN", phrase, index) for index in range(1, 4)]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
    )
    assert _candidate(report, "嘴角微微")["covered_by_rule_ids"] == ["ZH_002"]

    excluded = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(exclude_covered=True),
    )
    assert excluded["candidates"] == []


def test_output_schema_is_pending_and_not_a_runtime_rule_schema():
    messages = [
        miner.SourceMessage("en", "quiet lantern", index) for index in range(1, 4)
    ]

    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )
    candidate = _candidate(report, "quiet lantern")

    assert report["schema_version"] == "natural-expression-candidates/v1"
    assert report["artifact_type"] == "maintainer_review_candidates"
    assert candidate["status"] == "pending"
    assert set(candidate) == {
        "covered_by_rule_ids",
        "language",
        "message_count",
        "normalized_phrase",
        "occurrence_count",
        "phrase",
        "status",
    }
    assert "find" not in candidate and "replace" not in candidate
    assert "context" not in candidate and "conversation_id" not in candidate


def test_serialized_output_is_byte_deterministic(tmp_path: Path):
    messages = [
        miner.SourceMessage("en", "quiet lantern", index) for index in range(1, 4)
    ]
    report = miner.build_report(
        messages,
        input_record_count=3,
        config=_config(),
        rules_by_language={},
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    miner.write_report(first, report)
    miner.write_report(second, report)

    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    ("line", "error_fragment"),
    [
        ("not-json\n", "invalid JSON"),
        (json.dumps(["not", "an", "object"]) + "\n", "must be an object"),
        (
            json.dumps({"role": "assistant", "content": ["not text"], "lang": "en"})
            + "\n",
            "content must be a string",
        ),
        (json.dumps({"role": "assistant", "content": "hello"}) + "\n", "require lang"),
    ],
)
def test_bad_input_reports_line_without_echoing_content(
    tmp_path: Path, line, error_fragment
):
    input_path = tmp_path / "bad.jsonl"
    input_path.write_text(line, encoding="utf-8")

    with pytest.raises(miner.CandidateMinerError, match=error_fragment) as exc_info:
        miner.read_jsonl(input_path)

    assert "line 1" in str(exc_info.value)
    assert "hello" not in str(exc_info.value)


def test_cli_default_stdout_does_not_print_candidate_text(tmp_path: Path, capsys):
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "review.json"
    records = [
        {"role": "assistant", "content": "private synthetic phrase", "lang": "en"}
        for _ in range(3)
    ]
    input_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    return_code = miner.main(
        [
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--word-ngram-min",
            "3",
            "--word-ngram-max",
            "3",
        ]
    )
    captured = capsys.readouterr()

    assert return_code == 0
    assert "private synthetic phrase" not in captured.out
    assert "private synthetic phrase" in output_path.read_text(encoding="utf-8")


def test_user_review_requires_three_distinct_assistant_messages():
    one_message = [
        candidate_core.SourceMessage(
            "en",
            "quiet lantern. quiet lantern. quiet lantern",
            1,
        )
    ]

    one_message_report = candidate_core.build_user_review_report(
        one_message,
        rules_by_language={},
    )
    assert one_message_report["candidates"] == []

    three_messages = [
        candidate_core.SourceMessage("en", "quiet lantern", source_line)
        for source_line in range(1, 4)
    ]
    report = candidate_core.build_user_review_report(
        three_messages,
        rules_by_language={},
    )
    candidate = _candidate(report, "quiet lantern")

    assert report["artifact_type"] == "user_review_candidates"
    assert report["parameters"]["message_count_threshold"] == 3
    assert report["summary"] == {
        "assistant_message_count": 3,
        "analyzed_message_count": 3,
        "messages_truncated": False,
        "content_truncated": False,
        "candidate_count": 1,
        "returned_candidate_count": 1,
        "candidates_truncated": False,
    }
    assert candidate["occurrence_count"] == 3
    assert candidate["message_count"] == 3
    assert candidate["status"] == "pending"
    assert set(candidate) == {
        "covered_by_rule_ids",
        "language",
        "message_count",
        "normalized_phrase",
        "occurrence_count",
        "phrase",
        "status",
    }


def test_maintainer_report_keeps_occurrence_only_compatibility():
    messages = [
        candidate_core.SourceMessage(
            "en",
            "quiet lantern. quiet lantern. quiet lantern",
            1,
        )
    ]

    report = candidate_core.build_report(
        messages,
        input_record_count=1,
        config=_config(),
        rules_by_language={},
    )

    assert _candidate(report, "quiet lantern")["message_count"] == 1


def test_report_can_bound_retained_occurrences():
    messages = [candidate_core.SourceMessage("zh-CN", "安静灯笼安静灯笼", 1)]

    with pytest.raises(
        candidate_core.CandidateMinerError,
        match="assistant history exceeds local analysis limit",
    ):
        candidate_core.build_report(
            messages,
            input_record_count=1,
            config=_config(),
            rules_by_language={},
            max_occurrences=2,
        )


def test_user_review_narrows_window_when_character_budget_is_exceeded(monkeypatch):
    """An oversized history narrows the window instead of failing the request."""
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_INPUT_CHARACTERS", 30)
    messages = [
        candidate_core.SourceMessage("en", "quiet lantern", source_line)
        for source_line in range(1, 5)
    ]

    report = candidate_core.build_user_review_report(messages, rules_by_language={})

    summary = report["summary"]
    assert summary["assistant_message_count"] == 4
    assert summary["analyzed_message_count"] == 2
    assert summary["messages_truncated"] is True


def test_user_review_narrows_window_when_occurrence_budget_is_exceeded(monkeypatch):
    """The n-gram budget bites long before the character budget does.

    100 replies of ~280 unbroken Han characters blow through
    ``USER_REVIEW_MAX_OCCURRENCES`` at roughly 21% of
    ``USER_REVIEW_MAX_INPUT_CHARACTERS``.  Raising there produced a 422 the
    browser could only render as "please try again", which never succeeded.
    """
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_OCCURRENCES", 400)
    unbroken = "今天天气真好我们一起去散步你觉得怎么样我觉得非常开心因为可以和你聊天"
    messages = [
        candidate_core.SourceMessage("zh-CN", unbroken, source_line)
        for source_line in range(1, 17)
    ]

    report = candidate_core.build_user_review_report(messages, rules_by_language={})

    summary = report["summary"]
    assert summary["assistant_message_count"] == 16
    assert summary["messages_truncated"] is True
    assert 0 < summary["analyzed_message_count"] < 16


def test_user_review_still_reports_a_single_unanalyzable_message(monkeypatch):
    """The narrowing loop floors at one message rather than looping forever."""
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_OCCURRENCES", 1)
    messages = [
        candidate_core.SourceMessage("en", "quiet lantern glows", 1),
    ]

    with pytest.raises(candidate_core.CandidateBudgetExceededError):
        candidate_core.build_user_review_report(messages, rules_by_language={})


def test_user_review_caps_candidates_before_returning_them_to_the_browser(monkeypatch):
    candidates = [
        {
            "normalized_phrase": f"candidate {index}",
            "status": "pending",
        }
        for index in range(3)
    ]
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_CANDIDATES", 2)
    monkeypatch.setattr(
        candidate_core,
        "build_report",
        lambda *args, **kwargs: {
            "candidates": candidates,
            "parameters": {},
        },
    )

    report = candidate_core.build_user_review_report([], rules_by_language={})

    assert report["candidates"] == candidates[:2]
    assert report["summary"] == {
        "assistant_message_count": 0,
        "analyzed_message_count": 0,
        "messages_truncated": False,
        "content_truncated": False,
        "candidate_count": 3,
        "returned_candidate_count": 2,
        "candidates_truncated": True,
    }
    assert report["parameters"]["candidate_output_limit"] == 2


def test_user_review_rejects_invalid_distinct_message_threshold():
    with pytest.raises(
        candidate_core.CandidateMinerError,
        match="message_count_threshold must be at least 1",
    ):
        candidate_core.build_user_review_report([], message_count_threshold=0)


def test_narrowed_window_can_fall_below_the_distinct_message_threshold(monkeypatch):
    """A narrowed window can make the 3-message threshold unsatisfiable.

    The browser bases its minimum-sample check on `analyzed_message_count` for
    exactly this case: with fewer analyzed replies than the threshold no
    candidate can qualify, so "no candidates found" would misreport an
    impossible evaluation as a genuine absence.
    """
    # One such reply mines to 145 occurrences, so a 200 budget admits
    # exactly one message and the window narrows all the way down.
    monkeypatch.setattr(candidate_core, "USER_REVIEW_MAX_OCCURRENCES", 200)
    unbroken = "今天天气真好我们一起去散步你觉得怎么样我觉得非常开心因为可以和你聊天"
    messages = [
        candidate_core.SourceMessage("zh-CN", unbroken, source_line)
        for source_line in range(1, 33)
    ]

    summary = candidate_core.build_user_review_report(
        messages, rules_by_language={}
    )["summary"]

    assert summary["assistant_message_count"] == 32
    assert summary["messages_truncated"] is True
    assert summary["analyzed_message_count"] < 3
    assert summary["candidate_count"] == 0


def _blockquoted(*lines: str) -> str:
    return "\n".join("> " + line for line in lines)


def test_blockquoted_fenced_code_stays_protected():
    """A fence quoted inside a reply must protect its body, not just its rails.

    Stripping only whitespace left the `>` prefix in place, so the opening fence
    never matched. The inline-code pass then protected just the two delimiter
    lines and every identifier in between became an exportable candidate.
    """
    text = _blockquoted(
        "```python",
        "secret_token_helper = compute_secret_token_helper()",
        "secret_token_helper = compute_secret_token_helper()",
        "```",
    )
    messages = [
        candidate_core.SourceMessage("en", text, source_line)
        for source_line in range(1, 4)
    ]

    assert candidate_core._fenced_code_spans(text) == [(0, len(text))]
    report = candidate_core.build_report(
        messages,
        input_record_count=3,
        config=candidate_core.MiningConfig(),
        rules_by_language={},
    )
    assert report["candidates"] == []


def test_nested_and_unclosed_blockquoted_fences_stay_protected():
    nested = "\n".join(
        [
            ">> ```python",
            ">> secret_token_helper = compute_secret_token_helper()",
            ">> ```",
        ]
    )
    unclosed = _blockquoted(
        "```python",
        "secret_token_helper = compute_secret_token_helper()",
    )

    for text in (nested, unclosed):
        messages = [
            candidate_core.SourceMessage("en", text, source_line)
            for source_line in range(1, 4)
        ]
        report = candidate_core.build_report(
            messages,
            input_record_count=3,
            config=candidate_core.MiningConfig(),
            rules_by_language={},
        )
        assert report["candidates"] == [], text


def test_ordinary_quoted_prose_is_still_mined():
    """Blockquote handling must not over-protect: quoted prose is normal text."""
    text = _blockquoted(
        "I really think we should go outside today",
        "I really think we should go outside today",
    )
    messages = [
        candidate_core.SourceMessage("en", text, source_line)
        for source_line in range(1, 4)
    ]

    report = candidate_core.build_report(
        messages,
        input_record_count=3,
        config=candidate_core.MiningConfig(),
        rules_by_language={},
    )
    assert any(
        candidate["phrase"] == "go outside today" for candidate in report["candidates"]
    )


def test_only_blockquote_markers_count_as_a_quote_prefix():
    """The prefix must be `>` and nothing else.

    Widening it to any leading run of characters would make a mid-line fence
    look like an opening fence, swallowing the surrounding prose into a
    protected span and silently dropping real candidates.
    """
    text = "we always say the exact same thing and then ```code``` follows"
    messages = [
        candidate_core.SourceMessage("en", text, source_line)
        for source_line in range(1, 4)
    ]

    assert candidate_core._strip_blockquote_prefix(text) == text
    assert candidate_core._fenced_code_spans(text) == []
    report = candidate_core.build_report(
        messages,
        input_record_count=3,
        config=candidate_core.MiningConfig(),
        rules_by_language={},
    )
    assert any(
        candidate["phrase"] == "exact same thing"
        for candidate in report["candidates"]
    )


def _mined(text: str) -> list[str]:
    messages = [
        candidate_core.SourceMessage("en", text, source_line)
        for source_line in range(1, 4)
    ]
    report = candidate_core.build_report(
        messages,
        input_record_count=3,
        config=candidate_core.MiningConfig(),
        rules_by_language={},
    )
    return [candidate["phrase"] for candidate in report["candidates"]]


def test_blockquoted_indented_code_stays_protected():
    """The blockquote container is not part of a code block's indentation.

    `>     code` measured zero indent columns, so a quoted indented block was
    mined as prose while the unquoted form was protected.
    """
    quoted = "\n".join(
        [
            "> text before",
            ">",
            ">     secret_token_helper = compute()",
            ">",
            "> text after",
        ]
    )
    nested = "\n".join([">> a b c d", ">>", ">>     secret_token_helper = compute()"])

    for text in (quoted, nested):
        assert not [
            phrase for phrase in _mined(text) if "secret" in phrase or "helper" in phrase
        ], text
    # Prose around the block is still mined; the fix must not swallow the quote.
    assert "text before" in _mined(quoted)


def test_html_code_elements_protect_their_contents():
    """`<pre>` / `<code>` mark code as explicitly as a Markdown fence does.

    The generic `<...>` template pattern protected only the TAGS, leaving the
    code between them mineable and exportable.
    """
    for text in (
        "she said <code>secret token helper</code> again and again",
        "<pre><code>secret_token_helper = compute()</code></pre>",
        "she said <CODE>secret token helper</CODE> again and again",
    ):
        assert not [
            phrase for phrase in _mined(text) if "secret" in phrase or "helper" in phrase
        ], text


def test_unclosed_html_code_container_protects_to_end_of_text():
    """A reply truncated mid-code-block must not leak its body.

    Requiring a closing tag left the body of an unmatched container exposed all
    the way into persistence: `build_repeat_signature` returned the code
    identifier verbatim, which `anti_repeat_effects.json` would then store —
    against that module's stated no-code boundary. Unclosed fences already
    protect to end-of-text; the HTML containers now match that.
    """
    from memory.anti_repeat_effects import build_repeat_signature

    for text in (
        "she said <code>secret_token_helper = compute()",
        "she said <pre>secret_token_helper = compute()",
        "<code>aaa</code> middle text <code>secret_token_helper",
    ):
        assert not [
            phrase for phrase in _mined(text) if "secret" in phrase or "helper" in phrase
        ], text
        assert (
            build_repeat_signature(
                text, ["secret_token_helper"], language="en"
            )
            is None
        ), text


def test_html_code_protection_does_not_swallow_prose():
    for text in (
        "we always say the exact same thing",
        "if a < b and b > c we always say the exact same thing",
        "we always decode the exact same thing and encode it",
    ):
        assert any("always" in phrase for phrase in _mined(text)), text


def test_script_and_style_bodies_are_protected():
    """`<script>` / `<style>` are raw-text elements; their bodies are code."""
    for text in (
        "<script>secret_token_helper = compute()</script>",
        "<style>.secret_token_helper{color:red}</style>",
        "<script>secret_token_helper = compute()",
    ):
        assert not [
            phrase for phrase in _mined(text) if "secret" in phrase or "helper" in phrase
        ], text


def test_raw_text_containers_do_not_cross_match():
    """The closing tag is a backreference, so `<pre>` cannot close a `<code>`."""
    text = "<pre>alpha</pre> we always say the exact same thing <code>beta</code>"

    spans = candidate_core._protected_spans(text)

    assert len(spans) == 2
    assert any("always" in phrase for phrase in _mined(text))


def test_a_single_oversized_reply_is_truncated_to_the_character_budget():
    """Dropping whole messages floors at one, so the last one must be trimmed.

    Otherwise a reply larger than the advertised limit is mined in full and the
    report claims a budget it never enforced.
    """
    oversized = "alpha beta gamma delta. " * 9000
    assert len(oversized) > candidate_core.USER_REVIEW_MAX_INPUT_CHARACTERS

    summary = candidate_core.build_user_review_report(
        [candidate_core.SourceMessage("en", oversized, 1)], rules_by_language={}
    )["summary"]

    assert summary["analyzed_message_count"] == 1
    # Dropping messages did NOT happen; the single body was clipped.
    assert summary["messages_truncated"] is False
    assert summary["content_truncated"] is True


def test_a_quote_marker_inside_an_unquoted_fence_does_not_close_it():
    """CommonMark: a fence opened at depth 0 is not closed by a deeper line.

    Stripping the blockquote prefix unconditionally made "> ```" close a fence
    opened outside any quote, exposing the rest of the block — strictly worse
    than before blockquote handling existed. This is the regression that shipped
    because the blockquote tests only covered fully-quoted fences.
    """
    from memory.anti_repeat_effects import build_repeat_signature

    text = (
        "hello there\n```\nAPI_TOKEN = 'zqxjleak'\n> ```\n"
        "DB_PASSWORD = 'zqxjleak2'\n"
    )

    spans = candidate_core._protected_spans(text)
    unprotected = "".join(
        char
        for index, char in enumerate(text)
        if not any(start <= index < end for start, end in spans)
    )

    assert "DB_PASSWORD" not in unprotected
    assert "API_TOKEN" not in unprotected
    assert unprotected.strip() == "hello there"
    assert build_repeat_signature(text, ["DB_PASSWORD"], language="en") is None
    assert not [
        phrase for phrase in _mined(text) if "PASSWORD" in phrase or "TOKEN" in phrase
    ]


def test_an_unquoted_marker_does_not_close_a_quoted_fence():
    """A shallower marker is not a closer either, and this test used to say it was.

    It originally asserted that the trailing unquoted marker closes the quoted
    fence and that the prose after it is mineable -- pinning a leak as correct.
    Per CommonMark, leaving the blockquote implicitly ends the inner fence and
    the unquoted marker opens a NEW outer fence, so everything after it is still
    code.
    """
    from memory.anti_repeat_effects import build_repeat_signature

    text = _lines("> ```", "> API_TOKEN = 'zqxjleak'", "```", "DB_PASSWORD = 'zqxjleak2'")

    unprotected = _unprotected(text)

    assert "API_TOKEN" not in unprotected
    assert "DB_PASSWORD" not in unprotected
    assert build_repeat_signature(text, ["DB_PASSWORD"], language="en") is None


def test_an_equal_depth_marker_closes_the_fence_it_opened():
    """Depth-matched closers still work, at depth 0 and nested."""
    for text in (
        _lines("```", "API_TOKEN = 'zqxjleak'", "```", "we always say the same thing"),
        _lines("> ```", "> API_TOKEN = 'zqxjleak'", "> ```", "> we always say the same thing"),
        _lines(">> ```", ">> API_TOKEN = 'zqxjleak'", ">> ```", ">> we always say the same thing"),
    ):
        unprotected = _unprotected(text)
        assert "API_TOKEN" not in unprotected, text
        assert "we always say the same thing" in unprotected, text


def _lines(*rows: str, eol: str = "\n") -> str:
    return eol.join(rows) + eol


def _unprotected(text: str) -> str:
    spans = candidate_core._protected_spans(text)
    return "".join(
        char
        for index, char in enumerate(text)
        if not any(start <= index < end for start, end in spans)
    )


# Fence x blockquote-depth matrix. Written as a table because every regression in
# this helper so far came from testing SOME shapes and assuming the rest: a quote
# marker inside an unquoted fence, an unquoted marker after a quoted one, and a
# depth-matched marker arriving after that. `mined` lists the fragments that MUST
# still be analyzable, so the table pins over-protection as well as leaks.
_FENCE_MATRIX = [
    ("open0 closed at 0", ["```", "SECRET=1", "```", "tail"], ["tail"]),
    ("open1 closed at 1", ["> ```", "> SECRET=1", "> ```", "> tail"], ["tail"]),
    ("open2 closed at 2", [">> ```", ">> SECRET=1", ">> ```", ">> tail"], ["tail"]),
    ("open0, deeper marker is content", ["```", "SECRET=1", "> ```", "SECRET=2"], []),
    ("open1, shallower marker reopens", ["> ```", "> SECRET=1", "```", "SECRET=2"], []),
    (
        "open1, shallower then depth-matched marker",
        ["> ```", "> SECRET=1", "```", "SECRET=2", "> ```", "SECRET=3"],
        [],
    ),
    (
        "open1, shallower then closed at 0",
        ["> ```", "> SECRET=1", "```", "SECRET=2", "```", "tail"],
        ["tail"],
    ),
    (
        "open2, shallower then closed at 1",
        [">> ```", ">> SECRET=1", "> ```", "> SECRET=2", "> ```", "tail"],
        ["tail"],
    ),
    ("open0 unclosed", ["```", "SECRET=1"], []),
    ("open1 unclosed", ["> ```", "> SECRET=1"], []),
    ("open1, deeper marker is content", ["> ```", "> SECRET=1", ">> ```", "> SECRET=2"], []),
    ("tilde fence", ["~~~", "SECRET=1", "~~~", "tail"], ["tail"]),
    ("longer closer closes", ["```", "SECRET=1", "`````", "tail"], ["tail"]),
    ("shorter closer is content", ["`````", "SECRET=1", "```", "SECRET=2"], []),
    ("no fence at all", ["SAFE=1", "SAFE=2"], ["SAFE=1", "SAFE=2"]),
    # A list item is a container too: a fence written straight after its marker
    # was invisible to the parser, and stayed unprotected wherever the inline
    # scanner did not happen to cover it.
    # Continuation lines are INDENTED under the marker, which is the only
    # legal form. Re-marking every line ("- SECRET=1") is not a list fence at
    # all, and treating a re-marked line as a closer is the bug the
    # list-marker-as-content cases below pin down.
    ("bullet list fence", ["- ```", "  SECRET=1", "  ```", "tail"], ["tail"]),
    (
        "numbered list fence",
        ["1. ```", "   SECRET=1", "   ```", "tail"],
        ["tail"],
    ),
    ("list fence, unclosed", ["- ```", "  SECRET=1"], []),
    (
        "list fence, blank line inside",
        ["- ```", "  a=1", "", "  SECRET=1", "  ```"],
        [],
    ),
    ("bullet prose is not a fence", ["- SAFE=1 here", "- SAFE=1 here"], ["SAFE=1"]),
]


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "label, rows, must_remain_visible",
    _FENCE_MATRIX,
    ids=[row[0] for row in _FENCE_MATRIX],
)
def test_fence_blockquote_depth_matrix(label, rows, must_remain_visible, eol):
    """Line endings are a matrix dimension: persisted replies carry whatever the
    model emitted, and an LF-only assumption silently changes the verdict."""
    text = _lines(*rows, eol=eol)

    unprotected = _unprotected(text)

    assert "SECRET" not in unprotected, label
    for fragment in must_remain_visible:
        assert fragment in unprotected, label


# Inline code span matrix. `secret_visible` is the EXPECTED outcome, so the
# table pins both directions: a real code span must be protected, and text that
# is genuinely prose per CommonMark must stay mineable.
_INLINE_CODE_MATRIX = [
    ("single-line span", "she said `SECRET=1` again", False),
    ("multi-line span", "she said `a =\nSECRET=1` again", False),
    ("multi-line double backtick", "she said ``a =\nSECRET=1`` again", False),
    ("unterminated on one line", "she said `SECRET=1", False),
    # No closing delimiter: the backtick is literal text, so the following
    # lines really are prose and must not be swallowed.
    ("unterminated across lines", "she said `a =\nSECRET=1\nmore prose", True),
    # A code span cannot cross a blank line, so this is prose too.
    ("run interrupted by a blank line", "she said `a =\n\nSECRET=1 is prose", True),
    # A delimiter AFTER a blank line is a different paragraph and must not
    # be treated as this run's closer -- otherwise the search swallows the
    # prose in between. Pins the over-reach direction.
    (
        "closer only after a blank line",
        "she said `a =\n\nSECRET=1 is prose and `x` here",
        True,
    ),
    ("two spans on one line", "`a` prose here `SECRET=1`", False),
    ("backticks inside a fence", "```\n`SECRET=1`\n```\ntail", False),
]


@pytest.mark.parametrize("tick", ["`", "｀"], ids=["ascii", "fullwidth"])
@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "label, text, secret_visible",
    _INLINE_CODE_MATRIX,
    ids=[row[0] for row in _INLINE_CODE_MATRIX],
)
def test_inline_code_span_matrix(label, text, secret_visible, eol, tick):
    """Line endings AND delimiter style are matrix dimensions.

    An LF-only blank-line pattern skips a CRLF blank line, so the paragraph runs
    to end of text and a later delimiter is mistaken for the closer, swallowing
    prose that should have produced candidates. A CJK input method produces the
    fullwidth grave accent, which Markdown does not treat as code but which
    still wraps real code.
    """
    from memory.anti_repeat_effects import build_repeat_signature

    text = text.replace("\n", eol).replace("`", tick)
    unprotected = _unprotected(text)

    assert ("SECRET" in unprotected) is secret_visible, label
    signature = build_repeat_signature(text, ["SECRET"], language="en")
    assert (signature is not None) is secret_visible, label


def test_inline_code_protection_still_leaves_prose_minable():
    text = "we always say the exact same thing"

    assert "always" in _unprotected(text)


# Raw-text HTML containers. Nesting is the dimension the single non-greedy
# pattern could not express: it stopped at the FIRST closing tag and leaked the
# rest of the outer element.
_HTML_CONTAINER_MATRIX = [
    ("plain code element", "<code>SECRET=1</code> tail", ["tail"]),
    ("nested same tag", "<code>a <code>b</code> SECRET=1</code> tail", ["tail"]),
    ("nested pre", "<pre>a <pre>b</pre> SECRET=1</pre> tail", ["tail"]),
    (
        "triple nesting",
        "<code>1<code>2<code>3</code>4</code> SECRET=1</code> tail",
        ["tail"],
    ),
    (
        "two sibling containers",
        "<code>SECRET=1</code> mid <code>SECRET=2</code> tail",
        ["mid", "tail"],
    ),
    ("unterminated container", "she said <code>SECRET=1", []),
    ("script body", "<script>SECRET=1</script> tail", ["tail"]),
    ("style body", "<style>.SECRET=1{}</style> tail", ["tail"]),
    ("prose with a comparison", "we always say a < b and b > c", ["always"]),
]


@pytest.mark.parametrize(
    "label, text, must_remain_visible",
    _HTML_CONTAINER_MATRIX,
    ids=[row[0] for row in _HTML_CONTAINER_MATRIX],
)
def test_html_raw_text_container_matrix(label, text, must_remain_visible):
    unprotected = _unprotected(text)

    assert "SECRET" not in unprotected, label
    for fragment in must_remain_visible:
        assert fragment in unprotected, label


# Fullwidth tilde is the commonest elongation mark in this project's character
# speech. Treating it as an inline code delimiter protected the text between two
# of them and silently dropped real catchphrases -- the exact thing this feature
# exists to surface. Inline delimiters are backticks only; fences keep the tilde
# forms, which need a run of three at the start of a line.
_SPEECH_ELONGATION_CASES = [
    ("japanese", "そうですね～また明日ね～", "また明日ね"),
    ("chinese", "好呀～我们一起去吧～", "我们一起去吧"),
    ("repeated tildes", "はい～～ありがとう～～", "ありがとう"),
    ("ascii tilde prose", "we always say ~the exact same thing~ here", "always"),
]


@pytest.mark.parametrize(
    "label, text, must_remain_visible",
    _SPEECH_ELONGATION_CASES,
    ids=[row[0] for row in _SPEECH_ELONGATION_CASES],
)
def test_tilde_elongation_is_speech_not_code(label, text, must_remain_visible):
    assert must_remain_visible in _unprotected(text), label


_FULLWIDTH_CODE_CASES = [
    ("ascii inline", "a `SECRET=1` b"),
    ("fullwidth tick inline", "a ｀SECRET=1｀ b"),
    ("ascii fence", "```\nSECRET=1\n```\ntail"),
    ("tilde fence", "~~~\nSECRET=1\n~~~\ntail"),
    ("fullwidth tick fence", "｀｀｀\nSECRET=1\n｀｀｀\ntail"),
    ("fullwidth tilde fence", "～～～\nSECRET=1\n～～～\ntail"),
]


@pytest.mark.parametrize(
    "label, text",
    _FULLWIDTH_CODE_CASES,
    ids=[row[0] for row in _FULLWIDTH_CODE_CASES],
)
def test_fullwidth_code_delimiters_still_protect(label, text):
    assert "SECRET" not in _unprotected(text), label


# A list marker may only introduce an OPENING fence. Stripping it on every line
# let a CONTENT line that happens to read "- ```" be rewritten into a bare
# delimiter run, close the active fence early, and expose the rest of the block.
_LIST_MARKER_AS_CONTENT_CASES = [
    ("ascii backtick", "```\n- ```\nDB_PASSWORD = hunter2\n```\ntail"),
    ("ascii tilde", "~~~\n- ~~~\nDB_PASSWORD = hunter2\n~~~\ntail"),
    ("ordered marker", "~~~\n1. ~~~\nDB_PASSWORD = hunter2\n~~~\ntail"),
    ("quoted marker", "~~~\n> - ~~~\nDB_PASSWORD = hunter2\n~~~\ntail"),
    ("fullwidth tick", "｀｀｀\n- ｀｀｀\nDB_PASSWORD = hunter2\n｀｀｀\ntail"),
]


@pytest.mark.parametrize(
    "label, text",
    _LIST_MARKER_AS_CONTENT_CASES,
    ids=[row[0] for row in _LIST_MARKER_AS_CONTENT_CASES],
)
def test_a_list_marker_inside_a_fence_is_content_not_a_closer(label, text):
    assert "DB_PASSWORD" not in _unprotected(text), label


# A template body that merely wrapped was left unprotected while its single-line
# twin was masked. The two-line budget is what keeps that from becoming an
# over-protection bug: an unbounded newline-crossing match turns one stray
# delimiter in prose into a span that swallows the rest of the reply, and
# `<...>` is excluded outright because emoticons pair up across lines.
_MULTILINE_TEMPLATE_CASES = [
    ("jinja", "{{\nsecret helper phrase\n}}"),
    ("shell interpolation", "${\nsecret helper phrase\n}"),
    ("erb scriptlet", "<%\nsecret helper phrase\n%>"),
    ("jinja with a filter", "{{ secret helper phrase\n | default('x') }}"),
    # Opener and closer on their own lines with a two-line body: the shape a
    # hand-written template actually has, and the one a two-newline budget missed.
    ("jinja block", "{{\nalpha\nsecret helper phrase\n}}"),
    ("shell block", "${\nalpha\nsecret helper phrase\n}"),
    ("erb block", "<%\nalpha\nsecret helper phrase\n%>"),
    ("crlf jinja", "{{\r\nsecret helper phrase\r\n}}"),
]


@pytest.mark.parametrize(
    "label, text",
    _MULTILINE_TEMPLATE_CASES,
    ids=[row[0] for row in _MULTILINE_TEMPLATE_CASES],
)
def test_wrapped_template_bodies_are_protected(label, text):
    assert "secret helper phrase" not in _unprotected(text), label


_TEMPLATE_OVER_PROTECTION_CASES = [
    ("paired emoticons", "嘿嘿 >_<\n我们一起去吃饭吧\n晚安晚安 >_<", "我们一起去吃饭吧"),
    ("heart then arrow", "そうだね <3\nまた一緒に散歩しようね\n気分 ->", "また一緒に散歩しようね"),
    # Where the line is drawn, stated explicitly: a stray opener and a stray
    # closer THREE newlines apart are indistinguishable from a real template
    # block, so the budget protects both. Past the budget the blast radius stops
    # growing, which is the property this row exists to pin -- an unbounded
    # pattern swallowed 99.7% of a 200-line reply.
    ("stray brace past the budget", "那个 ${\nA呢\n我们一起去吃饭吧\nB呢\n最后那个括号 }", "我们一起去吃饭吧"),
    ("comparison operators", "记住哦 3 < 5\n我们一起去吃饭吧\n然后 10 > 7", "我们一起去吃饭吧"),
]


@pytest.mark.parametrize(
    "label, text, must_remain_visible",
    _TEMPLATE_OVER_PROTECTION_CASES,
    ids=[row[0] for row in _TEMPLATE_OVER_PROTECTION_CASES],
)
def test_stray_delimiters_in_speech_do_not_swallow_catchphrases(
    label, text, must_remain_visible
):
    assert must_remain_visible in _unprotected(text), label


# Containers alternate. One blockquote pass followed by one list pass only found
# an opener in the orders it happened to be written in, so a fence inside
# `- > ...` was invisible while `> - ...` worked.
_NESTED_CONTAINER_CASES = [
    ("list then quote", ["- > ~~~python", "  > SECRET=1", "  > ~~~", "tail"]),
    ("quote then list", ["> - ~~~python", ">   SECRET=1", ">   ~~~", "tail"]),
    ("list then quote, unclosed", ["- > ~~~python", "  > SECRET=1"]),
    ("two quote levels", ["- > > ~~~", "  > > SECRET=1", "  > > ~~~", "tail"]),
    ("ordered then quote", ["1. > ~~~", "   > SECRET=1", "   > ~~~", "tail"]),
    # Needs more than one strip: list, quote, list, quote. A single pass finds
    # the opener only for the shallowest nesting.
    (
        "alternating twice",
        ["- > - > ~~~", "  >   > SECRET=1", "  >   > ~~~", "tail"],
    ),
]


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "label, rows",
    _NESTED_CONTAINER_CASES,
    ids=[row[0] for row in _NESTED_CONTAINER_CASES],
)
def test_fences_nested_in_alternating_containers_are_protected(label, rows, eol):
    unprotected = _unprotected(_lines(*rows, eol=eol))
    assert "SECRET" not in unprotected, label
    if rows[-1] == "tail":
        # The fence must also CLOSE. Getting the container depth wrong hides
        # the secret too -- by never closing and swallowing the rest of the
        # reply -- so the leak assertion alone cannot tell the two apart.
        assert "tail" in unprotected, label


_NESTED_CONTAINER_SPEECH = [
    ("bulleted speech", ["- 今天也辛苦了呢", "- 我们一起去吃饭吧"]),
    ("quoted speech", ["> 今天也辛苦了呢", "> 我们一起去吃饭吧"]),
    ("bulleted quoted speech", ["- > 今天也辛苦了呢", "- > 我们一起去吃饭吧"]),
]


@pytest.mark.parametrize(
    "label, rows",
    _NESTED_CONTAINER_SPEECH,
    ids=[row[0] for row in _NESTED_CONTAINER_SPEECH],
)
def test_container_markers_alone_do_not_protect_speech(label, rows):
    assert "我们一起去吃饭吧" in _unprotected(_lines(*rows)), label


# A code-span closer must be a run of EXACTLY the opening length; a longer run is
# content. `find` accepted the opening-length prefix of a longer run, so a span
# ended mid-run and, once a second shorter run paired with the leftovers, the
# body after it was mined as prose. Harmless while the search was bounded to one
# line -- the leftovers re-opened and the coverage merged -- but this file now
# searches to the end of the paragraph, which turns it into a real leak that
# also reaches the persisted signature.
_LONGER_RUN_CASES = [
    ("two runs", "run this `echo ```a`` export SECRET_TOKEN` ok"),
    ("two runs, fullwidth", "reply ｀code ｀｀｀x｀｀ SECRET_TOKEN｀ done"),
    ("single longer run", "reply `code ``inner`` SECRET_TOKEN` done"),
    ("opened with two", "reply ``code ```x``` SECRET_TOKEN`` done"),
    ("across lines", "你好 `代码 x\n继续 ```y`` SECRET_TOKEN\n` 完毕"),
]


@pytest.mark.parametrize(
    "label, text",
    _LONGER_RUN_CASES,
    ids=[row[0] for row in _LONGER_RUN_CASES],
)
def test_a_longer_run_inside_a_code_span_is_content(label, text):
    assert "SECRET_TOKEN" not in _unprotected(text), label


_CLOSER_SPEECH_CASES = [
    ("stray single", "好呀我们一起去吧 ` 真开心", "我们一起去吧"),
    ("paired", "好呀`一起去吧`真开心", "真开心"),
    # `我们` is a legitimate span here and is protected, before and after --
    # what must survive is the prose outside it.
    ("two strays", "好呀`我们`一起去吧", "一起去吧"),
    ("english apostrophe-ish", "it's a `great day out there friend", "it's a "),
]


@pytest.mark.parametrize(
    "label, text, must_remain_visible",
    _CLOSER_SPEECH_CASES,
    ids=[row[0] for row in _CLOSER_SPEECH_CASES],
)
def test_the_exact_run_rule_does_not_swallow_speech(label, text, must_remain_visible):
    assert must_remain_visible in _unprotected(text), label


# Three container/target shapes that reached the report AND the persisted
# signature: a Markdown link's relative target, a wrapped HTML comment, and an
# indented code block nested in a list (alone or with a blockquote).
_UNPROTECTED_CONTAINER_CASES = [
    ("relative link target", "see [endpoint](/api/SECRET_TOKEN) here"),
    ("link target with title", 'see [x](/api/SECRET_TOKEN "t") here'),
    ("wrapped html comment", "<!--\nSECRET_TOKEN should never render\n-->"),
    ("comment inside prose", "hello <!--\nSECRET_TOKEN\n--> bye"),
    ("unterminated comment", "hello <!--\nSECRET_TOKEN"),
    ("list then quote, indented", "- >     SECRET_TOKEN = 1\n- >     more = 2"),
    ("quote then list, indented", "> -     SECRET_TOKEN = 1"),
    ("list, indented", "-     SECRET_TOKEN = 1"),
    ("ordered list, indented", "1.     SECRET_TOKEN = 1"),
]


@pytest.mark.parametrize(
    "label, text",
    _UNPROTECTED_CONTAINER_CASES,
    ids=[row[0] for row in _UNPROTECTED_CONTAINER_CASES],
)
def test_link_targets_comments_and_nested_indents_are_protected(label, text):
    unprotected = _unprotected(text)
    assert "SECRET_TOKEN" not in unprotected, label
    if text.endswith(" bye"):
        # The container must also END. Treating it as unterminated hides the
        # secret too -- by swallowing the rest of the reply -- so the leak
        # assertion alone cannot tell a closing scanner from a runaway one.
        assert "bye" in unprotected, label


_CONTAINER_SPEECH_CASES = [
    # Only the link TARGET is protected; the text is prose a character may
    # legitimately repeat, and a bare path in prose stays minable on purpose --
    # protecting every `/foo/bar` would eat dates.
    ("link text is prose", "[我们一起去吃饭吧](/x) 好不好", "我们一起去吃饭吧"),
    ("bare path", "今天是 2024/01/02 我们一起去吃饭吧", "我们一起去吃饭吧"),
    ("bulleted speech", "- 今天也辛苦了呢\n- 我们一起去吃饭吧", "我们一起去吃饭吧"),
    ("padded bullet", "-   我们一起去吃饭吧", "我们一起去吃饭吧"),
    ("quoted bullet", "> - 我们一起去吃饭吧", "我们一起去吃饭吧"),
    ("comparison", "记住 3 < 5\n我们一起去吃饭吧", "我们一起去吃饭吧"),
]


@pytest.mark.parametrize(
    "label, text, must_remain_visible",
    _CONTAINER_SPEECH_CASES,
    ids=[row[0] for row in _CONTAINER_SPEECH_CASES],
)
def test_the_new_containers_do_not_swallow_speech(label, text, must_remain_visible):
    assert must_remain_visible in _unprotected(text), label
