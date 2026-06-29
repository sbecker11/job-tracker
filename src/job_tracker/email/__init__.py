"""Email ingestion and classification."""

from job_tracker.email.classifier import classify
from job_tracker.email.labels import Label
from job_tracker.email.models import ClassificationResult, EmailMessage, ExtractedRole

__all__ = [
    "Label",
    "EmailMessage",
    "ExtractedRole",
    "ClassificationResult",
    "classify",
]
