from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledge.api import (
    INDEXED_SUBSCRIPTION_PROTOCOL_VERSION,
    load_canonical_pack_artifact,
)
from knowledge.packs import MAX_PACK_BYTES
from plugin.logging_config import get_logger
from plugin.settings import MARKET_API_URL, NEKO_AUTH_CLIENT_ID
from plugin.server.routes.market_bridge import (
    _ensure_valid_oauth_token,
    _main_server_port,
    get_bridge_token,
)


router = APIRouter(prefix="/market/knowledge", tags=["market-knowledge"])
logger = get_logger("server.routes.knowledge_market")
_tasks: dict[str, dict[str, Any]] = {}
_TASK_TTL_SECONDS = 60 * 60
_JOB_POLL_SECONDS = 5.0
_JOB_WAIT_TIMEOUT_SECONDS = 24 * 60 * 60
_MAX_INDEX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_VECTOR_BYTES = 5_000 * 256 * 2
_ALLOWED_ARTIFACT_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class KnowledgeSubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: int = Field(gt=0)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
    channel: Literal["stable", "beta"] = "stable"


class KnowledgeArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=1_000)
    sha256: str
    bytes: int = Field(gt=0)

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_sha256(cls, value: object) -> str:
        digest = str(value or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("artifact_sha256 must be a SHA-256 digest")
        return digest


class KnowledgeArtifactSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge: KnowledgeArtifactDescriptor
    index_manifest: KnowledgeArtifactDescriptor | None = None
    vectors: KnowledgeArtifactDescriptor | None = None


class KnowledgeVersionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[2]
    package_id: int = Field(gt=0)
    remote_id: str = Field(pattern=r"^knowledge/[a-z0-9][a-z0-9._-]{1,99}$")
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
    channel: Literal["stable", "beta"]
    artifacts: KnowledgeArtifactSet


class KnowledgeUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: int = Field(gt=0)
    collection: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")


class KnowledgeTaskResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: float
    message: str
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None


@router.post("/subscribe")
async def subscribe_knowledge_package(
    payload: KnowledgeSubscribeRequest,
    token: str = Query(...),
):
    _verify_bridge_token(token)
    _cleanup_tasks()
    task_id = secrets.token_urlsafe(16)
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "stage": "pending",
        "progress": 0.0,
        "message": "知识包订阅任务已创建",
        "result": None,
        "error": None,
        "error_code": None,
        "created_at": time.time(),
        "completed_at": None,
    }
    asyncio.create_task(
        _execute_subscription(task_id, payload),
        name=f"market-knowledge-{task_id}",
    )
    return {"task_id": task_id, "status": "pending"}


@router.get("/tasks/{task_id}", response_model=KnowledgeTaskResponse)
async def get_knowledge_task(task_id: str, token: str = Query(...)):
    _verify_bridge_token(token)
    _cleanup_tasks()
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="knowledge task not found")
    return task


@router.get("/subscriptions")
async def list_local_knowledge_subscriptions(token: str = Query(...)):
    _verify_bridge_token(token)
    collections = await _main_request("GET", "collections")
    items: list[dict[str, Any]] = []
    for collection in collections.get("collections", []):
        collection_id = str(collection.get("collection_id") or "")
        if not collection_id:
            continue
        payload = await _main_request(
            "GET", "packs", params={"collection": collection_id}
        )
        for pack in payload.get("packs", []):
            if isinstance(pack, dict) and isinstance(pack.get("subscription"), dict):
                items.append({"collection": collection_id, **pack})
    return {"ok": True, "subscriptions": items}


@router.post("/unsubscribe")
async def unsubscribe_knowledge_package(
    payload: KnowledgeUnsubscribeRequest,
    token: str = Query(...),
):
    _verify_bridge_token(token)
    result = await _main_request(
        "POST",
        "packs/remove",
        json={"collection": payload.collection, "pack_id": payload.pack_id},
    )
    if result.get("ok") is not True:
        raise HTTPException(
            status_code=409, detail=result.get("reason") or "unsubscribe failed"
        )
    await _report_unsubscribe_best_effort(payload.package_id)
    return result


