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
BANNED_FILENAMES = {
    "common.py",
    "utils.py",
    "helpers.py",
    "misc.py",
    "model.py",
    "policy.py",
    "events.py",
    "reports.py",
}
BANNED_HELPER_NAMES = {
    "_clamp01",
    "_unit",
    "_number",
    "_finite",
    "_text",
    "_clean_text",
    "_count",
    "_rate",
    "_average",
    "_mode",
    "_delta",
    "_snapshot",
    "_public_state",
}
REMOVED_PACKAGE_PATHS = {
    "application.py",
    "history.py",
    "runtime.py",
    "timing.py",
    "turn.py",
    "engine/active_bias.py",
    "engine/candidates.py",
    "engine/decisions.py",
    "feedback/contracts.py",
    "feedback/events.py",
    "feedback/pending.py",
    "feedback/reports.py",
    "feedback/rewards.py",
    "feedback/store.py",
    "feedback/text_signals.py",
    "observation/builder.py",
    "observation/reports.py",
    "observation/review.py",
    "observation/schema.py",
    "observation/store.py",
    "policy/__init__.py",
    "policy/bandit.py",
    "policy/personalization.py",
    "state/bandit.py",
    "state/feedback.py",
    "state/preference.py",
    "tuning/model.py",
    "tuning/policy.py",
    "tuning/store.py",
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
    assert not REMOVED_MODULES.intersection(
        path.name for path in main_logic.glob("*.py")
    )


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
        "proactive_recommendation.persistence",
        "proactive_recommendation.state",
        "main_routers",
    )
    offenders = []
    for path in (PACKAGE / "engine").glob("*.py"):
        if any(any(token in name for token in forbidden) for name in _imports(path)):
            offenders.append(path.name)
    assert offenders == []


def test_recommendation_directories_remain_compact_and_well_named():
    oversized = {}
    banned = []
    for directory in [PACKAGE, *(path for path in PACKAGE.rglob("*") if path.is_dir())]:
        modules = sorted(directory.glob("*.py"))
        if len(modules) > 5:
            oversized[directory.relative_to(PACKAGE).as_posix() or "."] = [
                path.name for path in modules
            ]
        banned.extend(
            path.relative_to(PACKAGE).as_posix()
            for path in modules
            if path.name in BANNED_FILENAMES
        )
    assert oversized == {}
    assert banned == []


def test_ambiguous_helpers_and_ctx_parameters_are_absent():
    banned_helpers = []
    ctx_parameters = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            location = f"{path.relative_to(PACKAGE).as_posix()}:{node.lineno}"
            if node.name in BANNED_HELPER_NAMES:
                banned_helpers.append(f"{location}:{node.name}")
            parameters = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if any(parameter.arg == "ctx" for parameter in parameters):
                ctx_parameters.append(f"{location}:{node.name}")
    assert banned_helpers == []
    assert ctx_parameters == []


def test_state_and_tuning_use_shared_atomic_persistence():
    forbidden_calls = {"read_text", "write_text", "replace"}
    offenders = []
    for subsystem in ("state", "tuning"):
        for path in (PACKAGE / subsystem).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in forbidden_calls:
                        offenders.append(
                            f"{path.relative_to(PACKAGE).as_posix()}:{node.lineno}"
                        )
    assert offenders == []


def test_internal_modules_do_not_import_package_root():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path == PACKAGE / "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "main_logic.proactive_recommendation"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(PACKAGE).as_posix())
    assert offenders == []


def test_removed_package_paths_do_not_return():
    assert (
        sorted(
            relative_path
            for relative_path in REMOVED_PACKAGE_PATHS
            if (PACKAGE / relative_path).exists()
        )
        == []
    )
