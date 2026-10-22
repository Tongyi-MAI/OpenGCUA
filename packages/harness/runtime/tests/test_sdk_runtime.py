from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest

import code_runtime
import demo_hud
import llm_entry
import mac_agent
import model_stream
from sdk_backend import SdkDesktop, SdkRpcServer


def test_checked_subprocess_failure_sets_cell_status_and_shows_child_stderr(capsys):
    namespace = {"subprocess": subprocess, "sys": sys}
    result = code_runtime._execute(
        "subprocess.run([sys.executable, '-c', "
        "'import sys; sys.stderr.write(str(7 * 7)); sys.exit(7)'], "
        "capture_output=True, text=True, check=True)",
        namespace,
    )
    captured = capsys.readouterr()
    assert result["exit_code"] == 1
    assert result["ok"] is False
    assert "child stderr (diagnostic data): '49'" in captured.err
    assert "exit status 7" in captured.err


def test_point_api_does_not_accept_wall_clock_expiry_keyword():
    import inspect
    from mai_ui.unified.observations import Image
    from mai_ui.unified._capture import Captured
    from mai_ui.runtime import Observation, UI

    for method in (Image.point, Captured.point, Observation.point, UI.click, UI.input_text, UI.scroll):
        assert "max_age_ms" not in inspect.signature(method).parameters


def test_legacy_point_remains_valid_without_age_or_user_boundary_expiry():
    from mai_ui.runtime import PointRef, Session

    session = Session(object())
    ui = session._device.ui
    point = PointRef("image:1", 10, 20, session, "revision:1", ui.surface.id)
    session._refs[id(point)] = point
    assert ui._validate(point)["x"] == 10
    session.begin_turn()
    session.end_turn()
    assert ui._validate(point)["x"] == 10


def test_checked_subprocess_error_limits_child_stderr():
    try:
        raise subprocess.CalledProcessError(3, ["child"], stderr="x" * 10000)
    except subprocess.CalledProcessError as exc:
        rendered = code_runtime._format_exception(exc)
    assert "child stderr (diagnostic data):" in rendered
    assert "[truncated]" in rendered
    assert len(rendered) < 3000


class FakeHttpResponse:
    def __init__(self, data, *, content_type="application/json"):
        self.status_code = 200
        self._data = data
        self.text = ""
        self.headers = {"content-type": content_type}

    async def aread(self):
        return b""

    def json(self):
        return self._data


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return self.response


