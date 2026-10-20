from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import signal
import sys
import pytest

import web_shim


def test_background_page_is_in_isolated_context_and_does_not_request_focus():
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, channel="chrome")
            try:
                context, cdp, context_id = await web_shim._isolated_background_context(
                    browser, viewport={"width": 800, "height": 600})
                calls = []
                original_send = cdp.send

                async def observed_send(method, params=None):
                    calls.append((method, params))
                    return await original_send(method, params)

                cdp.send = observed_send
                first, second = await asyncio.gather(
                    web_shim._new_background_page(context, cdp, context_id, 10000),
                    web_shim._new_background_page(context, cdp, context_id, 10000),
                )
                assert first is not second
                assert len(context.pages) == 2
                assert [params for method, params in calls
                        if method == "Target.createTarget"] == [
                    {"url": "about:blank", "browserContextId": context_id,
                     "background": True, "focus": False},
                    {"url": "about:blank", "browserContextId": context_id,
                     "background": True, "focus": False},
                ]
            finally:
                await browser.close()

    asyncio.run(run())


def test_create_web_uses_background_launcher_in_default_mac_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.delenv("MAC_AGENT_WEB_CDP", raising=False)
    calls = []

    async def foreground_launch(**_kwargs):
        raise AssertionError("foreground launch forbidden")

    async def start():
        return SimpleNamespace(chromium=SimpleNamespace(launch=foreground_launch))

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(
        async_playwright=lambda: SimpleNamespace(start=start)))

    async def background_launch(_pw, timeout_ms):
        calls.append(("launch", timeout_ms))
        return object(), tmp_path / "dedicated-profile"

    async def background_context(_browser, **options):
        calls.append(("context", options))
        return object(), object(), "isolated-context"

    monkeypatch.setattr(web_shim, "_launch_background_chromium", background_launch)
    monkeypatch.setattr(web_shim, "_isolated_background_context", background_context)
    web = asyncio.run(web_shim.create_web())

    assert web.browser._background_context_id == "isolated-context"
    assert web.browser._owned_profile == tmp_path / "dedicated-profile"
    assert calls[0][0] == "launch"


def test_create_web_cleans_owned_chrome_when_context_setup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1")
    monkeypatch.delenv("MAC_AGENT_WEB_CDP", raising=False)
    calls = []

    async def stop():
        calls.append("pw.stop")

    async def start():
        return SimpleNamespace(chromium=SimpleNamespace(), stop=stop)

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(
        async_playwright=lambda: SimpleNamespace(start=start)))
    owned = tmp_path / "dedicated-profile"

    async def background_launch(_pw, timeout_ms):
        return object(), owned

    async def failed_context(_browser, **_options):
        raise RuntimeError("context setup failed")

    async def close_owned(_browser, profile):
        calls.append(("close", profile))

    monkeypatch.setattr(web_shim, "_launch_background_chromium", background_launch)
    monkeypatch.setattr(web_shim, "_isolated_background_context", failed_context)
    monkeypatch.setattr(web_shim, "_close_owned_background_chromium", close_owned)
    with pytest.raises(RuntimeError, match="context setup failed"):
        asyncio.run(web_shim.create_web())
    assert calls == [("close", owned), "pw.stop"]


def test_owned_background_browser_is_closed_and_profile_removed(tmp_path):
    profile = tmp_path / "owned-profile"
    profile.mkdir()
    (profile / "marker").write_text("owned")
    calls = []

    async def send(method, params=None):
        calls.append(method)

    async def close_browser():
        calls.append("browser.close")

    async def close_context():
        calls.append("context.close")

    async def stop_playwright():
        calls.append("pw.stop")

    browser = SimpleNamespace(
        new_browser_cdp_session=lambda: asyncio.sleep(
            0, result=SimpleNamespace(send=send)), close=close_browser)
    shim = web_shim._Browser(
        SimpleNamespace(stop=stop_playwright), browser,
        SimpleNamespace(close=close_context), owned_profile=profile)
    asyncio.run(shim.close())
    assert not profile.exists()
    assert calls == ["context.close", "Browser.close", "browser.close", "pw.stop"]


