"""Log a Pending Actions "Message sent?" click against this machine's database.

The button used to open a leadstatus:// / recordreply:// / declinelead://
URL, which launches a helper app on the Mac running the browser. A browser
on another machine then wrote that machine's database. The page now posts
the same fields to the local API, and Vite on mini1 proxies that post here.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path


class LoggedSendError(ValueError):
    """The click could not be recorded. The message is safe to show in the page."""


def apply_logged_send(db: Path, fields: dict) -> str:
    """Run the helper's CLI for one Message sent? click. Returns its stdout."""
    scheme = str(fields.get("scheme") or "").strip().lower()
    argv = _argv(db, scheme, fields)
    err = io.StringIO()
    out = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = _main(scheme)(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    if code != 0:
        message = err.getvalue().strip() or out.getvalue().strip() or f"Could not log the message (exit {code})."
        raise LoggedSendError(message.splitlines()[-1])
    return out.getvalue().strip()


def _main(scheme: str):
    if scheme == "leadstatus":
        from job_tracker.cli.set_lead_status import main
    elif scheme == "recordreply":
        from job_tracker.cli.record_unmatched_reply import main
    elif scheme == "declinelead":
        from job_tracker.cli.decline_lead import main
    else:
        raise LoggedSendError("That Message sent action has no API.")
    return main


def _field(fields: dict, name: str) -> str:
    return str(fields.get(name) or "").strip()


def _argv(db: Path, scheme: str, fields: dict) -> list[str]:
    if scheme == "leadstatus":
        key = _field(fields, "key")
        status = _field(fields, "status")
        if not key or not status:
            raise LoggedSendError("Message sent needs a lead and a status.")
        argv = ["--db", str(db), "--key", key, "--status", status]
        reason = _field(fields, "reason")
        if reason:
            argv += ["--reason", reason]
        channel = _field(fields, "sent_channel")
        message = _field(fields, "sent_message")
        if channel or message:
            if not channel or not message:
                raise LoggedSendError("Message sent needs both the channel and the text.")
            argv += ["--sent-channel", channel, "--sent-message", message]
        return argv
    if scheme == "recordreply":
        message_id = _field(fields, "message_id")
        text = _field(fields, "text")
        if not message_id or not text:
            raise LoggedSendError("Message sent needs the recruiter's message and your reply.")
        argv = ["--db", str(db), "--message-id", message_id, "--text", text]
        channel = _field(fields, "channel") or "linkedin"
        argv += ["--channel", channel]
        name = _field(fields, "name")
        if name:
            argv += ["--contact-name", name]
        return argv
    if scheme == "declinelead":
        reason = _field(fields, "reason")
        if not reason:
            raise LoggedSendError("Decline needs a reason.")
        argv = ["--db", str(db), "--reason", reason]
        key = _field(fields, "key")
        message_id = _field(fields, "message_id")
        if key:
            argv += ["--key", key]
        elif message_id:
            argv += ["--message-id", message_id]
        else:
            raise LoggedSendError("Decline needs a lead.")
        for flag, name in (
            ("--note", "note"),
            ("--channel", "channel"),
            ("--letter-text", "letter_text"),
            ("--company", "company"),
            ("--title", "title"),
            ("--contact-name", "contact_name"),
        ):
            value = _field(fields, name)
            if value:
                argv += [flag, value]
        if _field(fields, "letter") == "0":
            argv.append("--no-letter")
        return argv
    raise LoggedSendError("That Message sent action has no API.")
