"""Create a conservative assistant proposal copy of a P44-F2-R0 manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_bytes, atomic_write_json
from tests.testbench.pipeline.recommendation_timing_annotation import (
    validate_timing_annotation_manifest,
)
from tests.testbench.pipeline.recommendation_timing_annotation_draft import (
    build_timing_annotation_assistant_draft,
)


def _markdown(draft: dict[str, object]) -> str:
    lines = [
        "# P44-F2-R0 助手预标注盲审清单",
        "",
        "本清单不含 production score、投递结果、反馈或 timing。助手建议不是正式主审；",
        "请仅按当前可见候选资料确认、修改或保留弃权。",
        "",
    ]
    for index, item in enumerate(draft.get("items") or [], start=1):
        context = item["context_for_blind_review"]
        proposal = item["assistant_pre_annotation"]
        interruptibility = dict(proposal.get("interruptibility") or {})
        proposed = "abstain" if proposal["status"] == "abstained" else str(proposal["should_recommend"]).lower()
        relevance = dict(proposal.get("candidate_relevance") or {})
        preferred = set(proposal.get("preferred_candidate_ids") or [])
        repeats = dict(proposal.get("candidate_repeat_evidence") or {})
        lines.extend([
            f"## {index:03d} · `{item['turn_id']}`",
            "",
            f"- Activity: `{context['activity_state']}`",
            (
                "- Interruptibility: "
                f"`{interruptibility.get('level', 'unknown')}` "
                f"(vision semantic `{interruptibility.get('vision_semantic_relevance', '?')}`, "
                f"competition `{interruptibility.get('vision_competition_score', '?')}`; "
                f"{interruptibility.get('reason', 'unknown')})"
            ),
            f"- Assistant proposal: `{proposed}` · confidence `{proposal['confidence']}` · `{proposal['reason_code']}`",
            "- Candidates:",
        ])
        episode = dict(proposal.get("review_context_episode") or {})
        if episode:
            lines.append(
                "- Vision review context: "
                f"occurrence `{episode.get('vision_occurrence')}`; "
                f"new evidence `{episode.get('new_evidence')}`"
            )
        for candidate in context["candidates"]:
            candidate_id = str(candidate.get("id") or "")
            title = str(candidate.get("safe_title") or "").replace("\n", " ")
            summary = str(candidate.get("safe_summary") or "").replace("\n", " ")
            detail = f" — {summary}" if summary and summary != title else ""
            badge = " · preferred" if candidate_id in preferred else ""
            repeat = repeats.get(candidate_id) or {}
            if repeat.get("occurrence") is None:
                repeat = {}
            repeat_badge = "" if not repeat else f" · repeat #{repeat['occurrence']} ({repeat['penalty']})"
            lines.append(f"  - `{candidate['source_type']}` · relevance `{relevance.get(candidate_id, 0)}`{badge}{repeat_badge}: {title}{detail}")
        pre_decision = dict(context.get("pre_decision_context") or {})
        messages = list(pre_decision.get("messages") or [])
        if messages:
            lines.append("- Pre-decision dialogue (timestamps omitted):")
            for message in messages:
                role = str(message.get("role") or "other")
                content = str(message.get("content") or "").replace("\n", " ")
                lines.append(f"  - `{role}`: {content}")
        notes = list(context.get("redaction_notes") or [])
        if notes:
            lines.append(f"- Redaction notes: {', '.join(str(note) for note in notes)}")
        lines.append("- Human confirmation: `true` / `false` / `abstain` (record in `primary_review`)\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review-output", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.resolve().read_text(encoding="utf-8"))
    errors = validate_timing_annotation_manifest(manifest)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    draft = build_timing_annotation_assistant_draft(manifest)
    atomic_write_json(args.output.resolve(), draft)
    if args.review_output:
        atomic_write_bytes(args.review_output.resolve(), _markdown(draft).encode("utf-8"))
    print(json.dumps(draft["assistant_pre_annotation_provenance"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
