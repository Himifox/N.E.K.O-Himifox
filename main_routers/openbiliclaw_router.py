# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Loopback-only status surface for the built-in OpenBiliClaw integration."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.openbiliclaw_runtime import get_openbiliclaw_runtime

router = APIRouter(prefix="/api/openbiliclaw", tags=["openbiliclaw"])


def _is_loopback_request(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    normalized_host = str(client_host or "").removeprefix("::ffff:")
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


@router.get("/status")
async def openbiliclaw_status(request: Request) -> JSONResponse:
    """Report Core and extension-bridge state without exposing credentials."""
    if not _is_loopback_request(request):
        return JSONResponse({"success": False, "error": "loopback_only"}, status_code=403)
    status = get_openbiliclaw_runtime().status.to_dict()
    return JSONResponse({"success": True, **status})


__all__ = ["router"]
