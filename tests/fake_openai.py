"""Loopback fake of the OpenAI chat API, scripted per model."""

import json
import socketserver
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse DNS lookup that can take 35 s on macOS CI.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeOpenAI:
    def __init__(self):
        self.requests: list[dict] = []
        self._queues: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def script(self, model: str, *replies: dict) -> None:
        with self._lock:
            self._queues[model].extend(replies)

    def _next(self, model: str) -> dict:
        with self._lock:
            queue = self._queues[model]
            return queue.popleft() if queue else {"content": "ok"}

    def _models(self) -> list[str]:
        with self._lock:
            return list(self._queues)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            data = [{"id": m, "object": "model"} for m in self.server.fake._models()]
            return self._json(200, {"object": "list", "data": data})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        fake = self.server.fake
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        body["_path"] = self.path
        body["_auth"] = self.headers.get("Authorization")
        fake.requests.append(body)
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        reply = fake._next(body.get("model", ""))
        if reply.get("delay"):
            time.sleep(reply["delay"])
        if "status" in reply:
            return self._json(reply["status"], {"error": {"message": f"scripted {reply['status']}"}})
        if body.get("stream"):
            return self._stream(body, reply)
        message = {"role": "assistant", "content": reply.get("content")}
        finish = "stop"
        if reply.get("tool_calls"):
            message["tool_calls"] = [_tool_call(i, tc) for i, tc in enumerate(reply["tool_calls"])]
            finish = "tool_calls"
        self._json(200, {
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
            "model": body.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def _stream(self, body, reply):
        model = body.get("model")
        deltas = [{"role": "assistant", "content": ""}]
        finish = "stop"
        content = reply.get("content")
        if content:
            size = max(1, -(-len(content) // 3))
            deltas += [{"content": content[i:i + size]} for i in range(0, len(content), size)]
        for i, tc in enumerate(reply.get("tool_calls") or []):
            args = tc["arguments"]
            half = len(args) // 2
            head = _tool_call(i, {"name": tc["name"], "arguments": ""})
            deltas.append({"tool_calls": [{"index": i, **head}]})
            for part in (args[:half], args[half:]):
                deltas.append({"tool_calls": [{"index": i, "function": {"arguments": part}}]})
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(payload: str) -> None:
            data = payload.encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def chunk(delta, finish_reason=None):
            return "data: " + json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }) + "\n\n"

        fail_after = reply.get("fail_after")
        for count, delta in enumerate(deltas):
            if fail_after is not None and count >= fail_after:
                self.close_connection = True  # no terminating chunk: the client sees a broken stream
                return
            if count and reply.get("delay"):
                time.sleep(reply["delay"])
            send(chunk(delta))
        send(chunk({}, finish))
        send("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def _tool_call(i: int, tc: dict) -> dict:
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]}}
