"""CARD-12: candidate-marketing mail is not a pitch; subject titles drop lead-in noise."""

from __future__ import annotations

from job_tracker.pipeline.qualifying_reply import _title_from_subject, is_candidate_marketing

MARKETING = """Hi Shawn,
Another engineer from the network, this time at the more senior end:
Senior Forward Deployed / Applied AI Engineer | 7-9 YOE
He's particularly strong where FDE hiring gets difficult.
Would a profile at this level be relevant to your team?
I'd be happy to send the profile if you're adding FDEs this year."""

PITCH = """Hi Shawn,
I came across your profile and think you'd be a great fit for a Senior Data Engineer
role with our client. It's remote, W2, and the team is growing. Would you be open to a call
to discuss how your experience fits their team?"""


def test_candidate_marketing_detected():
    assert is_candidate_marketing(MARKETING, subject="Re:")


def test_normal_pitch_is_not_marketing():
    assert not is_candidate_marketing(PITCH, subject="Senior Data Engineer - Remote")


def test_subject_title_drops_location_and_role_lead_in():
    subject = "Fwd: Remote /  Job role for Senior Facets / Java Developer – Medicaid Regulatory Solutions at Remote"
    assert _title_from_subject(subject) == "Senior Facets / Java Developer"
    assert _title_from_subject("Urgent Hiring: Data Engineer") == "Data Engineer"
    assert _title_from_subject("Sr. Analytics Engineer - Remote - FTE") == "Sr. Analytics Engineer"
