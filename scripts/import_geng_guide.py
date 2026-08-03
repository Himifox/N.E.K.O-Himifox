"""One-shot, idempotent importer for a user-provided 梗指南 Markdown export."""

from __future__ import annotations

import argparse
from pathlib import Path

from knowledge.moegirl_knowledge.sources.geng_guide import load_geng_guide_markdown
from knowledge.moegirl_knowledge.store import MoegirlKnowledgeStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    entries = load_geng_guide_markdown(args.input.read_bytes())
    results = MoegirlKnowledgeStore(args.database).replace_source("source:geng-guide", entries)
    print(
        f"entries={len(entries)} added={sum(item.created for item in results)} "
        f"updated={sum(item.updated for item in results)} unchanged={sum(item.unchanged for item in results)}"
    )


if __name__ == "__main__":
    main()
