"""Legacy CLI/import shim; install the separate mai-ui-repl integration."""

from mai_ui_repl import PythonRepl
from mai_ui_repl import main as _main


async def main():
    await _main(default_api="0.1")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
