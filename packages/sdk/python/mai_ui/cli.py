"""Installed command entrypoint; host administration is implemented in Rust."""

import argparse
import asyncio
import json

from . import _native


def main():
    parser = argparse.ArgumentParser(prog="mu")
    parser.add_argument("command", choices=("info", "setup", "doctor"))
    parser.add_argument("platform", nargs="?", choices=("macos", "browser", "linux"))
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--archive")
    parser.add_argument("--app", help="doctor: signed app path for isolated host validation")
    parser.add_argument("--socket", help="doctor: private local daemon socket")
    parser.add_argument("--engine", choices=("all", "chromium", "firefox", "webkit"), default="all")
    parser.add_argument("--display")
    parser.add_argument("--xauthority")
    parser.add_argument("--python", help="Linux adapter interpreter")
    args = parser.parse_args()
    if args.command == "info":
        print(
            json.dumps(
                dict(
                    sdk_api_version="0.3",
                    runtime="embedded",
                    adapters=["android", "macos", "browser", "linux"],
                    bindings=["python", "javascript"],
                )
            )
        )
        return
    if args.platform == "linux":
        from .unified.runtime import call

        async def linux():
            core = _native.RuntimeCore()
            try:
                result = await call(
                    core,
                    dict(
                        op="linux_admin",
                        operation=args.command,
                        display=args.display,
                        xauthority=args.xauthority,
                        python=args.python,
                    ),
                )
                print(json.dumps(result))
                if result.get("state") != "ready":
                    raise SystemExit(1)
            finally:
                await call(core, dict(op="close"))

        asyncio.run(linux())
        return
    if args.platform == "browser":

        async def browser():
            try:
                result = json.loads(await _native.browser_admin(args.command, args.engine))
                print(json.dumps(result))
                if result.get("state") != "ready":
                    raise SystemExit(1)
            except ValueError as error:
                print(str(error))
                raise SystemExit(1) from error

        asyncio.run(browser())
        return
    if args.platform != "macos":
        parser.error("specify macos")

    if args.command != "setup" and (args.replace or args.archive):
        parser.error("--replace and --archive apply only to setup")
    if args.command != "doctor" and (args.app or args.socket):
        parser.error("--app and --socket apply only to doctor")

    async def run():
        try:
            print(
                await _native.macos_admin(
                    args.command, args.replace, args.archive, args.app, args.socket
                )
            )
        except ValueError as e:
            print(json.dumps(dict(state="needs_user_action", error=str(e))))
            raise SystemExit(1) from e

    asyncio.run(run())
