import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
import pytest

import code_runtime
import mac_agent
from image_store import save_image
from sdk_backend import Observation


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_webp_preserves_all_pixels_including_transparent_rgb(tmp_path):
    original = Image.new("RGBA", (72, 40), (10, 20, 30, 0))
    original.putpixel((12, 13), (253, 129, 67, 200))
    path = save_image(png(original), tmp_path)
    assert path.suffix == ".webp"
    with Image.open(path) as archived:
        assert archived.size == original.size
        assert archived.convert("RGBA").tobytes() == original.tobytes()
    assert save_image(original, tmp_path) == path
    assert list(tmp_path.iterdir()) == [path]


def test_same_image_concurrently_publishes_one_complete_file(tmp_path):
    data = png(Image.new("RGB", (100, 100), "navy"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: save_image(data, tmp_path), range(8)))
    assert len(set(paths)) == 1
    assert list(tmp_path.iterdir()) == [paths[0]]
    Image.open(paths[0]).load()


def test_different_images_and_high_depth_pixels_are_preserved(tmp_path):
    assert save_image(Image.new("RGB", (4, 4), "red"), tmp_path) != save_image(Image.new("RGB", (4, 4), "blue"), tmp_path)
    original = Image.new("I;16", (4, 4), 40000)
    path = save_image(original, tmp_path)
    assert path.suffix == ".png"
    with Image.open(path) as archived:
        assert [archived.getpixel((x, y)) for x in range(4) for y in range(4)] == [original.getpixel((x, y)) for x in range(4) for y in range(4)]
    wide = save_image(Image.new("RGB", (16384, 1), "navy"), tmp_path)
    assert wide.suffix == ".png"
    with Image.open(wide) as archived:
        assert archived.size == (16384, 1) and archived.getpixel((16383, 0)) == (0, 0, 128)


@pytest.mark.parametrize("sdk", [True, False])
def test_worker_and_trajectory_share_the_same_archive(monkeypatch, tmp_path, sdk):
    config = {"ui_api": "mai-ui"}
    writer = mac_agent.TrajectoryWriter(tmp_path, "fixture", "image-test", config)
    source = Image.new("RGB", (100, 60), "navy")
    events = []
    monkeypatch.setattr(code_runtime, "_CONFIG", {"work_dir": str(writer.work_dir), "image_dir": str(writer.screenshot_dir)})
    monkeypatch.setattr(code_runtime, "_check_active_request", lambda: None)
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    if sdk:
        code_runtime._sdk_output({"type": "image", "data": png(source), "width": 100, "height": 60})
    else:
        code_runtime._emit_image(source, "fixture", events.append)
    event = events[-1]
    path = Path(event["path"])
    assert path.parent == writer.screenshot_dir.resolve()
    before = path.stat().st_ino
    control = {"screenshots": [dict(event)], "events": [dict(event)]}
    paths, notices = mac_agent.collect_emitted_images(control, writer, None)
    assert paths == (path,) and not any("失败" in notice for notice in notices)
    assert path.stat().st_ino == before
    assert not list(writer.work_dir.iterdir())
    assert list(writer.screenshot_dir.iterdir()) == [path]
    assert all(Path(item["path"]).is_file() for key in ("screenshots", "events") for item in control[key])
    assert writer.save_observation(Observation(png(source), "image/png", 100, 60)) == path
    # Current model transports still receive the same JPEG pixels from the archive.
    assert mac_agent.model_image_data_url(path.read_bytes(), max_dimension=0, quality=85) == mac_agent.model_image_data_url(png(source), max_dimension=0, quality=85)
    resumed = mac_agent.TrajectoryWriter(tmp_path, "fixture", "image-test", config, resume_dir=writer.run_dir)
    assert resumed.save_observation(Observation(png(source), "image/png", 100, 60)) == path
    assert list(resumed.screenshot_dir.iterdir()) == [path]


def test_old_pngs_remain_readable_on_resume_and_foreign_paths_are_rejected(tmp_path):
    config = {"ui_api": "mai-ui"}
    writer = mac_agent.TrajectoryWriter(tmp_path, "fixture", "legacy-test", config)
    original = Image.new("RGB", (100, 60), "navy")
    old = writer.screenshot_dir / "image_7.png"
    old.write_bytes(png(original))
    resumed = mac_agent.TrajectoryWriter(tmp_path, "fixture", "legacy-test", config, resume_dir=writer.run_dir)
    path = resumed.save_observation(Observation(png(original), "image/png", 100, 60))
    assert old.read_bytes() == png(original) and path.suffix == ".webp"
    outside = tmp_path / "outside.png"
    outside.write_bytes(png(original))
    paths, notices = mac_agent.collect_emitted_images({"screenshots": [{"path": str(outside)}]}, resumed, None)
    assert not paths and "当前 run" in notices[0]


def test_failed_archive_write_leaves_no_partial_image(monkeypatch, tmp_path):
    import image_store
    def fail(*_):
        raise OSError("disk full")
    monkeypatch.setattr(image_store.os, "link", fail)
    with pytest.raises(OSError, match="disk full"):
        save_image(Image.new("RGB", (20, 20), "navy"), tmp_path)
    assert not list(tmp_path.iterdir())


def test_real_worker_publishes_archive_path_without_runtime_copy(tmp_path):
    import asyncio
    from code_executor import CodeExecutor
    async def scenario():
        worker = tmp_path / "image_worker.py"
        worker.write_text("import sys\nsys.path.insert(0, " + repr(str(Path(code_runtime.__file__).parent)) + ")\nimport code_runtime as rt\nrt._bootstrap = lambda loop: {}\nraise SystemExit(rt.main(sys.argv))\n")
        executor = CodeExecutor(runtime_path=worker, work_dir=tmp_path/'runtime', image_dir=tmp_path/'images',
            rpc_socket=tmp_path/'unused.sock', timeout=10, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api='legacy-cua')
        try:
            result = await executor.run("""import code_runtime as rt
from PIL import Image
rt._emit_image(Image.new('RGB', (100, 60), 'navy'), None, rt._emit)
rt._emit_image(Image.new('RGB', (100, 60), 'navy'), None, rt._emit)
""", [])
            assert result.returncode == 0, result.stderr
            paths = [Path(event['path']) for event in result.control['screenshots']]
            assert len(paths) == 2 and paths[0] == paths[1]
            assert list((tmp_path/'images').iterdir()) == [paths[0]]
            assert not list((tmp_path/'runtime').glob('*.webp'))
        finally:
            await executor.close()
    asyncio.run(scenario())
