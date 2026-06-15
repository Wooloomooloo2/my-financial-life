"""Manage ▸ Bank Feeds… — unified, multi-provider feed UI (ADR-077 Amend. 2).

One dialog over all four providers on the framework:

- **OFX Direct Connect** — per-account server + credentials (no browser).
- **SimpleFIN** — paste a setup token once; one access URL yields all accounts.
- **Enable Banking** — app credentials once; pick a bank, consent in the
  browser, paste the redirected URL back (the desktop has no web server).
- **Plaid** — client credentials once; Plaid Hosted Link in the browser, then
  exchange the completed session for an access token.

Each provider differs only in *connecting* + storing credentials (handled here
and in ``feeds.sync``); fetching, dedup and commit are the shared pipeline
(``feeds.sync.fetch_raw_for_feed`` → ``ImportService.stage_feed`` →
``commit_import``). Browser-consent flows can't be exercised offscreen, so the
network calls go through the same clients the headless ``*-check`` probes use.
"""
from __future__ import annotations

import datetime
import uuid
import webbrowser
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.feeds import sync
from mfl_desktop.feeds.enablebanking import EnableBankingClient, EnableBankingError
from mfl_desktop.feeds.plaid import PlaidClient, PlaidError
from mfl_desktop.feeds.simplefin import SimpleFinClient, SimpleFinError, claim_access_url
from mfl_desktop.import_engine.import_service import ImportService
from mfl_desktop.ui import tokens
from mfl_desktop.ui.ofx_feeds_dialog import OfxConnectionDialog
from mfl_desktop.feeds import ofx_store


