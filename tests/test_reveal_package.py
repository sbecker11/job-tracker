from pathlib import Path

import pytest

from job_tracker.pipeline.reveal_package import package_attachments, reveal_package


def test_package_attachments_returns_resume_and_cover_only(tmp_path: Path):
    folder = tmp_path / "Acme" / "Engineer"
    folder.mkdir(parents=True)
    (folder / "Shawn_Becker_Resume_Acme_Engineer.docx").write_bytes(b"r")
    (folder / "Shawn_Becker_Cover_Letter_Acme_Engineer.docx").write_bytes(b"c")
    (folder / "full-LLM-review.docx").write_bytes(b"j")
    (folder / "notes.txt").write_text("no")

    found = package_attachments(folder, root=tmp_path)
    assert [p.name for p in found] == [
        "Shawn_Becker_Resume_Acme_Engineer.docx",
        "Shawn_Becker_Cover_Letter_Acme_Engineer.docx",
    ]


def test_package_attachments_rejects_a_folder_outside_the_resumes_root(tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    root = tmp_path / "Resumes"
    root.mkdir()
    with pytest.raises(ValueError, match="not a package folder"):
        package_attachments(outside, root=root)


def test_reveal_package_selects_the_two_documents(tmp_path: Path):
    folder = tmp_path / "Acme" / "Engineer"
    folder.mkdir(parents=True)
    (folder / "Resume.docx").write_bytes(b"r")
    (folder / "Cover_Letter.docx").write_bytes(b"c")
    opened: list[list[Path]] = []

    result = reveal_package(folder, root=tmp_path, opener=opened.append)

    assert result["ok"] is True
    assert result["files"] == ["Resume.docx", "Cover_Letter.docx"]
    assert opened == [[folder / "Resume.docx", folder / "Cover_Letter.docx"]]


def test_reveal_package_opens_the_folder_when_the_documents_are_missing(tmp_path: Path):
    folder = tmp_path / "Acme" / "Engineer"
    folder.mkdir(parents=True)
    opened: list[list[Path]] = []

    result = reveal_package(folder, root=tmp_path, opener=opened.append)

    assert result["files"] == []
    assert opened == [[folder]]
