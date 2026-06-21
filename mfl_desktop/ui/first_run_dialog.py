"""First-run welcome / onboarding dialog (ADR-098, P5).

Shown exactly once, the first time the app seeds a brand-new file (the
GUI launch path in ``__main__`` calls ``_seed_starter_db`` then opens this
on top of the freshly-created register). It turns the previously-silent
"dropped into an empty dashboard" experience into a two-field welcome:

  - pick your **base (reporting) currency** — previously hardcoded to GBP
    at seed time with no UI to change it;
  - name your **first account** (defaults to the seeded "Current account").

On accept it writes the base currency (``Repository.set_base_currency`` —
both the ``setting`` the app reads and the seeded ``person`` row) and
renames / re-currencies the starter account so a USD user isn't left with
a GBP "Current account". The currency the user picks is applied to that
starter account too (the common one-account-one-currency first case).

Two ways out, both of which apply the settings first:
  - **Get started** — close to the dashboard.
  - **Import a statement…** — close, and the caller opens the import
    picker on the starter account (``wants_import()`` returns True).

Qt-only; all persistence goes through the Repository. The dialog never
blocks the app — if the user just closes it (Esc), nothing is applied and
the GBP defaults stand, exactly as before this dialog existed.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.currencies import (
    ISO_4217_CODES,
    ISO_4217_CURRENCIES,
    currency_label,
)
from mfl_desktop import resources
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui import tokens


class FirstRunDialog(QDialog):
    """Welcome + base-currency + first-account naming for a new file."""

    def __init__(
        self,
        repo: Repository,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._wants_import = False
        # The starter account seeded by ``_seed_starter_db`` (first by id).
        self._starter = next(iter(repo.list_accounts()), None)

        self.setWindowTitle("Welcome to My Financial Life")
        self.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        outer.setSpacing(14)

        # Brand header: app icon (ADR-103) beside the welcome heading.
        icon_lbl = QLabel()
        icon_lbl.setPixmap(resources.app_pixmap(48))
        icon_lbl.setFixedSize(48, 48)
        icon_lbl.setScaledContents(True)
        heading = QLabel("Welcome to My Financial Life")
        tokens.themed(heading, "QLabel { font-size: 18px; font-weight: 600; color: {heading}; }")
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(icon_lbl, 0, Qt.AlignVCenter)
        header.addWidget(heading, 1, Qt.AlignVCenter)
        outer.addLayout(header)

        intro = QLabel(
            "Your whole financial life — accounts, investments and budgets — "
            "private and on your own device. Two quick choices to get set up; "
            "you can change either later."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "QLabel { color: {muted_strong}; }")
        outer.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(10)

        # Base currency — editable typeahead over the ISO list, default GBP.
        self._currency_combo = QComboBox()
        self._currency_combo.setEditable(True)
        self._currency_combo.setInsertPolicy(QComboBox.NoInsert)
        for code, _name in ISO_4217_CURRENCIES:
            self._currency_combo.addItem(currency_label(code), code)
        completer = self._currency_combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
        line_edit = self._currency_combo.lineEdit()
        if line_edit is not None:
            line_edit.textChanged.connect(self._uppercase_currency)
        self._set_currency(
            (self._starter.currency if self._starter else "GBP") or "GBP"
        )
        form.addRow("Base currency:", self._currency_combo)

        # First account name.
        self._name_edit = QLineEdit()
        self._name_edit.setText(self._starter.name if self._starter else "Current account")
        self._name_edit.setPlaceholderText("Current account")
        form.addRow("First account:", self._name_edit)

        outer.addLayout(form)

        hint = QLabel(
            "Tip: import a bank statement (OFX / QFX / QIF / CSV) to fill this "
            "account with real transactions — or start entering them by hand."
        )
        hint.setWordWrap(True)
        tokens.themed(hint, "QLabel { color: {muted}; font-size: 11px; }")
        outer.addWidget(hint)

        # Buttons — both apply first; "Import" additionally asks the caller to
        # open the import picker. No Cancel: there's nothing to cancel on a
        # brand-new file, but Esc still closes (QDialog default) without applying.
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        import_btn = QPushButton("Import a statement…")
        import_btn.clicked.connect(self._accept_with_import)
        start_btn = QPushButton("Get started")
        start_btn.setDefault(True)
        start_btn.setAutoDefault(True)
        start_btn.clicked.connect(self._accept)
        button_row.addWidget(import_btn)
        button_row.addWidget(start_btn)
        outer.addLayout(button_row)

    # ── currency combo helpers (mirror account_dialog) ──────────────────────

    def _uppercase_currency(self, text: str) -> None:
        upper = text.upper()
        if upper != text:
            le = self._currency_combo.lineEdit()
            if le is not None:
                pos = le.cursorPosition()
                le.setText(upper)
                le.setCursorPosition(pos)

    def _set_currency(self, code: str) -> None:
        target = (code or "").strip().upper()
        for i in range(self._currency_combo.count()):
            if self._currency_combo.itemData(i) == target:
                self._currency_combo.setCurrentIndex(i)
                return
        # Unknown code (shouldn't happen for a seeded GBP) — just type it.
        le = self._currency_combo.lineEdit()
        if le is not None:
            le.setText(target)

    def _chosen_currency(self) -> str:
        """The selected/typed code, validated against the ISO set; falls back
        to the starter account's currency (or GBP) if the user cleared it."""
        data = self._currency_combo.currentData()
        if isinstance(data, str) and data:
            code = data
        else:
            code = (self._currency_combo.currentText() or "").strip().upper()
            # The label form is "GBP — British Pound"; take the leading code.
            code = code.split(" ", 1)[0].split("—", 1)[0].strip()
        if code in ISO_4217_CODES:
            return code
        return (self._starter.currency if self._starter else "GBP") or "GBP"

    # ── apply ────────────────────────────────────────────────────────────────

    def _apply(self) -> None:
        """Persist the base currency + starter-account name/currency. Best
        effort per field — a bad value never strands the user on a new file."""
        code = self._chosen_currency()
        try:
            self._repo.set_base_currency(code)
        except Exception:
            pass
        if self._starter is not None:
            name = (self._name_edit.text() or "").strip() or self._starter.name
            try:
                self._repo.update_account(
                    self._starter.id,
                    name=name,
                    currency=code,
                    opening_balance=self._starter.opening_balance,
                )
            except Exception:
                pass

    def _accept(self) -> None:
        self._apply()
        self.accept()

    def _accept_with_import(self) -> None:
        self._wants_import = True
        self._apply()
        self.accept()

    def wants_import(self) -> bool:
        """True if the user chose "Import a statement…" — the caller should
        open the import picker on the starter account. Only meaningful after
        ``exec() == QDialog.Accepted``."""
        return self._wants_import

    def starter_account_iri(self) -> Optional[str]:
        """The seeded account's IRI, for the caller's import navigation."""
        return self._starter.iri if self._starter is not None else None
