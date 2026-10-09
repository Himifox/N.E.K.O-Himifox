"""VRM emotion mappings must preserve uploaded filenames and path boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from main_routers import vrm_router


pytestmark = pytest.mark.unit
API = "/api/model/vrm"


@pytest.fixture
def vrm_api(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    (project_root / "static" / "vrm").mkdir(parents=True)
    user_dir = tmp_path / "user_vrm"
    user_dir.mkdir()
    config = SimpleNamespace(
        project_root=project_root,
        vrm_dir=user_dir,
        ensure_vrm_directory=lambda: True,
    )
    monkeypatch.setattr(vrm_router, "get_config_manager", lambda: config)
    monkeypatch.setattr(
        vrm_router,
        "get_subscribed_workshop_items",
        AsyncMock(return_value={"success": True, "items": []}),
    )
    app = FastAPI()
    app.include_router(vrm_router.router)
    with TestClient(app) as client:
        yield client, config


@pytest.mark.parametrize(
    "model_name",
    ["Avatar", "My Avatar", "Avatar(1)", "Avatar[1]", "猫娘", "猫娘 🐱",
     "Cafe\u0301", "Avatar.v1_2-test", "Avatar#100%"],
)
def test_uploaded_model_emotion_mapping_roundtrip(vrm_api, model_name):
    client, config = vrm_api
    filename = f"{model_name}.vrm"
    # Upload routes store opaque bytes; VRM rendering is outside this test.
    content = b"glTF"
    uploaded = client.post(f"{API}/upload", files={"file": (filename, content)})
    assert uploaded.status_code == 200
    assert uploaded.json()["model_name"] == model_name
    models = client.get(f"{API}/models").json()["models"]
    assert any(model["name"] == model_name and model["filename"] == filename
               for model in models)

    url = f"{API}/emotion_mapping/{quote(model_name, safe='')}"
    loaded = client.get(url)
    assert loaded.status_code == 200
    assert loaded.json()["config"] == vrm_router.DEFAULT_MOOD_MAP
    mapping = {"happy": ["custom smile"], "sad": ["custom sadness"]}
    saved = client.post(url, json=mapping)
    assert saved.status_code == 200
    assert saved.json()["success"] is True
    assert client.get(url).json()["config"] == mapping
    config_path = config.project_root / "static" / "vrm" / "configs" / f"{model_name}_emotion.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == mapping
    assert (config.vrm_dir / filename).read_bytes() == content


def test_bundled_model_with_punctuation_can_save_mapping(vrm_api):
    client, config = vrm_api
    model_name = "Built-in Avatar(1)"
    (config.project_root / "static" / "vrm" / f"{model_name}.vrm").write_bytes(b"glTF")
    url = f"{API}/emotion_mapping/{quote(model_name, safe='')}"
    mapping = {"happy": ["custom smile"]}
    assert client.post(url, json=mapping).status_code == 200
    assert client.get(url).json()["config"] == mapping


@pytest.mark.parametrize("names", [("Avatar(1)", "Avatar1"), ("My Avatar", "MyAvatar")])
def test_distinct_model_names_keep_separate_mappings(vrm_api, names):
    client, config = vrm_api
    for index, name in enumerate(names):
        (config.vrm_dir / f"{name}.vrm").write_bytes(b"glTF")
        url = f"{API}/emotion_mapping/{quote(name, safe='')}"
        assert client.post(url, json={"happy": [f"expression-{index}"]}).status_code == 200
    for index, name in enumerate(names):
        url = f"{API}/emotion_mapping/{quote(name, safe='')}"
        assert client.get(url).json()["config"] == {"happy": [f"expression-{index}"]}
    assert len(list((config.project_root / "static" / "vrm" / "configs").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "model_name",
    ["", "../outside", "folder/model", r"folder\model", "/outside",
     r"C:\outside", "C:outside", "model:stream", "model\x00", "model\n",
     "model?", "model*", "<model>", '"model"', "model|stream"],
)
def test_invalid_model_names_are_rejected_without_writes(vrm_api, model_name):
    _, config = vrm_api
    assert vrm_router._get_emotion_config_path(model_name) is None
    assert vrm_router._get_model_path(model_name) == (None, "")
    assert not (config.project_root / "static" / "vrm" / "configs").exists()
    assert not list(config.vrm_dir.iterdir())


def test_missing_model_does_not_create_emotion_mapping(vrm_api):
    client, config = vrm_api
    response = client.post(f"{API}/emotion_mapping/Missing%20Avatar", json={"happy": ["smile"]})
    assert response.status_code == 404
    assert not (config.project_root / "static" / "vrm" / "configs").exists()


@pytest.mark.parametrize("location", ["config", "builtin", "user"])
def test_resolved_paths_cannot_escape_their_directory(vrm_api, tmp_path, location):
    _, config = vrm_api
    name = "My Avatar(1)"
    outside = tmp_path / "outside.vrm"
    outside.write_bytes(b"outside sentinel")
    static_dir = config.project_root / "static" / "vrm"
    if location == "config":
        config_dir = static_dir / "configs"
        config_dir.mkdir()
        link = config_dir / f"{name}_emotion.json"
    else:
        model_dir = static_dir if location == "builtin" else config.vrm_dir
        link = model_dir / f"{name}.vrm"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")

    if location == "config":
        assert vrm_router._get_emotion_config_path(name) is None
    else:
        assert vrm_router._get_model_path(name) == (None, "")
    assert outside.read_bytes() == b"outside sentinel"