def test_owned_profile_cleanup_retries_while_chrome_is_flushing(monkeypatch, tmp_path):
    profile = tmp_path / "closing-profile"
    profile.mkdir()
    (profile / "Local State").write_text("closing")
    actual_remove = web_shim.shutil.rmtree
    attempts = []

    def delayed_remove(path, *, ignore_errors=False):
        attempts.append(path)
        if len(attempts) < 3:
            return  # Chrome wrote another file while cleanup was in progress.
        actual_remove(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(web_shim.shutil, "rmtree", delayed_remove)

    async def send(_method):
        return None

    async def close_browser():
        return None

    browser = SimpleNamespace(
        new_browser_cdp_session=lambda: asyncio.sleep(
            0, result=SimpleNamespace(send=send)), close=close_browser)
    asyncio.run(web_shim._close_owned_background_chromium(browser, profile))
    assert len(attempts) == 3
    assert not profile.exists()


def test_background_launcher_opens_separate_chromium_without_activation(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(web_shim.tempfile, "mkdtemp", lambda prefix: str(profile))
    launched = []
    browser = object()

    async def fake_exec(*args, **kwargs):
        launched.append(args)
        (profile / "DevToolsActivePort").write_text("9345\n/devtools/browser/test\n")
        return SimpleNamespace(wait=lambda: asyncio.sleep(0, result=0))

    async def connect(url, **kwargs):
        launched.append((url, kwargs))
        return browser

    monkeypatch.setattr(web_shim.asyncio, "create_subprocess_exec", fake_exec)
    pw = SimpleNamespace(chromium=SimpleNamespace(
        executable_path="/Applications/Chromium.app/Contents/MacOS/Chromium",
        connect_over_cdp=connect))

    actual, owned_profile = asyncio.run(web_shim._launch_background_chromium(pw, 1000))
    assert actual is browser
    assert owned_profile == Path(profile)
    assert launched[0] == (
        "/usr/bin/open", "-g", "-n", "-a", "Google Chrome", "--args",
        "--remote-debugging-port=0", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--no-startup-window")
    assert launched[1][0] == "http://127.0.0.1:9345"


def test_failed_background_launch_stops_only_its_own_chrome(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(web_shim.tempfile, "mkdtemp", lambda prefix: str(profile))
    calls = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        if args[0] == "/usr/bin/open":
            return SimpleNamespace(wait=lambda: asyncio.sleep(0, result=0))
        if args[0] == "/bin/ps":
            return SimpleNamespace(communicate=lambda: asyncio.sleep(0, result=(
                (f"123 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                 f"--user-data-dir={profile}\n"
                 "456 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                 "--user-data-dir=/tmp/another-profile\n").encode(), b"")))
        raise AssertionError(args)

    killed = []
    monkeypatch.setattr(web_shim.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(web_shim.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    pw = SimpleNamespace(chromium=SimpleNamespace(
        executable_path="/Applications/Chromium.app/Contents/MacOS/Chromium"))
    with pytest.raises(RuntimeError, match="did not publish a CDP endpoint"):
        asyncio.run(web_shim._launch_background_chromium(pw, 10))
    assert killed == [(123, signal.SIGTERM)]
    assert not profile.exists()


def test_web_shim_new_tab_uses_background_target_instead_of_new_page():
    async def run():
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, channel="chrome")
        context, cdp, context_id = await web_shim._isolated_background_context(browser)
        context.new_page = lambda: (_ for _ in ()).throw(
            RuntimeError("foreground new_page forbidden"))
        shim = web_shim._Browser(
            pw, browser, context, background_cdp=cdp,
            background_context_id=context_id, background_tabs=True)
        try:
            tab = await shim.new_tab("data:text/html,<title>Background tab</title>")
            assert await tab.playwright.title() == "Background tab"
        finally:
            await shim.close()

    asyncio.run(run())


def test_web_shim_rebuild_preserves_background_tab_creation():
    async def run():
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, channel="chrome")
        context, cdp, context_id = await web_shim._isolated_background_context(browser)
        shim = web_shim._Browser(
            pw, browser, context, background_cdp=cdp,
            background_context_id=context_id, background_tabs=True,
            launch_kwargs={"headless": True, "channel": "chrome"})
        try:
            await browser.close()
            tab = await shim.new_tab("data:text/html,<title>Recovered</title>")
            assert await tab.playwright.title() == "Recovered"
            assert shim._background_context_id != context_id
        finally:
            await shim.close()

    asyncio.run(run())
