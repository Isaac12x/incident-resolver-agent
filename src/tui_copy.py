"""Shared text-selection copy behavior for Textual apps."""

from textual.app import App
from textual.widgets import DataTable


def copy_selection(app: App[object]) -> bool:
    """Copy the active screen's selection, returning whether text was copied."""
    selected_text = app.screen.get_selected_text()
    focused = app.screen.focused
    if not selected_text and focused is not None:
        selection = getattr(focused, "selection", None)
        if selection is not None and selection.start != selection.end:
            selected_text = getattr(focused, "selected_text", None)
    if not selected_text and isinstance(focused, DataTable) and focused.row_count:
        row = focused.cursor_row
        if row is not None:
            selected_text = "\t".join(str(value) for value in focused.get_row_at(row))
    if not selected_text:
        return False
    app.copy_to_clipboard(selected_text)
    return True
