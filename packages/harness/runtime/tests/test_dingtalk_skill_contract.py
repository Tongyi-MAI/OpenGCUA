"""离线契约：钉钉 Skill 文本与加载约束；不启动 App、不触网、不读取真实 runs。"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from action_feedback import _LABELS
from skill_context import SkillContext
from skill_registry import MAX_DESCRIPTION, MAX_SKILL_BYTES, SkillRegistry, default_skill_roots

SKILLS = Path(__file__).resolve().parents[2] / "skills"
FOLDER = SKILLS / "dingtalk-workflow"
DOC = FOLDER / "SKILL.md"
REFERENCE = "meeting-room.md"
NEGATION = re.compile(r"\b(?:not|never|no|don't|nothing)\b", re.IGNORECASE)


def text(name: str = "SKILL.md") -> str:
    return (FOLDER / name).read_text(encoding="utf-8")


def all_text() -> str:
    return text() + "\n" + text(REFERENCE)


def section(title: str) -> str:
    body = text()
    start = body.index(f"## {title}")
    end = body.find("\n## ", start + 1)
    return body[start:] if end == -1 else body[start:end]


@pytest.fixture
def registry(tmp_path):
    bundled, _personal = default_skill_roots(Path(__file__).resolve().parents[1] / "mac_agent.py")
    assert bundled == SKILLS.resolve()
    found = SkillRegistry(bundled, tmp_path / "personal")
    found.discover()
    return found


def test_loadable_by_existing_registry_single_slot_with_reference(registry):
    info = next(item for item in registry.discover() if item.name == "dingtalk-workflow")
    assert not [d for d in registry.diagnostics if "dingtalk" in d]
    assert info.source == "bundled" and len(info.description) <= MAX_DESCRIPTION
    head = DOC.read_text(encoding="utf-8").split("---")[1]
    assert {line.split(":", 1)[0] for line in head.strip().splitlines()} == {"name", "description"}
    selected = SkillContext(registry)
    document = selected.activate("dingtalk-workflow")
    assert selected.system_slot().count(document.body.strip()[:200]) == 1
    with pytest.raises(ValueError, match="单槽"):
        selected.activate("pdf-form")
    assert "offline-contract" in registry.read("dingtalk-workflow", REFERENCE)
    assert f'skills.read("dingtalk-workflow", "{REFERENCE}")' in text()


def test_main_document_is_concise_and_has_required_sections():
    raw = DOC.read_text(encoding="utf-8")
    assert len(raw.splitlines()) < 500 and len(raw.encode("utf-8")) < MAX_SKILL_BYTES
    for title in ("Status", "Choose a route", "Known limits and failures", "Failure recovery",
                  "Automatic action feedback", "Reporting"):
        assert f"## {title}" in raw
    status = section("Status")
    assert "App versions" in status and "Untested" in status


def test_validation_status_is_offline_contract_not_real_gui_verified():
    for name in ("SKILL.md", REFERENCE):
        body = text(name)
        assert "`offline-contract`" in body
        for line in body.splitlines():
            if "real-GUI-verified" in line:
                assert re.search(r"\bnot `real-GUI-verified`", line, re.IGNORECASE), line
    assert "No DingTalk window was inspected" in text()


def test_no_mcp_priority_and_no_auto_install_or_login():
    for line in all_text().splitlines():
        if "MCP" in line or re.search(r"\binstall", line, re.IGNORECASE):
            assert NEGATION.search(line), line
    assert not re.search(r"\b(?:pip3?|brew|npm|npx|uvx|uv)\s+(?:tool\s+)?install\b", all_text())
    assert not re.search(r"\bdws\s+(?:auth\s+)?login\b|\bdws\s+profile\s+(?:use|switch|set)", all_text())


def test_route_order_user_choice_then_gui_then_existing_dws_only():
    routes = section("Choose a route")
    user, gui, dws = (routes.index(key) for key in ("user's instruction comes first",
                                                    "Signed-in DingTalk GUI (default)", "DWS, only if it already works"))
    assert user < gui < dws
    assert "GUI-only" in routes and 'shutil.which("dws")' in routes
    assert "never change the default" in routes
    assert "never say a route is installed or connected unless you checked it" in routes


def test_no_fixed_coordinates_indexes_or_first_candidate():
    body = all_text()
    forbidden = (
        r"\[\s*-?\d+\s*\]",                          # 固定列表索引/首个候选
        r"\.point\(\s*\d",                           # 硬编码截图坐标
        r"\b[xy]\s*=\s*\d",                          # 硬编码 x/y
        r"\(\s*\d{2,4}\s*,\s*\d{2,4}\s*\)",          # 坐标元组
        r"node(?:_id)?\s*[=:]\s*['\"]?\d",           # 旧 node index
    )
    for pattern in forbidden:
        assert not re.search(pattern, body), pattern
    assert "Never take the first match" in body
    assert "Never pick the top hit or the first result" in body
    assert "Never default to the first app or window" in body
    assert "If there are zero candidates, or more than one plausible candidate, stop" in body


def test_send_requires_isolated_authorization_and_target_check():
    send = section("Recipe: send a message")
    assert "A foreground grant, a loaded Skill, an earlier approval for a different message" in send
    assert "is not approval to send" in send
    check, authorize, once = (send.index(key) for key in ("Check the target and the draft",
                                                          "**Authorize.**", "**Send once.**"))
    assert check < authorize < once
    assert "`ask_user` with the recipient, identity, and full text" in send
    assert "by meaning, not by position" in send


def test_post_send_verification_and_unknown_never_resends():
    send = section("Recipe: send a message")
    assert "user's own identity" in send and "intended text" in send and "composer" in send
    assert "never resend" in send and "mu.action_status(aid)" in send
    assert "Do not retry with a new `action_id`" in send
    room = text(REFERENCE)
    assert "Never submit again while a duplicate booking is possible" in room
    assert "mu.action_status(aid)" in room


def test_meeting_room_is_observation_based_with_review_and_missing_info():
    room = text(REFERENCE)
    assert "Use only what the current observation shows" in room
    assert "has no button names, menu paths, mini-app names, or field labels" in room
    assert "Ask once for whatever is missing" in room
    assert room.index("Review before submitting") < room.index("Submit once")
    assert "Still to be verified on a real client" in room
    assert "Do not invent buttons, menus, mini-app names, or field names" in text()


def test_known_failures_from_existing_knowledge_are_stated():
    limits = section("Known limits and failures")
    assert "can read back while DingTalk's own state" in limits
    assert "does not prove the node is a static label" in limits
    assert "editable_source=role_hint" in limits and "value_settable_source=unknown" in limits
    assert "Focus loss is not loss of permission" in limits
    assert "does not revoke the session's foreground grant" in limits
    assert "No DingTalk-specific helper exists" in limits


def test_reuses_d04_feedback_without_upgrading_verification():
    feedback = section("Automatic action feedback")
    for evaluation in _LABELS:
        assert f"`{evaluation}`" in feedback, evaluation
    assert "`verification: unverified`" in feedback and "Do not try to \"upgrade\" them" in feedback
    assert "technical receipt is not business success" in feedback


def test_task_evidence_is_not_permanent_permission():
    permanent = section("What must not become permanent")
    for item in ("preference", "foreground grant", "approval", "node ids or indexes", "coordinates"):
        assert item in permanent
    assert "standing permission" in permanent
    assert "only user confirmation makes it reusable" in permanent


def test_only_documented_sdk_calls_no_invented_helpers():
    allowed = {
        "device.apps.list", "app.windows.list", "ui.ax.observe", "ui.ax.click", "ui.ax.set_text",
        "ui.vision.observe", "ui.vision.click", "ui.input.text", "ui.input.press_key",
        "win.activate", "mu.action_status", "mu.emit", "mu.help", "skills.read", "shutil.which",
    }
    calls = set(re.findall(r"\b((?:device|app|win|ui|mu|skills|shutil)(?:\.[a-z_]+)+)\(", all_text()))
    assert calls and calls <= allowed, calls - allowed
    assert not re.search(r"\b(?:ui|mu|device)\.dingtalk\b|\bdingtalk_helper\b", all_text(), re.IGNORECASE)
