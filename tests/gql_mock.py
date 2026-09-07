"""Tiny HTTP server echoing GraphQL requests back — fixture for ajd gql."""

import http.server
import json
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        response = {
            "echo": body,
            "path": self.path,
            "header": self.headers.get("X-Test"),
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18888
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
