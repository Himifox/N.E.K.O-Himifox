"""Memory setting reads must distinguish defaults from unreadable storage."""

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main_routers import memory_router
from utils import config_manager
from utils.config_manager.storage_roots import StorageRootsMixin


pytestmark = pytest.mark.unit

SETTINGS = [
    ("review_config", "recent_memory_auto_review"),
    ("powerful_memory_config", "powerful_memory_enabled"),
]


@pytest.fixture
def setting_client(tmp_path, monkeypatch):
    # Use the real file loader without initializing or migrating a user root.
    manager = StorageRootsMixin.__new__(StorageRootsMixin)
    manager.config_dir = tmp_path / "config"
    manager.project_config_dir = tmp_path / "project-config"
    manager.config_dir.mkdir()
    manager.project_config_dir.mkdir()
    monkeypatch.setattr(config_manager, "get_config_manager", lambda: manager)
    app = FastAPI()
    app.include_router(memory_router.router)
    with TestClient(app) as client:
        yield client, manager


@pytest.mark.parametrize("endpoint,key", SETTINGS)
@pytest.mark.parametrize("value", ["missing_file", "missing_key", False, True])
def test_memory_setting_reads_preserve_defaults_and_saved_values(setting_client, endpoint, key, value):
    client, manager = setting_client
    path = manager.config_dir / "core_config.json"
    if value != "missing_file":
        data = {"unrelated_setting": "keep"}
        if value != "missing_key":
            data[key] = value
        path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None

    response = client.get(f"/api/memory/{endpoint}")

    assert response.status_code == 200
    assert response.json() == {"enabled": value if isinstance(value, bool) else True}
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("endpoint,key", SETTINGS)
def test_memory_setting_reads_preserve_project_config_fallback(setting_client, endpoint, key):
    client, manager = setting_client
    path = manager.project_config_dir / "core_config.json"
    path.write_text(json.dumps({key: False}), encoding="utf-8")

    response = client.get(f"/api/memory/{endpoint}")

    assert response.status_code == 200
    assert response.json() == {"enabled": False}
    assert not (manager.config_dir / "core_config.json").exists()


@pytest.mark.parametrize("endpoint,key", SETTINGS)
@pytest.mark.parametrize("payload", [b"{broken", b"[]", b"null", b'"text"', b"\xff"],
                         ids=["broken-json", "array", "null", "string", "bad-encoding"])
def test_memory_setting_reads_reject_invalid_files(setting_client, endpoint, key, payload):
    client, manager = setting_client
    path = manager.config_dir / "core_config.json"
    path.write_bytes(payload)

    response = client.get(f"/api/memory/{endpoint}")

    assert response.status_code == 503
    assert "enabled" not in response.json()
    assert path.read_bytes() == payload


@pytest.mark.parametrize("endpoint,key", SETTINGS)
@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}])
def test_memory_setting_reads_reject_non_boolean_values(setting_client, endpoint, key, value):
    client, manager = setting_client
    path = manager.config_dir / "core_config.json"
    payload = json.dumps({key: value}).encode("utf-8")
    path.write_bytes(payload)

    response = client.get(f"/api/memory/{endpoint}")

    assert response.status_code == 503
    assert "enabled" not in response.json()
    assert path.read_bytes() == payload


@pytest.mark.parametrize("endpoint,key", SETTINGS)
def test_memory_setting_reads_reject_permission_errors(setting_client, endpoint, key):
    client, manager = setting_client
    path = manager.config_dir / "core_config.json"
    payload = json.dumps({key: False}).encode("utf-8")
    path.write_bytes(payload)
    with patch("builtins.open", side_effect=PermissionError("config read denied")):
        response = client.get(f"/api/memory/{endpoint}")

    assert response.status_code == 503
    assert "enabled" not in response.json()
    assert path.read_bytes() == payload
