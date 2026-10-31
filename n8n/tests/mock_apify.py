"""Local stand-in for the Apify run-sync endpoint. Logs each request to requests.jsonl and
answers with apify_items.json (2 job records plus an expired record and a status record)."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "requests.jsonl"


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with LOG.open("a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization"),
                                "body": json.loads(body or "null")}) + "\n")
        out = (HERE / "apify_items.json").read_bytes()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
