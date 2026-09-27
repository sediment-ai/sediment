# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic Anthropic endpoint for the isolated Compose acceptance test."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.startswith("/v1/messages"):
            self.send_error(404)
            return
        body = {
            "id": "msg_" + uuid4().hex,
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": [{"type": "text", "text": "Compose capture works"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 4, "output_tokens": 3},
        }
        self.send_response(200)
        if not request.get("stream"):
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {
                "type": "message_start",
                "message": {**body, "content": [], "stop_reason": None},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Compose capture works"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
        for index, event in enumerate(events):
            wire = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            self.wfile.write(wire.encode())
            self.wfile.flush()
            if index == 2:
                time.sleep(2)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Provider).serve_forever()