def _muted(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    tokens.themed(label, "QLabel { color: {muted}; font-size: 11px; }")
    return label


class ProviderPickerDialog(QDialog):
    """Choose which provider to add."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a bank feed")
        self.choice: Optional[str] = None
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Which kind of feed do you want to add?"))
        self._buttons: list[tuple[QRadioButton, str]] = []
        order = [sync.ENABLEBANKING, sync.OFX, sync.SIMPLEFIN, sync.PLAID]
        for i, key in enumerate(order):
            rb = QRadioButton(sync.PROVIDER_LABELS[key])
            if i == 0:
                rb.setChecked(True)
            outer.addWidget(rb)
            self._buttons.append((rb, key))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def _on_ok(self) -> None:
        for rb, key in self._buttons:
            if rb.isChecked():
                self.choice = key
                break
        self.accept()


class AccountLinkDialog(QDialog):
    """Map each remote account to an MFL account (or leave it unlinked)."""

    def __init__(
        self,
        remote_rows: list[tuple[str, str]],   # (remote_id, label)
        mfl_accounts: list[AccountSummary],
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Link accounts")
        self.setMinimumWidth(460)
        self.mapping: dict[str, int] = {}
        self._combos: list[tuple[str, QComboBox]] = []

        outer = QVBoxLayout(self)
        outer.addWidget(_muted(QLabel(
            "Choose which MFL account each bank account feeds into. Leave one "
            "as “Don't link” to skip it."
        )))
        form = QFormLayout()
        for remote_id, label in remote_rows:
            combo = QComboBox()
            combo.addItem("Don't link", userData=None)
            for a in mfl_accounts:
                combo.addItem(f"{a.name}  ·  {a.currency}", userData=a.id)
            form.addRow(label, combo)
            self._combos.append((remote_id, combo))
        outer.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def _on_ok(self) -> None:
        chosen: dict[str, int] = {}
        used: set[int] = set()
        for remote_id, combo in self._combos:
            acct_id = combo.currentData()
            if acct_id is None:
                continue
            if acct_id in used:
                QMessageBox.warning(
                    self, "Duplicate account",
                    "Each MFL account can link to only one bank account. "
                    "Please pick distinct accounts.",
                )
                return
            used.add(acct_id)
            chosen[remote_id] = acct_id
        self.mapping = chosen
        self.accept()


class BankFeedsDialog(QDialog):
    """List feeds across all providers; add, remove, and update them."""

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
        self.setWindowTitle("Bank Feeds")
        self.setMinimumSize(660, 380)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.addWidget(_muted(QLabel(
            "Automatic transaction downloads. Each provider uses your own "
            "credentials; fetched transactions go through the same dedup and "
            "review as a file import before they post."
        )))

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Account", "Provider", "Institution", "Last updated", "Status"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        outer.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._add_btn = QPushButton("Add…")
        self._remove_btn = QPushButton("Remove")
        self._update_btn = QPushButton("Update selected")
        self._update_all_btn = QPushButton("Update all")
        self._add_btn.clicked.connect(self._on_add)
        self._remove_btn.clicked.connect(self._on_remove)
        self._update_btn.clicked.connect(lambda: self._update([self._selected_account_id()]))
        self._update_all_btn.clicked.connect(
            lambda: self._update([f.account_id for f in self._feeds])
        )
        row.addWidget(self._add_btn)
        row.addWidget(self._remove_btn)
        row.addStretch(1)
        row.addWidget(self._update_btn)
        row.addWidget(self._update_all_btn)
        outer.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        outer.addWidget(close)

        self._feeds: list = []
        self._reload()

    # ── list ──

    def _reload(self) -> None:
        self._feeds = list(self._repo.list_feed_accounts())
        by_id = {a.id: a for a in self._repo.list_accounts()}
        self._table.setRowCount(len(self._feeds))
        for r, feed in enumerate(self._feeds):
            acct = by_id.get(feed.account_id)
            name = acct.name if acct else f"(account {feed.account_id})"
            synced = (feed.last_synced_at or "—")[:16].replace("T", " ")
            label = sync.PROVIDER_LABELS.get(feed.provider, feed.provider)
            cells = [name, label, feed.institution_name or "—", synced, feed.status]
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
        self._remove_btn.setEnabled(has_sel)
        self._update_btn.setEnabled(has_sel)
        self._update_all_btn.setEnabled(bool(self._feeds))

    def _unlinked_accounts(self) -> list[AccountSummary]:
        linked = {f.account_id for f in self._repo.list_feed_accounts()}
        return [a for a in self._repo.list_accounts()
                if a.id not in linked and a.archived_at is None]

    # ── add (dispatch to a provider flow) ──

    def _on_add(self) -> None:
        if not self._unlinked_accounts():
            QMessageBox.information(
                self, "No accounts available",
                "Every account already has a feed (or there are no accounts).",
            )
            return
        picker = ProviderPickerDialog(self)
        if picker.exec() != QDialog.Accepted or not picker.choice:
            return
        provider = picker.choice
        try:
            if provider == sync.OFX:
                self._add_ofx()
            elif provider == sync.SIMPLEFIN:
                self._add_simplefin()
            elif provider == sync.ENABLEBANKING:
                self._add_enablebanking()
            elif provider == sync.PLAID:
                self._add_plaid()
        except (SimpleFinError, EnableBankingError, PlaidError) as e:
            QMessageBox.critical(self, "Couldn't connect", str(e))
        self._reload()

    def _link_mapped(self, provider, remote_rows, remote_labels, *,
                     requisition_for, post_link=None) -> int:
        """Show the mapping dialog and link the chosen accounts. ``remote_rows``
        is [(remote_id, label)]; ``requisition_for(remote_id)`` returns the
        item/session ref (or None); ``post_link(remote_id, account_id)`` runs
        any per-feed persistence. Returns how many were linked."""
        dlg = AccountLinkDialog(remote_rows, self._unlinked_accounts(), parent=self)
        if dlg.exec() != QDialog.Accepted or not dlg.mapping:
            return 0
        for remote_id, account_id in dlg.mapping.items():
            self._repo.link_feed_account(
                account_id=account_id, provider=provider,
                external_account_id=remote_id,
                requisition_id=requisition_for(remote_id),
                institution_name=remote_labels.get(remote_id) or None,
            )
            if post_link is not None:
                post_link(remote_id, account_id)
        return len(dlg.mapping)

    def _add_ofx(self) -> None:
        dlg = OfxConnectionDialog(self._repo, self._unlinked_accounts(), parent=self)
        if dlg.exec() != QDialog.Accepted or dlg.result_cfg is None:
            return
        cfg = ofx_store.save_config(self._repo, dlg.result_account_id, dlg.result_cfg)
        self._repo.link_feed_account(
            account_id=dlg.result_account_id, provider=sync.OFX,
            external_account_id=cfg["acct_id"],
            institution_name=cfg.get("institution_name") or None,
        )

    def _add_simplefin(self) -> None:
        access = sync.get_simplefin_access_url(self._repo)
        if not access:
            token, ok = QInputDialog.getText(
                self, "SimpleFIN setup",
                "Paste your SimpleFIN setup token (from your SimpleFIN Bridge "
                "account). It is claimed once for a stored access URL:",
            )
            if not ok or not token.strip():
                return
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            try:
                access = claim_access_url(token.strip())
            finally:
                QApplication.restoreOverrideCursor()
            sync.set_simplefin_access_url(self._repo, access)
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            accounts = SimpleFinClient(access).list_accounts()
        finally:
            QApplication.restoreOverrideCursor()
        if not accounts:
            QMessageBox.information(self, "No accounts", "SimpleFIN returned no accounts.")
            return
        rows = [(a.id, f"{a.name} ({a.currency} {a.balance})") for a in accounts]
        labels = {a.id: a.name for a in accounts}
        self._link_mapped(sync.SIMPLEFIN, rows, labels, requisition_for=lambda _id: None)

    def _add_enablebanking(self) -> None:
        if sync.get_enablebanking_app(self._repo) is None:
            if not self._collect_enablebanking_app():
                return
        app_id, key = sync.get_enablebanking_app(self._repo)
        client = EnableBankingClient(app_id, key)
        country, ok = QInputDialog.getText(
            self, "Country", "Two-letter country code of your bank:", text="GB",
        )
        if not ok or not country.strip():
            return
        country = country.strip().upper()
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            banks = client.list_aspsps(country)
        finally:
            QApplication.restoreOverrideCursor()
        if not banks:
            QMessageBox.information(self, "No banks", f"No banks listed for {country}.")
            return
        names = [b.name for b in banks]
        name, ok = QInputDialog.getItem(self, "Choose your bank", "Bank:", names, 0, False)
        if not ok or not name:
            return
        redirect = sync.get_enablebanking_redirect(self._repo)
        valid_until = (datetime.datetime.now(datetime.timezone.utc)
                       + datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            auth = client.start_authorization(
                aspsp_name=name, country=country, redirect_url=redirect,
                state=uuid.uuid4().hex, valid_until=valid_until,
            )
        finally:
            QApplication.restoreOverrideCursor()
        webbrowser.open(auth.url)
        code = self._prompt_for_code(
            "Enable Banking consent",
            "A browser has opened for you to log in to your bank. When it "
            f"redirects to {redirect} , copy the whole address-bar URL and "
            "paste it here:",
        )
        if not code:
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            session = client.create_session(code)
        finally:
            QApplication.restoreOverrideCursor()
        if not session.accounts:
            QMessageBox.information(self, "No accounts", "No accounts were authorised.")
            return
        rows = [(a.uid, f"{a.name or a.iban or a.uid} {a.currency}".strip())
                for a in session.accounts]
        labels = {a.uid: (a.name or a.iban or name) for a in session.accounts}

        def post_link(uid, account_id):
            sync.set_enablebanking_feed(self._repo, account_id, {
                "uid": uid, "valid_until": valid_until, "session_id": session.session_id,
            })

        self._link_mapped(
            sync.ENABLEBANKING, rows, labels,
            requisition_for=lambda _id: session.session_id, post_link=post_link,
        )

    def _add_plaid(self) -> None:
        if sync.get_plaid_creds(self._repo) is None:
            if not self._collect_plaid_creds():
                return
        client_id, secret, env = sync.get_plaid_creds(self._repo)
        client = PlaidClient(client_id, secret, environment=env)
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            link_token, url = client.create_hosted_link_token(
                user_id="mfl-user", client_name="My Financial Life",
            )
        finally:
            QApplication.restoreOverrideCursor()
        webbrowser.open(url)
        if QMessageBox.information(
            self, "Plaid",
            "A browser has opened to connect your bank via Plaid. Click OK "
            "here once you have finished in the browser.",
            QMessageBox.Ok | QMessageBox.Cancel,
        ) != QMessageBox.Ok:
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            public_token = client.get_link_public_token(link_token)
            if not public_token:
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(
                    self, "Not finished yet",
                    "Plaid hasn't reported a completed connection. If you did "
                    "finish, wait a moment and add the feed again.",
                )
                return
            access_token, item_id = client.exchange_public_token(public_token)
            accounts = client.accounts_get(access_token)
        finally:
            QApplication.restoreOverrideCursor()
        sync.set_plaid_item(self._repo, item_id, {"access_token": access_token, "cursor": ""})
        rows = [(a.account_id, f"{a.name} ••{a.mask}".strip()) for a in accounts]
        labels = {a.account_id: a.name for a in accounts}
        self._link_mapped(
            sync.PLAID, rows, labels, requisition_for=lambda _id: item_id,
        )

    # ── small credential collectors ──

    def _collect_enablebanking_app(self) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Enable Banking application")
        form = QFormLayout(dlg)
        app_edit = QLineEdit()
        key_edit = QLineEdit()
        key_edit.setReadOnly(True)
        browse = QPushButton("Choose private-key file…")
        key_pem = {"text": ""}

        def pick():
            path, _ = QFileDialog.getOpenFileName(
                dlg, "Enable Banking private key", "", "PEM files (*.pem *.key);;All files (*)")
            if path:
                try:
                    key_pem["text"] = open(path, "r", encoding="utf-8").read()
                    key_edit.setText(path)
                except OSError as e:
                    QMessageBox.warning(dlg, "Couldn't read key", str(e))

        browse.clicked.connect(pick)
        form.addRow("Application ID:", app_edit)
        form.addRow("Private key:", key_edit)
        form.addRow("", browse)
        form.addRow(_muted(QLabel(
            "From your free Enable Banking application. Stored inside this .mfl "
            "file; remove before sharing snapshots.")))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return False
        if not app_edit.text().strip() or not key_pem["text"].strip():
            QMessageBox.warning(self, "Missing details", "Application ID and a private key are required.")
            return False
        sync.set_enablebanking_app(self._repo, app_edit.text().strip(), key_pem["text"])
        return True

    def _collect_plaid_creds(self) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Plaid credentials")
        form = QFormLayout(dlg)
        cid = QLineEdit()
        sek = QLineEdit()
        sek.setEchoMode(QLineEdit.Password)
        env = QComboBox()
        env.addItems(["production", "sandbox"])
        form.addRow("Client ID:", cid)
        form.addRow("Secret:", sek)
        form.addRow("Environment:", env)
        form.addRow(_muted(QLabel(
            "From your Plaid dashboard. Stored inside this .mfl file; remove "
            "before sharing snapshots.")))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return False
        if not cid.text().strip() or not sek.text().strip():
            QMessageBox.warning(self, "Missing details", "Client ID and secret are required.")
            return False
        sync.set_plaid_creds(self._repo, cid.text().strip(), sek.text().strip(), env.currentText())
        return True

    def _prompt_for_code(self, title: str, message: str) -> Optional[str]:
        """Ask the user to paste the redirected URL; extract the ``code``."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        v.addWidget(_muted(QLabel(message)))
        edit = QPlainTextEdit()
        edit.setPlaceholderText("https://…?code=…&state=…")
        v.addWidget(edit)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return None
        text = edit.toPlainText().strip()
        if not text:
            return None
        import urllib.parse
        if "code=" in text:
            qs = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
            params = urllib.parse.parse_qs(qs)
            if params.get("code"):
                return params["code"][0]
        return text  # assume they pasted the bare code

    # ── remove ──

    def _on_remove(self) -> None:
        account_id = self._selected_account_id()
        if account_id is None:
            return
        feed = self._repo.get_feed_account(account_id)
        acct = self._repo.get_account_by_id(account_id)
        name = acct.name if acct else "this account"
        if QMessageBox.question(
            self, "Remove feed",
            f"Remove the bank feed for {name}? Already-imported transactions "
            "stay; only the connection is removed.",
        ) != QMessageBox.Yes:
            return
        self._repo.unlink_feed_account(account_id)
        if feed and feed.provider == sync.OFX:
            ofx_store.clear_config(self._repo, account_id)
        elif feed and feed.provider == sync.ENABLEBANKING:
            sync.set_enablebanking_feed(self._repo, account_id, {})
        self._reload()

    # ── update (fetch → stage → commit, shared pipeline) ──

    def _update(self, account_ids: list) -> None:
        account_ids = [a for a in account_ids if a is not None]
        if not account_ids:
            return
        by_id = {a.id: a for a in self._repo.list_accounts()}
        feed_by_acct = {f.account_id: f for f in self._repo.list_feed_accounts()}
        lines: list[str] = []
        any_committed = False
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            for account_id in account_ids:
                acct = by_id.get(account_id)
                feed = feed_by_acct.get(account_id)
                name = acct.name if acct else f"account {account_id}"
                if acct is None or feed is None:
                    lines.append(f"• {name}: not linked — skipped.")
                    continue
                try:
                    raw = sync.fetch_raw_for_feed(self._repo, feed)
                    token = self._service.stage_feed(acct.iri, raw, provider=feed.provider)
                    pending = self._service.get_pending(token)
                    accepted = {tx.fitid for tx in pending.transactions
                                if tx.status == "potential_match"}
                    result = self._service.commit_import(
                        token, pending.suggested_status, accepted)
                    self._repo.mark_feed_synced(account_id)
                    any_committed = any_committed or result.imported > 0
                    lines.append(
                        f"• {name}: {result.imported} new, "
                        f"{result.skipped} skipped, {result.matched} matched.")
                except Exception as e:
                    self._repo.set_feed_status(account_id, "error")
                    lines.append(f"• {name}: ✗ {e}")
        finally:
            QApplication.restoreOverrideCursor()
        self._reload()
        if any_committed and self._on_updated is not None:
            self._on_updated()
        QMessageBox.information(self, "Bank feed update", "\n".join(lines))