async def _execute_subscription(
    task_id: str, payload: KnowledgeSubscribeRequest
) -> None:
    task = _tasks[task_id]
    try:
        _stage(task, "resolving", 0.05, "正在读取可信市场版本信息")
        descriptor = await _fetch_version_descriptor(payload)
        _stage(task, "downloading", 0.15, "正在下载知识包")
        raw = await _download_verified_artifact(
            descriptor.artifacts.knowledge,
            max_bytes=MAX_PACK_BYTES,
            required_suffix=".neko-knowledge.json",
        )
        _stage(task, "verifying", 0.55, "正在校验知识包")
        try:
            pack_payload = load_canonical_pack_artifact(raw)
        except ValueError as exc:
            raise _KnowledgeTaskError("invalid_artifact", str(exc)) from exc
        if not isinstance(pack_payload, dict):
            raise _KnowledgeTaskError("invalid_artifact", "知识包根必须是对象")
        if pack_payload.get("pack_id") != descriptor.pack_id:
            raise _KnowledgeTaskError(
                "package_identity_mismatch", "市场条目与知识包身份不一致"
            )

        manifest_raw: bytes | None = None
        vectors_raw: bytes | None = None
        index_fallback_reason = ""
        manifest_descriptor = descriptor.artifacts.index_manifest
        vector_descriptor = descriptor.artifacts.vectors
        if bool(manifest_descriptor) != bool(vector_descriptor):
            index_fallback_reason = "incomplete_index_descriptor"
        elif manifest_descriptor is not None and vector_descriptor is not None:
            try:
                manifest_raw = await _download_verified_artifact(
                    manifest_descriptor,
                    max_bytes=_MAX_INDEX_MANIFEST_BYTES,
                    required_suffix=".neko-knowledge.index.json",
                )
                vectors_raw = await _download_verified_artifact(
                    vector_descriptor,
                    max_bytes=_MAX_VECTOR_BYTES,
                    required_suffix=".neko-knowledge.vectors.f16",
                )
            except _KnowledgeTaskError as exc:
                manifest_raw = None
                vectors_raw = None
                index_fallback_reason = exc.code
        _stage(task, "installing", 0.75, "正在写入本地知识库")
        result = await _main_indexed_subscription_request(
            subscription={
                "provider": "plugin-market",
                "remote_id": descriptor.remote_id,
                "version": descriptor.version,
                "channel": descriptor.channel,
                "artifact_sha256": descriptor.artifacts.knowledge.sha256,
                "index_manifest_sha256": (
                    manifest_descriptor.sha256 if manifest_raw is not None else ""
                ),
                "vectors_sha256": (
                    vector_descriptor.sha256 if vectors_raw is not None else ""
                ),
                "trust": "trusted_market",
            },
            pack_raw=raw,
            manifest_raw=manifest_raw,
            vectors_raw=vectors_raw,
            index_fallback_reason=index_fallback_reason,
        )
        if result.get("ok") is not True:
            raise _KnowledgeTaskError(
                str(result.get("reason") or "install_failed"),
                "本地知识库拒绝了该知识包",
            )
        job_id = str(result.get("job_id") or "")
        if job_id:
            activated = await _wait_for_pack_job(
                task,
                job_id=job_id,
                collection_id=str(
                    result.get("collection_id")
                    or pack_payload.get("collection_id")
                    or ""
                ),
            )
            result = {**result, "activation": activated}
        task["result"] = result
        task["status"] = "completed"
        task["stage"] = "completed"
        task["progress"] = 1.0
        task["message"] = "知识包订阅完成"
        task["completed_at"] = time.time()
        await _report_subscription_best_effort(descriptor, result)
    except _KnowledgeTaskError as exc:
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = exc.message
        task["error_code"] = exc.code
        task["message"] = exc.message
        task["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("knowledge subscription task failed: {}", type(exc).__name__)
        task["status"] = "failed"
        task["stage"] = "failed"
        task["error"] = "知识包订阅失败"
        task["error_code"] = "internal_error"
        task["message"] = "知识包订阅失败"
        task["completed_at"] = time.time()


async def _fetch_version_descriptor(
    request: KnowledgeSubscribeRequest,
) -> KnowledgeVersionDescriptor:
    headers = {"Accept": "application/json"}
    token_data = await _ensure_valid_oauth_token()
    if token_data and token_data.get("access_token"):
        headers["Authorization"] = f"Bearer {token_data['access_token']}"
    url = (
        f"{MARKET_API_URL.rstrip('/')}/api/v1/knowledge/packages/"
        f"{request.package_id}/versions/{quote(request.version, safe='')}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
        ) as client:
            response = await client.get(
                url, params={"channel": request.channel}, headers=headers
            )
            response.raise_for_status()
            descriptor = KnowledgeVersionDescriptor.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise _KnowledgeTaskError(
            "catalog_resolution_failed",
            "无法读取可信市场版本信息",
        ) from exc
    if (
        descriptor.package_id != request.package_id
        or descriptor.version != request.version
        or descriptor.channel != request.channel
    ):
        raise _KnowledgeTaskError(
            "catalog_identity_mismatch",
            "市场版本身份不一致",
        )
    return descriptor


async def _download_verified_artifact(
    descriptor: KnowledgeArtifactDescriptor,
    *,
    max_bytes: int,
    required_suffix: str,
) -> bytes:
    if descriptor.bytes > max_bytes:
        raise _KnowledgeTaskError("artifact_too_large", "知识制品超过大小限制")
    _validate_artifact_url(descriptor.url, required_suffix=required_suffix)
    raw = await _download_artifact(descriptor.url, max_bytes=max_bytes)
    if len(raw) != descriptor.bytes:
        raise _KnowledgeTaskError("artifact_size_mismatch", "知识制品大小不一致")
    digest = hashlib.sha256(raw).hexdigest()
    if not secrets.compare_digest(digest, descriptor.sha256):
        raise _KnowledgeTaskError("artifact_hash_mismatch", "知识制品摘要校验失败")
    return raw


async def _download_artifact(url: str, *, max_bytes: int = MAX_PACK_BYTES) -> bytes:
    headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
    chunks: list[bytes] = []
    size = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                for hop in (*response.history, response):
                    try:
                        _validate_artifact_url(str(hop.url))
                    except HTTPException as exc:
                        raise _KnowledgeTaskError(
                            "unsafe_artifact_redirect",
                            "知识包下载发生了不安全的重定向",
                        ) from exc
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise _KnowledgeTaskError(
                        "artifact_too_large", "知识制品超过大小限制"
                    )
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise _KnowledgeTaskError(
                            "artifact_too_large", "知识制品超过大小限制"
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise _KnowledgeTaskError("download_failed", "知识包下载失败") from exc
    return b"".join(chunks)


async def _main_indexed_subscription_request(
    *,
    subscription: dict[str, str],
    pack_raw: bytes,
    manifest_raw: bytes | None,
    vectors_raw: bytes | None,
    index_fallback_reason: str,
) -> dict[str, Any]:
    import config

    port = _main_server_port()
    headers = {
        "Accept": "application/json",
        "Origin": f"http://127.0.0.1:{port}",
        "X-CSRF-Token": str(config.AUTOSTART_CSRF_TOKEN),
    }
    data = {
        "protocol_version": str(INDEXED_SUBSCRIPTION_PROTOCOL_VERSION),
        "subscription": json.dumps(subscription, separators=(",", ":"), sort_keys=True),
        "index_fallback_reason": index_fallback_reason,
    }
    files: dict[str, tuple[str, bytes, str]] = {
        "pack": ("pack.neko-knowledge.json", pack_raw, "application/json"),
    }
    if manifest_raw is not None and vectors_raw is not None:
        files["index_manifest"] = (
            "pack.neko-knowledge.index.json",
            manifest_raw,
            "application/json",
        )
        files["vectors"] = (
            "pack.neko-knowledge.vectors.f16",
            vectors_raw,
            "application/octet-stream",
        )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=2.0),
            trust_env=False,
        ) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/api/public-knowledge/subscriptions/apply-v2",
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _KnowledgeTaskError(
            "main_server_unavailable", "Main Server 不可用"
        ) from exc
    return payload if isinstance(payload, dict) else {}


