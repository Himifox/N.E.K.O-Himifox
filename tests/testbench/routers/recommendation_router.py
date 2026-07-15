"""Session-independent API for deterministic recommendation experiments."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from tests.testbench import config as tb_config
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.recommendation_adapter import run_calibration
from tests.testbench.pipeline.recommendation_export import build_report_json, build_report_markdown
from tests.testbench.pipeline.recommendation_runner import RecommendationRunError, check_reproducibility, delete_run, list_runs, preview_scenario, read_run, run_experiment
from tests.testbench.pipeline.recommendation_scenario import RecommendationScenarioError, delete_user_scenario, duplicate_scenario, list_scenarios, read_scenario, save_user_scenario, validate_scenario_dict
from tests.testbench.pipeline.recommendation_personalization import PersonalizationTraceError, run_personalization_trace
from tests.testbench.pipeline.recommendation_shadow import annotation_summary, audit_shadow_dataset, p44_readiness, validate_annotations
from tests.testbench.pipeline.recommendation_coverage import build_coverage_report
from tests.testbench.pipeline.recommendation_baseline import BaselineSignoffError, list_baselines, read_baseline, signoff_canonical_baseline, validate_known_regression

router = APIRouter(prefix="/api/recommendation-testbench", tags=["recommendation-testbench"])
MAX_IMPORT_RECORDS = 1000

class ScenarioBody(BaseModel): scenario: dict[str, Any]
class DuplicateBody(BaseModel):
    source_id: str; target_id: str; overwrite: bool = False
class PreviewBody(BaseModel):
    scenario_id: str | None = None; scenario: dict[str, Any] | None = None
    variant: dict[str, Any] = Field(default_factory=lambda: {"id": "production_default"})
class RunBody(BaseModel):
    name: str = "recommendation experiment"; scenario_filter: dict[str, Any] = Field(default_factory=dict)
    suite_mode: str = "canonical_builtin"; baseline_variant: str | None = None; variants: list[dict[str, Any]]
class ImportBody(BaseModel):
    name: str = "shadow replay"; observations: list[dict[str, Any]] = Field(default_factory=list); feedback: list[dict[str, Any]] = Field(default_factory=list)
class CalibrationBody(BaseModel):
    dataset_id: str; variant: dict[str, Any] = Field(default_factory=dict)
class PersonalizationTraceBody(BaseModel):
    scenario_id: str = "competition_15"; users: list[dict[str, Any]]
class AnnotationsBody(BaseModel): annotations: list[dict[str, Any]]
class BaselineSignoffBody(BaseModel):
    baseline_id: str = "canonical-production-default-v1"; overwrite: bool = False

def _http(exc):
    detail = {"error_type": exc.code, "message": exc.message}
    if getattr(exc, "errors", None): detail["errors"] = exc.errors
    return HTTPException(exc.status, detail=detail)

@router.get("/scenarios")
def scenarios_list(): return {"scenarios": list_scenarios()}
@router.get("/coverage")
def coverage(): return build_coverage_report()
@router.post("/baselines/signoff")
def baselines_signoff(body: BaselineSignoffBody):
    try: return signoff_canonical_baseline(body.baseline_id, overwrite=body.overwrite)
    except BaselineSignoffError as exc:
        raise HTTPException(422, detail={"error_type": "RecommendationBaselineSignoffFailed", "message": str(exc)}) from exc
@router.get("/baselines")
def baselines_list(): return {"baselines": list_baselines()}
@router.get("/baselines/{baseline_id}")
def baselines_read(baseline_id: str):
    try: return read_baseline(baseline_id)
    except BaselineSignoffError as exc:
        raise HTTPException(404, detail={"error_type": "RecommendationBaselineNotFound", "message": str(exc)}) from exc
@router.post("/baselines/{baseline_id}/known-regression-check")
def baselines_known_regression(baseline_id: str):
    try: return validate_known_regression(baseline_id)
    except BaselineSignoffError as exc:
        raise HTTPException(422, detail={"error_type": "RecommendationKnownRegressionFailed", "message": str(exc)}) from exc
@router.get("/scenarios/{scenario_id}")
def scenarios_read(scenario_id: str):
    try: return read_scenario(scenario_id)
    except RecommendationScenarioError as exc: raise _http(exc) from exc
@router.post("/scenarios/validate")
def scenarios_validate(body: ScenarioBody): return validate_scenario_dict(body.scenario)
@router.post("/scenarios")
def scenarios_save(body: ScenarioBody):
    try: return save_user_scenario(body.scenario)
    except RecommendationScenarioError as exc: raise _http(exc) from exc
@router.delete("/scenarios/{scenario_id}")
def scenarios_delete(scenario_id: str):
    try: return delete_user_scenario(scenario_id)
    except RecommendationScenarioError as exc: raise _http(exc) from exc
@router.post("/scenarios/duplicate")
def scenarios_duplicate(body: DuplicateBody):
    try: return duplicate_scenario(body.source_id, body.target_id, body.overwrite)
    except RecommendationScenarioError as exc: raise _http(exc) from exc
@router.post("/preview")
def preview(body: PreviewBody):
    try:
        scenario = body.scenario or read_scenario(str(body.scenario_id or ""))
        valid = validate_scenario_dict(scenario)
        if not valid["ok"]: raise RecommendationScenarioError("RecommendationScenarioInvalid", "scenario validation failed", 422, valid["errors"])
        return preview_scenario(valid["normalized"], body.variant)
    except (RecommendationScenarioError, RecommendationRunError) as exc: raise _http(exc) from exc

@router.post("/runs")
def runs_create(body: RunBody):
    try:
        data = body.model_dump()
        if not data.get("baseline_variant") and data["variants"]: data["baseline_variant"] = data["variants"][0].get("id")
        return run_experiment(data)
    except RecommendationRunError as exc: raise _http(exc) from exc
@router.post("/runs/reproducibility-check")
def runs_reproducibility(body: RunBody):
    try:
        data = body.model_dump()
        if not data.get("baseline_variant") and data["variants"]: data["baseline_variant"] = data["variants"][0].get("id")
        return check_reproducibility(data)
    except RecommendationRunError as exc: raise _http(exc) from exc
@router.get("/runs")
def runs_list(): return {"runs": list_runs()}
@router.get("/runs/{run_id}")
def runs_read(run_id: str):
    try: return read_run(run_id)
    except RecommendationRunError as exc: raise _http(exc) from exc
@router.delete("/runs/{run_id}")
def runs_delete(run_id: str):
    try: return delete_run(run_id)
    except RecommendationRunError as exc: raise _http(exc) from exc
@router.get("/runs/{run_id}/export")
@router.post("/runs/{run_id}/export")
def runs_export(run_id: str, format: str = "markdown"):
    try: run = read_run(run_id)
    except RecommendationRunError as exc: raise _http(exc) from exc
    if format == "json": content, media, suffix = build_report_json(run), "application/json", "json"
    elif format in {"markdown", "md"}: content, media, suffix = build_report_markdown(run), "text/markdown", "md"
    else: raise HTTPException(422, detail={"error_type": "RecommendationExportInvalid", "message": "format must be markdown or json"})
    tb_config.RECOMMENDATION_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = tb_config.RECOMMENDATION_EXPORTS_DIR / f"recommendation-{run_id}.{suffix}"
    target.write_text(content, encoding="utf-8")
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{target.name}"'})

def _dataset_path(dataset_id): return tb_config.RECOMMENDATION_DATASETS_DIR / f"{dataset_id}.json"
def _dataset_summary(row):
    return {"id": row.get("id"), "name": row.get("name"), "kind": row.get("kind"), "created_at": row.get("created_at"),
            "observation_count": len(row.get("observations") or []), "feedback_count": len(row.get("feedback") or []),
            "import_summary": row.get("import_summary"), "quality": audit_shadow_dataset(row)}
@router.post("/datasets/import")
def datasets_import(body: ImportBody):
    if len(body.observations) + len(body.feedback) > MAX_IMPORT_RECORDS: raise HTTPException(422, detail={"error_type": "RecommendationDatasetLimit", "message": "maximum 1000 records"})
    from main_logic.proactive_recommendation_observer import sanitize_recommendation_observation
    from main_logic.proactive_recommendation_feedback import has_forbidden_feedback_fields, sanitize_recommendation_feedback_event
    observations = [sanitize_recommendation_observation(row) for row in body.observations]
    feedback, rejected = [], []
    for index, row in enumerate(body.feedback):
        if has_forbidden_feedback_fields(row): rejected.append({"kind": "feedback", "index": index, "reason": "forbidden_sensitive_fields"})
        else: feedback.append(sanitize_recommendation_feedback_event(row))
    did = uuid4().hex
    payload = {"schema_version": 1, "id": did, "kind": "shadow_replay", "name": body.name, "created_at": datetime.now(timezone.utc).isoformat(),
               "observations": observations, "feedback": feedback, "import_summary": {"accepted": len(observations)+len(feedback), "rejected": len(rejected), "rejections": rejected}}
    tb_config.RECOMMENDATION_DATASETS_DIR.mkdir(parents=True, exist_ok=True); atomic_write_json(_dataset_path(did), payload)
    return _dataset_summary(payload)
@router.get("/datasets")
def datasets_list():
    rows = []
    for path in tb_config.RECOMMENDATION_DATASETS_DIR.glob("*.json") if tb_config.RECOMMENDATION_DATASETS_DIR.exists() else []:
        try: rows.append(_dataset_summary(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError): continue
    return {"datasets": sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)}
@router.get("/datasets/{dataset_id}")
def datasets_read(dataset_id: str):
    path = _dataset_path(dataset_id)
    if not path.exists(): raise HTTPException(404, detail={"error_type": "RecommendationDatasetNotFound", "message": "dataset not found"})
    return json.loads(path.read_text(encoding="utf-8"))
@router.delete("/datasets/{dataset_id}")
def datasets_delete(dataset_id: str):
    path = _dataset_path(dataset_id)
    if not path.exists(): raise HTTPException(404, detail={"error_type": "RecommendationDatasetNotFound", "message": "dataset not found"})
    path.unlink()
    annotation_path = _annotation_path(dataset_id)
    if annotation_path.exists(): annotation_path.unlink()
    return {"deleted": dataset_id}
@router.post("/calibration")
def calibration(body: CalibrationBody): return run_calibration(datasets_read(body.dataset_id), body.variant)
@router.post("/personalization/trace")
def personalization_trace(body: PersonalizationTraceBody):
    try: return run_personalization_trace(body.model_dump())
    except PersonalizationTraceError as exc:
        raise HTTPException(422, detail={"error_type": "RecommendationPersonalizationInvalid", "message": str(exc)}) from exc

def _annotation_path(dataset_id: str): return tb_config.RECOMMENDATION_DATASETS_DIR / "annotations" / f"{dataset_id}.json"
def _read_annotations(dataset_id: str):
    path = _annotation_path(dataset_id)
    if not path.exists(): return []
    try: return json.loads(path.read_text(encoding="utf-8")).get("annotations") or []
    except (OSError, ValueError): return []

@router.get("/datasets/{dataset_id}/quality")
def dataset_quality(dataset_id: str):
    dataset = datasets_read(dataset_id); annotations = _read_annotations(dataset_id)
    return {"dataset_id": dataset_id, "audit": audit_shadow_dataset(dataset),
            "annotation": annotation_summary(dataset, annotations), "readiness": p44_readiness(dataset, annotations)}

@router.get("/datasets/{dataset_id}/annotations")
def annotations_read(dataset_id: str):
    dataset = datasets_read(dataset_id); annotations = _read_annotations(dataset_id)
    by_turn = {row.get("turn_id"): row for row in annotations}
    tasks = [{"turn_id": row.get("turn_id"), "observation": row, "annotation": by_turn.get(row.get("turn_id"))}
             for row in dataset.get("observations") or []]
    return {"dataset_id": dataset_id, "tasks": tasks, "summary": annotation_summary(dataset, annotations)}

@router.post("/datasets/{dataset_id}/annotations/validate")
def annotations_validate(dataset_id: str, body: AnnotationsBody):
    return validate_annotations(datasets_read(dataset_id), body.annotations)

@router.post("/datasets/{dataset_id}/annotations")
def annotations_save(dataset_id: str, body: AnnotationsBody):
    dataset = datasets_read(dataset_id); result = validate_annotations(dataset, body.annotations)
    if not result["ok"]:
        raise HTTPException(422, detail={"error_type": "RecommendationAnnotationInvalid",
                                         "message": "annotation validation failed", "errors": result["errors"]})
    payload = {"schema_version": 1, "dataset_id": dataset_id, "annotations": result["normalized"]}
    _annotation_path(dataset_id).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_annotation_path(dataset_id), payload)
    return {"dataset_id": dataset_id, "summary": annotation_summary(dataset, result["normalized"])}

@router.post("/datasets/{dataset_id}/golden")
def datasets_promote_golden(dataset_id: str):
    dataset = datasets_read(dataset_id); annotations = _read_annotations(dataset_id)
    readiness = p44_readiness(dataset, annotations)
    if not readiness["ready_for_weight_candidates"]:
        raise HTTPException(422, detail={"error_type": "RecommendationGoldenNotReady",
                                         "message": "dataset does not meet P44 gates", "blockers": readiness["blockers"]})
    golden_id = f"golden-{uuid4().hex}"
    payload = {"schema_version": 1, "id": golden_id, "kind": "shadow_golden",
               "name": f"{dataset.get('name', dataset_id)} golden", "created_at": datetime.now(timezone.utc).isoformat(),
               "parent_dataset_id": dataset_id, "observations": dataset.get("observations") or [],
               "feedback": dataset.get("feedback") or [], "annotations": annotations,
               "readiness_snapshot": readiness, "production_config_modified": False}
    atomic_write_json(_dataset_path(golden_id), payload)
    return _dataset_summary(payload)
