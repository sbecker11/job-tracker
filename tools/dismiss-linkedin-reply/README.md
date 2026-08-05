# DismissLinkedInReply — mark a LinkedIn reply card done from the page

The static pending-actions page cannot write sqlite itself. This helper registers:

```
dlr://dismiss?kind=lead&key=<normalized_key>&message_id=<optional>
dlr://dismiss?kind=unmatched&message_id=<id>
```

and shells out to:

```bash
dismiss-linkedin-reply --kind ... --key ... --message-id ...
```

- **lead** — appends an outbound `job_conversations` row (`Marked replied (pending-actions)`), which drops the card from the LinkedIn replies queue on the next render (and immediately via optimistic UI).
- **unmatched** — stamps `unmatched_messages.dismissed_at` so the park + queue drop it.

## Install (once)

```bash
cd job-tracker
pip install -e .          # registers dismiss-linkedin-reply in .venv
cd tools/dismiss-linkedin-reply
./install.sh
```

## Smoke test

```bash
# after a real key/message_id exists:
open 'dlr://dismiss?kind=unmatched&message_id=imap:example'
```
