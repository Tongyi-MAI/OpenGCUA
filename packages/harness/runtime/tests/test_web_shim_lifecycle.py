"""专用浏览器生命周期：慢页面只作废标签、reset_web 复用同一浏览器、死 worker 的浏览器被接管或回收。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

import code_runtime
import web_shim


@pytest.fixture(autouse=True)
def _isolated_leases(monkeypatch, tmp_path):
    monkeypatch.setenv("GENERALIST_DATA_DIR", str(tmp_path / "data"))


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class Page:
    """名字必须是 Page：代理按 Playwright 类型名决定是否补 wait_until。"""

    def __init__(self, behaviour="ok"):
        self.calls = []
        self.behaviour = behaviour

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self.behaviour == "playwright-timeout":
            from playwright.async_api import TimeoutError as PlaywrightTimeout
            raise PlaywrightTimeout("Timeout exceeded")
        if self.behaviour == "hang":
            await asyncio.sleep(60)

    async def title(self):
        return "still usable"


def _tab(page):
    browser = web_shim._Browser(SimpleNamespace(), SimpleNamespace(is_connected=lambda: True),
                                SimpleNamespace())
    return browser, web_shim._Tab(browser, page)


def test_navigation_waits_for_dom_and_lets_playwright_time_out_first():
    page = Page()
    _browser, tab = _tab(page)
    receipt = asyncio.run(tab.playwright.goto("https://cart.taobao.com/", timeout_ms=30000))
    assert receipt.error is None
    assert page.calls == [{"url": "https://cart.taobao.com/", "wait_until": "domcontentloaded",
                           "timeout": 28000}]
    explicit = Page()
    _browser, tab = _tab(explicit)
    asyncio.run(tab.playwright.goto("https://example.test/", wait_until="load"))
    assert explicit.calls[0]["wait_until"] == "load"
    assert explicit.calls[0]["timeout"] < web_shim._NAV_DEADLINE_MS


def test_playwright_navigation_timeout_keeps_tab_and_browser_usable():
    page = Page("playwright-timeout")
    browser, tab = _tab(page)
    receipt = asyncio.run(tab.playwright.goto("https://cart.taobao.com/", timeout_ms=5000))
    assert receipt.effect == "unknown" and receipt.error["code"] == "DEADLINE_EXCEEDED"
    assert "still usable" in receipt.error["message"]
    assert tab._stale is None
    assert asyncio.run(tab.playwright.title()) == "still usable"
    assert browser.is_alive()


def test_hard_deadline_abandons_only_that_tab(monkeypatch):
    monkeypatch.setattr(web_shim, "_inner_timeout", lambda deadline_ms: 120000)
    page = Page("hang")
    browser, tab = _tab(page)
    receipt = asyncio.run(tab.playwright.goto("https://slow.test/", timeout_ms=1000))
    assert receipt.effect == "unknown"
    assert "new_tab" in receipt.error["message"] and "reset_web" in receipt.error["message"]
    with pytest.raises(web_shim.MuError, match="STALE_TARGET"):
        asyncio.run(tab.playwright.title())
    assert not hasattr(browser, "_wedged")
    assert browser.is_alive()


def test_launch_records_lease_and_close_stops_chrome_even_when_cdp_hangs(monkeypatch, tmp_path):
    profile = Path(tempfile.mkdtemp(prefix=web_shim._BACKGROUND_PROFILE_PREFIX))
    monkeypatch.setattr(web_shim.tempfile, "mkdtemp", lambda prefix: str(profile))

    async def fake_exec(*args, **_kwargs):
        (profile / "DevToolsActivePort").write_text("9345\n/devtools/browser/x\n")
        return SimpleNamespace(wait=lambda: asyncio.sleep(0, result=0))

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    browser = SimpleNamespace(new_browser_cdp_session=hang, close=hang)

    async def connect(_url, **_kwargs):
        return browser

    stopped = []
    monkeypatch.setattr(web_shim.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(web_shim, "_stop_owned_profile_processes", stopped.append)
    pw = SimpleNamespace(chromium=SimpleNamespace(
        executable_path="/Applications/Chromium.app/Contents/MacOS/Chromium",
        connect_over_cdp=connect))
    _browser, owned = asyncio.run(web_shim._launch_background_chromium(pw, 1000))
    lease = web_shim._lease_dir() / f"{profile.name}.json"
    assert json.loads(lease.read_text())["profile"] == str(profile)

    asyncio.run(web_shim._close_owned_background_chromium(browser, owned, timeout_s=0.05))
    assert stopped == [profile]
    assert not lease.exists() and not profile.exists()


def test_close_still_stops_chrome_when_teardown_is_cancelled(monkeypatch):
    profile = Path(tempfile.mkdtemp(prefix=web_shim._BACKGROUND_PROFILE_PREFIX))
    stopped = []
    monkeypatch.setattr(web_shim, "_stop_owned_profile_processes", stopped.append)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    browser = SimpleNamespace(new_browser_cdp_session=hang, close=hang)

    async def run():
        task = asyncio.ensure_future(web_shim._close_owned_background_chromium(browser, profile))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert stopped == [profile] and not profile.exists()


def _orphan_lease(profile: Path, owner: int) -> Path:
    directory = web_shim._lease_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lease = directory / f"{profile.name}.json"
    lease.write_text(json.dumps({"profile": str(profile), "owner_pid": owner}))
    return lease


def test_dead_workers_browser_is_adopted_instead_of_opening_another(monkeypatch):
    profile = Path(tempfile.mkdtemp(prefix=web_shim._BACKGROUND_PROFILE_PREFIX))
    (profile / "DevToolsActivePort").write_text("9555\n/devtools/browser/x\n")
    lease = _orphan_lease(profile, _dead_pid())
    adopted = object()
    connected = []

    async def connect(url, **_kwargs):
        connected.append(url)
        return adopted

    async def forbidden_open(*args, **_kwargs):
        raise AssertionError(f"must not launch another browser: {args}")

    monkeypatch.setattr(web_shim, "_profile_pids", lambda path: [4242] if path == profile else [])
    monkeypatch.setattr(web_shim.asyncio, "create_subprocess_exec", forbidden_open)
    pw = SimpleNamespace(chromium=SimpleNamespace(
        executable_path="/Applications/Chromium.app/Contents/MacOS/Chromium",
        connect_over_cdp=connect))
    try:
        browser, owned = asyncio.run(web_shim._launch_background_chromium(pw, 1000))
        assert browser is adopted and owned == profile
        assert connected == ["http://127.0.0.1:9555"]
        assert json.loads(lease.read_text())["owner_pid"] == web_shim.os.getpid()
    finally:
        web_shim.shutil.rmtree(profile, ignore_errors=True)


def test_unreachable_orphans_are_stopped_and_live_owners_are_left_alone(monkeypatch):
    dead = Path(tempfile.mkdtemp(prefix=web_shim._BACKGROUND_PROFILE_PREFIX))
    live = Path(tempfile.mkdtemp(prefix=web_shim._BACKGROUND_PROFILE_PREFIX))
    dead_lease = _orphan_lease(dead, _dead_pid())
    live_lease = _orphan_lease(live, 1)  # launchd: always alive
    stopped = []
    monkeypatch.setattr(web_shim, "_profile_pids", lambda _path: [])
    monkeypatch.setattr(web_shim, "_stop_owned_profile_processes", stopped.append)
    try:
        assert asyncio.run(web_shim._adopt_orphaned_background_chromium(SimpleNamespace())) is None
        assert stopped == [dead] and not dead.exists() and not dead_lease.exists()
        assert live.exists() and live_lease.exists()
    finally:
        web_shim.shutil.rmtree(live, ignore_errors=True)


def test_lease_pointing_outside_temp_is_never_deleted(monkeypatch, tmp_path):
    victim = tmp_path / f"{web_shim._BACKGROUND_PROFILE_PREFIX}user-data"
    victim.mkdir()
    lease = _orphan_lease(victim, _dead_pid())
    stopped = []
    monkeypatch.setattr(web_shim, "_stop_owned_profile_processes", stopped.append)
    asyncio.run(web_shim._adopt_orphaned_background_chromium(SimpleNamespace()))
    assert victim.exists() and not lease.exists() and stopped == []


class ShimWeb:
    resource_id = "browser:web-shim"

    def __init__(self, alive=True, background=True):
        self.alive = alive
        self.closed = []
        self.browser = SimpleNamespace(_background_tabs=background,
                                       storage_state=self._state)

    async def _state(self, **_kwargs):
        return {"cookies": [], "origins": []}

    def is_alive(self):
        return self.alive

    async def close(self, *, timeout_ms=30000):
        self.closed.append(timeout_ms)


@pytest.fixture
def runtime_globals(monkeypatch, tmp_path):
    monkeypatch.setattr(code_runtime, "_WEB_STATE", tmp_path / "web_state.json")
    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", object())
    monkeypatch.setattr(code_runtime, "_MAI_SESSION", object())
    monkeypatch.setattr(code_runtime, "_EXEC_NAMESPACE", {})
    created = []

    async def create(_runtime, _session, visible=False):
        created.append(visible)
        return ShimWeb(background=not visible)

    monkeypatch.setattr(code_runtime, "_create_web_resource", create)
    return created


def test_reset_web_returns_the_running_browser_instead_of_opening_another(monkeypatch, runtime_globals):
    current = ShimWeb()
    monkeypatch.setattr(code_runtime, "_SDK_WEB", current)
    assert asyncio.run(code_runtime.reset_web()) is current
    assert runtime_globals == [] and current.closed == []
    assert code_runtime._EXEC_NAMESPACE["web"] is current


def test_reset_web_closes_a_dead_browser_before_starting_one(monkeypatch, runtime_globals, tmp_path):
    dead = ShimWeb(alive=False)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", dead)
    replacement = asyncio.run(code_runtime.reset_web())
    assert replacement is not dead and dead.closed == [5000] and runtime_globals == [False]


def test_reset_web_has_no_visible_mode(monkeypatch, runtime_globals):
    # 展示浏览器由用户自己点开；不存在重建成可见浏览器的入口。
    monkeypatch.setattr(code_runtime, "_SDK_WEB", ShimWeb(background=True))
    with pytest.raises(TypeError):
        asyncio.run(code_runtime.reset_web(visible=True))
    assert runtime_globals == []


class SdkWeb:
    """SDK-shaped web whose list_tabs probe fails with a given error."""

    def __init__(self, error=None):
        outer = self
        self.error = error
        self.closed = []

        class Browser:
            async def list_tabs(self, *, timeout_ms):
                if outer.error is not None:
                    raise outer.error
                return []

        self.browser = Browser()

    # SDK 浏览器本身没有 close()；模拟宿主 remove() 的端点清理。
    resource_id = "browser:fixture"


def install_sdk_removal(monkeypatch, web):
    class Runtime:
        async def remove(self, resource_id, *, timeout_ms):
            assert resource_id == web.resource_id
            web.closed.append(timeout_ms)

    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", Runtime())
    monkeypatch.setattr(code_runtime, "_WEB_RETRY_AT", 0.0)


def test_busy_tab_does_not_rebuild_the_browser(monkeypatch, runtime_globals, capsys):
    from mai_ui import MuError

    busy = SdkWeb(MuError("DEVICE_OFFLINE", "browser:1/tab:2 is temporarily blocked: browser.playwright.locator.click ..."))
    monkeypatch.setattr(code_runtime, "_SDK_WEB", busy)
    monkeypatch.setattr(code_runtime, "_WEB_BUSY_SINCE", None)
    asyncio.run(code_runtime._ensure_web())
    assert code_runtime._SDK_WEB is busy and busy.closed == [] and runtime_globals == []
    assert "尚未结束" in capsys.readouterr().out


def test_structured_scope_fence_keeps_browser_without_english_matching(monkeypatch, runtime_globals):
    from mai_ui.unified.runtime import decode_error

    error = decode_error(ValueError(json.dumps({
        "code": "DEVICE_OFFLINE", "message": "网页操作尚未结束",
        "details": {"cause": "scope_fenced", "scope": "browser:fixture/tab:2",
                    "reason": "等待之前的操作完成", "next_step": "wait_for_operation"},
    })))
    busy = SdkWeb(error)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", busy)
    monkeypatch.setattr(code_runtime, "_WEB_BUSY_SINCE", None)
    monkeypatch.setattr(code_runtime, "_WEB_RETRY_AT", 0.0)
    assert asyncio.run(code_runtime._reusable_web(busy)) is True
    asyncio.run(code_runtime._ensure_web())
    assert code_runtime._SDK_WEB is busy and busy.closed == [] and runtime_globals == []


def test_structured_endpoint_offline_overrides_busy_words(monkeypatch, runtime_globals):
    from mai_ui.unified.runtime import decode_error

    error = decode_error(ValueError(json.dumps({
        "code": "DEVICE_OFFLINE", "message": "worker stopped after being temporarily blocked",
        "details": {"cause": "endpoint_offline", "scope": "browser:fixture",
                    "reason": "worker stopped", "next_step": "host_reconnect"},
    })))
    dead = SdkWeb(error)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", dead)
    monkeypatch.setattr(code_runtime, "_WEB_BUSY_SINCE", None)
    install_sdk_removal(monkeypatch, dead)
    assert asyncio.run(code_runtime._reusable_web(dead)) is False
    asyncio.run(code_runtime._ensure_web())
    assert dead.closed == [5000] and runtime_globals == [False]


def test_browser_busy_beyond_any_budget_is_rebuilt(monkeypatch, runtime_globals):
    from mai_ui import MuError

    stuck = SdkWeb(MuError("DEADLINE_EXCEEDED", "browser.list_tabs did not finish within its 3000 ms budget"))
    monkeypatch.setattr(code_runtime, "_SDK_WEB", stuck)
    install_sdk_removal(monkeypatch, stuck)
    monkeypatch.setattr(code_runtime, "_WEB_BUSY_SINCE", code_runtime.time.monotonic() - 1000)
    asyncio.run(code_runtime._ensure_web())
    assert stuck.closed == [5000] and runtime_globals == [False]


def test_exited_worker_is_rebuilt_with_its_reason(monkeypatch, runtime_globals, capsys):
    from mai_ui import MuError

    dead = SdkWeb(MuError("DEVICE_OFFLINE",
                          "browser:1 is offline: the browser worker process stopped: exit status: 1"))
    monkeypatch.setattr(code_runtime, "_SDK_WEB", dead)
    install_sdk_removal(monkeypatch, dead)
    asyncio.run(code_runtime._ensure_web())
    assert dead.closed == [5000] and runtime_globals == [False]
    assert "worker process stopped" in capsys.readouterr().out


def test_failed_old_sdk_removal_does_not_create_another_browser(monkeypatch, runtime_globals):
    class Runtime:
        async def remove(self, resource_id, *, timeout_ms):
            raise RuntimeError("provider did not stop")

    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", Runtime())
    monkeypatch.setattr(code_runtime, "_SDK_WEB", SdkWeb())
    assert asyncio.run(code_runtime._rebuild_web()) is None
    assert runtime_globals == []
    assert "provider did not stop" in code_runtime._WEB_UNAVAILABLE


def test_session_close_shuts_the_shim_browser_and_keeps_login(monkeypatch, tmp_path):
    state = tmp_path / "browser" / "web_state.json"
    web = ShimWeb()
    monkeypatch.setattr(code_runtime, "_WEB_STATE", state)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", web)
    monkeypatch.setattr(code_runtime, "_MAI_SESSION", None)
    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", None)
    monkeypatch.setattr(code_runtime, "_SDK_KEEPALIVE", None)
    asyncio.run(code_runtime._close_sdk_session(close_runtime=True))
    assert web.closed == [5000]
    assert json.loads(state.read_text()) == {"cookies": [], "origins": []}
    assert state.stat().st_mode & 0o777 == 0o600


def test_bound_browser_is_kept_while_it_runs(monkeypatch, runtime_globals):
    current = ShimWeb()
    monkeypatch.setattr(code_runtime, "_SDK_WEB", current)
    asyncio.run(code_runtime._ensure_web())
    assert code_runtime._SDK_WEB is current and runtime_globals == [] and current.closed == []


def test_closed_browser_is_restored_before_the_next_code_request(monkeypatch, runtime_globals):
    dead = ShimWeb(alive=False)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", dead)
    asyncio.run(code_runtime._ensure_web())
    assert dead.closed == [5000] and runtime_globals == [False]
    assert code_runtime._SDK_WEB is not dead and code_runtime._EXEC_NAMESPACE["web"] is code_runtime._SDK_WEB


def test_failed_browser_start_is_retried_at_most_once_a_minute(monkeypatch, runtime_globals):
    attempts = []

    async def unavailable(_runtime, _session, visible=False):
        attempts.append(visible)
        return None

    monkeypatch.setattr(code_runtime, "_create_web_resource", unavailable)
    monkeypatch.setattr(code_runtime, "_SDK_WEB", None)
    monkeypatch.setattr(code_runtime, "_WEB_RETRY_AT", 0.0)
    asyncio.run(code_runtime._ensure_web())
    asyncio.run(code_runtime._ensure_web())
    assert attempts == [False]
    monkeypatch.setattr(code_runtime, "_WEB_RETRY_AT", 0.0)
    asyncio.run(code_runtime._ensure_web())
    assert attempts == [False, False]