async def _main_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    import config

    port = _main_server_port()
    headers = {"Accept": "application/json"}
    if method == "POST":
        headers.update(
            {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "X-CSRF-Token": str(config.AUTOSTART_CSRF_TOKEN),
            }
        )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=2.0),
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                f"http://127.0.0.1:{port}/api/public-knowledge/{path}",
                params=params,
                json=json,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _KnowledgeTaskError(
            "main_server_unavailable", "Main Server 不可用"
        ) from exc
    return payload if isinstance(payload, dict) else {}


async def _wait_for_pack_job(
    task: dict[str, Any],
    *,
    job_id: str,
    collection_id: str,
) -> dict[str, Any]:
    """Keep marketplace install pending until the staged pack is truly active."""
    deadline = time.monotonic() + _JOB_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        payload = await _main_request(
            "GET",
            "packs/jobs",
            params={"collection": collection_id},
        )
        job = next(
            (
                item
                for item in payload.get("jobs", [])
                if isinstance(item, dict) and item.get("job_id") == job_id
            ),
            None,
        )
        if job is None:
            raise _KnowledgeTaskError("job_not_found", "knowledge job not found")
        state = str(job.get("state") or "")
        if state == "active":
            return job
        if state in {"cancelled", "failed"}:
            raise _KnowledgeTaskError(
                f"job_{state}",
                "knowledge job did not complete",
            )
        percent = max(0.0, min(float(job.get("indexed_percent") or 0.0), 100.0))
        task["stage"] = "indexing"
        task["progress"] = 0.8 + percent * 0.0019
        task["message"] = "Knowledge pack indexing in the background"
        await asyncio.sleep(_JOB_POLL_SECONDS)
    raise _KnowledgeTaskError("job_timeout", "knowledge job timed out")


