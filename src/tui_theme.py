"""GrokNight chrome shared by the configuration and executions TUIs."""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme

GROKNIGHT = Theme(
    name="groknight",
    primary="#bb9af7",
    secondary="#7aa2f7",
    accent="#e0af68",
    foreground="#e1e1e1",
    background="#141414",
    surface="#1c1c1c",
    panel="#0c0c0c",
    boost="#242424",
    success="#9ece6a",
    warning="#e0af68",
    error="#f7768e",
    dark=True,
    variables={
        "block-cursor-background": "#bb9af7",
        "block-cursor-foreground": "#141414",
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": "#bb9af7 35%",
        "block-cursor-blurred-foreground": "#e1e1e1",
        "footer-background": "#0c0c0c",
        "footer-key-foreground": "#bb9af7",
        "footer-description-foreground": "#6c6c6c",
        "border": "#bb9af7",
        "border-blurred": "#414141",
        "input-cursor-background": "#bb9af7",
        "input-cursor-foreground": "#141414",
        "input-selection-background": "#bb9af7 35%",
        "button-color-foreground": "#141414",
        "button-focus-text-style": "bold",
        "scrollbar": "#414141",
        "scrollbar-hover": "#bb9af7",
        "scrollbar-background": "#141414",
        "scrollbar-background-hover": "#141414",
    },
)

CHROME_CSS = """
Screen { background: $background; }
Header {
    background: $background;
    color: $text;
    border-bottom: hkey #414141;
}
Header.-tall { background: $background; }
HeaderIcon { display: none; }
HeaderTitle { text-style: none; color: $text; }
Footer {
    background: $background;
    color: $text-muted;
    border-top: hkey #414141;
}
Tabs { background: $background; }
Tab {
    padding: 0 2;
    color: #6c6c6c;
    background: $background;
    text-style: none;
}
Tab.-active {
    color: $primary;
    text-style: bold;
    background: $background;
}
Underline > .underline--bar {
    color: $primary;
    background: #414141;
}
Input, Select, TextArea {
    background: $boost;
    color: $text;
    border: none;
}
Input {
    height: 1;
    padding: 0 1;
    border: none !important;
    background: $boost;
}
Input:focus {
    border: none !important;
    background: #2a2a2a;
}
Select.-textual-compact {
    min-height: 1;
}
SelectCurrent {
    height: 1 !important;
    min-height: 1;
    background: $boost;
    border: none !important;
    padding: 0 1;
}
Select:focus > SelectCurrent {
    border: none !important;
    background: #2a2a2a;
}
SelectOverlay, Select > SelectOverlay {
    background: $boost;
    border: solid #414141 !important;
    padding: 0;
}
SelectOverlay:focus, Select > SelectOverlay:focus {
    border: solid $primary !important;
    background: $boost;
}
SelectOverlay > .option-list--option-highlighted,
Select > SelectOverlay > .option-list--option-highlighted,
SelectOverlay:focus > .option-list--option-highlighted {
    background: $primary;
    color: #141414;
    text-style: none;
}
Button {
    margin-right: 1;
    margin-bottom: 1;
    min-width: 10;
    height: 1;
    border: none !important;
    background: $boost;
    color: $text;
    text-style: none;
    padding: 0 1;
}
Button.-primary {
    background: $primary !important;
    color: #141414;
    text-style: bold;
}
Button.-warning {
    background: #f7768e 20% !important;
    color: $error;
}
Button:focus {
    text-style: bold;
    background-tint: $primary 25%;
}
Button.-primary:focus {
    background: $primary !important;
}
Checkbox, ToggleButton {
    border: none !important;
    background: transparent;
    height: 1;
    padding: 0 1;
}
Checkbox:focus, ToggleButton:focus {
    border: none !important;
    background: $boost;
}
Checkbox:focus > .toggle--label, ToggleButton:focus > .toggle--label {
    background: transparent;
    color: $text;
    text-style: none;
}
Checkbox.-on > .toggle--button, ToggleButton.-on > .toggle--button {
    color: $primary;
    background: $background;
}
DataTable {
    background: $background;
    color: $text;
}
DataTable > .datatable--header {
    background: $background;
    color: #6c6c6c;
    text-style: none;
}
"""


def apply_theme(app: App[None]) -> None:
    """Register and select GrokNight on a Textual app."""
    app.register_theme(GROKNIGHT)
    app.theme = GROKNIGHT.name
