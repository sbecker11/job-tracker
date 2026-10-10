"""The Pending Actions URL-handler umbrella covers every tools/*/install.sh helper."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UMBRELLA = REPO / "tools" / "install-pending-action-handlers.sh"
SKIP = frozenset({"pending-actions-ui-server", "lan-port80-redirect"})


def _handler_names_from_umbrella() -> list[str]:
    text = UMBRELLA.read_text(encoding="utf-8")
    start = text.index("HANDLERS=(")
    end = text.index(")", start)
    names: list[str] = []
    for line in text[start:end].splitlines()[1:]:
        raw = line.strip().split("#", 1)[0].strip()
        if raw:
            names.append(raw)
    return names


def test_umbrella_script_exists_and_is_executable():
    assert UMBRELLA.is_file()
    assert UMBRELLA.stat().st_mode & 0o111


def test_umbrella_covers_every_url_helper_install_sh():
    on_disk = sorted(
        p.parent.name
        for p in (REPO / "tools").glob("*/install.sh")
        if p.parent.name not in SKIP
    )
    listed = _handler_names_from_umbrella()
    assert sorted(listed) == on_disk, (listed, on_disk)
    for name in listed:
        installer = REPO / "tools" / name / "install.sh"
        assert installer.is_file(), name


def test_ui_server_install_calls_umbrella():
    text = (REPO / "tools" / "pending-actions-ui-server" / "install.sh").read_text(
        encoding="utf-8"
    )
    assert "install-pending-action-handlers.sh" in text
