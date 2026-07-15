"""Export sanitized production Shadow logs into a Testbench import bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main_logic.proactive_recommendation_feedback import FEEDBACK_LOG_FILENAME, load_recommendation_feedback_jsonl
from main_logic.proactive_recommendation_observer import OBSERVATION_LOG_FILENAME, load_recommendation_observations_jsonl
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_shadow import audit_shadow_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Output JSON path (default: Testbench exports directory)")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--config-dir", type=Path, help="Explicit production config directory")
    args = parser.parse_args()
    if args.config_dir:
        config_dir = args.config_dir.resolve()
    else:
        try:
            from utils.config_manager import get_config_manager
            config_dir = Path(get_config_manager(migrate=False).config_dir)
        except Exception as exc:
            parser.error(f"cannot resolve config directory ({type(exc).__name__}); pass --config-dir")
    observation_path = config_dir / OBSERVATION_LOG_FILENAME
    feedback_path = config_dir / FEEDBACK_LOG_FILENAME
    observations = load_recommendation_observations_jsonl(observation_path, limit=max(1, args.limit)) if observation_path.exists() else []
    feedback = load_recommendation_feedback_jsonl(feedback_path, limit=max(1, args.limit * 4)) if feedback_path.exists() else []
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {"schema_version": 1, "name": f"production-shadow-{created_at[:10]}", "kind": "shadow_replay",
               "created_at": created_at, "observations": observations, "feedback": feedback,
               "source": {"config_dir": str(config_dir), "observation_filename": OBSERVATION_LOG_FILENAME,
                          "feedback_filename": FEEDBACK_LOG_FILENAME},
               "quality_preview": audit_shadow_dataset({"observations": observations, "feedback": feedback})}
    output = args.output or (PROJECT_ROOT / "tests" / "testbench_data" / "recommendation" / "exports" /
                             f"shadow-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    output = output.resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output, payload)
    print(json.dumps({"output": str(output), "config_dir": str(config_dir),
                      "observation_log_exists": observation_path.exists(), "feedback_log_exists": feedback_path.exists(),
                      "quality": payload["quality_preview"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
