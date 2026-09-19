"""Shared GrokNight theme for Textual surfaces."""

from textual.app import App

from src.tui_theme import CHROME_CSS, GROKNIGHT, apply_theme


class _ThemeApp(App[None]):
    CSS = CHROME_CSS


def test_groknight_palette() -> None:
    assert GROKNIGHT.name == "groknight"
    assert GROKNIGHT.primary == "#bb9af7"
    assert GROKNIGHT.background == "#141414"
    assert GROKNIGHT.surface == "#1c1c1c"
    assert GROKNIGHT.panel == "#0c0c0c"
    assert GROKNIGHT.accent == "#e0af68"
    assert GROKNIGHT.success == "#9ece6a"
    assert GROKNIGHT.error == "#f7768e"
    assert "block-cursor-background" in GROKNIGHT.variables
    assert "HeaderIcon" in CHROME_CSS
    assert "Tab.-active" in CHROME_CSS


def test_apply_theme_selects_groknight() -> None:
    app = _ThemeApp()
    apply_theme(app)
    assert app.theme == "groknight"
    assert "groknight" in app.available_themes
