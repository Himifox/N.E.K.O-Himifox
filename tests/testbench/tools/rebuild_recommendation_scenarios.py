"""One-shot migration from the synthetic 44-case matrix to a curated v1 golden set.

Candidate IDs are produced by the production builders; human-authored source
acceptance remains the judgement used to assign relevance.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_runner import preview_scenario
from tests.testbench.pipeline.recommendation_scenario import read_scenario


CORE_IDS = [
    "privacy_01", "privacy_04",
    "activity_07", "activity_08", "activity_09", "activity_10",
    "competition_15", "competition_16", "competition_17", "competition_18",
    "diversity_23", "diversity_24",
    "edge_35", "edge_36", "edge_37",
]
MATERIAL_IDS = [
    "material_news_preferred", "material_music_preferred",
    "material_meme_preferred", "material_vision_preferred",
]
ACTIVE_BIAS_EXPECTED = {
    "material_news_preferred": False,
    "material_music_preferred": True,
    "material_meme_preferred": True,
    "material_vision_preferred": False,
}


def enrich(scenario_id: str) -> dict:
    scenario = read_scenario(scenario_id)
    clean = {k: copy.deepcopy(v) for k, v in scenario.items()
             if not k.startswith("_") and k not in {"has_builtin", "has_user", "overriding_builtin"}}
    result = preview_scenario(clean, {"id": "production_default", "active_min_score_gap": 0.05})
    snapshot = result["snapshot"]
    oracle = clean.setdefault("oracle", {})
    acceptable = set(oracle.get("acceptable_top1_sources") or [])
    oracle["relevance"] = {
        row["id"]: (3 if row["source_type"] in acceptable else 1)
        for row in snapshot.get("ranked_candidates") or []
    }
    filtered = snapshot.get("filtered_reasons") or {}
    oracle["must_filter_candidate_ids"] = sorted(filtered)
    oracle["expected_filter_reasons"] = dict(sorted(filtered.items()))
    oracle["expected_empty"] = not snapshot.get("ranked_candidates")
    oracle["active_bias_expected"] = ACTIVE_BIAS_EXPECTED.get(scenario_id)
    clean["description"] = f"Curated golden case: {scenario_id}; one controlled input combination."
    return clean


def main() -> None:
    core = [enrich(sid) for sid in CORE_IDS]
    material = [enrich(sid) for sid in MATERIAL_IDS]
    root = Path(__file__).resolve().parents[1] / "recommendation_scenarios"
    atomic_write_json(root / "builtin_core_matrix.json", core)
    atomic_write_json(root / "builtin_material_matrix.json", material)
    print(f"rebuilt {len(core) + len(material)} curated recommendation scenarios")


if __name__ == "__main__":
    main()
