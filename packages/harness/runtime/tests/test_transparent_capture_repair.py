import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import code_runtime


def _png(color, size=(100, 80)):
    image = Image.new("RGBA", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_valid_sdk_image_is_unchanged(monkeypatch):
    result = {"images": [{"png": _png((255, 0, 0, 255))}]}
    monkeypatch.setattr(code_runtime.subprocess, "run", lambda *a, **k: pytest.fail("unexpected fallback"))
    assert code_runtime._repair_transparent_observation(result) is result


def test_transparent_window_uses_exact_window_capture(monkeypatch):
    result = {"images": [{"png": _png((0, 0, 0, 0)),
                          "target": {"window_id": "window:12345:8"}}]}

    def capture(command, **_kwargs):
        assert command[command.index("-l") + 1] == "12345"
        Image.new("RGBA", (125, 100), (50, 100, 150, 255)).save(command[-1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(code_runtime.subprocess, "run", capture)
    repaired = code_runtime._repair_transparent_observation(result)
    assert repaired is not result
    assert repaired["images"][0]["target"] == result["images"][0]["target"]
    with Image.open(io.BytesIO(base64.b64decode(repaired["images"][0]["png"]))) as image:
        assert image.getpixel((0, 0)) == (50, 100, 150, 255)
    with Image.open(io.BytesIO(base64.b64decode(result["images"][0]["png"]))) as original:
        assert original.getchannel("A").getbbox() is None


def test_transparent_window_without_native_id_fails():
    result = {"images": [{"png": _png((0, 0, 0, 0)), "target": {"window_id": "opaque"}}]}
    with pytest.raises(RuntimeError, match="native window ID"):
        code_runtime._repair_transparent_observation(result)


def test_fallback_rejects_unrelated_window_geometry(monkeypatch):
    result = {"images": [{"png": _png((0, 0, 0, 0)),
                          "target": {"window_id": "window:12345:8"}}]}

    def capture(command, **_kwargs):
        Image.new("RGBA", (150, 80), (50, 100, 150, 255)).save(command[-1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(code_runtime.subprocess, "run", capture)
    with pytest.raises(RuntimeError, match="geometry differs"):
        code_runtime._repair_transparent_observation(result)
