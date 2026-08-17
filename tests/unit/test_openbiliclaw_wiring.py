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

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_main_server_owns_openbiliclaw_without_plugin_or_mcp() -> None:
    main_server = (REPO_ROOT / "app" / "main_server" / "__init__.py").read_text(
        encoding="utf-8"
    )
    runtime = (REPO_ROOT / "app" / "openbiliclaw_runtime.py").read_text(encoding="utf-8")

    assert "await start_openbiliclaw_runtime(_config_manager)" in main_server
    assert "await stop_openbiliclaw_runtime()" in main_server
    assert "from plugin" not in runtime
    assert "app.agent_server.channels" not in runtime


def test_status_router_is_registered_before_pages_fallback() -> None:
    web_app = (REPO_ROOT / "app" / "main_server" / "web_app.py").read_text(encoding="utf-8")

    status_router = web_app.index("app.include_router(openbiliclaw_router)")
    pages_fallback = web_app.index("app.include_router(pages_router)")
    assert status_router < pages_fallback
