"""Manual native integration fixture. Loopback only; never returns desktop actions.

Run with Python, configure the app to http://127.0.0.1:18861/v1, model fixture,
no API key. First response asks a question; the next completes. Each new task
starts another pair. Only request count is printed, never prompts or credentials.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


class Handler(BaseHTTPRequestHandler):
    count = 0
    def log_message(self, *args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        Handler.count += 1
        count = Handler.count
        print(f"Fixture request {count}", flush=True)
        time.sleep(5)  # lets the operator exercise the real pause boundary
        content = ('<intent>等待你确认本地验收</intent><code>ask_user("这是本地集成测试，没有执行桌面操作。输入任意回复后完成验收。")</code>'
                   if count % 2 else '<intent>完成本地验收</intent><code>terminate(status="success", answer="本地验收通过：原生窗口、任务管道、问题回复和结果保存已连通。未执行桌面操作。")</code>')
        body = json.dumps({"id": f"fixture-{count}", "object": "chat.completion", "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass


if __name__ == "__main__":
    print("Local native integration fixture: http://127.0.0.1:18861/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 18861), Handler).serve_forever()
