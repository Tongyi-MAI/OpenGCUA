"""Host embedding: run on a configured local Mac with a running TextEdit instance."""
import asyncio

from mai_ui import Runtime


async def main():
    async with Runtime() as runtime:
        await runtime.add_macos(name="local-mac")
        async with runtime.session() as mu:
            mac = await mu.register("local-mac", name="mac")
            app = await mac.apps.get("com.apple.TextEdit")
            await mu.emit(await app.ui.ax.observe())
            await mu.emit(await app.windows.list())


if __name__ == "__main__":
    asyncio.run(main())
