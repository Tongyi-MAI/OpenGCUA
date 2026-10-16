"""AT-SPI lives in a killable helper, retaining native proxies until invalidation."""

import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from common import Error, completed


class Client:
    def __init__(self, event, track=None):
        self.event = event
        self.track = track
        self.proc = None
        self.replies = None
        self.reason = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [sys.executable, __file__, "--helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        if self.track:
            self.track("process", self.proc.pid, True)
        replies = queue.Queue()
        self.replies = replies
        proc = self.proc

        def read():
            try:
                for line in proc.stdout:
                    value = json.loads(line)
                    if "event" in value:
                        self.event(value["event"], value.get("data", {}))
                    else:
                        replies.put(value)
            finally:
                replies.put(
                    {
                        "error": {
                            "code": "OBSERVATION_FAILED",
                            "message": "Accessibility helper exited",
                        }
                    }
                )

        threading.Thread(target=read, daemon=True).start()

    def request(self, q, control):
        self.start()
        q = {**q, "timeout_ms": max(1, int(control.remaining() * 1000))}
        try:
            self.proc.stdin.write(json.dumps(q) + "\n")
            self.proc.stdin.flush()
            if q.get("op") == "action":
                control.started = True
            while True:
                control.check()
                try:
                    reply = self.replies.get(timeout=min(0.02, control.remaining()))
                except queue.Empty:
                    continue
                if "error" in reply:
                    raise Error(**reply["error"])
                return reply["result"]
        except BaseException:
            # Even validation errors invalidate this helper's proxies, avoiding stale cached state.
            self.close()
            self.event("ax_invalidated", {"reason": "helper_reset"})
            raise

    def available(self, control):
        try:
            self.request({"op": "probe"}, control)
            self.reason = None
            return True
        except Exception as exc:
            self.reason = str(exc)
            return False

    def close(self):
        if self.proc:
            proc = self.proc
            self.proc = None
            proc.terminate()
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
            if self.track:
                self.track("process", proc.pid, False)
            proc.stdin.close()
            proc.stdout.close()


class Registry:
    def __init__(self, atspi, emit):
        self.a = atspi
        self.emit = emit
        self.epoch = uuid.uuid4().hex
        self.objects = {}
        self.by_object = {}
        self.actions = {}
        self.invalid = set()
        self.lock = threading.RLock()
        self.a.Registry.registerEventListener(
            self.changed,
            "object:state-changed",
            "object:children-changed",
            "window:create",
            "window:destroy",
        )
        threading.Thread(target=self.a.Registry.start, daemon=True).start()

    @staticmethod
    def native_key(obj):
        try:
            owner, path = obj.app.bus_name, obj.path
            return (owner, path) if owner and owner.startswith(":") and path else None
        except (AttributeError, TypeError):
            return None

    def changed(self, event):
        if not (
            "children-changed:remove" in event.type
            or "window:destroy" in event.type
            or ("state-changed:defunct" in event.type and event.detail1)
        ):
            return
        with self.lock:
            obj = event.any_data if "children-changed:remove" in event.type else event.source
            key = self.native_key(obj)
            ident = self.by_object.pop(key, None)
            if ident:
                self.invalid.add(ident)
                self.emit({"event": "ax_invalidated", "data": {"node_id": ident}})

    def identity(self, obj):
        if obj == self.a.Registry.getDesktop(0):
            return self.epoch + ":desktop"
        key = self.native_key(obj)
        with self.lock:
            ident = self.by_object.get(key) if key else None
            if ident is None:
                ident = self.epoch + ":" + uuid.uuid4().hex
                if key:
                    self.by_object[key] = ident
                    self.objects[ident] = obj
            return ident

    def alive(self, obj):
        desktop = self.a.Registry.getDesktop(0)
        desktop.clear_cache()
        roots = {self.native_key(app) for app in desktop}
        roots.discard(None)
        seen = set()
        while obj is not None:
            key = self.native_key(obj)
            if key in seen:
                return False
            seen.add(key)
            if len(seen) > 64 or obj.getState().contains(self.a.STATE_DEFUNCT):
                return False
            if obj == desktop or key in roots:
                return True
            ident = self.by_object.get(key)
            if ident in self.invalid:
                return False
            obj.clear_cache()
            obj = obj.parent
        return False

    def capture(self, timeout_ms):
        deadline = time.monotonic() + max(0.01, timeout_ms / 1000 - 0.05)
        desktop = self.a.Registry.getDesktop(0)
        desktop.clear_cache()
        pending = deque([(desktop, None, 0)])
        nodes = []
        seen = set()
        partial = False
        while pending:
            if len(nodes) >= 2048 or time.monotonic() >= deadline:
                partial = True
                break
            obj, parent, depth = pending.popleft()
            if obj in seen:
                continue
            seen.add(obj)
            try:
                states = obj.getState()
                if states.contains(self.a.STATE_DEFUNCT):
                    continue
                ident = self.identity(obj)
                password = obj.getRoleName() == "password text"
                name = "" if password else obj.name
                text = None
                selection = None
                try:
                    t = obj.queryText()
                    if not password:
                        count = t.characterCount
                        text = t.getText(0, min(count, 65536))
                        if count > 65536:
                            partial = True
                        if t.getNSelections():
                            selection = t.getSelection(0)
                except NotImplementedError:
                    pass
                actions = []
                try:
                    action = obj.queryAction()
                    for i in range(action.nActions):
                        actions.append(
                            dict(
                                id=f"atspi:{i}:{action.getName(i)}",
                                arguments=[],
                                label=action.getName(i),
                            )
                        )
                except NotImplementedError:
                    pass
                self.actions[ident] = actions
                bounds = None
                if states.contains(self.a.STATE_SHOWING):
                    try:
                        x, y, w, h = obj.queryComponent().getExtents(self.a.XY_SCREEN)
                        if w > 0 and h > 0:
                            bounds = dict(x=x, y=y, width=w, height=h)
                    except NotImplementedError:
                        pass
                node = dict(
                    id=ident,
                    parent_id=parent,
                    app_id=None,
                    window_id=None,
                    class_name=obj.getRoleName(),
                    automation_id=None,
                    role={
                        "push button": "button",
                        "text": "textbox",
                        "entry": "textbox",
                        "password text": "textbox",
                        "check box": "checkbox",
                        "radio button": "radio",
                        "combo box": "combobox",
                    }.get(obj.getRoleName(), obj.getRoleName().replace(" ", "_")),
                    name=name,
                    text=text,
                    hint=None,
                    text_status="redacted" if password else "readable",
                    bounds=bounds,
                    enabled=states.contains(self.a.STATE_ENABLED),
                    visible=states.contains(self.a.STATE_SHOWING),
                    focused=states.contains(self.a.STATE_FOCUSED),
                    editable=states.contains(self.a.STATE_EDITABLE),
                    password=password,
                    actions=actions,
                    target={"ax_epoch": self.epoch},
                    targetable=ident in self.objects,
                )
                # Shared semantic normalization accepts UTF-16 wire offsets.
                if selection and text is not None:
                    start, end = selection
                    if 0 <= start <= end <= len(text):
                        node.update(
                            selection_start=len(text[:start].encode("utf-16-le")) // 2,
                            selection_end=len(text[:end].encode("utf-16-le")) // 2,
                        )
                nodes.append(node)
                count = obj.childCount
                if depth >= 50 or count > 1024:
                    partial |= count > 0
                # Bound both traversal and materialization, including Calc's virtual cell tables.
                capacity = min(1024, 2048 - len(nodes) - len(pending))
                if count > capacity:
                    partial = True
                if depth < 50:
                    for i in range(min(count, capacity)):
                        if time.monotonic() >= deadline:
                            partial = True
                            break
                        child = obj.getChildAtIndex(i)
                        if child is not None:
                            pending.append((child, ident, depth + 1))
            except Exception:
                partial = True
        # Retain at most a bounded number of native proxies. Eviction makes old captures stale.
        if len(self.objects) > 8192:
            keep = {n["id"] for n in nodes}
            self.objects = {k: v for k, v in self.objects.items() if k in keep}
            self.by_object = {self.native_key(v): k for k, v in self.objects.items()}
            self.invalid.intersection_update(self.objects)
            self.actions = {k: v for k, v in self.actions.items() if k in keep}
        return dict(
            nodes=nodes,
            truncated=partial,
            identity_mode="native_lifetime",
            identity_epoch=self.epoch,
            components=[dict(id="desktop", status="partial" if partial else "ok")],
        )

    def action(self, q):
        ident = q.get("node_id")
        obj = self.objects.get(ident)
        if (
            q.get("ax_epoch") != self.epoch
            or obj is None
            or ident in self.invalid
            or not self.alive(obj)
        ):
            raise Error("STALE_TARGET")
        action = q["action"]
        try:
            if action == "set_text":
                success = obj.queryEditableText().setTextContents(q["text"])
            elif action == "select_text":
                text = obj.queryText()
                if (
                    obj.getRoleName() == "password text"
                    or text.getText(0, text.characterCount) != q["selection_text"]
                ):
                    raise Error("STALE_TARGET", "Text changed since capture")
                if text.getNSelections():
                    success = text.setSelection(0, q["start"], q["end"])
                else:
                    success = text.addSelection(q["start"], q["end"])
            elif action == "focus":
                success = obj.queryComponent().grabFocus()
                if success and not obj.getState().contains(self.a.STATE_FOCUSED):
                    raise Error(
                        "ACTION_OUTCOME_UNKNOWN",
                        "Focus request was sent but focus is not established",
                    )
            elif action in ("click", "perform"):
                api = obj.queryAction()
                actions = [
                    (i, f"atspi:{i}:{api.getName(i)}", api.getName(i).lower())
                    for i in range(api.nActions)
                ]
                if action == "click":
                    if q.get("button", "primary") != "primary" or q.get("count", 1) != 1:
                        raise Error("UNSUPPORTED_CAPABILITY")
                    matches = [
                        i for i, _, name in actions if name in ("click", "press", "activate")
                    ]
                    if len(matches) != 1:
                        raise Error("UNSUPPORTED_CAPABILITY", "Use an advertised AX action")
                    index = matches[0]
                else:
                    match = [
                        i
                        for i, key, _ in actions
                        if key == q["node_action"]
                        and any(a["id"] == key for a in self.actions.get(ident, []))
                    ]
                    if len(match) != 1:
                        raise Error("STALE_TARGET", "Native actions changed")
                    index = match[0]
                success = api.doAction(index)
            else:
                raise Error("UNSUPPORTED_CAPABILITY")
        except NotImplementedError as exc:
            raise Error(
                "UNSUPPORTED_CAPABILITY", "Accessible lacks the requested native interface"
            ) from exc
        if not success:
            raise Error("INPUT_REFUSED", "AT-SPI operation was refused")
        return completed("linux.atspi")


def helper():
    output = threading.Lock()

    def emit(value):
        with output:
            print(json.dumps(value), flush=True)

    try:
        import pyatspi

        registry = Registry(pyatspi, emit)
    except Exception as exc:
        emit({"error": {"code": "UNSUPPORTED_CAPABILITY", "message": f"AT-SPI unavailable: {exc}"}})
        return
    for line in sys.stdin:
        try:
            q = json.loads(line)
            if q["op"] == "probe":
                pyatspi.Registry.getDesktop(0)
                result = {"ready": True}
            elif q["op"] == "observe":
                result = registry.capture(q["timeout_ms"])
            else:
                result = registry.action(q)
            emit({"result": result})
        except Exception as exc:
            emit(
                {
                    "error": {
                        "code": getattr(
                            exc,
                            "code",
                            "OBSERVATION_FAILED"
                            if q.get("op") != "action"
                            else "ACTION_OUTCOME_UNKNOWN",
                        ),
                        "message": str(exc),
                    }
                }
            )


if __name__ == "__main__":
    helper()
