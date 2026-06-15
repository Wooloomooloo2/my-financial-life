"""Manage ▸ Bank Feeds… — OFX Direct Connect setup + manual update (ADR-077).

A free, fully-local auto-feed for US banks that still support OFX Direct
Connect. The user links an MFL account to their bank's OFX server (URL / ORG /
FID from ofxhome.com) with their online-banking credentials; **Update** then
pulls recent transactions and runs them through the *same* import path as a
file (FITID dedup, the manual-match heuristic, commit) — nothing here is a new
posting route. Credentials are stored in this ``.mfl`` (per ADR-035); the
disclaimer says so.

Two dialogs: ``OfxConnectionDialog`` edits one connection (with a no-commit
"Test connection" probe); ``OfxFeedsDialog`` lists the linked feeds and runs
Update.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.feeds import ofx_store
from mfl_desktop.feeds.ofx_direct import OfxDirectError
from mfl_desktop.import_engine.import_service import ImportService
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


class OfxFeedsDialog(QDialog):
    """List the OFX feeds, add/edit/remove them, and run Update."""

    def __init__(
        self,
        repo: Repository,
        service: ImportService,
        *,
        on_updated=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._service = service
        self._on_updated = on_updated
        self.setWindowTitle("Bank Feeds — OFX Direct Connect")
        self.setMinimumSize(620, 360)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        intro = QLabel(
            "Free, fully-local auto-feed for US banks that support OFX Direct "
            "Connect. Fetched transactions go through the same dedup and review "
            "as a file import before they post."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "QLabel { color: {muted}; font-size: 11px; }")
        outer.addWidget(intro)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Account", "Institution", "Last updated", "Status"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        self._table.doubleClicked.connect(lambda *_: self._on_edit())
        outer.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._add_btn = QPushButton("Add…")
        self._edit_btn = QPushButton("Edit…")
        self._remove_btn = QPushButton("Remove")
        self._update_btn = QPushButton("Update selected")
        self._update_all_btn = QPushButton("Update all")
        self._add_btn.clicked.connect(self._on_add)
        self._edit_btn.clicked.connect(self._on_edit)
        self._remove_btn.clicked.connect(self._on_remove)
        self._update_btn.clicked.connect(lambda: self._update([self._selected_account_id()]))
        self._update_all_btn.clicked.connect(self._on_update_all)
        for b in (self._add_btn, self._edit_btn, self._remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._update_btn)
        row.addWidget(self._update_all_btn)
        outer.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)

        self._feeds: list = []
        self._reload()

    # ── data ──

    def _reload(self) -> None:
        self._feeds = [
            f for f in self._repo.list_feed_accounts()
            if f.provider == ofx_store.PROVIDER
        ]
        by_id = {a.id: a for a in self._repo.list_accounts()}
        self._table.setRowCount(len(self._feeds))
        for r, feed in enumerate(self._feeds):
            acct = by_id.get(feed.account_id)
            name = acct.name if acct else f"(account {feed.account_id})"
            synced = (feed.last_synced_at or "—")[:16].replace("T", " ")
            cells = [name, feed.institution_name or "—", synced, feed.status]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, feed.account_id)
                self._table.setItem(r, c, item)
        self._sync_buttons()

    def _selected_account_id(self) -> Optional[int]:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._table.item(rows[0].row(), 0).data(Qt.UserRole)

    def _sync_buttons(self) -> None:
        has_sel = self._selected_account_id() is not None
        self._edit_btn.setEnabled(has_sel)
        self._remove_btn.setEnabled(has_sel)
        self._update_btn.setEnabled(has_sel)
        self._update_all_btn.setEnabled(bool(self._feeds))

    # ── add / edit / remove ──

    def _on_add(self) -> None:
        linked = {f.account_id for f in self._repo.list_feed_accounts()}
        free = [a for a in self._repo.list_accounts()
                if a.id not in linked and a.archived_at is None]
        if not free:
            QMessageBox.information(
                self, "No accounts available",
                "Every account already has a feed (or there are no accounts).",
            )
            return
        dlg = OfxConnectionDialog(self._repo, free, parent=self)
        if dlg.exec() != QDialog.Accepted or dlg.result_cfg is None:
            return
        cfg = ofx_store.save_config(self._repo, dlg.result_account_id, dlg.result_cfg)
        self._repo.link_feed_account(
            account_id=dlg.result_account_id, provider=ofx_store.PROVIDER,
            external_account_id=cfg["acct_id"],
            institution_name=cfg.get("institution_name") or None,
        )
        self._reload()

    def _on_edit(self) -> None:
        account_id = self._selected_account_id()
        if account_id is None:
            return
        acct = self._repo.get_account_by_id(account_id)
        cfg = ofx_store.load_config(self._repo, account_id) or ofx_store.empty_config()
        dlg = OfxConnectionDialog(self._repo, [], account=acct, cfg=cfg, parent=self)
        if dlg.exec() != QDialog.Accepted or dlg.result_cfg is None:
            return
        saved = ofx_store.save_config(self._repo, account_id, dlg.result_cfg)
        self._repo.link_feed_account(
            account_id=account_id, provider=ofx_store.PROVIDER,
            external_account_id=saved["acct_id"],
            institution_name=saved.get("institution_name") or None,
        )
        self._reload()

    def _on_remove(self) -> None:
        account_id = self._selected_account_id()
        if account_id is None:
            return
        acct = self._repo.get_account_by_id(account_id)
        name = acct.name if acct else "this account"
        if QMessageBox.question(
            self, "Remove feed",
            f"Remove the OFX feed for {name}? Already-imported transactions "
            "stay; only the connection and its stored credentials are removed.",
        ) != QMessageBox.Yes:
            return
        self._repo.unlink_feed_account(account_id)
        ofx_store.clear_config(self._repo, account_id)
        self._reload()

    # ── update (fetch → stage → commit, reusing the import pipeline) ──

    def _on_update_all(self) -> None:
        self._update([f.account_id for f in self._feeds])

    def _update(self, account_ids: list) -> None:
        account_ids = [a for a in account_ids if a is not None]
        if not account_ids:
            return
        by_id = {a.id: a for a in self._repo.list_accounts()}
        lines: list[str] = []
        any_committed = False
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            for account_id in account_ids:
                acct = by_id.get(account_id)
                name = acct.name if acct else f"account {account_id}"
                cfg = ofx_store.load_config(self._repo, account_id)
                if cfg is None or acct is None:
                    lines.append(f"• {name}: no saved config — skipped.")
                    continue
                try:
                    raw = ofx_store.fetch_transactions(cfg, days=90)
                    token = self._service.stage_feed(
                        acct.iri, raw, provider=ofx_store.PROVIDER,
                    )
                    pending = self._service.get_pending(token)
                    accepted = {
                        tx.fitid for tx in pending.transactions
                        if tx.status == "potential_match"
                    }
                    result = self._service.commit_import(
                        token, pending.suggested_status, accepted,
                    )
                    self._repo.mark_feed_synced(account_id)
                    any_committed = any_committed or result.imported > 0
                    lines.append(
                        f"• {name}: {result.imported} new, "
                        f"{result.skipped} skipped, {result.matched} matched."
                    )
                except OfxDirectError as e:
                    self._repo.set_feed_status(account_id, "error")
                    lines.append(f"• {name}: ✗ {e}")
                except Exception as e:  # never leave the cursor stuck
                    self._repo.set_feed_status(account_id, "error")
                    lines.append(f"• {name}: ✗ unexpected error: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        self._reload()
        if any_committed and self._on_updated is not None:
            self._on_updated()
        QMessageBox.information(self, "Bank feed update", "\n".join(lines))
