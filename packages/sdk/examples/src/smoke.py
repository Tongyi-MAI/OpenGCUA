from mai_ui._testing import node, wait_for
import asyncio, json, os
from mai_ui import connect


async def main():
    async with connect(
        backend="adb", serial=os.environ.get("MAI_UI_SERIAL", "127.0.0.1:27666")
    ) as mu:
        device = await mu.get_device()
        app = device.get_app("app:dev.maiui.demo")
        await mu.emit(await app.launch())
        obs = await wait_for(app.ui, lambda obs: obs.foreground_app_id == app.id)
        await mu.emit(obs)
        result = await device.shell.run(["id"], check=True)
        await mu.emit(result.stdout_text())


if __name__ == "__main__":
    asyncio.run(main())
