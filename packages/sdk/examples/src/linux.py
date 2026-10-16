"""Run inside an existing Ubuntu X11 desktop after `mu setup linux`."""

import asyncio

from mai_ui import Runtime


def output(value):
    if value["type"] == "image":
        print(f"Captured {value['width']}×{value['height']} image")
    else:
        print(value.get("text", value))


async def main():
    async with Runtime() as runtime:
        resource = await runtime.add_linux(name="desktop")
        mu = runtime.session(output=output)
        desktop = await mu.register(resource, name="desktop")
        await mu.emit(await desktop.ui.vision.observe())
        await mu.emit(await desktop.ui.ax.observe(timeout_ms=5000))


if __name__ == "__main__":
    asyncio.run(main())
