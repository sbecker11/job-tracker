"""SMB URLs for company and role folders on the mini1 File Sharing share.

Local packages live in ``~/Desktop/Resumes/2026/<company>/<role>``. The share
named ``Resumes`` is that ``Desktop/Resumes`` directory, so Finder on
HomeOffice and on mini1 both open:

    smb://mini1.local/Resumes/2026/<company>/<role>
"""

from __future__ import annotations

from urllib.parse import quote

SMB_FOLDER_BASE = "smb://mini1.local/Resumes/2026"
_MARKER = "/Resumes/2026/"


def smb_folder_url(folder_path: str | None) -> str | None:
    """Map a package path to an smb:// URL, or None when it is not under Resumes/2026.

    Accepts a relative path (``Acme/Engineer``) or an absolute path whose
    suffix is ``/Resumes/2026/<company>/<role>``.
    """
    if folder_path is None:
        return None
    text = str(folder_path).strip()
    if not text:
        return None
    if text.startswith("smb://"):
        return text
    marker_at = text.find(_MARKER)
    if marker_at >= 0:
        rest = text[marker_at + len(_MARKER) :]
    elif text.startswith("/"):
        return None
    else:
        rest = text
    parts = [quote(part, safe="") for part in rest.split("/") if part]
    if not parts:
        return SMB_FOLDER_BASE
    return f"{SMB_FOLDER_BASE}/{'/'.join(parts)}"