async def _report_subscription_best_effort(
    descriptor: KnowledgeVersionDescriptor,
    result: dict[str, Any],
) -> None:
    try:
        token_data = await _ensure_valid_oauth_token()
        if not token_data or not token_data.get("access_token"):
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{MARKET_API_URL.rstrip('/')}/api/v1/me/knowledge-subscriptions",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
                json={
                    "package_id": descriptor.package_id,
                    "version": descriptor.version,
                    "channel": descriptor.channel,
                    "artifact_sha256": descriptor.artifacts.knowledge.sha256,
                    "installed_pack_id": result.get("pack_id"),
                    "client_id": NEKO_AUTH_CLIENT_ID,
                },
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning("knowledge subscription report failed: {}", type(exc).__name__)


async def _report_unsubscribe_best_effort(package_id: int) -> None:
    try:
        token_data = await _ensure_valid_oauth_token()
        if not token_data or not token_data.get("access_token"):
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"{MARKET_API_URL.rstrip('/')}/api/v1/me/knowledge-subscriptions/{package_id}",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning("knowledge unsubscribe report failed: {}", type(exc).__name__)


def _verify_bridge_token(token: str) -> None:
    if not secrets.compare_digest(token, get_bridge_token()):
        raise HTTPException(status_code=403, detail="invalid bridge token")


def _validate_artifact_url(url: str, *, required_suffix: str = "") -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or (parsed.hostname or "").lower() not in _ALLOWED_ARTIFACT_HOSTS
    ):
        raise HTTPException(status_code=400, detail="artifact URL is not allowed")
    if required_suffix and not parsed.path.lower().endswith(required_suffix):
        raise HTTPException(status_code=400, detail="invalid knowledge artifact suffix")


def _stage(task: dict[str, Any], stage: str, progress: float, message: str) -> None:
    task["status"] = stage
    task["stage"] = stage
    task["progress"] = progress
    task["message"] = message


def _cleanup_tasks() -> None:
    now = time.time()
    expired = [
        task_id
        for task_id, task in _tasks.items()
        if task.get("completed_at")
        and now - float(task["completed_at"]) > _TASK_TTL_SECONDS
    ]
    for task_id in expired:
        _tasks.pop(task_id, None)


class _KnowledgeTaskError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
