"""Post the LinkedIn thread on the clipboard to the local Pending Actions API.

Copy the thread in LinkedIn, then:

    python scripts/paste_linkedin_lead.py

A pipe works too (`pbpaste | python scripts/paste_linkedin_lead.py`). The
API is the one already serving Find JD email, on 127.0.0.1:3175.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:3175/api/linkedin-lead"


def read_paste(*, stdin_text: str, clipboard_text: str, stdin_is_tty: bool) -> str:
    if not stdin_is_tty:
        return stdin_text
    return clipboard_text


def post_paste(text: str, url: str, *, timeout: float = 180) -> dict:
    request = urllib.request.Request(
        url,
        data=text.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": body or f"HTTP {exc.code}"}
        payload["ok"] = False
        return payload


def clipboard_text() -> str:
    try:
        return subprocess.check_output(["pbpaste"], text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Couldn't read the clipboard ({exc}).") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"API endpoint (default: {DEFAULT_URL})")
    args = ap.parse_args(argv)

    text = read_paste(
        stdin_text="" if sys.stdin.isatty() else sys.stdin.read(),
        clipboard_text="" if not sys.stdin.isatty() else clipboard_text(),
        stdin_is_tty=sys.stdin.isatty(),
    )
    if not text.strip():
        print("The clipboard is empty. Copy the LinkedIn thread, then run this again.", file=sys.stderr)
        return 1

    try:
        result = post_paste(text, args.url)
    except urllib.error.URLError:
        print(
            "Pending Actions API is not running on 127.0.0.1:3175.\n"
            "Start it with: python scripts/find_mail_api.py",
            file=sys.stderr,
        )
        return 1

    if not result.get("ok"):
        print(result.get("error") or "The paste was refused.", file=sys.stderr)
        return 1

    company, title = result.get("company") or "", result.get("title") or ""
    if result.get("created"):
        print(f"Added {title} @ {company}.")
    elif result.get("changed"):
        print(f"Attached this message to {title} @ {company}.")
    else:
        print(f"Already on file: {title} @ {company} (status {result.get('status') or 'unknown'}).")
    if result.get("threadUrl"):
        print(result["threadUrl"])
    print("It will show on Pending Actions (http://127.0.0.1:3174/) within a minute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
