"""Mock oinone backend for testing `ajd oinone`: echoes the gql query it
received, handles login (sets a session cookie) and countByWrapper
(requires the cookie).  Usage: python3 oinone_mock.py <port>"""

import http.cookiejar
import http.server
import json
import sys
import threading

REQUESTS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        REQUESTS.append({"path": self.path, "body": body,
                         "cookie": self.headers.get("Cookie", "")})
        query = body.get("query", "")
        logged_in = "pamirs_uc_session_id" in (self.headers.get("Cookie") or "")
        if "pamirsUserTransientMutation" in query and "login" in query:
            data = {"pamirsUserTransientMutation": {"login": {"errorCode": 0}}}
            self.send_response(200)
            self.send_header("Set-Cookie",
                             "pamirs_uc_session_id=mock-session; Path=/")
        elif "countByWrapper" in query:
            data = {"actionQuery": {"countByWrapper": "3" if logged_in else None}}
            self.send_response(200)
            if not logged_in:
                data["errors"] = [{"message": "not logged in"}]
        else:
            data = {"echo": query}
            self.send_response(200)
        payload = json.dumps({"data": data}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18899
    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
