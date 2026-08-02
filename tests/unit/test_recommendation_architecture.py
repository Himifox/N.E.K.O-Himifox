import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "main_logic" / "proactive_recommendation"
REMOVED_MODULES = {
    "proactive_recommendation_bandit.py",
    "proactive_recommendation_bandit_state.py",
    "proactive_recommendation_feedback.py",
    "proactive_recommendation_feedback_state.py",
    "proactive_recommendation_observer.py",
    "proactive_recommendation_personalization.py",
    "proactive_recommendation_preference.py",
    "proactive_recommendation_runtime.py",
    "proactive_recommendation_timing.py",
    "proactive_recommendation_tuning.py",
}


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_flat_recommendation_modules_are_removed():
    main_logic = ROOT / "main_logic"
    assert not (main_logic / "proactive_recommendation.py").exists()
    assert not REMOVED_MODULES.intersection(path.name for path in main_logic.glob("*.py"))


def test_no_source_imports_removed_flat_namespace():
    forbidden_prefix = "main_logic.proactive_recommendation_"
    offenders = []
    for source_root in (ROOT / "main_logic", ROOT / "main_routers", ROOT / "tests"):
        for path in source_root.glob("**/*.py"):
            if any(name.startswith(forbidden_prefix) for name in _imports(path)):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_feedback_does_not_import_tuning():
    offenders = []
    for path in (PACKAGE / "feedback").glob("*.py"):
        if any("proactive_recommendation.tuning" in name for name in _imports(path)):
            offenders.append(path.name)
    assert offenders == []


def test_engine_is_pure_with_respect_to_storage_and_state():
    forbidden = (
        "proactive_recommendation.storage",
        "proactive_recommendation.state",
        "main_routers",
    )
    offenders = []
    for path in (PACKAGE / "engine").glob("*.py"):
        if any(any(token in name for token in forbidden) for name in _imports(path)):
            offenders.append(path.name)
    assert offenders == []
