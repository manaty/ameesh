# SPDX-License-Identifier: AGPL-3.0-only
"""Faux fournisseur DeepSeek compatible Anthropic Messages (essai L115).

    python3 fake_provider.py PORT JOURNAL

`POST /v1/messages` (flux SSE ou réponse JSON) répond « ESSAI-OK » avec un
usage fixe ; `GET /v1/models` liste quelques modèles. Chaque requête est
journalisée (chemin, modèle, en-têtes sans la clé) dans JOURNAL, en JSON
Lines. Aucun réseau sortant, aucune clé réelle."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, JOURNAL = int(sys.argv[1]), sys.argv[2]
USAGE_IN = {"input_tokens": 1200, "output_tokens": 0, "cache_read_input_tokens": 300}
USAGE_OUT = {"output_tokens": 40}


def journal(entry):
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Faux(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _headers(self):
        return {k.lower(): ("<masqué>" if k.lower() in ("x-api-key", "authorization") else v)
                for k, v in self.headers.items()}

    def do_GET(self):
        journal({"ts": time.time(), "m": "GET", "path": self.path, "h": self._headers()})
        body = json.dumps({"data": [{"id": "deepseek-chat", "type": "model"},
                                    {"id": "deepseek-flash", "type": "model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw)
        except ValueError:
            req = {}
        model = req.get("model") or "deepseek-chat"
        journal({"ts": time.time(), "m": "POST", "path": self.path, "model": model,
                 "stream": req.get("stream"), "key_present": bool(self.headers.get("x-api-key")),
                 "h": self._headers()})
        if not req.get("stream"):
            body = json.dumps({"id": "msg_essai", "type": "message", "role": "assistant",
                               "model": model, "content": [{"type": "text", "text": "ESSAI-OK"}],
                               "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": dict(USAGE_IN, **USAGE_OUT)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_essai", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": USAGE_IN}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "ESSAI-OK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                 "stop_sequence": None},
                               "usage": USAGE_OUT}),
            ("message_stop", {"type": "message_stop"}),
        ]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        for name, data in events:
            self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode())
            self.wfile.flush()
        self.close_connection = True


ThreadingHTTPServer(("127.0.0.1", PORT), Faux).serve_forever()
