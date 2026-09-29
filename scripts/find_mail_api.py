"""Local HTTP API for Pending Actions "Find JD email" (IMAP + leads.db).

    python scripts/find_mail_api.py --port 3175

Binds 127.0.0.1 only. Vite proxies /api → this port. Do not expose on LAN.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.find_mail import search  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_PORT = 3175
DEFAULT_HOST = "127.0.0.1"


class FindMailHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB_PATH

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in {"/api/find-mail", "/find-mail"}:
            self._send(404, {"error": "not found"})
            return
        query = (parse_qs(parsed.query).get("q") or [""])[0]
        conn = connect(self.db_path)
        try:
            result = search(query, conn=conn)
        finally:
            conn.close()
        self._send(200, result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args(argv)
    FindMailHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), FindMailHandler)
    print(f"find-mail API http://{args.host}:{args.port}/api/find-mail?q=", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
