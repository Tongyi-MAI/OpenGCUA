# Harness configuration

Copy `macos_demo.json` to the gitignored `local.json`, then edit it:

```bash
cp packages/harness/configs/macos_demo.json packages/harness/configs/local.json
```

The most important keys are:

| Key | Description |
|---|---|
| `llm_protocol` | `openai` for OpenAI-compatible endpoints; `anthropic` for Anthropic Messages endpoints |
| `model_name` | Model identifier accepted by the endpoint |
| `llm_base_url` | Base URL ending at `/v1`; the client appends `/chat/completions` or `/messages` |
| `api_key` | Credential stored only in gitignored `local.json` |
| `reasoning_effort` | `low`, `medium`, or `high`; use `none` for endpoints such as plain vLLM; basic Anthropic mode ignores it |
| `max_tokens` | Per-response output token limit |
| `api_timeout` / `api_retries` | Request wall-clock timeout and retry count |
| `llm_compaction` | Enables token-triggered LLM summarization for `mac_agent` |
| `compaction_trigger_tokens` | Estimated total-context threshold for `mac_agent` compaction |
| `history_turns` | Raw-turn limit used when `mac_agent` compaction is disabled |
| `img_keep_n` / `img_interval` | Image-retention count and trim cadence |

CLI flags override file values. `MAC_AGENT_CONFIG=/path/to/config.json` selects a different config file.

Direct Anthropic mode promotes system messages to the top-level `system` field,
converts image data URLs to Anthropic image blocks, authenticates with `x-api-key`,
and parses both JSON and `content_block_delta` SSE responses. Extended Anthropic
thinking/signature replay is not supported; use the basic text response mode.

See the [Quick Start configuration reference](../../../QUICKSTART.md#configuration)
for every shipped key, default and interaction.
