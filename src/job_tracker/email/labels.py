"""Email classification labels for the recruiting inbox pipeline."""

from __future__ import annotations

from enum import Enum


class Label(str, Enum):
    """One label per message; priority order is defined in classifier.py."""

    SINGLE_JD = "single-jd"
    MULTI_JD_IN_BODY = "multi-jd-in-body"
    LINK_ONLY_DIGEST = "link-only-digest"
    RECRUITER_OUTREACH = "recruiter-outreach"
    REJECTION = "rejection"
    NOISE = "noise"
