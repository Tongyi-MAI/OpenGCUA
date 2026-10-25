"""Focused SDK regression tests using checkout Python and the installed native core."""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


def _load_checkout_package():
    """Load checkout Python modules while resolving _native from the installed wheel."""
    source_root = Path(__file__).resolve().parents[1] / "python"
    source_package = source_root / "mai_ui"
    try:
        wheel_package = Path(
            importlib.metadata.distribution("qwen-intelligence-sdk").locate_file("mai_ui")
        )
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("requires the SDK wheel for its compiled RuntimeCore")

    spec = importlib.machinery.PathFinder.find_spec("mai_ui", [str(source_root)])
    assert spec is not None and spec.submodule_search_locations is not None
    assert Path(spec.origin).resolve() == (source_package / "__init__.py").resolve()
    spec.submodule_search_locations.append(str(wheel_package))
    module = importlib.util.module_from_spec(spec)
    sys.modules["mai_ui"] = module
    spec.loader.exec_module(module)

    from mai_ui import MuError, _native
    from mai_ui.unified._capture import Captured
    from mai_ui.unified.observations import AXObservation, Image
    from mai_ui.unified import ui as ui_module

    assert Path(module.__file__).resolve() == (source_package / "__init__.py").resolve()
    assert Path(ui_module.__file__).resolve() == (source_package / "unified" / "ui.py").resolve()
    assert Path(_native.__file__).resolve().parent == wheel_package.resolve()
    return _native, MuError, Captured, AXObservation, Image, ui_module.AX, ui_module.Vision


def _node(identity, x=10):
    return {
        "id": identity,
        "role": "button",
        "name": identity,
        "actions": [],
        "parent_id": None,
        "window_id": None,
        "app_id": None,
        "class_name": None,
        "automation_id": None,
        "text": None,
        "hint": None,
        "text_status": "unavailable",
        "enabled": True,
        "visible": True,
        "focused": False,
        "editable": False,
        "password": False,
        "bounds": {"x": x, "y": 10, "width": 20, "height": 20},
    }


def _handle(core, scope):
    calls = []

    async def dispatch(action, **kwargs):
        calls.append({"action": action, **kwargs})
        return {"execution": "completed"}

    handle = NS(
        _session=NS(_runtime=NS(_core=core), _in_turn=False, coordinate_space="pixels"),
        _scope=scope,
        _app_id=None,
        _backend=NS(_observations={}, _action=dispatch),
        _ax_values={},
        _image_values={},
        _turn_images={},
        _presented_images={},
        name="probe",
        resource_id=scope,
        platform="linux",
        _check=lambda **kwargs: None,
        _require=lambda capability: None,
    )
    return handle, calls


def _capture(handle, Captured, *, nodes, revision, tree):
    data = {
        "width": 100,
        "height": 100,
        "png": "aW1hZ2U=",
        "rotation": 0,
        "captured_at": 0,
        "revision": revision,
        "source": "probe",
        "truncated": False,
        "identity_mode": "native_lifetime",
        "identity_epoch": "epoch1",
        "geometry": {"x": 0, "y": 0, "width": 100, "height": 100},
        "nodes": nodes,
    }
    return Captured(handle, data, tree=tree)


@pytest.fixture
def sdk():
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "mai_ui" or name.startswith("mai_ui.")
    }
    for name in original_modules:
        if name != "mai_ui._native":
            sys.modules.pop(name, None)

    try:
        _native, MuError, Captured, AXObservation, Image, AX, Vision = _load_checkout_package()
        core = _native.RuntimeCore()
        handle, calls = _handle(core, "probe/window")

        def ax(nodes, revision):
            captured = _capture(handle, Captured, nodes=nodes, revision=revision, tree=True)
            return AXObservation(handle, captured)

        def image(revision):
            captured = _capture(handle, Captured, nodes=[], revision=revision, tree=False)
            return Image(handle, captured)

        yield NS(
            core=core,
            MuError=MuError,
            handle=handle,
            calls=calls,
            ax=ax,
            image=image,
            AX=AX,
            Vision=Vision,
            Captured=Captured,
            AXObservation=AXObservation,
            Image=Image,
        )
    finally:
        for name in tuple(sys.modules):
            if name == "mai_ui" or name.startswith("mai_ui."):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)


def test_removed_ax_node_is_rejected_before_vision_dispatch(sdk):
    old = sdk.ax([_node("removed")], "1").tree.nodes[0]
    sdk.ax([_node("replacement", 60)], "2")
    image = sdk.image("2")

    with pytest.raises(sdk.MuError) as caught:
        asyncio.run(sdk.Vision(sdk.handle).click(old, image=image))

    assert getattr(caught.value, "code", None) == "STALE_TARGET"
    assert sdk.calls == []


def test_current_ax_node_still_maps_and_dispatches(sdk):
    current = sdk.ax([_node("current", 60)], "1").tree.nodes[0]
    image = sdk.image("1")

    asyncio.run(sdk.Vision(sdk.handle).click(current, image=image))

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["action"] == "point_click"
    assert (sdk.calls[0]["x"], sdk.calls[0]["y"]) == (70, 20)


def test_native_lifetime_node_stays_valid_across_revision(sdk):
    old = sdk.ax([_node("stable", 10)], "1").tree.nodes[0]
    sdk.ax([_node("stable", 60)], "2")
    image = sdk.image("2")

    asyncio.run(sdk.Vision(sdk.handle).click(old, image=image))

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["action"] == "point_click"
    assert (sdk.calls[0]["x"], sdk.calls[0]["y"]) == (20, 20)


def test_coordinate_only_click_remains_unchanged(sdk):
    image = sdk.image("1")

    asyncio.run(sdk.Vision(sdk.handle).click(25, 35, image=image))

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["action"] == "point_click"
    assert (sdk.calls[0]["x"], sdk.calls[0]["y"]) == (25, 35)


def test_vision_rejects_ax_node_from_foreign_scope(sdk):
    foreign_handle, _ = _handle(sdk.core, "other/window")
    captured = _capture(
        foreign_handle,
        sdk.Captured,
        nodes=[_node("foreign")],
        revision="1",
        tree=True,
    )
    foreign = sdk.AXObservation(foreign_handle, captured).tree.nodes[0]
    image = sdk.image("1")

    with pytest.raises(sdk.MuError) as caught:
        asyncio.run(sdk.Vision(sdk.handle).click(foreign, image=image))

    assert getattr(caught.value, "code", None) == "INVALID_ARGUMENT"
    assert sdk.calls == []
