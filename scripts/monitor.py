#!/usr/bin/env python3
"""Regiekamer — lokale monitor + controller voor de davinci-edit pipeline.

Start:  .venv/bin/python scripts/monitor.py   (of python3; geen dependencies)
Open:   http://127.0.0.1:8765

Leest ~/.cache/resolve-mcp-bridge/monitor/ (state.json + events.jsonl, geschreven
door base_layer tijdens het editen) en schrijft UI-commando's naar commands.jsonl
(opgepakt door Claude via base_layer.monitor_poll()). Bindt alleen op localhost.
"""

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MON = os.path.expanduser("~/.cache/resolve-mcp-bridge/monitor")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
PORT = int(os.environ.get("REGIEKAMER_PORT", "8765"))
ALLOWED_FILE_ROOT = os.path.expanduser("~/.cache/resolve-mcp-bridge")


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _read_jsonl(path, tail=None):
    try:
        with open(path) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        return rows[-tail:] if tail else rows
    except Exception:  # noqa: BLE001
        return []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # stil
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(WEB, "monitor.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            return self._send(200, {
                "state": _read_json(os.path.join(MON, "state.json"), {}),
                "events": _read_jsonl(os.path.join(MON, "events.jsonl"), tail=150),
                "commands": _read_jsonl(os.path.join(MON, "commands.jsonl"), tail=40),
                "now": time.time()})
        if self.path.startswith("/api/file"):
            # alleen bestanden onder ~/.cache/resolve-mcp-bridge (QA-plaatjes)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query).get("p", [""])[0]
            real = os.path.realpath(q)
            if real.startswith(os.path.realpath(ALLOWED_FILE_ROOT)) and os.path.isfile(real):
                ext = os.path.splitext(real)[1].lower()
                ct = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".md": "text/plain; charset=utf-8", ".json": "application/json"}
                with open(real, "rb") as f:
                    return self._send(200, f.read(), ct.get(ext, "application/octet-stream"))
            return self._send(404, {"error": "niet toegestaan"})
        return self._send(404, {"error": "onbekend pad"})

    def do_POST(self):
        if self.path.startswith("/api/command"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                return self._send(400, {"error": "geen json"})
            cmd = {"id": uuid.uuid4().hex[:8], "t": time.time(), "status": "new",
                   "type": str(body.get("type", "note"))[:40],
                   "payload": body.get("payload", {})}
            os.makedirs(MON, exist_ok=True)
            with open(os.path.join(MON, "commands.jsonl"), "a") as f:
                f.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            return self._send(200, {"ok": True, "id": cmd["id"]})
        return self._send(404, {"error": "onbekend pad"})


if __name__ == "__main__":
    os.makedirs(MON, exist_ok=True)
    print(f"Regiekamer -> http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
