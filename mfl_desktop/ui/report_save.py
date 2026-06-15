"""Shared Save-As resolution for the report windows (ADR-076 follow-up).

Every saved-report window had the same bug: *Save As* with a name+folder that
already exists created a duplicate rather than replacing it — and a no-folder
report has ``folder_id = NULL``, which SQLite's ``UNIQUE(name, folder_id)``
treats as distinct, so even same-name duplicates slipped through. This one
helper resolves a Save-As target consistently for all of them.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QMessageBox


def resolve_save_as(
    parent,
    repo,
    current_report_id: Optional[int],
    report_type: str,
    name: str,
    folder_id: Optional[int],
    filters_json: str,
):
    """Create or overwrite a saved report for a Save-As action.

    Returns the saved ``ReportRow``, or ``None`` if the user cancelled (or a
    different-type name clash was rejected). Overwrites an existing report
    with the same ``(name, folder_id)`` instead of duplicating: onto the
    current report it's a silent save; onto a different same-type report it
    asks "Replace?"; a different *type* with that name is rejected. DB errors
    propagate to the caller's try/except.
    """
    existing = next(
        (r for r in repo.list_reports()
         if r.name == name and r.folder_id == folder_id),
        None,
    )
    if existing is not None:
        is_self = existing.id == current_report_id
        if existing.type != report_type and not is_self:
            QMessageBox.warning(
                parent, "Name already in use",
                f"A different report named “{name}” already exists "
                f"{'in that folder' if folder_id else 'here'}. "
                f"Choose another name.",
            )
            return None
        if not is_self and QMessageBox.question(
            parent, "Replace report?",
            f"A report named “{name}” already exists here. Replace it?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return None
        return repo.update_report(
            existing.id, name=name, folder_id=folder_id, filters_json=filters_json,
        )
    return repo.create_report(
        name=name, type_key=report_type, folder_id=folder_id,
        filters_json=filters_json,
    )
