"""Email ingestion and classification."""

from job_tracker.email.classifier import classify
from job_tracker.email.gmail_reader import (
    fetch_message_by_id,
    fetch_unread,
    get_gmail_service,
    parse_gmail_message,
)
from job_tracker.email.labels import Label
from job_tracker.email.models import ClassificationResult, EmailMessage, ExtractedRole

__all__ = [
    "Label",
    "EmailMessage",
    "ExtractedRole",
    "ClassificationResult",
    "classify",
    "fetch_unread",
    "fetch_message_by_id",
    "get_gmail_service",
    "parse_gmail_message",
]
