"""AX capability provenance through the Python SDK, driven by an offline fake adapter.

Rows mirror the macOS provider projection of Cua 0.36 element rows: `editable` is a
role hint, AXValue settability is never reported, and only a nonempty native action
list is a native report. No driver, TCC, or real application is involved.
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

APP = "app:42:1"


def _row(id, role, *, name="", text=None, parent=None, editable=False, actions=(), **extra):
    row = {
        "id": id, "parent_id": parent, "app_id": APP, "window_id": "w1", "class_name": role,
        "automation_id": None, "role": role, "name": name, "text": text, "hint": None,
        "text_status": "readable", "bounds": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
        "enabled": None, "visible": None, "focused": None, "editable": editable, "password": False,
        "actions": [{"id": a} for a in actions],
    }
    row.update(extra)
    return row


def _macos(id, role, *, native_actions=None, **kw):
    native_actions = bool(kw.get("actions")) if native_actions is None else native_actions
    provenance = {"editable_source": "role_hint", "value_settable": None, "value_settable_source": "unknown",
                  "actions_source": "native" if native_actions else "unknown"}
    return _row(id, role, **{**provenance, **kw})


MACOS_ROWS = [
    # Static PDF label that natively reports AXPress: still not editable or settable.
    _macos("label", "AXStaticText", name="Name:", actions=["macos.ax.press"]),
    # TextField with a native AXPress: editable is only a role hint, settable unknown.
    _macos("pressable", "AXTextField", name="Name", text="", editable=True, actions=["macos.ax.press"]),
    # TextField whose driver row carried no actions: empty list proves nothing.
    _macos("field", "AXTextField", name="City", text="", editable=True),
    # Out-of-contract claims must not become native: unknown settable source is cleared.
    _macos("forged", "AXStaticText", name="Total", native_actions=True,
           value_settable=True, value_settable_source="unknown"),
]

LEGACY_ROWS = [_row("legacy", "textbox", text="", editable=True)]


class Adapter:
    provider = "fixture.ax_metadata"
    platform = "macos"

    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def request(self, message):
        self.requests.append(dict(message))
        op = message["op"]
        if op == "describe":
            caps = ("device.list_apps", "ui.observe.tree", "ui.click.primary", "ui.set_text", "ui.perform")
            return {"result": {"protocol_major": 1, "platform": self.platform,
                               "capabilities": {c: {"state": "ready", "provider": self.provider} for c in caps}}}
        if op == "list_apps":
            return {"result": {"apps": [{"id": APP, "label": "Preview", "launchable": True}], "complete": True}}
        if op == "observe":
            return {"result": {"width": 800, "height": 600, "captured_at": 0, "revision": "r1",
                               "truncated": False, "source": "fixture", "nodes": json.loads(json.dumps(self.rows))}}
        raise AssertionError(f"unexpected adapter op {op}")


def _observe(rows, body):
    from mai_ui import Runtime

    async def scenario():
        adapter = Adapter(rows)
        async with Runtime() as runtime:
            resource = await runtime.add_adapter(adapter, name="fixture", resource_id="macos:fixture")
            async with runtime.session() as session:
                device = await session.register(resource, name="device")
                app = await device.apps.get(APP)
                ax = await app.ui.ax.observe()
                adapter.requests.clear()
                await body(app, ax, adapter)

    asyncio.run(scenario())


def _by_name(ax):
    return {n.id: n for n in ax.tree.nodes}


def test_legacy_payload_defaults_every_capability_field_to_unknown():
    async def body(app, ax, adapter):
        node = _by_name(ax)["legacy"]
        assert node.editable is True
        assert (node.editable_source, node.value_settable, node.value_settable_source, node.actions_source) == (
            "unknown", None, "unknown", "unknown")
        assert 'textbox text="" editable' in ax.text
        assert "role_hint" not in ax.text and "value_settable" not in ax.text
    _observe(LEGACY_ROWS, body)


def test_role_hints_are_never_promoted_and_settable_stays_unknown():
    async def body(app, ax, adapter):
        nodes = _by_name(ax)
        assert all(n.value_settable is None and n.value_settable_source == "unknown" for n in nodes.values())
        assert all(n.editable_source == "role_hint" for n in nodes.values())
        label, pressable, field, forged = (nodes[k] for k in ("label", "pressable", "field", "forged"))
        assert label.editable is False and [a.id for a in label.actions] == ["macos.ax.press"]
        assert label.actions_source == "native"
        assert pressable.editable is True and pressable.actions_source == "native"
        assert field.editable is True and field.actions == () and field.actions_source == "unknown"
        assert forged.actions == () and forged.actions_source == "native"
        lines = ax.text.splitlines()
        assert any(l.endswith('AXStaticText name="Name:" actions=["macos.ax.press"]') for l in lines)
        assert any(l.endswith('AXTextField name="City" text="" editable=role_hint') for l in lines)
        assert any('AXTextField name="Name" text="" editable=role_hint actions=["macos.ax.press"]' in l for l in lines)
        assert "value_settable" not in ax.text
        assert not adapter.requests, "reading metadata must not observe, probe, or write again"
    _observe(MACOS_ROWS, body)


def test_unknown_actions_still_reject_raw_perform_before_the_adapter():
    from mai_ui._errors import MuError

    async def body(app, ax, adapter):
        field = _by_name(ax)["field"]
        with pytest.raises(MuError, match="UNSUPPORTED_CAPABILITY"):
            await app.ui.ax.perform(field, "macos.ax.press")
        label = _by_name(ax)["label"]
        with pytest.raises(MuError, match="UNSUPPORTED_CAPABILITY"):
            await app.ui.ax.perform(label, "macos.ax.scroll_to_visible")
        assert not adapter.requests
    _observe(MACOS_ROWS, body)


def test_node_targets_and_geometry_are_unchanged_by_metadata():
    async def body(app, ax, adapter):
        field = _by_name(ax)["field"]
        assert (field.bounds.x, field.bounds.y, field.bounds.width, field.bounds.height) == (10, 20, 30, 40)
        assert isinstance(field.index, int)
        core, h = app._session._runtime._core, app
        wire = json.loads(core.node_wire(h._scope, ax.id, "field"))
        assert wire["node_id"] == "field" and wire["_node_actions"] == []
        assert not any(k.endswith("_source") or k == "value_settable" for k in wire)
    _observe(MACOS_ROWS, body)


def test_node_dataclass_accepts_old_construction_without_new_fields():
    from mai_ui.types import Node

    node = Node(**{k: v for k, v in LEGACY_ROWS[0].items() if k not in ("bounds", "actions")},
                bounds=None, actions=(), last_verified_at=None, dirty=False)
    assert (node.editable_source, node.value_settable, node.value_settable_source, node.actions_source) == (
        "unknown", None, "unknown", "unknown")


@pytest.mark.parametrize("row, expected", [
    (LEGACY_ROWS[0], ("unknown", None, "unknown", "unknown", 'textbox text="" editable')),
    (MACOS_ROWS[2], ("role_hint", None, "unknown", "unknown", 'AXTextField name="City" text="" editable=role_hint')),
    (MACOS_ROWS[3], ("role_hint", None, "unknown", "native", 'AXStaticText name="Total"')),
    (_row("n", "AXTextField", text="", editable=True, editable_source="native", value_settable=False,
          value_settable_source="native"),
     ("native", False, "native", "unknown", 'AXTextField text="" editable value_settable=false')),
    (_row("g", "AXTextField", text="", editable=True, editable_source="guess", value_settable="yes",
          value_settable_source="native", actions_source="guess"),
     ("unknown", None, "unknown", "unknown", 'AXTextField text="" editable')),
])
def test_legacy_runtime_mirrors_core_provenance_normalization(row, expected):
    from mai_ui import runtime as legacy

    node = legacy._node(row, 0.0, "r1", False)
    obs = object.__new__(legacy.Observation)
    obs._tree = types.SimpleNamespace(nodes=(node,))
    obs._indices = {1: node.id}
    records, _, _ = obs._ax_records()
    assert (node.editable_source, node.value_settable, node.value_settable_source, node.actions_source,
            records[1]) == expected


def test_ax_help_documents_provenance_without_new_helpers():
    from rich_help import build_help

    guide = build_help("ax")
    assert guide.startswith("## AX\n")
    for text in ("editable_source", "role_hint", "value_settable_source", "actions_source", "unknown"):
        assert text in guide
    for helper in ("find(", "inspect(", "wait_for(", "act_checked("):
        assert helper not in guide


def test_macos_contract_declares_conservative_ax_metadata():
    path = Path(__file__).resolve().parents[3] / "sdk/api/contract/python-macos-v0.2.json"
    policies = json.loads(path.read_text())["policies"]
    assert policies["ax_editable_source"] == "role_hint"
    assert policies["ax_value_settable"] == "unknown_not_reported_by_driver"
    assert policies["ax_actions_source"] == "native_when_nonempty_else_unknown"
    assert policies["ax_capability_metadata_default"] == "unknown"
