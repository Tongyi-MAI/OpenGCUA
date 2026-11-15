"""Windows facade contracts through the installed native SDK, without a desktop."""

import asyncio
from types import SimpleNamespace

import pytest


def test_windows_background_delivery_reaches_backend_without_foreground_default():
    from mai_ui.unified.ui import ActionBase

    requests = []

    async def dispatch(action, **arguments):
        requests.append((action, arguments))
        return dict(execution="completed")

    handle = SimpleNamespace(
        platform="windows",
        _check=lambda **kwargs: None,
        _require=lambda cap: None,
        _app_id="observed-app",
        _backend=SimpleNamespace(_action=dispatch),
    )
    facade = ActionBase(handle)
    asyncio.run(facade._action("input_text", capability="ui.input_text", text="example"))
    assert requests[0][1]["delivery"] == "background"
    assert requests[0][1]["app_id"] == "observed-app"
    assert requests[0][1]["timeout_ms"] == 15000


def test_explicit_foreground_request_is_not_silently_rewritten():
    from mai_ui.unified.ui import ActionBase

    calls = []

    async def dispatch(action, **arguments):
        calls.append(arguments)

    handle = SimpleNamespace(
        platform="windows",
        _check=lambda **kwargs: None,
        _require=lambda cap: None,
        _app_id=None,
        _backend=SimpleNamespace(_action=dispatch),
    )
    asyncio.run(
        ActionBase(handle)._action(
            "input_text", capability="ui.input_text", text="x", delivery="foreground"
        )
    )
    assert calls[0]["delivery"] == "foreground"  # The provider must explicitly refuse it.


@pytest.mark.parametrize("name,driver", [("", None), (" ", None), (False, None), ("desktop", 5)])
def test_invalid_registration_never_starts_a_provider(name, driver, monkeypatch):
    from mai_ui import MuError
    from mai_ui.unified import runtime as module

    monkeypatch.setattr(module.sys, "platform", "win32")
    with pytest.raises(MuError) as caught:
        asyncio.run(module.Runtime.add_windows(object(), name=name, driver_path=driver))
    assert caught.value.code == "INVALID_ARGUMENT"
