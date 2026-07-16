from pathlib import Path
import tarfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE2D_CORE_PATH = PROJECT_ROOT / "static" / "live2d" / "live2d-core.js"
LIVE2D_MODEL_PATH = PROJECT_ROOT / "static" / "live2d" / "live2d-model.js"
AUDIO_PLAYBACK_PATH = PROJECT_ROOT / "static" / "app" / "app-audio-playback.js"
YUI_ARCHIVE_PATH = PROJECT_ROOT / "assets" / "yui-origin.tar.gz"


def _read_yui_member(member_name: str) -> str:
    with tarfile.open(YUI_ARCHIVE_PATH, "r:gz") as archive:
        member = archive.extractfile(member_name)
        assert member is not None
        return member.read().decode("utf-8")


def test_yui_mouth_form_is_not_registered_as_an_audio_lipsync_parameter():
    yui_model = _read_yui_member("yui-origin/yui-origin.model3.json")
    yui_display_info = _read_yui_member("yui-origin/yui-origin.cdi3.json")
    yui_idle_motion = _read_yui_member("yui-origin/idle1.motion3.json")

    assert '"Name": "LipSync",\n      "Ids": []' in yui_model
    assert '"Id": "ParamMouthForm"' in yui_display_info
    assert '"Id": "ParamMouthOpenY"' in yui_display_info
    assert '"Id": "ParamMouthForm"' in yui_idle_motion
    assert '"Id": "ParamMouthOpenY"' in yui_idle_motion

    core_source = LIVE2D_CORE_PATH.read_text(encoding="utf-8")
    model_source = LIVE2D_MODEL_PATH.read_text(encoding="utf-8")
    assert "    'ParamMouthForm'," not in core_source
    assert "ParamMouthForm', 'ParamMouthOpen" not in model_source
    assert "ParamMouthForm 是嘴形/微笑参数" in model_source


def test_audio_lipsync_removes_silence_noise_and_normalizes_tts_levels():
    source = AUDIO_PLAYBACK_PATH.read_text(encoding="utf-8")

    assert "const LIP_SYNC_NOISE_FLOOR = 0.012;" in source
    assert "const LIP_SYNC_FULL_OPEN_RMS = 0.060;" in source
    assert "const LIP_SYNC_MIN_VISIBLE_OPEN = 0.020;" in source
    assert "(rms - LIP_SYNC_NOISE_FLOOR) / (LIP_SYNC_FULL_OPEN_RMS - LIP_SYNC_NOISE_FLOOR)" in source
    assert "if (mouthOpen < LIP_SYNC_MIN_VISIBLE_OPEN) mouthOpen = 0;" in source
