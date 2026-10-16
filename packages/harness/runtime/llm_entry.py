"""Single entry point for LLM configuration and client construction.

Both ``mac_agent`` (code+GUI) and ``gui_agent`` (pure pyautogui baseline) resolve
their model endpoint, credentials and sampling/compaction hyper-parameters through
this module and talk to the model through :class:`model_stream.ModelClient`, so the
two agents never carry divergent request-building or compaction code.

Configuration priority (first hit wins), CLI flags always override file values::

    env MAC_AGENT_CONFIG  >  packages/harness/configs/local.json  >  packages/harness/configs/macos_demo.json

API key priority::

    config file "api_key" (non-empty)  >  env DASHSCOPE_API_KEY / API_KEY
    >  packages/harness/runtime/.api_key  >  "" (server may allow keyless endpoints)

Sampling knobs ``temperature`` / ``top_p`` were removed on purpose: deterministic
GUI control does not benefit from sampling jitter and the two agents kept drifting
apart when each carried its own defaults. The remaining tunables are exposed by
:func:`add_llm_cli_args` and documented in QUICKSTART.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from llm_compaction import CompactionConfig
from model_stream import ModelClient

_HERE = Path(__file__).resolve().parent
CONFIG_DIR = _HERE.parent / "configs"
API_KEY_FILE = _HERE / ".api_key"

DEFAULT_MODEL_NAME = "qwen3.8-flash"
DEFAULT_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_API_TIMEOUT = 300.0
DEFAULT_API_RETRIES = 3
DEFAULT_CONTEXT_LIMIT = 200000
DEFAULT_COMPACT_TRIGGER_TOKENS = DEFAULT_CONTEXT_LIMIT


def _config_path() -> Path:
    env = os.environ.get("MAC_AGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    local = CONFIG_DIR / "local.json"
    if local.is_file():
        return local
    return CONFIG_DIR / "macos_demo.json"


def load_llm_config() -> dict[str, Any]:
    """Flat dest-key config file; unknown and ``_``-prefixed keys are ignored."""
    path = _config_path()
    raw: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = {k: v for k, v in loaded.items()
                   if isinstance(k, str) and not k.startswith("_")}
    except (OSError, ValueError):
        raw = {}
    return raw


def resolve_api_key(cfg: dict[str, Any], protocol: str = "openai") -> str:
    from_file = str(cfg.get("api_key") or "").strip()
    if from_file:
        return from_file
    protocol_env = os.environ.get("ANTHROPIC_API_KEY") if protocol == "anthropic" else None
    from_env = str(protocol_env or os.environ.get("DASHSCOPE_API_KEY")
                   or os.environ.get("OPENAI_API_KEY")
                   or os.environ.get("API_KEY") or "").strip()
    if from_env:
        return from_env
    try:
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def add_llm_cli_args(parser: argparse.ArgumentParser) -> None:
    """Register the unified, shared LLM hyper-parameters (no temperature/top_p)."""
    cfg = load_llm_config()
    parser.add_argument("--model-name",
                        default=str(cfg.get("model_name") or DEFAULT_MODEL_NAME))
    parser.add_argument("--llm-base-url",
                        default=str(cfg.get("llm_base_url") or DEFAULT_LLM_BASE_URL))
    parser.add_argument("--llm-protocol",
                        default=str(cfg.get("llm_protocol") or "openai"),
                        choices=["openai", "openai-thinking", "anthropic", "vertex"],
                        help="wire protocol: openai=Chat Completions; openai-thinking=Responses with encrypted reasoning replay")
    parser.add_argument("--api-key", default=None,
                        help="override the resolved API key for this run")
    parser.add_argument("--reasoning-effort",
                        default=cfg.get("reasoning_effort") or "low",
                        choices=["none", "low", "medium", "high", "xhigh", "max"],
                        help="server-side reasoning budget; use none for plain OpenAI/vLLM")
    parser.add_argument("--max-tokens", type=int,
                        default=int(cfg.get("max_tokens") or DEFAULT_MAX_TOKENS),
                        help="per-call output token budget")
    parser.add_argument("--api-timeout", type=float,
                        default=float(cfg.get("api_timeout") or DEFAULT_API_TIMEOUT))
    parser.add_argument("--api-retries", type=int,
                        default=int(cfg.get("api_retries") or DEFAULT_API_RETRIES))
    parser.add_argument("--context-limit", type=int,
                        default=int(cfg.get("context_limit") or DEFAULT_CONTEXT_LIMIT),
                        help="soft context budget fed to the token estimator")
    parser.add_argument("--compact-trigger-tokens", type=int,
                        default=int(cfg.get("compact_trigger_tokens")
                                    or DEFAULT_COMPACT_TRIGGER_TOKENS),
                        help="LLM compaction fires above this estimated input size")
    parser.add_argument("--disable-llm-compaction", action="store_true",
                        help="turn off token-triggered history compaction")
    parser.add_argument("--no-stream", action="store_true",
                        help="disable streaming (reasoning deltas) for model calls")


def resolve_llm_settings(args: argparse.Namespace) -> dict[str, Any]:
    """Merge config file + CLI into the canonical settings dict."""
    cfg = load_llm_config()
    protocol = getattr(args, "llm_protocol", None) or cfg.get("llm_protocol") or "openai"
    api_key = (getattr(args, "api_key", None) or resolve_api_key(cfg, protocol))
    return {
        "protocol": protocol,
        "model_name": args.model_name,
        "base_url": args.llm_base_url,
        "api_key": api_key,
        "reasoning_effort": getattr(args, "reasoning_effort", None) or None,
        "max_tokens": getattr(args, "max_tokens", DEFAULT_MAX_TOKENS),
        "timeout": getattr(args, "api_timeout", DEFAULT_API_TIMEOUT),
        "retries": getattr(args, "api_retries", DEFAULT_API_RETRIES),
        "context_limit": getattr(args, "context_limit", DEFAULT_CONTEXT_LIMIT),
        "stream": not getattr(args, "no_stream", False),
        "anthropic_thinking": cfg.get("anthropic_thinking"),
    }


def build_model_client(
    settings: dict[str, Any],
    *,
    on_event: Any = None,
    on_attempt: Any = None,
    extra_body: dict[str, Any] | None = None,
) -> ModelClient:
    """The one place a ModelClient is constructed for either agent."""
    body = dict(extra_body or {})
    if settings.get("protocol") == "anthropic":
        thinking = settings.get("anthropic_thinking")
        if thinking is not None:
            if not isinstance(thinking, dict):
                raise ValueError("anthropic_thinking must be an object or null")
            body.setdefault("thinking", thinking)
    return ModelClient(
        protocol=settings.get("protocol", "openai"),
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        model_name=settings["model_name"],
        timeout=settings["timeout"],
        retries=settings["retries"],
        on_event=on_event,
        on_attempt=on_attempt,
        stream=settings.get("stream", True),
        context_limit=settings.get("context_limit", DEFAULT_CONTEXT_LIMIT),
        reasoning_effort=(None if settings.get("reasoning_effort") in (None, "", "none")
                          else settings.get("reasoning_effort")),
        reasoning_code_salvage=bool(settings.get("reasoning_code_salvage", False)),
        extra_body=body,
    )


class SyncClient:
    """Synchronous facade over the shared async ModelClient.

    Lets sync agents (gui-only baseline) use the exact same request/streaming/retry/
    salvage/compaction code path as the async mac agent without rewriting their loops.
    """

    def __init__(self, settings: dict[str, Any], *, on_event: Any = None,
                 extra_body: dict[str, Any] | None = None) -> None:
        import asyncio
        from llm_compaction import maybe_compact

        self._asyncio = asyncio
        self._maybe_compact = maybe_compact
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._client = build_model_client(
            settings, on_event=on_event, extra_body=extra_body)
        self._loop.run_until_complete(self._client.__aenter__())

    @property
    def client(self) -> ModelClient:
        return self._client

    @property
    def token_estimator(self) -> Any:
        return self._client.token_estimator

    @property
    def last_call_attempts(self) -> list[dict[str, Any]]:
        return self._client.last_call_attempts

    def complete(self, messages: list[dict[str, Any]], max_tokens: int,
                 purpose: str = "action") -> tuple[str, str, dict[str, Any]]:
        return self._loop.run_until_complete(
            self._client.complete(messages, max_tokens=max_tokens, purpose=purpose))

    def compact(self, context: Any, *, current: dict[str, Any],
                action_max_tokens: int) -> dict[str, Any] | None:
        return self._loop.run_until_complete(
            self._maybe_compact(context, self._client, current=current,
                                action_max_tokens=action_max_tokens))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def shutdown() -> None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                current = self._asyncio.current_task()
                pending = {
                    task for task in self._asyncio.all_tasks()
                    if task is not current and not task.done()
                }
                for task in pending:
                    task.cancel()
                if pending:
                    await self._asyncio.gather(*pending, return_exceptions=True)
                await self._loop.shutdown_asyncgens()

        try:
            self._loop.run_until_complete(shutdown())
        finally:
            self._loop.close()

    def __enter__(self) -> "SyncClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def build_compaction_config(args: argparse.Namespace) -> CompactionConfig:
    """Shared compaction policy so gui-only and mac agents compact identically."""
    return CompactionConfig(
        enabled=not getattr(args, "disable_llm_compaction", False),
        trigger_tokens=args.compact_trigger_tokens,
    )
