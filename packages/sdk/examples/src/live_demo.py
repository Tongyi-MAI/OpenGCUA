from mai_ui._testing import node, wait_for
"""Real generic UI actions + independent app-file oracle; emits film-ready evidence."""

import asyncio, dataclasses, io, json, os, statistics, time
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image, ImageStat
from mai_ui import connect, MuError, ShellError

ROOT = Path(__file__).resolve().parents[1]
SERIAL = os.environ.get("MAI_UI_SERIAL", "127.0.0.1:27666")
TITLE = "Hello, mai-ui 👋"
BODY = "One API. Python + Rust. 你好，Android。"


async def main():
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    evidence = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "serial": SERIAL,
            "platform": "android",
        },
        "checks": {},
        "receipts": [],
    }

    def check(name, condition, **detail):
        assert condition, (name, detail)
        evidence["checks"][name] = {"passed": True, **detail}

    async with connect(backend="adb", serial=SERIAL) as mu:
        # This standalone embedding writes explicit image output to an artifact.
        mu.image_sink = lambda data, caption: (
            out / "analysis-thumbnail.png"
        ).write_bytes(data)
        device = await mu.get_device()
        app = device.get_app("app:dev.maiui.demo")
        ui = app.ui
        launched = await app.launch()
        evidence["receipts"].append(dataclasses.asdict(launched))
        obs = await wait_for(ui, lambda obs: obs.foreground_app_id == app.id)
        check("foreground", obs.foreground_app_id == app.id)
        # Start this fixture with cleared app data (adb shell pm clear dev.maiui.demo).
        baseline = await ui.observe()
        check("empty_fixture", all(n.text == "" for n in baseline._tree.nodes if n.editable))
        await mu.emit(baseline)
        for name, text in [("Title", TITLE), ("Body", BODY)]:
            obs = await ui.observe()
            await mu.emit(obs)
            target_id = node(obs, role="textbox", name=name).id
            index = next(i for i, native_id in obs._indices.items() if native_id == target_id)
            receipt = await ui.input_text(text, target=index)
            evidence["receipts"].append(dataclasses.asdict(receipt))
            await asyncio.sleep(0.5)
        obs = await wait_for(ui, lambda obs: node(obs, name="Body", role="textbox").node.text == BODY)
        obs = await ui.observe()
        receipt = await ui.click(node(obs, role="button", name="SAVE NOTE"))
        evidence["receipts"].append(dataclasses.asdict(receipt))
        after = await ui.observe()
        # Receipt is only delivery evidence. Read the independent app oracle over shell.
        result = await device.shell.run(
            ["run-as", "dev.maiui.demo", "cat", "files/state.json"], check=True
        )
        oracle = json.loads(result.stdout_text())
        check(
            "app_oracle",
            oracle["title"] == TITLE
            and oracle["body"] == BODY
            and oracle["saves"] >= 1,
            result=oracle,
        )
        after = await wait_for(ui, lambda obs: node(obs, name="Save status").node.text == f"Saved · {oracle['saves']}")
        check("ui_save_status", True, text=f"Saved · {oracle['saves']}")
        check(
            "ax_diff",
            after._delta is not None
            and 0 < len(after._delta.upserted) < len(after._tree.nodes),
            upserted=len(after._delta.upserted),
            total=len(after._tree.nodes),
            removed=list(after._delta.removed),
        )
        await mu.emit(after)
        # Read and print exactly what the movie will display.
        result = await device.shell.run(["getprop", "ro.build.version.sdk"], check=True)
        evidence["target"]["api_level"] = result.stdout_text().strip()
        identity = (await device.shell.providers())[0].identity
        evidence["target"]["identity"] = dataclasses.asdict(identity)
        try:
            await device.shell.run("printf problem >&2; exit 7", check=True)
        except ShellError as e:
            check(
                "shell_nonzero",
                e.code == "SHELL_EXIT_NONZERO" and e.shell_result.exit_code == 7,
                code=e.code,
                exit_code=e.shell_result.exit_code,
            )
        timeout = await device.shell.run(["sleep", "3"], timeout_ms=100)
        check(
            "shell_timeout",
            timeout.state == "timed_out",
            state=timeout.state,
            cleanup=timeout.cleanup,
        )
        literal = await device.shell.run(["printf", "%s", "hello; $(id)"], check=True)
        check("shell_literal_argv", literal.stdout_text() == "hello; $(id)")
        try:
            await ui.pointer.move(None)
        except MuError as e:
            check("unsupported", e.code == "UNSUPPORTED_CAPABILITY", code=e.code)
        # Measure sequential real round trips after warm-up, without capture footage.
        await device.ui.observe()
        times = {}
        for label, kwargs in [
            ("cached_tree", {"mode": "cached"}),
            ("refresh_tree", {"mode": "refresh"}),
            ("screenshot", {"tree": False, "screenshot": True}),
        ]:
            samples = []
            for _ in range(12):
                start = time.perf_counter()
                o = await device.ui._observe(**kwargs)
                samples.append(round((time.perf_counter() - start) * 1000, 3))
            times[label] = {
                "samples_ms": samples,
                "median_ms": round(statistics.median(samples), 2),
                "p95_ms": round(sorted(samples)[-1], 2),
                "n": len(samples),
            }
            if label == "screenshot":
                raw = await mu.images.read(o.image.frame)
                image = Image.open(io.BytesIO(raw)).convert("RGB")
                mean = ImageStat.Stat(image).mean
                check(
                    "screenshot_decode",
                    image.width == o.display.width
                    and image.height == o.display.height
                    and sum(mean) > 0,
                    width=image.width,
                    height=image.height,
                )
                # Image processing in the same Python session; no screenshots in the video.
                thumbnail = image.resize((180, 400))
                buf = io.BytesIO()
                thumbnail.save(buf, format="PNG")
                await mu.emit(
                    buf.getvalue(), caption="Python-generated analysis thumbnail"
                )
                evidence["image_analysis"] = {
                    "source_size": list(image.size),
                    "thumbnail_size": list(thumbnail.size),
                    "mean_rgb": [round(x, 2) for x in mean],
                }
        evidence["latency"] = times
        stopped = await mu.close()
        check(
            "stop",
            stopped.queue_fenced and stopped.in_flight_drained,
            state=stopped.state,
        )
        try:
            await device.shell.run(["true"])
        except MuError as e:
            check("stop_fences_writes", e.code == "SESSION_CLOSED", code=e.code)
        evidence["stop"] = dataclasses.asdict(stopped)
    (out / "live-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
