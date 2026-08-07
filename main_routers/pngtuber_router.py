# -*- coding: utf-8 -*-
"""PNGTuber model package endpoints."""

import asyncio
import hashlib
import json
import math
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .pngtuber_importers import PNGTuberImportError, import_pngtuber_package
from .shared_state import get_config_manager
from utils.logger_config import get_module_logger

router = APIRouter(prefix="/api/model/pngtuber", tags=["pngtuber"])
logger = get_module_logger(__name__, "Main")

PNGTUBER_USER_PATH = "/user_pngtuber"
PNGTUBER_BUILTIN_PATH = "/api/model/pngtuber/builtin"
PNGTUBER_PACKS_DIRNAME = "pngtuber-packs"
PNGTUBER_EXTENSIONS = {".png", ".gif", ".jpg", ".jpeg", ".webp"}
PNGTUBER_ASSET_EXTENSIONS = PNGTUBER_EXTENSIONS | {".json"}
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_PACKAGE_SIZE = 250 * 1024 * 1024
MAX_ARCHIVE_FILES = 1000
CHUNK_SIZE = 1024 * 1024
_builtin_extract_locks: dict[str, asyncio.Lock] = {}


def _slugify_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", (name or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._-")
    return cleaned or "pngtuber_model"


def _safe_relative_path(raw_path: str) -> PurePosixPath | None:
    normalized = (raw_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    rel = PurePosixPath(normalized)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        return None
    return rel


def _resolve_delete_folder_from_key(key: str) -> str | None:
    normalized = (key or "").replace("\\", "/").strip()
    if not normalized:
        return None

    parsed = urlsplit(normalized)
    if parsed.scheme and parsed.path:
        normalized = parsed.path
    else:
        normalized = normalized.split("?", 1)[0].split("#", 1)[0]

    rel = _safe_relative_path(normalized)
    if rel is None:
        return None

    parts = rel.parts
    user_prefix = PNGTUBER_USER_PATH.strip("/")
    if parts and parts[0] == user_prefix:
        parts = parts[1:]
    if not parts:
        return None

    if parts[-1].lower() == "model.json":
        if len(parts) != 2:
            return None
        return parts[-2]

    if len(parts) != 1:
        return None
    return parts[0]


def _split_upload_root(paths: list[PurePosixPath]) -> tuple[str, dict[PurePosixPath, PurePosixPath]]:
    first_parts = {p.parts[0] for p in paths if p.parts}
    if len(first_parts) == 1 and all(len(p.parts) > 1 for p in paths):
        root = next(iter(first_parts))
        return root, {p: PurePosixPath(*p.parts[1:]) for p in paths}
    return "", {p: p for p in paths}


def _read_model_json(package_dir: Path) -> dict:
    with open(package_dir / "model.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _builtin_packs_dir(config_mgr) -> Path:
    return Path(config_mgr.project_root) / "static" / PNGTUBER_PACKS_DIRNAME


def _read_builtin_manifest(config_mgr) -> list[dict]:
    manifest_path = _builtin_packs_dir(config_mgr) / "manifest.json"
    if not manifest_path.is_file():
        return []
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    models = manifest.get("models") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(models, list):
        raise ValueError("内置 PNGTuber 清单格式无效")
    return models


def _find_builtin_model(config_mgr, folder: str) -> dict | None:
    if _safe_relative_path(folder) != PurePosixPath(folder) or "/" in folder or "\\" in folder:
        return None
    for model in _read_builtin_manifest(config_mgr):
        if isinstance(model, dict) and model.get("folder") == folder:
            return model
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_layered_metadata(package_dir: Path, model_json: dict) -> None:
    config = model_json.get("pngtuber") or model_json.get("_reserved", {}).get("avatar", {}).get("pngtuber") or {}
    metadata_name = config.get("layered_metadata") or config.get("metadata")
    if not metadata_name:
        return
    rel = _safe_relative_path(metadata_name) if isinstance(metadata_name, str) else None
    if rel is None or rel.suffix.lower() != ".json":
        raise ValueError("内置 PNGTuber 分层 metadata 路径无效")
    metadata_path = package_dir / rel.as_posix()
    if not metadata_path.is_file():
        raise ValueError("内置 PNGTuber 分层 metadata 不存在")
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)
    layers = metadata.get("layers") if isinstance(metadata, dict) else None
    if not isinstance(layers, list):
        raise ValueError("内置 PNGTuber 分层 metadata 格式无效")
    for layer in layers:
        image_name = layer.get("image") if isinstance(layer, dict) else None
        image_rel = _safe_relative_path(image_name) if isinstance(image_name, str) else None
        if image_rel is None or image_rel.suffix.lower() not in PNGTUBER_EXTENSIONS:
            raise ValueError("内置 PNGTuber 图层图片路径无效")
        if not (package_dir / image_rel.as_posix()).is_file():
            raise ValueError(f"内置 PNGTuber 图层图片不存在: {image_name}")


def _extract_builtin_model(config_mgr, model: dict) -> Path:
    folder = model.get("folder")
    archive_name = model.get("archive")
    expected_sha256 = str(model.get("archive_sha256") or "").lower()
    folder_rel = _safe_relative_path(folder) if isinstance(folder, str) else None
    if folder_rel is None or len(folder_rel.parts) != 1:
        raise ValueError("内置 PNGTuber 文件夹名称无效")
    archive_rel = _safe_relative_path(archive_name) if isinstance(archive_name, str) else None
    if archive_rel is None or len(archive_rel.parts) != 1 or archive_rel.suffix.lower() != ".zip":
        raise ValueError("内置 PNGTuber 压缩包名称无效")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("内置 PNGTuber 压缩包哈希无效")

    archive_path = _builtin_packs_dir(config_mgr) / archive_rel.as_posix()
    cache_parent = Path(config_mgr.app_docs_dir) / "cache" / "builtin_pngtuber" / folder
    target_dir = cache_parent / expected_sha256[:16]
    ready_marker = target_dir / ".ready"
    if ready_marker.is_file() and ready_marker.read_text(encoding="utf-8").strip() == expected_sha256:
        return target_dir
    if not archive_path.is_file() or _sha256_file(archive_path) != expected_sha256:
        raise ValueError(f"内置 PNGTuber 压缩包缺失或校验失败: {archive_name}")

    cache_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = cache_parent / f".{expected_sha256[:16]}.{uuid.uuid4().hex}.tmp"
    temp_dir.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ValueError("内置 PNGTuber 压缩包文件数量过多")
            if sum(info.file_size for info in infos) > MAX_PACKAGE_SIZE:
                raise ValueError("内置 PNGTuber 解压后体积过大")
            if model.get("file_count") != len(infos) or model.get("unpacked_size") != sum(info.file_size for info in infos):
                raise ValueError("内置 PNGTuber 压缩包与清单不一致")

            seen: set[str] = set()
            for info in infos:
                raw_name = info.filename.replace("\\", "/")
                rel = _safe_relative_path(info.filename)
                unix_mode = info.external_attr >> 16
                if (
                    rel is None
                    or raw_name.startswith("/")
                    or re.match(r"^[a-zA-Z]:", raw_name)
                    or stat.S_IFMT(unix_mode) == stat.S_IFLNK
                ):
                    raise ValueError(f"内置 PNGTuber 压缩包包含不安全路径: {info.filename}")
                normalized = rel.as_posix().casefold()
                if normalized in seen:
                    raise ValueError(f"内置 PNGTuber 压缩包包含重复路径: {info.filename}")
                seen.add(normalized)
                if info.file_size > MAX_FILE_SIZE:
                    raise ValueError(f"内置 PNGTuber 单个文件过大: {info.filename}")
                output_path = (temp_dir / rel.as_posix()).resolve()
                output_path.relative_to(temp_dir.resolve())
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(output_path, "xb") as output:
                    shutil.copyfileobj(source, output, CHUNK_SIZE)

        model_json = _read_model_json(temp_dir)
        ok, error = _validate_model_package(temp_dir, model_json)
        if not ok:
            raise ValueError(error)
        _validate_layered_metadata(temp_dir, model_json)
        (temp_dir / ".ready").write_text(expected_sha256, encoding="utf-8")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        temp_dir.rename(target_dir)
        return target_dir
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


async def _ensure_builtin_model(config_mgr, model: dict) -> Path:
    folder = str(model.get("folder") or "")
    lock = _builtin_extract_locks.setdefault(folder, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(_extract_builtin_model, config_mgr, model)


def _normalize_pngtuber_config(
    model_dir_name: str,
    model_json: dict,
    url_root: str = PNGTUBER_USER_PATH,
) -> dict:
    raw = model_json.get("pngtuber") or model_json.get("_reserved", {}).get("avatar", {}).get("pngtuber") or {}
    result: dict = {}
    model_url_root = f"{url_root.rstrip('/')}/{model_dir_name}"
    image_fields = [
        "idle_image",
        "talking_image",
        "drag_image",
        "click_image",
        "happy_image",
        "sad_image",
        "angry_image",
        "surprised_image",
    ]

    for field in image_fields:
        value = raw.get(field, "")
        if not isinstance(value, str) or not value.strip():
            result[field] = ""
            continue
        stripped = value.replace("\\", "/").strip()
        if stripped.startswith(("/", "http://", "https://")):
            result[field] = stripped
        else:
            rel = _safe_relative_path(stripped)
            result[field] = f"{model_url_root}/{rel.as_posix()}" if rel else ""

    metadata_path = raw.get("layered_metadata") or raw.get("metadata")
    if isinstance(metadata_path, str) and metadata_path.strip():
        stripped = metadata_path.strip().replace("\\", "/")
        if stripped.startswith(("/", "http://", "https://")):
            result["layered_metadata"] = stripped
        else:
            rel = _safe_relative_path(stripped)
            result["layered_metadata"] = f"{model_url_root}/{rel.as_posix()}" if rel else ""
    else:
        result["layered_metadata"] = ""

    adapter = raw.get("adapter")
    if isinstance(adapter, str):
        result["adapter"] = adapter
    elif result["layered_metadata"]:
        result["adapter"] = "layered_canvas_v1"
    else:
        result["adapter"] = ""

    result["scale"] = raw.get("scale", 1)
    result["offset_x"] = raw.get("offset_x", 0)
    result["offset_y"] = raw.get("offset_y", 0)
    try:
        scale_number = float(result["scale"])
        mobile_scale_default = min(scale_number, 1) if math.isfinite(scale_number) else 1
    except (TypeError, ValueError):
        mobile_scale_default = 1
    result["mobile_scale"] = raw.get("mobile_scale", mobile_scale_default)
    result["mobile_offset_x"] = raw.get("mobile_offset_x", 0)
    result["mobile_offset_y"] = raw.get("mobile_offset_y", 0)
    result["mirror"] = bool(raw.get("mirror", False))
    result["source_type"] = raw.get("source_type") or "transparent_asset"
    result["source_format"] = model_json.get("source_format") or raw.get("source_format") or result["source_type"]
    return result


def _validate_model_package(package_dir: Path, model_json: dict) -> tuple[bool, str]:
    if model_json.get("model_type") != "pngtuber":
        return False, "model.json 的 model_type 必须是 pngtuber"

    config = model_json.get("pngtuber") or model_json.get("_reserved", {}).get("avatar", {}).get("pngtuber") or {}
    idle_image = config.get("idle_image")
    if not isinstance(idle_image, str) or not idle_image.strip():
        return False, "PNGTuber 模型必须配置 idle_image"

    for key, value in config.items():
        if not key.endswith("_image") or not isinstance(value, str) or not value.strip():
            continue
        if value.startswith(("/", "http://", "https://")):
            continue
        rel = _safe_relative_path(value)
        if rel is None:
            return False, f"{key} 路径无效: {value}"
        if rel.suffix.lower() not in PNGTUBER_EXTENSIONS:
            return False, f"{key} 文件格式不支持: {value}"
        if not (package_dir / rel.as_posix()).exists():
            return False, f"{key} 引用的文件不存在: {value}"
    return True, ""


@router.post("/upload_model")
async def upload_pngtuber_model(files: list[UploadFile] = File(...)):
    if not files:
        return JSONResponse(status_code=400, content={"success": False, "error": "没有上传文件"})

    config_mgr = get_config_manager()
    if not config_mgr.ensure_pngtuber_directory():
        return JSONResponse(status_code=500, content={"success": False, "error": "PNGTuber目录创建失败"})

    upload_paths: list[PurePosixPath] = []
    by_path: dict[PurePosixPath, UploadFile] = {}
    for file in files:
        rel = _safe_relative_path(file.filename or "")
        if rel is None:
            return JSONResponse(status_code=400, content={"success": False, "error": f"上传路径无效: {file.filename}"})
        upload_paths.append(rel)
        by_path[rel] = file

    upload_root, stripped_paths = _split_upload_root(upload_paths)
    model_name_seed = upload_root or ""
    if not model_name_seed:
        model_file = next((f for p, f in by_path.items() if stripped_paths[p] == PurePosixPath("model.json")), None)
        if model_file:
            model_name_seed = Path(model_file.filename or "pngtuber_model").stem
        elif len(upload_paths) == 1:
            model_name_seed = Path(upload_paths[0].name or "pngtuber_model").stem
        else:
            # Multi-file third-party uploads (e.g. a .save/.pngRemix plus sidecar
            # images) carry no shared root and no model.json, but the importer will
            # name the model after the project file. Seed from that file so the
            # early target_dir.exists() check uses the real name instead of the
            # placeholder, which would otherwise reject any upload whenever a folder
            # literally named "pngtuber_model" already exists.
            project_exts = {".save", ".pngremix", ".veadomini", ".veado"}
            project_files = [p for p in upload_paths if p.suffix.lower() in project_exts]
            if len(project_files) == 1:
                model_name_seed = project_files[0].stem
            else:
                model_name_seed = "pngtuber_model"
    model_dir_name = _slugify_name(model_name_seed)

    target_dir = config_mgr.pngtuber_dir / model_dir_name
    if target_dir.exists():
        return JSONResponse(status_code=400, content={"success": False, "error": f"PNGTuber模型 {model_dir_name} 已存在，请先删除或重命名"})

    temp_dir = config_mgr.pngtuber_dir / f".{model_dir_name}.uploading"
    if temp_dir.exists():
        await asyncio.to_thread(shutil.rmtree, temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    total_size = 0
    try:
        resolved_temp = temp_dir.resolve()
        for original_rel, file in by_path.items():
            stripped_rel = stripped_paths[original_rel]
            target_file = (temp_dir / stripped_rel.as_posix()).resolve()
            try:
                target_file.relative_to(resolved_temp)
            except ValueError:
                raise ValueError(f"上传路径越界: {file.filename}")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            with open(target_file, "xb") as out:
                while True:
                    chunk = await file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_PACKAGE_SIZE:
                        raise ValueError(f"模型包过大，最大允许 {MAX_PACKAGE_SIZE // (1024 * 1024)}MB")
                    if target_file.stat().st_size + len(chunk) > MAX_FILE_SIZE:
                        raise ValueError(f"单个文件过大，最大允许 {MAX_FILE_SIZE // (1024 * 1024)}MB")
                    out.write(chunk)

        import_result = import_pngtuber_package(temp_dir, model_dir_name)
        model_json = import_result.model_json
        ok, error = _validate_model_package(temp_dir, model_json)
        if not ok:
            return JSONResponse(status_code=400, content={"success": False, "error": error})

        model_dir_name = _slugify_name(import_result.model_name or model_json.get("name") or model_dir_name)
        target_dir = config_mgr.pngtuber_dir / model_dir_name
        if target_dir.exists():
            return JSONResponse(status_code=400, content={"success": False, "error": f"PNGTuber模型 {model_dir_name} 已存在，请先删除或重命名"})

        normalized_config = _normalize_pngtuber_config(model_dir_name, model_json)
        model_json["model_type"] = "pngtuber"
        model_json["pngtuber"] = normalized_config
        model_json["source_format"] = import_result.source_format
        with open(temp_dir / "model.json", "w", encoding="utf-8") as f:
            json.dump(model_json, f, ensure_ascii=False, indent=2)

        temp_dir.rename(target_dir)
        logger.info("PNGTuber模型上传成功: %s", target_dir)
        return JSONResponse(content={
            "success": True,
            "message": import_result.message or f"PNGTuber模型 {model_json.get('name') or model_dir_name} 上传成功",
            "model_type": "pngtuber",
            "model_name": model_json.get("name") or model_dir_name,
            "name": model_json.get("name") or model_dir_name,
            "folder": model_dir_name,
            "url": f"{PNGTUBER_USER_PATH}/{model_dir_name}/model.json",
            "pngtuber": normalized_config,
            "source_format": import_result.source_format,
            "warnings": import_result.warnings,
            "file_size": total_size,
        })
    except PNGTuberImportError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": str(exc),
                "source_format": exc.source_format,
                "warnings": exc.warnings,
            },
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    except Exception as exc:
        logger.error("上传PNGTuber模型失败: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})
    finally:
        for file in files:
            try:
                await file.close()
            except Exception:
                pass
        if temp_dir.exists():
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)


@router.get("/models")
async def get_pngtuber_models():
    try:
        config_mgr = get_config_manager()
        config_mgr.ensure_pngtuber_directory()
        models = []
        for model_json in sorted(_read_builtin_manifest(config_mgr), key=lambda item: str(item.get("folder", "")).lower()):
            try:
                folder = model_json.get("folder")
                if not isinstance(folder, str) or _find_builtin_model(config_mgr, folder) is None:
                    continue
                pngtuber = _normalize_pngtuber_config(folder, model_json, PNGTUBER_BUILTIN_PATH)
                models.append({
                    "name": model_json.get("name") or folder,
                    "folder": folder,
                    "filename": folder,
                    "location": "builtin",
                    "type": "pngtuber",
                    "model_type": "pngtuber",
                    "url": f"{PNGTUBER_BUILTIN_PATH}/{folder}/model.json",
                    "pngtuber": pngtuber,
                    "source_format": model_json.get("source_format", "simple_package"),
                })
            except Exception as exc:
                logger.warning("跳过无效内置 PNGTuber 模型 %s: %s", model_json, exc)

        root = config_mgr.pngtuber_dir
        if root.is_dir():
            for package_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not package_dir.is_dir() or not (package_dir / "model.json").exists():
                    continue
                try:
                    model_json = await asyncio.to_thread(_read_model_json, package_dir)
                    if model_json.get("model_type") != "pngtuber":
                        continue
                    pngtuber = _normalize_pngtuber_config(package_dir.name, model_json, PNGTUBER_USER_PATH)
                    display_name = model_json.get("name") or package_dir.name
                    models.append({
                        "name": display_name,
                        "folder": package_dir.name,
                        "filename": package_dir.name,
                        "location": "user",
                        "type": "pngtuber",
                        "model_type": "pngtuber",
                        "url": f"{PNGTUBER_USER_PATH}/{package_dir.name}/model.json",
                        "pngtuber": pngtuber,
                        "source_format": model_json.get("source_format", "simple_package"),
                    })
                except Exception as exc:
                    logger.warning("跳过无效PNGTuber模型 %s: %s", package_dir, exc)
        return JSONResponse(content={"success": True, "models": models})
    except Exception as exc:
        logger.error("获取PNGTuber模型列表失败: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@router.get("/builtin/{folder}/{asset_path:path}")
async def get_builtin_pngtuber_asset(folder: str, asset_path: str):
    config_mgr = get_config_manager()
    try:
        model = _find_builtin_model(config_mgr, folder)
    except Exception as exc:
        logger.error("读取内置 PNGTuber 清单失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="读取内置 PNGTuber 清单失败") from exc
    if model is None:
        raise HTTPException(status_code=404, detail="内置 PNGTuber 模型不存在")

    rel = _safe_relative_path(asset_path)
    if rel is None or rel.suffix.lower() not in PNGTUBER_ASSET_EXTENSIONS:
        raise HTTPException(status_code=404, detail="内置 PNGTuber 资源不存在")
    try:
        package_dir = await _ensure_builtin_model(config_mgr, model)
        asset = (package_dir / rel.as_posix()).resolve()
        asset.relative_to(package_dir.resolve())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("解压内置 PNGTuber 模型 %s 失败: %s", folder, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="内置 PNGTuber 模型解压失败") from exc
    if not asset.is_file():
        raise HTTPException(status_code=404, detail="内置 PNGTuber 资源不存在")
    return FileResponse(
        asset,
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/model")
async def delete_pngtuber_model(payload: dict = Body(...)):
    key = payload.get("folder") or payload.get("url") or payload.get("name")
    if not isinstance(key, str) or not key.strip():
        return JSONResponse(status_code=400, content={"success": False, "error": "缺少PNGTuber模型标识"})
    folder = _resolve_delete_folder_from_key(key)
    if not folder:
        return JSONResponse(status_code=400, content={"success": False, "error": "无效的PNGTuber模型标识"})

    config_mgr = get_config_manager()
    config_mgr.ensure_pngtuber_directory()
    target_dir = (config_mgr.pngtuber_dir / folder).resolve()
    root_dir = config_mgr.pngtuber_dir.resolve()
    try:
        target_dir.relative_to(root_dir)
    except ValueError:
        return JSONResponse(status_code=400, content={"success": False, "error": "路径越界"})
    if not target_dir.exists() or not target_dir.is_dir():
        return JSONResponse(status_code=404, content={"success": False, "error": "PNGTuber模型不存在"})
    await asyncio.to_thread(shutil.rmtree, target_dir)
    return JSONResponse(content={"success": True, "message": f"PNGTuber模型 {folder} 已删除"})
