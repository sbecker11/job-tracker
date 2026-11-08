"""smb:// company and role folder URLs."""

from job_tracker.pipeline.folder_url import smb_folder_url


def test_relative_role_and_company_paths():
    assert smb_folder_url("Acme/Engineer") == "smb://mini1.local/Resumes/2026/Acme/Engineer"
    assert smb_folder_url("Acme") == "smb://mini1.local/Resumes/2026/Acme"


def test_absolute_desktop_path_drops_the_home_prefix():
    path = "/Users/sbecker11/Desktop/Resumes/2026/Acme/Staff_Engineer"
    assert smb_folder_url(path) == "smb://mini1.local/Resumes/2026/Acme/Staff_Engineer"


def test_encodes_reserved_characters_in_a_segment():
    assert smb_folder_url("A&B/Role") == "smb://mini1.local/Resumes/2026/A%26B/Role"


def test_blank_and_unrelated_absolute_paths():
    assert smb_folder_url(None) is None
    assert smb_folder_url("  ") is None
    assert smb_folder_url("/tmp/somewhere") is None
