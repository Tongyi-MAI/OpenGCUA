"""Deterministic MCP fixture with snapshot-bound Cua tokens; no desktop access."""

import base64
import json
import os
import struct
import sys

version = 0
calls = []
closed = set()
expired = False
png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 100, 200)).decode()
for line in sys.stdin:
    q = json.loads(line)
    if "id" not in q:
        continue
    if q["method"] == "initialize":
        result = dict(
            protocolVersion="2025-06-18",
            capabilities={"tools": {}},
            serverInfo={"name": "fixture", "version": "0.28.1"},
        )
    elif q["method"] == "tools/call":
        name = q["params"]["name"]
        args = q["params"].get("arguments", {})
        calls.append((name, args))
        data = {}
        images = []
        diagnostic = None
        if name == "fixture_get_calls":
            data = {"calls": [{"name": called, "arguments": values} for called, values in calls[:-1]]}
        elif name == "fixture_close_window":
            closed.add(args["window_id"])
        elif name == "fixture_expire":
            expired = True
        elif name == "fixture_refuse":
            data = dict(
                code="background_input_refused",
                effect="refused",
                route="background_keyboard",
                delivery=dict(mode="background"),
                observed=dict(focused_pid=42),
            )
        elif name == "fixture_native_failure":
            data = dict(code="tool_invocation_failed")
            diagnostic = "set_value failed: AXValue is not settable" + "x" * 600
        elif name == "list_apps":
            data = {
                "apps": [
                    dict(pid=os.getpid(), name="Fixture", bundle_id="dev.mai-ui.fixture"),
                    dict(pid=None, name="Installed", running=False),
                ]
            }
        elif name == "list_windows":
            data = {
                "windows": [
                    dict(
                        pid=os.getpid(),
                        window_id=i,
                        title=f"Window {i}",
                        bounds=dict(x=0, y=0, width=100, height=200),
                    )
                    for i in range(1, 10)
                    if i not in closed
                ]
            }
        elif name == "get_window_state":
            if args.get("include_accessibility_tree"):
                version += 1
                snapshot = f"s{version:08x}"
                data = dict(
                    snapshot_id=snapshot,
                    elements_complete=False,
                    elements=[
                        dict(
                            element_index=0,
                            element_token=snapshot + ":0",
                            role="AXButton",
                            label="Click",
                            actions=["AXPress"],
                            frame=dict(x=1, y=2, w=10, h=20),
                            parent_index=None,
                        )
                    ],
                )
            data["window_bounds"] = dict(x=0, y=0, width=100, height=200)
            if args.get("include_screenshot"):
                images = [dict(type="image", mimeType="image/png", data=png)]
        elif name == "click":
            data = dict(
                effect="unverifiable",
                route="accessibility",
                delivery=dict(mode=args.get("delivery_mode")),
                evidence=[],
            )
        elif name == "check_permissions":
            data = dict(accessibility=True, screen_recording=True, source=dict(pid=os.getpid()))
        if expired and name != "fixture_expire":
            data = dict(code="session_expired")
        result = dict(
            structuredContent=data,
            content=[dict(type="text", text=diagnostic or json.dumps(data)), *images],
            isError=(expired and name != "fixture_expire") or name == "fixture_native_failure",
        )
    else:
        print(
            json.dumps(
                dict(jsonrpc="2.0", id=q["id"], error=dict(code=-32601, message="unknown method"))
            ),
            flush=True,
        )
        continue
    print(json.dumps(dict(jsonrpc="2.0", id=q["id"], result=result)), flush=True)
