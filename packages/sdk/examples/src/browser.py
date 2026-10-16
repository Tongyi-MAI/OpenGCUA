"""Run after `mu setup browser --engine chromium`."""

import asyncio

from mai_ui import Runtime


async def main():
    async with Runtime() as runtime:
        await runtime.add_browser_factory("chromium", engine="chromium")
        async with runtime.session(
            output=lambda output: print(output.get("text", output["type"]))
        ) as mu:
            web = await mu.browsers.create(name="example", provider="chromium")
            tab = await web.browser.new_tab()
            await tab.evaluate(
                "html=>{document.body.innerHTML=html;return null}",
                "<button onclick=\"this.textContent='Done'\">Continue</button>",
            )
            obs = await tab.ui.ax.observe()
            await mu.emit(obs)
            # Programmatic example: use an exact node from this captured fixture.
            button = next(n for n in obs.tree.nodes if n.name == "Continue")
            await tab.ui.ax.click(button)
            await mu.emit(await tab.ui.ax.observe())


if __name__ == "__main__":
    asyncio.run(main())
