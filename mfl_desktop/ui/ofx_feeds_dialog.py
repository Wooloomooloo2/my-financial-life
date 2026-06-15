"""OFX Direct Connect connection editor (ADR-077).

``OfxConnectionDialog`` adds/edits one OFX Direct Connect connection — bank
server (URL / ORG / FID from ofxhome.com), online-banking credentials, the
account at the bank, and a no-commit "Test connection" probe. It is launched by
the unified ``BankFeedsDialog`` (``ui/bank_feeds_dialog.py``) when adding an OFX
feed. Credentials are stored in this ``.mfl`` (per ADR-035); the disclaimer
says so.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.feeds import ofx_store
from mfl_desktop.feeds.ofx_direct import OfxDirectError
from mfl_desktop.ui import tokens

# Bank account types address BANKACCTFROM and so need a routing/bank id;
# credit-card and investment requests do not.
_NEEDS_BANK_ID = {"CHECKING", "SAVINGS", "MONEYMRKT", "CREDITLINE", "CD"}


class OfxConnectionDialog(QDialog):
    """Add or edit one OFX Direct Connect connection for a single account."""

    def __init__(
        self,
        repo: Repository,
        selectable_accounts: list[AccountSummary],
        *,
        account: Optional[AccountSummary] = None,
        cfg: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._editing = account is not None
        self._cfg = cfg or ofx_store.empty_config()
        self.result_account_id: Optional[int] = account.id if account else None
        self.result_cfg: Optional[dict] = None

        self.setWindowTitle(
            "Edit bank feed" if self._editing else "Add bank feed"
        )
        self.setMinimumWidth(460)
        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        # ── account ──
        acct_form = QFormLayout()
        if self._editing:
            acct_form.addRow("Account:", QLabel(f"{account.name}  ·  {account.currency}"))
            self._account_combo = None
        else:
            self._account_combo = QComboBox()
            for a in selectable_accounts:
                self._account_combo.addItem(f"{a.name}  ·  {a.currency}", userData=a.id)
            acct_form.addRow("Account:", self._account_combo)
        outer.addLayout(acct_form)

        # ── bank server (from ofxhome.com) ──
        server_box = QGroupBox("Bank OFX server (from ofxhome.com)")
        server_form = QFormLayout(server_box)
        self._inst_edit = QLineEdit(self._cfg.get("institution_name", ""))
        self._inst_edit.setPlaceholderText("e.g. Ally Bank (for display)")
        self._url_edit = QLineEdit(self._cfg.get("url", ""))
        self._url_edit.setPlaceholderText("https://…/ofx")
        self._org_edit = QLineEdit(self._cfg.get("org", ""))
        self._fid_edit = QLineEdit(self._cfg.get("fid", ""))
        server_form.addRow("Institution name:", self._inst_edit)
        server_form.addRow("OFX URL:", self._url_edit)
        server_form.addRow("ORG:", self._org_edit)
        server_form.addRow("FID:", self._fid_edit)
        outer.addWidget(server_box)

        # ── credentials ──
        cred_box = QGroupBox("Your online-banking credentials")
        cred_form = QFormLayout(cred_box)
        self._user_edit = QLineEdit(self._cfg.get("username", ""))
        self._pass_edit = QLineEdit(self._cfg.get("password", ""))
        self._pass_edit.setEchoMode(QLineEdit.Password)
        cred_form.addRow("User id:", self._user_edit)
        cred_form.addRow("Password:", self._pass_edit)
        disclaimer = QLabel(
            "Stored inside this .mfl file so feeds can refresh. Remove before "
            "sharing snapshots. Some banks require Direct Connect to be enabled "
            "or issue a separate PIN — check at ofxhome.com."
        )
        disclaimer.setWordWrap(True)
        tokens.themed(disclaimer, "QLabel { color: {muted}; font-size: 11px; }")
        cred_box.layout().addWidget(disclaimer)
        outer.addWidget(cred_box)

        # ── account at the bank ──
        bank_box = QGroupBox("Account at the bank")
        bank_form = QFormLayout(bank_box)
        self._type_combo = QComboBox()
        for label, value in ofx_store.ACCT_TYPE_CHOICES:
            self._type_combo.addItem(label, userData=value)
        self._select_type(self._cfg.get("acct_type", "CHECKING"))
        self._type_combo.currentIndexChanged.connect(self._sync_bankid_enabled)
        self._acctid_edit = QLineEdit(self._cfg.get("acct_id", ""))
        self._acctid_edit.setPlaceholderText("Account number")
        self._bankid_edit = QLineEdit(self._cfg.get("bank_id", ""))
        self._bankid_edit.setPlaceholderText("Routing number (bank accounts)")
        self._brokerid_edit = QLineEdit(self._cfg.get("broker_id", ""))
        self._brokerid_edit.setPlaceholderText("Broker id (investment accounts)")
        bank_form.addRow("Type:", self._type_combo)
        bank_form.addRow("Account number:", self._acctid_edit)
        bank_form.addRow("Routing / bank id:", self._bankid_edit)
        bank_form.addRow("Broker id:", self._brokerid_edit)
        outer.addWidget(bank_box)
        self._sync_bankid_enabled()

        # ── advanced (Quicken impersonation defaults — rarely changed) ──
        adv_box = QGroupBox("Advanced")
        adv_form = QFormLayout(adv_box)
        self._appid_edit = QLineEdit(str(self._cfg.get("app_id", "QWIN")))
        self._appver_edit = QLineEdit(str(self._cfg.get("app_version", "2700")))
        self._ofxver_edit = QLineEdit(str(self._cfg.get("ofx_version", 102)))
        adv_form.addRow("App id:", self._appid_edit)
        adv_form.addRow("App version:", self._appver_edit)
        adv_form.addRow("OFX version:", self._ofxver_edit)
        adv_box.setToolTip(
            "Most banks expect the Quicken identity (QWIN / 2700 / 102). "
            "Only change these if ofxhome.com says your bank needs different values."
        )
        outer.addWidget(adv_box)

        # ── test + buttons ──
        self._status = QLabel("")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._on_test)
        row.addWidget(self._test_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        outer.addLayout(row)

    # ── helpers ──

    def _select_type(self, value: str) -> None:
        for i in range(self._type_combo.count()):
            if self._type_combo.itemData(i) == value:
                self._type_combo.setCurrentIndex(i)
                return

    def _sync_bankid_enabled(self) -> None:
        acct_type = self._type_combo.currentData()
        self._bankid_edit.setEnabled(acct_type in _NEEDS_BANK_ID)
        self._brokerid_edit.setEnabled(acct_type == "INVESTMENT")

    def _gather(self) -> dict:
        cfg = ofx_store.empty_config()
        cfg.update(self._cfg)  # preserve client_uid across edits
        cfg.update({
            "institution_name": self._inst_edit.text().strip(),
            "url": self._url_edit.text().strip(),
            "org": self._org_edit.text().strip(),
            "fid": self._fid_edit.text().strip(),
            "username": self._user_edit.text(),
            "password": self._pass_edit.text(),
            "acct_type": self._type_combo.currentData(),
            "acct_id": self._acctid_edit.text().strip(),
            "bank_id": self._bankid_edit.text().strip(),
            "broker_id": self._brokerid_edit.text().strip(),
            "app_id": self._appid_edit.text().strip() or "QWIN",
            "app_version": self._appver_edit.text().strip() or "2700",
            "ofx_version": self._ofxver_edit.text().strip() or "102",
        })
        return cfg

    def _validate(self, cfg: dict) -> Optional[str]:
        if not self._editing and self._account_combo.currentData() is None:
            return "Choose which MFL account this feed posts to."
        required = {
            "OFX URL": cfg["url"], "ORG": cfg["org"], "FID": cfg["fid"],
            "User id": cfg["username"], "Password": cfg["password"],
            "Account number": cfg["acct_id"],
        }
        missing = [name for name, val in required.items() if not str(val).strip()]
        if missing:
            return "Please fill in: " + ", ".join(missing) + "."
        if cfg["acct_type"] in _NEEDS_BANK_ID and not cfg["bank_id"]:
            return "A routing / bank id is required for this account type."
        try:
            int(cfg["ofx_version"])
        except (TypeError, ValueError):
            return "OFX version must be a number (usually 102)."
        return None

    def _on_test(self) -> None:
        cfg = self._gather()
        err = self._validate(cfg)
        if err:
            self._status.setText(err)
            return
        self._status.setText("Connecting…")
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            txns = ofx_store.fetch_transactions(cfg, days=30)
        except OfxDirectError as e:
            self._status.setText(f"✗ {e}")
            return
        except Exception as e:  # defensive — never crash the dialog
            self._status.setText(f"✗ Unexpected error: {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._status.setText(
            f"✓ Connected — {len(txns)} transactions in the last 30 days."
        )

    def _on_save(self) -> None:
        cfg = self._gather()
        err = self._validate(cfg)
        if err:
            self._status.setText(err)
            return
        if not self._editing:
            self.result_account_id = self._account_combo.currentData()
        self.result_cfg = cfg
        self.accept()