class FakeAnthropicStream:
    status_code = 200
    text = ""
    headers = {"content-type": "text/event-stream"}

    async def aiter_lines(self):
        events = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
            ("content_block_start", {"type": "content_block_start", "content_block": {"type": "thinking", "thinking": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "plan"}}),
            ("content_block_start", {"type": "content_block_start", "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        import json as _json
        for name, data in events:
            yield f"event: {name}"
            yield "data: " + _json.dumps(data)
            yield ""


class FakeApps:
    def __init__(self) -> None:
        self.timeouts = []

    async def list(self, *, timeout_ms=5000):
        self.timeouts.append(timeout_ms)
        return object()


class FakeDevice:
    def __init__(self) -> None:
        self.ui = object()
        self.apps = FakeApps()


class FakeSession:
    def __init__(self, output) -> None:
        self.output = output
        self.coordinate_space = "pixels"
        self.closed = False
        self.reset_count = 0
        self.device = FakeDevice()

    async def __aenter__(self):
        return self

    async def register(self, resource, *, name):
        rid = resource.id if hasattr(resource, "id") else resource.get("id")
        assert rid == "mac-resource"
        assert name == "mac"
        return self.device

    async def close(self, *, timeout_ms=5000):
        self.closed = True
        return object()

    async def _request(self, device, message):
        return {"ok": True, "echo": message}

    def begin_turn(self):
        pass

    def end_turn(self):
        pass

    def reset_output_context(self):
        self.reset_count += 1


class FakeRuntime:
    instances: list["FakeRuntime"] = []

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.closed = False
        self.added = []
        self.reconnected: list[tuple] = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def add_macos(self, *, name):
        self.added.append(name)
        return {"id": "mac-resource"}

    async def reconnect(self, resource_id, *, timeout_ms=8000, force=False):
        self.reconnected.append((resource_id, force))
        return types.SimpleNamespace(id="mac-resource")

    def session(self, *, output):
        session = FakeSession(output)
        self.sessions.append(session)
        return session

    async def close(self, *, timeout_ms=5000):
        self.closed = True


def install_fake_sdk(monkeypatch):
    module = types.ModuleType("mai_ui")
    module.Runtime = FakeRuntime
    monkeypatch.setitem(sys.modules, "mai_ui", module)
    FakeRuntime.instances.clear()


def cleanup_runtime_globals():
    code_runtime._MAI_SESSION = None
    code_runtime._SDK_RUNTIME = None
    code_runtime._SDK_DEVICE = None
    code_runtime._SDK_RESOURCE = None


def test_rebind_window_finds_non_chrome_window_from_device_inventory():
    rebound = object()

    class DeviceWindows:
        async def list(self, *, timeout_ms):
            assert timeout_ms == 15000
            return types.SimpleNamespace(windows=(
                types.SimpleNamespace(
                    id="window:4242:9", app_id="app:preview", title="trip.pdf"
                ),
            ))

        async def get(self, window_id, *, timeout_ms):
            assert window_id == "window:4242:9"
            assert timeout_ms == 15000
            return rebound

    class Apps:
        async def get(self, app, *, timeout_ms):
            raise AssertionError(f"default rebind must not query a specific app: {app}")

    code_runtime._SDK_DEVICE = types.SimpleNamespace(
        windows=DeviceWindows(), apps=Apps()
    )
    try:
        assert asyncio.run(code_runtime.rebind_window("4242")) is rebound
    finally:
        cleanup_runtime_globals()


@pytest.mark.parametrize("reference", ["window:4242:1", 4242])
def test_rebind_window_accepts_provider_id_or_native_number(reference):
    rebound = object()

    class Windows:
        async def list(self, *, timeout_ms):
            return types.SimpleNamespace(windows=[types.SimpleNamespace(id="window:4242:9")], complete=True)

        async def get(self, window_id, *, timeout_ms):
            assert window_id == "window:4242:9"
            return rebound

    code_runtime._SDK_DEVICE = types.SimpleNamespace(windows=Windows())
    try:
        assert asyncio.run(code_runtime.rebind_window(reference)) is rebound
    finally:
        cleanup_runtime_globals()


def test_rebind_window_accepts_old_window_object_not_attachment_id():
    rebound = object()

    class Windows:
        async def list(self, *, timeout_ms):
            return types.SimpleNamespace(windows=[types.SimpleNamespace(id="window:4242:9")], complete=True)

        async def get(self, window_id, *, timeout_ms):
            assert window_id == "window:4242:9"
            return rebound

    code_runtime._SDK_DEVICE = types.SimpleNamespace(windows=Windows())
    old_window = types.SimpleNamespace(id="64835-18", window_id="window:4242:1")
    try:
        assert asyncio.run(code_runtime.rebind_window(old_window)) is rebound
        with pytest.raises(ValueError, match="attachment"):
            asyncio.run(code_runtime.rebind_window(old_window.id))
    finally:
        cleanup_runtime_globals()


def test_error_playbook_uses_failed_tool_codes_not_stdout_mentions():
    assert mac_agent.error_playbook([]) == ""
    assert mac_agent.error_playbook([{
        "tool": "mai_ui.action.point_click", "ok": True,
        "result": {"message": "DEVICE_OFFLINE"},
    }]) == ""
    result = mac_agent.error_playbook([{
        "tool": "mai_ui.action.point_click", "ok": False,
        "error_code": "DEVICE_OFFLINE", "error": "DEVICE_OFFLINE",
    }])
    assert "SDK endpoint" in result
    assert "rebind_window(旧win)" in result


def test_error_playbook_routes_browser_failures_without_macos_window_advice():
    browser_offline = mac_agent.error_playbook([{
        "tool": "mai_ui.browser.new_tab", "ok": False,
        "arguments": {"platform": "browser"},
        "error_code": "DEVICE_OFFLINE", "error": "broker unavailable",
    }])
    assert "temporarily blocked" in browser_offline and "worker process stopped" in browser_offline
    assert "rebind_window" not in browser_offline

    browser_unsupported = mac_agent.error_playbook([{
        "tool": "mai_ui.browser.new_tab", "ok": False,
        "arguments": {"platform": "browser"},
        "error_code": "UNSUPPORTED_CAPABILITY", "error": "Playwright missing",
    }])
    assert "window.capabilities()" not in browser_unsupported
    assert "mu setup browser" in browser_unsupported

    mac_unsupported = mac_agent.error_playbook([{
        "tool": "mai_ui.action.set_text", "ok": False,
        "arguments": {"platform": "macos"},
        "error_code": "UNSUPPORTED_CAPABILITY", "error": "not supported",
    }])
    assert "window.capabilities()" in mac_unsupported

    phone_offline = mac_agent.error_playbook([{
        "tool": "mai_ui.action.point_click", "ok": False,
        "arguments": {"platform": "android"},
        "error_code": "DEVICE_OFFLINE", "error": "ADB unavailable",
    }])
    assert "rebind_window" not in phone_offline


def test_context_prompt_never_combines_background_and_demo_modes(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.setenv("MAC_AGENT_DEMO", "1")
    assert mac_agent.active_prompt_mode() == "demo"
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=1, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80,
    )
    assert "演示/前台模式（已开启" in context.system_prompt
    assert "后台工作模式（已开启" not in context.system_prompt


def test_background_mode_is_default_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("MAC_AGENT_BACKGROUND", raising=False)
    monkeypatch.delenv("MAC_AGENT_DEMO", raising=False)
    parser = mac_agent.build_parser()
    assert parser.parse_args([]).background_mode is True
    assert parser.parse_args(["--disable-background-mode"]).background_mode is False
    assert parser.parse_args(["--background-mode"]).background_mode is True


def test_background_prompt_uses_ax_first_and_one_consented_takeover(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.delenv("MAC_AGENT_DEMO", raising=False)
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=1, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80,
    )
    prompt = context.system_prompt
    assert "device.apps.launch" in prompt
    assert "AX" in prompt and "后台投递" in prompt
    assert "前台接管" in prompt
    assert "不必提前 ask_user" in prompt
    assert "本任务内跨窗口复用" in prompt
    assert "后台 SDK vision.click、AX 和网页 DOM 操作不触发前台授权" in prompt
    assert "无头" not in mac_agent.BACKGROUND_MODE_NOTE


def test_background_isolated_browser_is_headed_and_uses_sdk_factory(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.delenv("MAC_AGENT_DEMO", raising=False)
    monkeypatch.setenv("MAC_AGENT_WEB_PROFILE", "isolated")
    calls = []

    class Runtime:
        async def add_browser_factory(self, *args, **kwargs):
            calls.append((args, kwargs))

    asyncio.run(code_runtime._add_web_factory(Runtime()))
    assert calls == [(("web",), {"engine": "chromium", "headless": False,
                                 "channel": "chrome", "background_launch": True})]

    calls.clear()
    monkeypatch.delenv("MAC_AGENT_BACKGROUND")
    assert asyncio.run(code_runtime._add_web_factory(Runtime())) == "web-visible"
    assert calls == [(("web-visible",), {"engine": "chromium", "headless": False,
                                         "channel": "chrome", "background_launch": False})]


def test_web_factory_is_registered_once_per_runtime(monkeypatch):
    from mai_ui import MuError

    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.setenv("MAC_AGENT_WEB_PROFILE", "isolated")
    calls = []

    class Runtime:
        async def add_browser_factory(self, name, **_kwargs):
            calls.append(name)
            if len(calls) > 1:
                raise MuError("ALREADY_REGISTERED")

    runtime = Runtime()
    assert asyncio.run(code_runtime._add_web_factory(runtime)) == "web"
    assert asyncio.run(code_runtime._add_web_factory(runtime)) == "web"
    assert calls == ["web"]
    # A registration left by an earlier runtime wrapper is reused, not an error.
    assert asyncio.run(code_runtime._add_web_factory(Runtime())) == "web"


def test_app_never_falls_back_to_another_browser(monkeypatch, tmp_path, capsys):
    shim_starts = []

    class Browsers:
        async def create(self, **_kwargs):
            raise RuntimeError("DEVICE_OFFLINE: worker.js exited (exit status: 1)")

    async def add_factory(_runtime):
        return "web"

    async def create_shim(**_kwargs):
        shim_starts.append(1)

    monkeypatch.setenv("MAC_AGENT_WEB_SHIM", "0")
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    monkeypatch.delenv("MAC_AGENT_REQUIRE_NATIVE_WEB", raising=False)
    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    monkeypatch.setitem(sys.modules, "web_shim", types.SimpleNamespace(create_web=create_shim))
    session = types.SimpleNamespace(browsers=Browsers())
    assert asyncio.run(code_runtime._create_web_resource(object(), session)) is None
    assert shim_starts == []
    assert "exit status: 1" in code_runtime._WEB_UNAVAILABLE
    assert "不会改用其他浏览器" in capsys.readouterr().out


def test_app_disables_shim_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MAC_AGENT_WEB_SHIM", raising=False)
    monkeypatch.setattr(code_runtime, "__file__", str(
        tmp_path / "OpenGCUA.app/Contents/Resources/runtime/code_runtime.py"))
    assert code_runtime._web_shim_allowed() is False
    monkeypatch.setenv("MAC_AGENT_WEB_SHIM", "1")
    assert code_runtime._web_shim_allowed() is True


def test_browser_worker_log_goes_to_the_run_directory(monkeypatch, tmp_path):
    class Browsers:
        async def create(self, **_kwargs):
            return object()

    async def add_factory(_runtime):
        return "web"

    monkeypatch.setattr(code_runtime, "_CONFIG", {"work_dir": str(tmp_path)})
    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    monkeypatch.delenv("MAI_UI_PROVIDER_LOG", raising=False)
    asyncio.run(code_runtime._create_web_resource(object(), types.SimpleNamespace(browsers=Browsers())))
    assert code_runtime.os.environ["MAI_UI_PROVIDER_LOG"] == str(tmp_path / "browser.log")
    assert "[host] SDK browser created" in (tmp_path / "browser.log").read_text()
    assert (tmp_path / "browser.log").stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv("MAI_UI_PROVIDER_LOG")


def test_source_checkout_prefers_its_current_browser_worker(monkeypatch, tmp_path):
    runtime_file = tmp_path / "packages/harness/runtime/code_runtime.py"
    runtime_file.parent.mkdir(parents=True)
    worker = tmp_path / "packages/sdk/adapters/browser-playwright/dist/worker.js"
    worker.parent.mkdir(parents=True)
    worker.write_text("// compiled worker")
    playwright = worker.parent.parent / "node_modules/playwright/package.json"
    playwright.parent.mkdir(parents=True)
    playwright.write_text("{}")
    monkeypatch.setattr(code_runtime, "__file__", str(runtime_file))
    monkeypatch.delenv("MAI_UI_PLAYWRIGHT_WORKER", raising=False)

    code_runtime._prefer_local_browser_worker()
    assert code_runtime.os.environ["MAI_UI_PLAYWRIGHT_WORKER"] == str(worker)

    monkeypatch.setenv("MAI_UI_PLAYWRIGHT_WORKER", "/explicit/worker.js")
    code_runtime._prefer_local_browser_worker()
    assert code_runtime.os.environ["MAI_UI_PLAYWRIGHT_WORKER"] == "/explicit/worker.js"


def test_app_bundle_uses_its_own_browser_worker_and_playwright_node(monkeypatch, tmp_path):
    resources = tmp_path / "OpenGCUA.app/Contents/Resources"
    runtime_file = resources / "runtime/code_runtime.py"
    runtime_file.parent.mkdir(parents=True)
    worker = resources / "browser-worker/dist/worker.js"
    worker.parent.mkdir(parents=True)
    worker.write_text("// bundled worker")
    package = resources / "browser-worker/node_modules/playwright/package.json"
    package.parent.mkdir(parents=True)
    package.write_text("{}")
    node = tmp_path / "driver-node"
    node.write_text("")
    node.chmod(0o755)
    monkeypatch.setattr(code_runtime, "__file__", str(runtime_file))
    monkeypatch.setattr(code_runtime, "_playwright_driver_node", lambda: node)
    monkeypatch.delenv("MAI_UI_PLAYWRIGHT_WORKER", raising=False)
    monkeypatch.delenv("MAI_UI_NODE", raising=False)

    code_runtime._prefer_local_browser_worker()
    assert code_runtime.os.environ["MAI_UI_PLAYWRIGHT_WORKER"] == str(worker)
    assert code_runtime.os.environ["MAI_UI_NODE"] == str(node)


def test_background_session_binds_its_browser_when_the_session_opens(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    install_fake_sdk(monkeypatch)
    cleanup_runtime_globals()
    launches = []

    async def create_web(*_args, **_kwargs):
        launches.append(1)
        return object()

    monkeypatch.setattr(code_runtime, "_create_web_resource", create_web)
    try:
        runtime, resource, session, device = asyncio.run(code_runtime._open_sdk_session())
        assert code_runtime._SDK_WEB is not None
        assert launches == [1]
    finally:
        asyncio.run(code_runtime._close_sdk_session(close_runtime=True))
        cleanup_runtime_globals()


def test_runtime_mode_follows_each_task_in_native_host(monkeypatch):
    monkeypatch.delenv("MAC_AGENT_BACKGROUND", raising=False)
    monkeypatch.delenv("MAC_AGENT_DEMO", raising=False)
    args = mac_agent.build_parser().parse_args([])
    mac_agent.configure_runtime_mode(args)
    assert mac_agent.active_prompt_mode() == "background"
    args.demo_mode = True
    mac_agent.configure_runtime_mode(args)
    assert mac_agent.active_prompt_mode() == "demo"
    args.demo_mode = False
    args.background_mode = False
    mac_agent.configure_runtime_mode(args)
    assert mac_agent.active_prompt_mode() == "balanced"


def test_legacy_prompt_does_not_receive_sdk_mode_instructions(monkeypatch):
    monkeypatch.delenv("MAC_AGENT_BACKGROUND", raising=False)
    monkeypatch.setenv("MAC_AGENT_DEMO", "1")
    options = dict(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=1, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80, ui_api="legacy-cua",
    )
    context = mac_agent.ContextManager(**options)
    assert "delivery=\"foreground\"" not in context.system_prompt
    assert "演示/前台模式（已开启" in context.system_prompt

    monkeypatch.delenv("MAC_AGENT_DEMO", raising=False)
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    with pytest.raises(ValueError, match="legacy-cua.*background"):
        mac_agent.ContextManager(**options)


def test_web_creation_uses_timeout_accepted_by_sdk_host(monkeypatch, tmp_path):
    calls = []
    expected_web = object()

    async def add_factory(_runtime, *, visible=False):
        pass

    class Browsers:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return expected_web

    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    session = types.SimpleNamespace(browsers=Browsers())
    assert asyncio.run(code_runtime._create_web_resource(object(), session)) is expected_web
    assert calls == [{
        "name": "web", "provider": "web", "storage_state": None,
        "timeout_ms": 30000,
    }]


def test_required_native_web_does_not_fall_back_to_incompatible_shim(monkeypatch, tmp_path):
    async def add_factory(_runtime, *, visible=False):
        pass

    class Browsers:
        async def create(self, **_kwargs):
            raise ValueError("browser endpoint failed")

    monkeypatch.setenv("MAC_AGENT_REQUIRE_NATIVE_WEB", "1")
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    session = types.SimpleNamespace(browsers=Browsers())
    with pytest.raises(RuntimeError, match="native SDK browser unavailable"):
        asyncio.run(code_runtime._create_web_resource(object(), session))


def test_web_fallback_retries_sdk_first_on_every_start(monkeypatch, tmp_path):
    attempts = []
    shim_starts = []
    shim = types.SimpleNamespace(resource_id="browser:web-shim")

    class Browsers:
        async def create(self, **_kwargs):
            attempts.append(1)
            raise RuntimeError("browser worker exited")

    async def add_factory(_runtime, *, visible=False):
        pass

    async def create_shim(**_kwargs):
        shim_starts.append(1)
        return shim

    monkeypatch.setenv("MAC_AGENT_WEB_SHIM", "1")
    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    monkeypatch.setitem(sys.modules, "web_shim", types.SimpleNamespace(create_web=create_shim))
    monkeypatch.setattr(code_runtime, "_start_keepalive", lambda: None)
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    code_runtime._SDK_REVIVE_PENDING = False
    session = types.SimpleNamespace(browsers=Browsers())
    try:
        assert asyncio.run(code_runtime._create_web_resource(object(), session)) is shim
        assert asyncio.run(code_runtime._create_web_resource(object(), session)) is shim
        # 一次失败不永久降级：每次启动都先试 SDK。
        assert len(attempts) == 2
        assert code_runtime._SDK_REVIVE_PENDING is False

        cleanup_runtime_globals()
        runtime = FakeRuntime()
        runtime.session = lambda *, output: FakeSession(output)
        code_runtime._SDK_RUNTIME = runtime
        code_runtime._SDK_RESOURCE = types.SimpleNamespace(id="mac-resource")
        code_runtime._MAI_SESSION = None
        code_runtime._SDK_WEB = shim
        code_runtime._EXEC_NAMESPACE = None
        asyncio.run(code_runtime._reset_sdk_session())
        assert code_runtime._SDK_WEB is shim
        assert len(attempts) == 2
        assert len(shim_starts) == 2  # initial call and retry above; reset reuses the live shim
    finally:
        cleanup_runtime_globals()
        code_runtime._SDK_WEB = None


def test_web_recreation_uses_timeout_accepted_by_sdk_host(monkeypatch, tmp_path):
    calls = []

    class Browsers:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return object()

    original_session = FakeRuntime.session

    def session_with_browser(runtime, *, output):
        session = original_session(runtime, output=output)
        session.browsers = Browsers()
        return session

    async def add_factory(_runtime, *, visible=False):
        pass

    monkeypatch.setattr(FakeRuntime, "session", session_with_browser)
    monkeypatch.setattr(code_runtime, "_add_web_factory", add_factory)
    monkeypatch.setattr(code_runtime, "_start_keepalive", lambda: None)
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "absent.json")
    monkeypatch.delenv("MAC_AGENT_WEB", raising=False)
    cleanup_runtime_globals()
    code_runtime._SDK_WEB = types.SimpleNamespace(resource_id="browser:web")
    code_runtime._EXEC_NAMESPACE = None
    code_runtime._SDK_RUNTIME = FakeRuntime()
    code_runtime._SDK_RESOURCE = types.SimpleNamespace(id="mac-resource")
    try:
        asyncio.run(code_runtime._reset_sdk_session())
        assert calls == [{
            "name": "web", "provider": "web", "storage_state": None,
            "timeout_ms": 30000,
        }]
    finally:
        cleanup_runtime_globals()
        code_runtime._SDK_WEB = None


def test_sdk_worker_opens_and_resets_real_sdk_shape(monkeypatch):
    install_fake_sdk(monkeypatch)
    cleanup_runtime_globals()

    runtime, resource, session, device = asyncio.run(code_runtime._open_sdk_session())
    assert isinstance(runtime, FakeRuntime)
    assert resource == {"id": "mac-resource"}
    assert session.coordinate_space == "normalized_1000"
    assert device is session.device
    assert device.apps.timeouts == [30000]

    code_runtime._SDK_RUNTIME = runtime
    code_runtime._SDK_RESOURCE = resource
    code_runtime._MAI_SESSION = session
    code_runtime._SDK_DEVICE = device
    new_session, new_device = asyncio.run(code_runtime._reset_sdk_session())
    assert session.closed is True
    assert new_session is runtime.sessions[-1] and new_session is not session
    assert new_session.coordinate_space == "normalized_1000"
    assert new_device is new_session.device
    assert new_device.apps.timeouts == [30000]
    # auto_reconnect must be enabled so a DEVICE_OFFLINE fence rebuilds transparently.
    assert session.auto_reconnect is True and new_session.auto_reconnect is True

    asyncio.run(code_runtime._close_sdk_session(close_runtime=True))
    assert new_session.closed is True and runtime.closed is True
    cleanup_runtime_globals()


def test_reset_sdk_session_rebuilds_offline_endpoint(monkeypatch):
    # A DEVICE_OFFLINE fence is latched on the Rust endpoint; reset must reconnect it,
    # not merely open a new Session over the same (still-offline) endpoint.
    install_fake_sdk(monkeypatch)
    cleanup_runtime_globals()
    runtime = FakeRuntime()
    code_runtime._SDK_RUNTIME = runtime
    code_runtime._SDK_RESOURCE = types.SimpleNamespace(id="mac-resource")
    code_runtime._MAI_SESSION = None
    new_session, new_device = asyncio.run(code_runtime._reset_sdk_session())
    assert runtime.reconnected == [("mac-resource", True)]
    assert new_session.auto_reconnect is True
    assert new_device is new_session.device
    cleanup_runtime_globals()


def test_bootstrap_injects_actual_sdk_handles(monkeypatch):
    install_fake_sdk(monkeypatch)
    cleanup_runtime_globals()
    loop = asyncio.new_event_loop()
    try:
        namespace = code_runtime._bootstrap(loop)
        assert namespace["mu"] is code_runtime._MAI_SESSION
        assert namespace["device"] is code_runtime._SDK_DEVICE
        assert namespace["ui"] is code_runtime._SDK_DEVICE.ui
        assert isinstance(namespace["skills"], code_runtime.SkillRuntime)
        assert "cua" not in namespace and "pyautogui" not in namespace and "emit" not in namespace
        loop.run_until_complete(code_runtime._close_sdk_session(close_runtime=True))
    finally:
        loop.close()
        cleanup_runtime_globals()


def test_device_offline_marks_pending_revive_without_swallowing_error(monkeypatch):
    from mai_ui import MuError

    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    code_runtime._CONFIG = {"ui_api": "mai-ui"}
    code_runtime._REQUEST_CONTEXT.request_id = "r1"
    code_runtime._SDK_REVIVE_PENDING = False
    token = code_runtime._ACTIVE_REQUEST.set({"active": True, "request_id": "r1"})

    class OfflineSession(FakeSession):
        async def _request(self, device, message):
            raise MuError("DEVICE_OFFLINE", "endpoint fenced")

    try:
        session = OfflineSession(events.append)
        code_runtime._instrument_sdk_session(session)
        with pytest.raises(MuError):
            asyncio.run(session._request(session.device, {"op": "observe"}))
        assert code_runtime._SDK_REVIVE_PENDING is True
        assert "endpoint_offline_before=" in events[-1]["error"]
        assert "last_gui_success_age_seconds=" in events[-1]["error"]

        # 浏览器标签被阻塞不是 Mac 端点离线：重建 Mac 会话会把浏览器一起关掉。
        code_runtime._SDK_REVIVE_PENDING = False
        tab = types.SimpleNamespace(platform="browser", resource_id="browser:1/tab:1")
        with pytest.raises(MuError):
            asyncio.run(session._request(tab, {"op": "browser", "operation": "evaluate"}))
        assert code_runtime._SDK_REVIVE_PENDING is False
    finally:
        code_runtime._ACTIVE_REQUEST.reset(token)
        del code_runtime._REQUEST_CONTEXT.request_id
        code_runtime._SDK_REVIVE_PENDING = False


def test_sdk_request_instrumentation_emits_tool_audit(monkeypatch):
    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    code_runtime._CONFIG = {"ui_api": "mai-ui"}
    code_runtime._REQUEST_CONTEXT.request_id = "r1"
    token = code_runtime._ACTIVE_REQUEST.set({"active": True, "request_id": "r1"})
    try:
        session = FakeSession(events.append)
        code_runtime._instrument_sdk_session(session)
        result = asyncio.run(session._request(session.device, {"op": "action", "action": "click"}))
    finally:
        code_runtime._ACTIVE_REQUEST.reset(token)
        del code_runtime._REQUEST_CONTEXT.request_id
    assert result["ok"] is True
    assert events[-1]["type"] == "tool_call"
    assert events[-1]["tool"] == "mai_ui.action.click"
    assert events[-1]["ok"] is True


@pytest.mark.parametrize("action,delivery,protected", [
    ("point_click", "background", False),
    ("point_click", "foreground", True),
    ("activate", None, True),
    ("input_text", "foreground", True),
    ("press_key", "foreground", True),
    ("drag", "foreground", True),
    ("move", "foreground", True),
    ("click", "background", False),
    ("set_text", "background", False),
])
def test_gui_action_scope_classifies_protected_dispatch(
    action, delivery, protected,
):
    device = types.SimpleNamespace(platform="macos", _app_id="app:a", _window_id="window:7")
    message = {"op": "action", "action": action}
    if delivery is not None:
        message["delivery"] = delivery
    result = code_runtime.gui_action_scope(device, message)
    assert result == ("app:a", "window:7") if protected else result is None


def test_protected_sdk_action_waits_for_approval_and_denial_never_dispatches(monkeypatch):
    from mai_ui import MuError
    sequence = []

    class Session:
        async def _request(self, device, message):
            sequence.append("dispatch")
            return {"execution": "completed"}

    async def deny(*_args, **_kwargs):
        sequence.append("approval")
        return False

    monkeypatch.setattr(code_runtime, "_CONFIG", {"gui_approval": True, "overlay_guard": False})
    monkeypatch.setattr(code_runtime, "_request_gui_approval", deny)
    monkeypatch.setattr(code_runtime, "_record_tool", lambda *a, **k: None)
    session = Session()
    code_runtime._instrument_sdk_session(session)
    device = types.SimpleNamespace(platform="macos", _app_id="app:a", _window_id="window:7")
    with pytest.raises(MuError, match="Foreground input denied"):
        asyncio.run(session._request(device, {
            "op": "action", "action": "point_click", "delivery": "foreground",
        }))
    assert sequence == ["approval"]


def test_background_ax_action_dispatches_without_approval(monkeypatch):
    sequence = []

    class Session:
        async def _request(self, device, message):
            sequence.append("dispatch")
            return {"execution": "completed"}

    async def deny(*_args, **_kwargs):
        sequence.append("approval")
        return False

    monkeypatch.setattr(code_runtime, "_CONFIG", {"gui_approval": True, "overlay_guard": False})
    monkeypatch.setattr(code_runtime, "_request_gui_approval", deny)
    monkeypatch.setattr(code_runtime, "_record_tool", lambda *a, **k: None)
    session = Session()
    code_runtime._instrument_sdk_session(session)
    device = types.SimpleNamespace(platform="macos", _app_id="app:a", _window_id="window:7")
    asyncio.run(session._request(device, {
        "op": "action", "action": "set_text", "delivery": "background",
    }))
    asyncio.run(session._request(device, {
        "op": "action", "action": "point_click", "delivery": "background",
    }))
    assert sequence == ["dispatch", "dispatch"]


@pytest.mark.parametrize("app_id,window_id,message", [
    ("app:a", None, {"op": "action", "action": "point_click", "delivery": "foreground"}),
    ("app:a", None, {"op": "action", "action": "point_click",
                      "delivery": "foreground", "window_id": "window:7"}),
    ("app:a", "window:7", {"op": "action", "action": "input_text",
                            "delivery": "foreground", "window_id": "window:8"}),
])
def test_protected_action_refuses_missing_or_mismatched_window_before_approval(
    monkeypatch, app_id, window_id, message,
):
    from mai_ui import MuError
    calls = []

    class Session:
        async def _request(self, device, query):
            calls.append("dispatch")
            return {"execution": "completed"}

    async def approve(*_args, **_kwargs):
        calls.append("approval")
        return True

    monkeypatch.setattr(code_runtime, "_CONFIG", {"gui_approval": True})
    monkeypatch.setattr(code_runtime, "_request_gui_approval", approve)
    session = Session()
    code_runtime._instrument_sdk_session(session)
    device = types.SimpleNamespace(platform="macos", _app_id=app_id, _window_id=window_id)
    with pytest.raises(MuError, match="exact bound app/window"):
        asyncio.run(session._request(device, message))
    assert calls == []


def test_sdk_output_translates_text_and_image(monkeypatch, tmp_path):
    import io
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (10, 20), "navy").save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    code_runtime._CONFIG = {"ui_api": "mai-ui", "work_dir": str(tmp_path)}
    code_runtime._REQUEST_CONTEXT.request_id = "r1"
    token = code_runtime._ACTIVE_REQUEST.set({"active": True, "request_id": "r1"})
    try:
        code_runtime._sdk_output({"type": "text", "text": "AX view"})
        code_runtime._sdk_output({
            "type": "image", "data": image_bytes, "mime_type": "image/png",
            "image_id": "image-1", "width": 10, "height": 20,
            "coordinate_space": "normalized_1000", "caption": "window",
        })
    finally:
        code_runtime._ACTIVE_REQUEST.reset(token)
        del code_runtime._REQUEST_CONTEXT.request_id

    assert events[0]["type"] == "display" and events[0]["text"] == "AX view"
    assert events[1]["type"] == "screenshot"
    path = Path(events[1]["path"])
    assert path.suffix == ".webp"
    with Image.open(path) as image:
        assert image.size == (10, 20)
        assert image.convert("RGB").tobytes() == Image.new("RGB", (10, 20), "navy").tobytes()
    assert events[1]["visual_ref"] == "image-1"
    assert events[1]["coordinate_space"] == "normalized_1000"


def test_idle_worker_tick_does_not_accumulate_execution_tasks():
    loop = asyncio.new_event_loop()
    before = set(code_runtime._EXECUTION_TASKS)
    try:
        loop.set_task_factory(code_runtime._task_factory)
        for _ in range(10):
            code_runtime._drive_worker_loop_once(loop)
        assert code_runtime._EXECUTION_TASKS == before
    finally:
        loop.close()


def test_periodic_keepalive_is_silent_and_retains_the_session(monkeypatch):
    async def scenario():
        requests = []
        async def request(message):
            requests.append(message)
            return {"alive": True}
        device = types.SimpleNamespace(_backend=types.SimpleNamespace(_request=request))
        session = object()
        monkeypatch.setattr(code_runtime, "_SDK_KEEPALIVE", None)
        monkeypatch.setattr(code_runtime, "_SDK_DEVICE", device)
        monkeypatch.setattr(code_runtime, "_MAI_SESSION", session)
        monkeypatch.setattr(code_runtime, "_SDK_LAST_KEEPALIVE_ERROR", "old error")
        code_runtime._start_keepalive(interval=.01)
        task = code_runtime._SDK_KEEPALIVE
        try:
            for _ in range(30):
                if len(requests) >= 3:
                    break
                await asyncio.sleep(.01)
            assert len(requests) >= 3
            assert all(message == {"op": "keepalive", "timeout_ms": 5000} for message in requests)
            assert code_runtime._SDK_LAST_KEEPALIVE_ERROR is None
            assert code_runtime._MAI_SESSION is session
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_keepalive_failure_does_not_fall_back_to_visible_app_inventory(monkeypatch):
    async def scenario():
        requests = []
        async def request(message):
            requests.append(message)
            raise RuntimeError("offline fixture")
        device = types.SimpleNamespace(_backend=types.SimpleNamespace(_request=request))
        monkeypatch.setattr(code_runtime, "_SDK_KEEPALIVE", None)
        monkeypatch.setattr(code_runtime, "_SDK_DEVICE", device)
        monkeypatch.setattr(code_runtime, "_MAI_SESSION", object())
        monkeypatch.setattr(code_runtime, "_SDK_LAST_KEEPALIVE_ERROR", None)
        code_runtime._start_keepalive(interval=.01)
        task = code_runtime._SDK_KEEPALIVE
        try:
            for _ in range(30):
                if code_runtime._SDK_LAST_KEEPALIVE_ERROR:
                    break
                await asyncio.sleep(.01)
            assert requests and all(message["op"] == "keepalive" for message in requests)
            assert code_runtime._SDK_LAST_KEEPALIVE_ERROR == "offline fixture"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["macos", "android"])
def test_native_runtime_routes_keepalive_only_to_a_whole_mac_endpoint(platform):
    from mai_ui import Runtime
    from mai_ui._errors import MuError
    class Adapter:
        provider = "fixture.keepalive"
        async def start(self): self.requests = []
        async def close(self): pass
        async def request(self, message):
            self.requests.append(dict(message))
            if message["op"] == "describe":
                return {"result": {"protocol_major": 1, "platform": self.platform, "capabilities": {}}}
            return {"result": {"alive": True}}
    async def scenario():
        adapter = Adapter(); adapter.platform = platform
        async with Runtime() as runtime:
            resource = await runtime.add_adapter(adapter, name="fixture", resource_id=platform+":fixture")
            async with runtime.session() as session:
                device = await session.register(resource, name="device")
                adapter.requests.clear()
                if platform == "macos":
                    assert await device._backend._request({"op": "keepalive", "timeout_ms": 5000}) == {"alive": True}
                    assert len(adapter.requests) == 1 and adapter.requests[0]["op"] == "keepalive"
                    adapter.requests.clear()
                    with pytest.raises(MuError, match="UNSUPPORTED_CAPABILITY"):
                        await device._backend._request({"op": "keepalive", "app_id": "app:fixture", "timeout_ms": 5000})
                else:
                    with pytest.raises(MuError, match="UNSUPPORTED_CAPABILITY"):
                        await device._backend._request({"op": "keepalive", "timeout_ms": 5000})
                assert not adapter.requests, "Rejected heartbeat scopes must never reach the adapter"
    asyncio.run(scenario())


def test_sync_client_close_cancels_owned_loop_tasks(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False
            self.cancelled = False
            self.background = None

        async def _background(self):
            try:
                await asyncio.sleep(3600)
            finally:
                self.cancelled = True

        async def __aenter__(self):
            self.background = asyncio.create_task(self._background())
            return self

        async def __aexit__(self, *_exc):
            self.closed = True

    fake = FakeClient()
    monkeypatch.setattr(llm_entry, "build_model_client", lambda *_a, **_k: fake)
    client = llm_entry.SyncClient({})
    client.close()
    client.close()  # idempotent shutdown is important for nested cleanup paths
    assert fake.closed is True
    assert fake.cancelled is True
    assert fake.background.done() is True


def test_demo_hud_renders_sdk_audit_calls():
    observe = demo_hud._cua_action_view({
        "tool": "mai_ui.observe", "arguments": {"tree": True, "window_id": "w1"},
        "ok": True, "result": {"elapsed_seconds": 1.25},
    }, 1)
    click = demo_hud._cua_action_view({
        "tool": "mai_ui.action.click", "arguments": {"node_id": "n1", "window_id": "w1"},
        "ok": True, "result": {"elapsed_seconds": 0.4},
    }, 2)
    assert (observe["title"], observe["elapsed"]) == ("AX observe", 1.25)
    assert click["title"] == "SDK click" and click["icon"] == "♿"


def test_degenerate_screenshot_is_detected_and_below_model_minimum():
    # A 2px capture makes qwen reject the whole request with HTTP 400 and poisons
    # every retry; the harness must catch it before it reaches the model.
    import io as _io

    from PIL import Image

    def png(w, h):
        buf = _io.BytesIO()
        Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
        return buf.getvalue()

    assert mac_agent.image_pixel_size(png(2, 2)) == (2, 2)
    assert mac_agent.image_pixel_size(png(120, 60)) == (120, 60)
    assert mac_agent.image_pixel_size(b"not an image") == (0, 0)
    # 2px is degenerate, a normal window capture is not.
    assert 2 < mac_agent.MIN_MODEL_IMAGE_DIM <= 60


def test_agent_defaults_to_sdk_backend_and_canonical_coordinates(tmp_path):
    args = mac_agent.build_parser().parse_args(["task"])
    assert args.driver_mode == "sdk"
    assert 'space="normalized_1000"' in mac_agent.MAI_UI_SYSTEM_PROMPT
    assert 'space="normalized")' not in mac_agent.MAI_UI_SYSTEM_PROMPT
    # ax.text 分页续读与截图绑定的 vision.click(node) 已在新 SDK 实现。
    assert "ax.text.search" in mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "ui.vision.click(node,image=image)" in mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "绝不代答、破解" in mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "先自解" not in mac_agent.MAI_UI_SYSTEM_PROMPT
    # DEVICE_OFFLINE / 多窗键盘歧义 的处置规则已迁到 ERROR_PLAYBOOK（出错时随反馈懒注入），
    # 断言口径改为“存在于静态 prompt 或懒注入手册中”。
    delivered = mac_agent.MAI_UI_SYSTEM_PROMPT + "\n" + "\n".join(mac_agent.ERROR_PLAYBOOK.values())
    assert "DEVICE_OFFLINE 表示 SDK endpoint 已被隔离" in delivered
    # 键盘歧义对齐新 SDK：错误码是 INPUT_REFUSED，same_pid_keyboard_ambiguity 只是 native_code。
    assert "same_pid_keyboard_ambiguity" in delivered
    assert "timeout_ms<=5000" in mac_agent.MAI_UI_SYSTEM_PROMPT
    # 旧 cua driver 词汇与错误默认值不得残留。
    prompt = mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "completed/unverified" not in prompt
    assert "before mutation dispatch" not in prompt
    assert "默认 timeout_ms=30000" not in prompt
    assert "各方法默认值不同" in prompt
    assert "CANCELLED_BEFORE_DISPATCH" in delivered
    # 纯问答/问候当轮 terminate，不得只 print 续轮（四桶重组曾丢失该句致问候任务空转 5 步）。
    assert "只需回答问题/问候时，直接 terminate" in prompt
    # SDK 窗口 id 末尾序号 ≠ osascript window N，必须显式警告以防模型混用。
    assert "window:269273:3" in prompt and "绝不可互换" in prompt
    # osascript 与 SDK 是两套地址空间的心智模型 + 分工铁律。
    assert "两套互不相通的地址空间" in prompt and "绝不可互换" in prompt
    # 并发鲁棒纪律：对象引用建窗、bundle id 解析、复用句柄、不用 osascript 序号导航。
    assert "set w to make new window" in prompt
    assert "com.google.Chrome" in prompt and "APP_INVENTORY_INCOMPLETE" in prompt
    assert "绝不用 window N" in prompt and "复用 win/ui" in prompt
    # Web 副作用不可因后续异常而重放；控制句柄也不能直接 emit。
    assert "App/Window/Tab/Browser/Locator/Inventory" in prompt
    assert "代码块不是事务" in prompt and "不从头重放整段" in prompt
    assert "一次 new_tab" not in prompt  # 精简后不再用“一次 new_tab”提示，规则在用法示例中体现
    assert "new_tab 是不可回滚" in prompt
    assert "绝不 await time.sleep" not in prompt  # 精简后已移除、工作循环已覆盖
    assert "new_tab(url, action_id=new_tab_id)" in prompt
    assert "mu.action_status(new_tab_id)" in prompt
    assert "web.browser.list_tabs()" in prompt
    # 标签超时只作废该标签；reset_web 复用仍在运行的浏览器，不作为超时恢复手段。
    assert "STALE_TARGET 只作废该标签" in prompt and "不要 reset_web" in prompt
    # 网页只在预绑定浏览器内操作；白名单通道、降级保持后台、禁止另起实例与抢前台。
    assert "网页操作的唯一合法通道是预置的 web" in prompt
    assert "不要另外打开新的浏览器" in prompt
    assert "没有其他合法方式创建或连接浏览器实例" in prompt
    assert "session.browsers.create" not in prompt
    assert "Playwright launch/connect" not in prompt
    assert "确需额外页面时只用 web.browser.new_tab" in prompt
    assert "仅在确认整个浏览器被关闭或失效且系统自动恢复未成功时使用" in prompt
    assert "普通网页操作必须保持后台" in prompt
    assert "已有前台授权不是显示浏览器的指令" in prompt
    assert "不能因出现登录页就自行显示或重启浏览器" in prompt
    assert "用户完成登录后先在原 tab/pg 核验" in prompt
    # 不再有可见重建：用户自己点开专用浏览器；网页视觉核验走 tab.ui.vision。
    assert "visible=True" not in prompt
    assert "请自行点开它完成操作后回复我" in prompt
    assert "不要改用用户自己的 Chrome" in prompt
    assert "tab.ui.vision.observe()" in prompt and "不要用桌面截图判断它是否存在" in prompt
    assert "浏览器失效时宿主会在下一个代码块前自动重建并打印原因" in prompt
    assert "自动降级" not in prompt
    assert "按需启动" not in prompt
    # PDF 表单合并到网页节末尾，不再独占一个 ## 节。
    assert "## PDF" not in prompt
    assert "pdf-form" in prompt
    # DEVICE_OFFLINE 后用 CGWindowID 确定性重绑。
    assert "rebind_window" in prompt
    # AX 树是扁平 nodes + parent_id，无 children/depth（防模型臆造层级属性）。
    assert "没有 children/depth 属性" in prompt
    # 禁用 osascript execute javascript（Chrome 默认关闭且无法程序化开启）。
    assert "不通过 osascript execute javascript" in prompt
    # 点击三级降级阶梯：元素 AX ＞ vision 坐标 ＞ 前台真实 GUI（保底）；ok≠生效需重观察；同级不反复重试。
    assert "逐级降级" in prompt and "不在同一级反复重试" in prompt
    assert "不证明业务成功" in prompt

    desktop = SdkDesktop(session_id="test")
    assert desktop.driver_mode == "sdk" and desktop.generation == 0
    desktop.invalidate_references()
    assert desktop.generation == 1
    server = SdkRpcServer(desktop, tmp_path / "unused.sock")
    asyncio.run(server.start())
    asyncio.run(server.close())
    asyncio.run(desktop.close())
    assert server.calls == []


def test_macos_prompt_does_not_treat_ax_value_as_foreground_typing():
    prompt = mac_agent.MAI_UI_SYSTEM_PROMPT
    assert 'ui.ax.set_text(node,value,delivery="foreground"' not in prompt
    assert "AXValue 替换" in prompt and "它是后台写入" in prompt
    assert 'ui.input.text(value,delivery="foreground"' in prompt
    assert 'skills.load("pdf-form")' in prompt
    pdf_skill = (Path(__file__).resolve().parents[2] / "skills/pdf-form/SKILL.md").read_text(encoding="utf-8")
    assert "`editable=True` as proof" in pdf_skill


def test_macos_prompt_bounds_temporary_pip_install():
    prompt = mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "安装依赖前先检查" in prompt
    assert "importlib.util.find_spec" in prompt
    assert "shutil.which" in prompt
    assert prompt.index("安装依赖前先检查") < prompt.index("确需临时 pip install")
    assert "pip install" in prompt
    assert "timeout=60" in prompt
    assert "不要用无超时的 pip install" in prompt


def test_macos_prompt_finds_editable_ax_roles_and_checks_partial_tree():
    prompt = mac_agent.MAI_UI_SYSTEM_PROMPT
    for role in ("AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"):
        assert role in prompt
    assert "ax.tree.truncated" in prompt
    assert "不代表控件不存在" in prompt


def test_macos_prompt_uses_only_advertised_window_actions():
    prompt = mac_agent.MAI_UI_SYSTEM_PROMPT
    assert "AXMinimized=true/false" not in prompt
    assert "ax scroll 到底" not in prompt
    assert "ax scroll" not in "\n".join(mac_agent.ERROR_PLAYBOOK.values())
    assert "窗内 hover 不可用" in prompt
    assert "win.id 是会话附件 ID" in prompt
    assert "win = await app.windows.get(id)" in prompt
    assert "ui.vision.click(node,image=image" in prompt


def test_desktop_capture_timeout_kills_and_reaps_child(monkeypatch):
    class HungCapture:
        returncode = None

        def __init__(self):
            self.communicate_calls = 0
            self.killed = False

        async def communicate(self):
            self.communicate_calls += 1
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = HungCapture()

    async def fake_spawn(*_args, **_kwargs):
        return process

    async def fake_wait_for(awaitable, *, timeout):
        assert timeout == 10.0
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    with pytest.raises(RuntimeError, match="Screen Recording permission"):
        asyncio.run(SdkDesktop(session_id="timeout", call_timeout=0.1).observe())
    assert process.killed is True
    assert process.communicate_calls == 1


def test_reset_web_removes_sdk_endpoint_and_refreshes_namespace(monkeypatch):
    # 真实 SDK 返回 Device，仅有 destroy()，没有 close()；必须从 runtime 移除。
    old = types.SimpleNamespace(resource_id="browser:old")
    removed = []

    class Runtime:
        async def remove(self, resource_id, *, timeout_ms):
            removed.append((resource_id, timeout_ms))

    new = object()
    namespace = {"web": old}
    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", Runtime())
    monkeypatch.setattr(code_runtime, "_MAI_SESSION", object())
    monkeypatch.setattr(code_runtime, "_SDK_WEB", old)
    monkeypatch.setattr(code_runtime, "_EXEC_NAMESPACE", namespace)

    async def no_save():
        return None

    async def create(_runtime, _session):
        return new

    monkeypatch.setattr(code_runtime, "_save_web_state", no_save)
    monkeypatch.setattr(code_runtime, "_create_web_resource", create)

    async def gone(_web):
        return False

    monkeypatch.setattr(code_runtime, "_reusable_web", gone)
    assert asyncio.run(code_runtime.reset_web()) is new
    assert removed == [("browser:old", 5000)] and code_runtime._SDK_WEB is new
    assert namespace["web"] is new


def test_strict_protocol_code_first_then_fence_and_no_prose_fallback():
    from model_stream import parse_code_action, CodeParseError

    # 真机样本：think/intent 闭合错配 + 无 intent 登记 + 完整 code 块 → 必须解析出代码
    raw = (
        "<think>Get Levels 5,6,7 dice data</intent>\n<code>\n"
        "script = r'''\nimport openpyxl\n'''\n"
        "open('/tmp/l567.py','w').write(script)\n</code>"
    )
    action = parse_code_action(raw)
    assert "openpyxl" in action.code and not action.code.startswith("ask_user")
    # 围栏兜底（仅当无 code 标签时）
    fenced = "plan\n```python\nprint(1)\n```"
    assert parse_code_action(fenced).code.strip() == "print(1)"
    # 纯散文不再 salvage 成 ask_user：协议违规走重试
    for bad in ("我需要确认签名地点填哪里？", "", "half <code> print("):
        try:
            parse_code_action(bad)
            raise AssertionError(f"expected CodeParseError for {bad!r}")
        except CodeParseError:
            pass


def test_parse_salvages_stray_tags_text_and_unterminated_code():
    from model_stream import parse_code_action
    # 块外散文 + 多余 </code> + 空 intent/code 块：都应 salvage，不整段丢弃
    out = ("<intent>do it</intent>\nSome stray prose\n<code>\nprint(1)\n</code>\n"
           "</code>\n<intent></intent><code></code>\ntrailing words")
    action = parse_code_action(out)
    assert action.code.strip() == "print(1)"
    # 未闭合的 code 块按已有内容 salvage
    a2 = parse_code_action("<intent>x</intent><code>print(2)")
    assert "print(2)" in a2.code
    # 多段 code 仍按序保留
    a3 = parse_code_action("<intent>a</intent><code>x=1</code><intent>b</intent><code>print(x)</code>")
    assert len(a3.ordered_blocks) == 2


def test_parse_salvages_foreign_protocol_trailer_after_valid_python():
    from model_stream import parse_code_action

    raw = (
        "<intent>prepare</intent><code>\n"
        "result = await bash('echo ok')\n"
        "print(result)\n"
        "</parameter>\n</function>"
    )
    action = parse_code_action(raw)
    assert action.code == "result = await bash('echo ok')\nprint(result)"


def test_anthropic_message_conversion_and_nonstream_response():
    system, messages = model_stream.anthropic_messages([
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,YWJj",
            }},
        ]},
    ])
    assert system == "system rules"
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "YWJj",
        }},
    ]}]

    response = FakeHttpResponse({
        "content": [{"type": "thinking", "thinking": "plan", "signature": "sig"},
                    {"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 2},
    })
    http = FakeHttpClient(response)
    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://api.anthropic.com/v1",
        api_key="secret", model_name="claude-test", timeout=10, retries=0,
        stream=False,
    )
    client.client = http
    content, reasoning, usage = asyncio.run(client.complete(
        [{"role": "system", "content": "rules"},
         {"role": "user", "content": "hello"}], max_tokens=32,
    ))
    assert (content, reasoning) == ("OK", "plan")
    assert usage == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    url, payload, headers = http.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert payload["system"] == "rules" and payload["max_tokens"] == 32
    assert all(message["role"] != "system" for message in payload["messages"])
    assert headers["x-api-key"] == "secret"
    assert "Authorization" not in headers


def test_anthropic_stream_response():
    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://api.anthropic.com/v1",
        api_key="secret", model_name="claude-test", timeout=10, retries=0,
    )
    preview = model_stream._TextPreview()
    content, reasoning, usage, finish = asyncio.run(
        client._anthropic_sse_result(FakeAnthropicStream(), preview)
    )
    assert (content, reasoning, finish) == ("OK", "plan", "stop")
    assert usage == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_llm_protocol_config_and_endpoint():
    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://host/protocol/anthropic/v1",
        api_key="", model_name="claude-test", timeout=10, retries=0,
    )
    assert client.endpoint() == "https://host/protocol/anthropic/v1/messages"
    with pytest.raises(ValueError, match="protocol"):
        model_stream.ModelClient(
            protocol="other", base_url="https://host/v1", api_key="",
            model_name="x", timeout=10, retries=0,
        )


def test_hub_inline_js_parses_with_node():
    """Regression: a Python-style ternary once slipped into the hub's inline JS and
    killed every handler at parse time (buttons appeared dead)."""
    import re as _re
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        return
    import demo_hub
    scripts = _re.findall(r"<script>(.*?)</script>", demo_hub.HUB_HTML, _re.S)
    assert scripts, "hub html has no inline script"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(scripts[-1])
        path = fh.name
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:800]
