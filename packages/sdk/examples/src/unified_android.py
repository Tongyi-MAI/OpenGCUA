"""Silent capture and explicit delivery against one explicitly selected Android."""

import argparse
import asyncio
from pathlib import Path

from mai_ui import Runtime


async def main(serial):
    output_dir = Path("artifacts/unified-example")
    output_dir.mkdir(parents=True, exist_ok=True)

    def emit(item):
        if item["type"] == "image":
            path = output_dir / (item["image_id"] + ".png")
            path.write_bytes(item["data"])
            print(item["caption"], path.resolve())
        else:
            print(item["text"])

    async with Runtime() as runtime:
        resource = await runtime.add_android(serial, name="android")
        async with runtime.session(output=emit) as mu:
            phone = await mu.register(resource, name="phone")
            await mu.emit(await phone.ui.ax.observe())
            await mu.emit(await phone.ui.vision.observe())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    asyncio.run(main(parser.parse_args().serial))
