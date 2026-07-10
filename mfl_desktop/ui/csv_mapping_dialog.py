"""Modal dialog: turn an unknown CSV's columns into a CsvColumnMapping.

Shown when ImportService.parse_and_stage returns ("...", "map") — i.e. the
file isn't Banktivity / credit-card / OFX / QFX, so we don't know which
column is the date, which is the amount, etc. The user maps each MFL field
to a column from their file; the dialog returns a CsvColumnMapping that the
caller passes back to ImportService.apply_mapping_and_stage.

Three-section layout, top to bottom:
  1. File preview — original headers + first 5 rows from the file, read-only.
  2. Mapping form — date / amount / payee / memo / category combos.
     Smart-defaults are pre-selected from csv_parser's existing alias lists,
     so a conventional Date / Amount / Note / Category file (e.g. Pocketsmith)
     opens with everything already set; the user just glances at the preview
     and clicks Import.
  3. After-mapping preview — the same 5 rows re-parsed through the current
     mapping, refreshed on every widget change. Surfaces parse failures
     (wrong date format, wrong amount column) as "(unparseable)" cells so
     mistakes are visible before commit, not after.

The dialog is purely value-producing: accepts → caller reads .mapping;
rejects → caller calls ImportService.discard_pending_map(token). It never
touches the service or repository itself.

See ADR-021.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.import_engine import csv_parser
from mfl_desktop.import_engine.csv_parser import (
    CsvColumnMapping,
    _AMOUNT_ALIASES,
    _CATEGORY_ALIASES,
    _CREDIT_ALIASES,
    _DATE_ALIASES,
    _DEBIT_ALIASES,
    _MEMO_ALIASES,
    _PAYEE_ALIASES,
    _decode,
    _parse_amount_str,
    make_generic_date_parser,
)
from mfl_desktop.import_engine.import_service import PendingCsvMap

# Date format options offered in the date-format combo. "auto" infers the
# column's day/month order from the file (ADR-148); the remaining entries are
# explicit strptime patterns the user can pick when the column is entirely
# ambiguous and auto's day-first fallback is wrong. The combo is editable, so
# a user can type a custom strptime pattern too.
_DATE_FORMAT_PRESETS = (
    "auto",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y%m%d",
)

# Sentinel for the "(none)" entry in optional combos.
_NONE = ""


class CsvMappingDialog(QDialog):
    """Single-screen modal: file preview, mapping form, after-mapping preview."""

    def __init__(self, pending_map: PendingCsvMap, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if not pending_map.headers:
            # Degenerate case — caller should have caught it. Guard anyway so
            # the dialog can't silently render an empty form.
            raise ValueError(
                "Cannot map a CSV with no header row. The file appears to be "
                "empty or missing column headers."
            )

        self._pending = pending_map
        self._mapping: Optional[CsvColumnMapping] = None

        self.setWindowTitle(f"Column mapping — {self._short_filename()}")
        self.setModal(True)

        # ── file preview (top) ──

        file_preview_label = QLabel(
            f"<b>File preview</b> — first {len(pending_map.preview_rows)} rows of "
            f"<code>{self._short_filename()}</code>"
        )
        self._file_preview = self._build_file_preview()

        # ── mapping form (middle) ──

        form_label = QLabel(
            "<b>Map each field</b> to a column from your file. "
            "Sensible defaults are picked from common header names — adjust as needed."
        )
        form_label.setWordWrap(True)

        self._date_combo = self._make_required_combo()
        self._date_format_combo = QComboBox()
        self._date_format_combo.setEditable(True)
        self._date_format_combo.addItems(_DATE_FORMAT_PRESETS)

        self._single_radio = QRadioButton("Single signed column (negative = debit / outflow)")
        self._split_radio = QRadioButton("Separate debit and credit columns")
        self._amount_style_group = QButtonGroup(self)
        self._amount_style_group.addButton(self._single_radio)
        self._amount_style_group.addButton(self._split_radio)

        self._amount_combo = self._make_optional_combo()
        self._invert_check = QCheckBox("Invert sign (positive = debit / outflow)")
        self._debit_combo = self._make_optional_combo()
        self._credit_combo = self._make_optional_combo()

        self._payee_combo = self._make_optional_combo()
        self._memo_combo = self._make_optional_combo()
        self._category_combo = self._make_optional_combo()

        form = QGridLayout()
        row = 0
        form.addWidget(QLabel("Date column:"), row, 0)
        form.addWidget(self._date_combo, row, 1); row += 1
        form.addWidget(QLabel("Date format:"), row, 0)
        form.addWidget(self._date_format_combo, row, 1); row += 1

        form.addWidget(QLabel("Amount style:"), row, 0)
        form.addWidget(self._single_radio, row, 1); row += 1
        form.addWidget(QLabel(""), row, 0)
        form.addWidget(self._split_radio, row, 1); row += 1

        form.addWidget(QLabel("Amount column:"), row, 0)
        form.addWidget(self._amount_combo, row, 1); row += 1
        form.addWidget(QLabel(""), row, 0)
        form.addWidget(self._invert_check, row, 1); row += 1
        form.addWidget(QLabel("Debit column:"), row, 0)
        form.addWidget(self._debit_combo, row, 1); row += 1
        form.addWidget(QLabel("Credit column:"), row, 0)
        form.addWidget(self._credit_combo, row, 1); row += 1

        form.addWidget(QLabel("Payee column:"), row, 0)
        form.addWidget(self._payee_combo, row, 1); row += 1
        form.addWidget(QLabel("Memo column:"), row, 0)
        form.addWidget(self._memo_combo, row, 1); row += 1
        form.addWidget(QLabel("Category column:"), row, 0)
        form.addWidget(self._category_combo, row, 1); row += 1

        form.setColumnStretch(1, 1)

        # ── after-mapping preview (bottom) ──

        preview_label = QLabel(
            "<b>Preview after mapping</b> — what your file looks like with the "
            "current settings. "
            "<i>(unparseable)</i> cells mean the date or amount didn't match — "
            "check the column or format above."
        )
        preview_label.setWordWrap(True)
        self._mapped_preview = self._build_mapped_preview()

        # ── buttons ──

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("&Import")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        # ── layout ──

        layout = QVBoxLayout(self)
        layout.addWidget(file_preview_label)
        layout.addWidget(self._file_preview)
        layout.addSpacing(8)
        layout.addWidget(form_label)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(preview_label)
        layout.addWidget(self._mapped_preview)
        layout.addWidget(buttons)

        # Make the dialog comfortably wide so the previews don't horizontal-scroll
        # for the typical 6–10 column CSV. Height adapts to content.
        self.resize(820, 720)

        # ── populate + wire ──

        self._populate_column_combos()
        self._apply_smart_defaults()
        self._wire_signals()
        self._on_amount_style_changed()
        self._refresh_preview()

    # ── public api ──

    @property
    def mapping(self) -> Optional[CsvColumnMapping]:
        """The accepted mapping, or None if the dialog was cancelled or hasn't
        been shown yet."""
        return self._mapping

    # ── construction helpers ──

    def _short_filename(self) -> str:
        # Use a basename-style view of the staged filename. The PendingCsvMap
        # stores whatever the caller passed (typically a full path on disk);
        # only the leaf matters for the dialog title.
        name = self._pending.filename
        for sep in ("\\", "/"):
            if sep in name:
                name = name.rsplit(sep, 1)[-1]
        return name

    def _make_required_combo(self) -> QComboBox:
        combo = QComboBox()
        return combo

    def _make_optional_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.addItem("(none)", userData=_NONE)
        return combo

    def _build_file_preview(self) -> QTableWidget:
        rows = self._pending.preview_rows
        headers = self._pending.headers
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                item = QTableWidgetItem(cell)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(r, c, item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.setMaximumHeight(140)
        return table

    def _build_mapped_preview(self) -> QTableWidget:
        cols = ("Date", "Payee", "Amount", "Direction", "Category")
        table = QTableWidget(0, len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setMaximumHeight(180)
        return table

    def _populate_column_combos(self) -> None:
        headers = self._pending.headers
        # Required combo: only real headers, no "(none)".
        for h in headers:
            self._date_combo.addItem(h, userData=h)
        # Optional combos already have "(none)" as item 0.
        for combo in (
            self._amount_combo, self._debit_combo, self._credit_combo,
            self._payee_combo, self._memo_combo, self._category_combo,
        ):
            for h in headers:
                combo.addItem(h, userData=h)

    def _apply_smart_defaults(self) -> None:
        """Pre-select combos by matching the file's headers against the
        alias lists already in csv_parser. Pocketsmith's
        Date / Amount / Note / Category headers all land on the right combo;
        the user can override anything that's wrong."""
        headers = self._pending.headers
        norm = {h.strip().lower(): h for h in headers if h}

        date_match     = self._first_match(norm, _DATE_ALIASES)
        amount_match   = self._first_match(norm, _AMOUNT_ALIASES)
        debit_match    = self._first_match(norm, _DEBIT_ALIASES)
        credit_match   = self._first_match(norm, _CREDIT_ALIASES)
        payee_match    = self._first_match(norm, _PAYEE_ALIASES)
        memo_match     = self._first_match(norm, _MEMO_ALIASES)
        category_match = self._first_match(norm, _CATEGORY_ALIASES)

        if date_match:
            self._set_combo_to(self._date_combo, date_match)

        # Style selection: prefer single-column. Only fall to split if there's
        # no amount match but there *are* debit and credit matches.
        if amount_match:
            self._single_radio.setChecked(True)
            self._set_combo_to(self._amount_combo, amount_match)
        elif debit_match and credit_match:
            self._split_radio.setChecked(True)
            self._set_combo_to(self._debit_combo, debit_match)
            self._set_combo_to(self._credit_combo, credit_match)
        else:
            self._single_radio.setChecked(True)

        if payee_match:
            self._set_combo_to(self._payee_combo, payee_match)
        if memo_match:
            self._set_combo_to(self._memo_combo, memo_match)
        if category_match:
            self._set_combo_to(self._category_combo, category_match)

        # Date format defaults to "auto" (already first in the combo).

    @staticmethod
    def _first_match(norm_headers: dict, aliases: tuple) -> Optional[str]:
        for alias in aliases:
            if alias in norm_headers:
                return norm_headers[alias]
        return None

    @staticmethod
    def _set_combo_to(combo: QComboBox, header: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == header:
                combo.setCurrentIndex(i)
                return

    def _wire_signals(self) -> None:
        """Refresh the after-mapping preview on every form change."""
        self._date_combo.currentIndexChanged.connect(self._refresh_preview)
        self._date_format_combo.editTextChanged.connect(self._refresh_preview)
        self._amount_combo.currentIndexChanged.connect(self._refresh_preview)
        self._invert_check.toggled.connect(self._refresh_preview)
        self._debit_combo.currentIndexChanged.connect(self._refresh_preview)
        self._credit_combo.currentIndexChanged.connect(self._refresh_preview)
        self._payee_combo.currentIndexChanged.connect(self._refresh_preview)
        self._memo_combo.currentIndexChanged.connect(self._refresh_preview)
        self._category_combo.currentIndexChanged.connect(self._refresh_preview)
        self._amount_style_group.buttonToggled.connect(self._on_amount_style_changed)

    # ── interaction ──

    def _on_amount_style_changed(self, *_args) -> None:
        """Enable/disable amount-related combos to match the radio choice.

        Disabled rather than hidden so the form's layout stays stable as the
        user toggles back and forth.
        """
        single = self._single_radio.isChecked()
        self._amount_combo.setEnabled(single)
        self._invert_check.setEnabled(single)
        self._debit_combo.setEnabled(not single)
        self._credit_combo.setEnabled(not single)
        self._refresh_preview()

    def _current_mapping(self) -> CsvColumnMapping:
        """Build a CsvColumnMapping from the current widget state.

        Used by both the live preview and (after validation) the OK path.
        Returns a mapping whose ``date_col`` may be empty if the user hasn't
        chosen one — validation happens separately in ``_on_accept``.
        """
        date_col = self._date_combo.currentData() or ""
        date_format = self._date_format_combo.currentText().strip() or "auto"

        if self._single_radio.isChecked():
            amount_col = self._amount_combo.currentData() or ""
            debit_col = ""
            credit_col = ""
            invert = self._invert_check.isChecked()
        else:
            amount_col = ""
            debit_col = self._debit_combo.currentData() or ""
            credit_col = self._credit_combo.currentData() or ""
            invert = False

        return CsvColumnMapping(
            date_col=date_col,
            date_format=date_format,
            amount_col=amount_col,
            amount_inverted=invert,
            debit_col=debit_col,
            credit_col=credit_col,
            payee_col=self._payee_combo.currentData() or "",
            memo_col=self._memo_combo.currentData() or "",
            category_col=self._category_combo.currentData() or "",
        )

    # ── preview ──

    def _refresh_preview(self) -> None:
        """Render the after-mapping preview from the current widget state.

        Reads the first 5 data rows directly from the staged file bytes via
        csv.DictReader so we can show one preview row per file row — including
        rows whose date or amount fails to parse, which parse_with_mapping
        would otherwise drop. Cells that fail to parse render as
        "(unparseable)"; cells with no mapped column render as a soft em dash.
        """
        mapping = self._current_mapping()
        table = self._mapped_preview
        table.setRowCount(0)

        if not mapping.date_col:
            self._show_preview_message(
                "Pick a Date column to see the parsed preview."
            )
            return
        if (
            self._single_radio.isChecked() and not mapping.amount_col
        ) or (
            self._split_radio.isChecked()
            and not (mapping.debit_col or mapping.credit_col)
        ):
            self._show_preview_message(
                "Pick an Amount column (or both Debit + Credit) "
                "to see the parsed preview."
            )
            return

        try:
            content = _decode(self._pending.file_bytes)
        except ValueError:
            self._show_preview_message("Could not decode the file as text.")
            return

        all_rows = list(csv.DictReader(io.StringIO(content)))
        # Infer day/month order from the whole column, not the five rows on
        # screen — otherwise the preview can disagree with the import (ADR-148).
        parse_date = make_generic_date_parser(
            (r.get(mapping.date_col, "") for r in all_rows),
        )

        for raw_row in all_rows[:5]:
            date_cell, payee_cell, amount_cell, direction_cell, category_cell = (
                self._preview_cells(raw_row, mapping, parse_date)
            )
            self._append_preview_row(
                table, date_cell, payee_cell, amount_cell,
                direction_cell, category_cell,
            )

    def _show_preview_message(self, message: str) -> None:
        table = self._mapped_preview
        table.setRowCount(1)
        item = QTableWidgetItem(message)
        item.setFlags(Qt.ItemIsEnabled)
        item.setForeground(Qt.gray)
        table.setItem(0, 0, item)
        table.setSpan(0, 0, 1, table.columnCount())

    def _append_preview_row(
        self, table: QTableWidget,
        date: str, payee: str, amount: str, direction: str, category: str,
    ) -> None:
        r = table.rowCount()
        table.insertRow(r)
        for c, val in enumerate((date, payee, amount, direction, category)):
            item = QTableWidgetItem(val)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if val == "(unparseable)":
                item.setForeground(Qt.red)
            elif val == "—":
                item.setForeground(Qt.gray)
            table.setItem(r, c, item)

    def _preview_cells(
        self, raw_row: dict, mapping: CsvColumnMapping,
        parse_date: Callable[[str], str],
    ) -> tuple[str, str, str, str, str]:
        """Compute one preview row's cells from a raw CSV row + current mapping.

        Mirrors the per-row logic in csv_parser.parse_with_mapping but surfaces
        failures as "(unparseable)" instead of silently dropping the row.
        ``parse_date`` is the column-bound parser the import will use.
        """
        # Date
        date_raw = (raw_row.get(mapping.date_col, "") or "").strip()
        if mapping.date_format == "auto":
            date_iso = parse_date(date_raw)
        else:
            from datetime import datetime
            try:
                date_iso = datetime.strptime(
                    date_raw, mapping.date_format,
                ).strftime("%Y-%m-%d")
            except ValueError:
                date_iso = parse_date(date_raw)
        date_cell = date_iso if date_iso else "(unparseable)"

        # Amount + direction
        if mapping.amount_col:
            raw = (raw_row.get(mapping.amount_col, "") or "").strip()
            value = _parse_amount_str(raw)
            if value is None:
                amount_cell = "(unparseable)"
                direction_cell = "—"
            else:
                if mapping.amount_inverted:
                    direction_cell = "debit" if value > 0 else "credit"
                else:
                    direction_cell = "debit" if value < 0 else "credit"
                amount_cell = f"{abs(value)}"
        elif mapping.debit_col or mapping.credit_col:
            d_raw = (raw_row.get(mapping.debit_col, "") or "").strip() if mapping.debit_col else ""
            c_raw = (raw_row.get(mapping.credit_col, "") or "").strip() if mapping.credit_col else ""
            d_val = _parse_amount_str(d_raw) if d_raw else None
            c_val = _parse_amount_str(c_raw) if c_raw else None
            if d_val is not None and abs(d_val) > 0:
                amount_cell = f"{abs(d_val)}"
                direction_cell = "debit"
            elif c_val is not None and abs(c_val) > 0:
                amount_cell = f"{abs(c_val)}"
                direction_cell = "credit"
            elif d_raw or c_raw:
                amount_cell = "(unparseable)"
                direction_cell = "—"
            else:
                amount_cell = "—"
                direction_cell = "—"
        else:
            amount_cell = "—"
            direction_cell = "—"

        # Payee / Category (free text; "—" if no column mapped)
        payee_cell = self._cell_or_dash(raw_row, mapping.payee_col)
        category_cell = self._cell_or_dash(raw_row, mapping.category_col)
        return date_cell, payee_cell, amount_cell, direction_cell, category_cell

    @staticmethod
    def _cell_or_dash(raw_row: dict, col: str) -> str:
        if not col:
            return "—"
        value = (raw_row.get(col, "") or "").strip()
        return value if value else "—"

    # ── accept / validate ──

    def _on_accept(self) -> None:
        mapping = self._current_mapping()

        if not mapping.date_col:
            QMessageBox.warning(
                self, "Date column required",
                "Pick the column that contains the transaction date.",
            )
            return

        if self._single_radio.isChecked():
            if not mapping.amount_col:
                QMessageBox.warning(
                    self, "Amount column required",
                    "Pick the column that contains the transaction amount, or "
                    "switch to 'Separate debit and credit columns' if your file "
                    "splits them.",
                )
                return
        else:
            if not (mapping.debit_col and mapping.credit_col):
                QMessageBox.warning(
                    self, "Debit and Credit columns required",
                    "Pick both the Debit and Credit columns, or switch back to "
                    "'Single signed column' if your file uses one column.",
                )
                return

        # Final sanity: at least one preview row should parse with these
        # settings. A wholly empty preview almost always means the date format
        # or amount column is wrong; better to flag than to commit zero rows.
        if not self._any_row_parses(mapping):
            answer = QMessageBox.question(
                self, "No rows parsed",
                "With the current settings, none of the preview rows produced "
                "a valid date and amount. Importing now will likely add zero "
                "transactions.\n\n"
                "Import anyway? (Choose No to adjust the mapping.)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._mapping = mapping
        self.accept()

    def _any_row_parses(self, mapping: CsvColumnMapping) -> bool:
        try:
            content = _decode(self._pending.file_bytes)
        except ValueError:
            return False
        # parse_with_mapping silently skips rows that fail; if it returns at
        # least one row, the mapping works for some data.
        rows = csv_parser.parse_with_mapping(content, mapping)
        return len(rows) > 0
