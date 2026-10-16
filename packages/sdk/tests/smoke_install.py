"""Run with an installed wheel and `python -I`; no devices or permissions needed."""

import asyncio
from importlib.metadata import distribution
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys

import mai_ui
from mai_ui import _native
import mai_ui.integrations
import qwen_intelligence
from qwen_intelligence.core import Runtime


async def main():
    dist = distribution("qwen-intelligence-sdk")
    wheel = dist.read_text("WHEEL")
    assert "Root-Is-Purelib: false" in wheel, wheel
    assert "-none-any" not in wheel, wheel
    assert f"Tag: cp{sys.version_info.major}{sys.version_info.minor}-" in wheel, wheel
    assert Runtime is qwen_intelligence.Runtime is mai_ui.Runtime
    assert Path(_native.__file__).is_file()
    for resource in (
        "unified/playwright-contract.json",
        "unified/api.pyi",
        "unified/playwright.pyi",
        "THIRD_PARTY_NOTICES.md",
        "licenses/rmcp-LICENSE.txt",
    ):
        assert files("mai_ui").joinpath(resource).is_file(), resource
    runtime = Runtime()
    try:
        session = runtime.session()
        assert not (await session.discover()).resources
        await session.close()
    finally:
        await runtime.close()
    cli = Path(sys.executable).parent / "mu"
    info = json.loads(subprocess.check_output([str(cli), "info"], text=True))
    assert info["runtime"] == "embedded", info
    print(f"qwen-intelligence-sdk {dist.version}: wheel, native runtime, resources and CLI OK")


if __name__ == "__main__":
    asyncio.run(main())
