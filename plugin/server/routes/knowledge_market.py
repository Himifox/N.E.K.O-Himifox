from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from knowledge.api import (
    SUBSCRIPTION_PROTOCOL_VERSION,
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
_ALLOWED_ARTIFACT_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class KnowledgeSubscribeRequest(BaseModel):
    package_id: int = Field(gt=0)
    remote_id: str = Field(pattern=r"^knowledge/[a-z0-9][a-z0-9-]{1,99}$")
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    version: str = Field(min_length=1, max_length=100)
    channel: Literal["stable", "beta"] = "stable"
    artifact_url: str = Field(min_length=1, max_length=1_000)
    artifact_sha256: str

    @field_validator("artifact_sha256", mode="before")
    @classmethod
    def validate_sha256(cls, value: object) -> str:
        digest = str(value or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("artifact_sha256 must be a SHA-256 digest")
        return digest


class KnowledgeUnsubscribeRequest(BaseModel):
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
    _validate_artifact_url(payload.artifact_url, require_suffix=True)
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
        payload = await _main_request("GET", "packs", params={"collection": collection_id})
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
        raise HTTPException(status_code=409, detail=result.get("reason") or "unsubscribe failed")
    await _report_unsubscribe_best_effort(payload.package_id)
    return result


async def _execute_subscription(task_id: str, payload: KnowledgeSubscribeRequest) -> None:
    task = _tasks[task_id]
    try:
        _stage(task, "downloading", 0.15, "正在下载知识包")
        raw = await _download_artifact(payload.artifact_url)
        _stage(task, "verifying", 0.55, "正在校验知识包")
        digest = hashlib.sha256(raw).hexdigest()
        if not secrets.compare_digest(digest, payload.artifact_sha256):
            raise _KnowledgeTaskError("artifact_hash_mismatch", "知识包摘要校验失败")
        try:
            pack_payload = load_canonical_pack_artifact(raw)
        except ValueError as exc:
            raise _KnowledgeTaskError("invalid_artifact", str(exc)) from exc
        if not isinstance(pack_payload, dict):
            raise _KnowledgeTaskError("invalid_artifact", "知识包根必须是对象")
        if pack_payload.get("pack_id") != payload.pack_id:
            raise _KnowledgeTaskError("package_identity_mismatch", "市场条目与知识包身份不一致")
        _stage(task, "installing", 0.75, "正在写入本地知识库")
        result = await _main_request(
            "POST",
            "subscriptions/apply",
            json={
                "protocol_version": SUBSCRIPTION_PROTOCOL_VERSION,
                "subscription": {
                    "provider": "plugin-market",
                    "remote_id": payload.remote_id,
                    "version": payload.version,
                    "channel": payload.channel,
                    "artifact_sha256": payload.artifact_sha256,
                },
                "pack": pack_payload,
            },
            timeout=30.0,
        )
        if result.get("ok") is not True:
            raise _KnowledgeTaskError(
                str(result.get("reason") or "install_failed"),
                "本地知识库拒绝了该知识包",
            )
        task["result"] = result
        task["status"] = "completed"
        task["stage"] = "completed"
        task["progress"] = 1.0
        task["message"] = "知识包订阅完成"
        task["completed_at"] = time.time()
        await _report_subscription_best_effort(payload, result)
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


async def _download_artifact(url: str) -> bytes:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
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
                        _validate_artifact_url(str(hop.url), require_suffix=False)
                    except HTTPException as exc:
                        raise _KnowledgeTaskError(
                            "unsafe_artifact_redirect",
                            "知识包下载发生了不安全的重定向",
                        ) from exc
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_PACK_BYTES:
                    raise _KnowledgeTaskError("artifact_too_large", "知识包超过 10 MB")
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > MAX_PACK_BYTES:
                        raise _KnowledgeTaskError("artifact_too_large", "知识包超过 10 MB")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise _KnowledgeTaskError("download_failed", "知识包下载失败") from exc
    return b"".join(chunks)


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
        headers.update({
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "X-CSRF-Token": str(config.AUTOSTART_CSRF_TOKEN),
        })
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
        raise _KnowledgeTaskError("main_server_unavailable", "Main Server 不可用") from exc
    return payload if isinstance(payload, dict) else {}


async def _report_subscription_best_effort(
    request: KnowledgeSubscribeRequest,
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
                    "package_id": request.package_id,
                    "version": request.version,
                    "channel": request.channel,
                    "artifact_sha256": request.artifact_sha256,
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


def _validate_artifact_url(url: str, *, require_suffix: bool) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or (parsed.hostname or "").lower() not in _ALLOWED_ARTIFACT_HOSTS
    ):
        raise HTTPException(status_code=400, detail="artifact URL is not allowed")
    if require_suffix and not parsed.path.lower().endswith(".neko-knowledge.json"):
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
        if task.get("completed_at") and now - float(task["completed_at"]) > _TASK_TTL_SECONDS
    ]
    for task_id in expired:
        _tasks.pop(task_id, None)


class _KnowledgeTaskError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
