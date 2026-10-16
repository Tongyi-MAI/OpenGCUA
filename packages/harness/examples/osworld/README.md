# OSWorld integration example

Standalone reference agents for integrating the OpenGCUA design with an external
evaluation environment:

- `maiui_agent.py` assembles screenshot observations, calls an OpenAI-compatible
  model, manages history, and parses returned actions.
- `maiui_code_agent.py` builds on that agent and returns executor-neutral
  `code_exec` actions.

The caller supplies screenshots, executes the returned actions or code, and feeds
execution results back to the agent. Environment setup, task scheduling, and
scoring belong to the caller. This example does not include a complete OSWorld
evaluation runner.

For the runnable macOS agent, see the [harness runtime](../../runtime/).
