"""Tests for the shared, structure-preserving HTML -> text converter."""

from __future__ import annotations

from job_tracker.htmltext import html_to_text


def test_block_tags_become_line_breaks():
    html = "<p>First paragraph</p><p>Second paragraph</p><ul><li>Bullet one</li><li>Bullet two</li></ul>"
    text = html_to_text(html)
    lines = [ln for ln in text.splitlines() if ln]
    assert "First paragraph" in lines
    assert "Second paragraph" in lines
    assert "Bullet one" in lines
    assert "Bullet two" in lines


def test_inline_tags_do_not_break_the_line():
    html = "<p>Senior <b>Software</b> <i>Engineer</i></p>"
    text = html_to_text(html)
    assert text.strip() == "Senior Software Engineer"


def test_style_and_script_blocks_are_dropped_entirely():
    html = (
        "<style>p { color: red; }</style>"
        "<script>doSomething();</script>"
        "<p>Real content</p>"
    )
    text = html_to_text(html)
    assert "color: red" not in text
    assert "doSomething" not in text
    assert "Real content" in text


def test_entity_escaped_input_is_unescaped():
    # Some ATS APIs (e.g. Greenhouse) return content pre-escaped as a string.
    raw = "&lt;p&gt;Motion Graphics &amp;amp; Illustration&lt;/p&gt;"
    text = html_to_text(raw)
    assert "Motion Graphics &amp; Illustration" in text or "Motion Graphics & Illustration" in text


def test_blank_line_runs_collapse_to_one():
    html = "<p>A</p><br><br><br><p>B</p>"
    text = html_to_text(html)
    assert "\n\n\n" not in text


def test_empty_input_returns_empty_string():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""
