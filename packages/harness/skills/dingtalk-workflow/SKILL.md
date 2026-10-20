---
name: dingtalk-workflow
description: Use when a macOS agent needs to find DingTalk people, chats, messages, events, or todos, or act on them for a user.
---

# DingTalk workflow

Use the shortest *available* route that preserves the requested sender identity. This Skill is guidance, not a DingTalk connection, login, or permission grant. Loading it does not make MCP tools available.

## Choose a route

1. Check the tools actually exposed in this run. If DingTalk MCP tools are present, inspect their current names, schemas, scopes, and sender identity before calling them. Never invent a tool name or assume a server is connected.
2. If no suitable MCP tool is exposed, check whether `dws` is already installed (`shutil.which("dws")`). For a user-identity task, check its selected profile with `dws profile list --format json` and, if needed, `dws auth status --readonly --format json`. A read-only status with a `reason` is inconclusive, not proof of logout. Use the explicit `--profile` selector for the chosen account; do not silently switch the persistent default. Discover the specific operation with `dws <service> --help` or `dws schema --cli-path "<service> +<shortcut>" --compact --format json`; avoid dumping a whole catalog for a known operation.
3. If neither route can perform the task with the right identity, use the signed-in DingTalk GUI: observe the target window, prefer AX/DOM-like controls, and follow the harness's foreground-permission rule before any foreground input. If no suitable session exists, explain the missing connection or login and ask the user. Do not install, log in, or configure an app on their behalf just to satisfy this Skill.

The official app-credential DingTalk MCP may send through a **robot/app**, not as the signed-in person. If the user asks to send “as me,” use an authenticated user-identity route such as DWS or their GUI session, never quietly substitute the robot.

## Common tasks

| Intent | Smallest useful sequence |
| --- | --- |
| Find a person or group | Search the selected organization; compare name and context; resolve ambiguity before acting. |
| Find a message | Search the relevant chat and date range; return the matching sender, time, and context. |
| Check calendar or todos | Read the specified account and range; identify timezone, due time, and organizer/owner. |
| Send a message or create an item | Resolve the exact recipient or destination, inspect the tool's write parameters, perform one write, then verify its returned ID/status or visible result. |

For CLI calls, use structured JSON output where supported and a finite subprocess timeout. Treat a timed-out write as **outcome unknown**: inspect the destination before any retry. Do not paste tokens, secrets, full contact exports, or unrelated private messages into the model context or logs.

Example: “用我的身份提醒研发一群今天 17:00 开会。” First identify the signed-in account and the exact group. An app/robot MCP alone is insufficient for “我的身份.” If DWS has the matching user profile, use its discovered chat-send operation once and verify; otherwise use that user's GUI session under the existing foreground rules. If the group is ambiguous, ask which one before sending.

## Common mistakes

- Treating an installed MCP package as an exposed, authenticated tool.
- Sending as a robot when the user requested their own identity, or trusting the default DWS profile when several organizations are signed in.
- Guessing a group ID or repeating a timed-out send without checking whether it already landed.
