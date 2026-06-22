"""Generate marketing screenshots from the public demo into ./screenshots/.

Run offscreen with the Qt interpreter, from the repo root:

    QT_QPA_PLATFORM=offscreen PYTHONPATH="$(pwd)" \
        /opt/homebrew/Caskroom/miniforge/base/bin/python3 tools/make_screenshots.py

Renders the main windows + a couple of dialogs against a throwaway copy of
mfl_public.mfl (light theme, plus one dark-mode shot). The demo's expense
subcategories are re-parented to top-level in the temp copy so the Sankey and
category breakdowns show distinct colours (the committed demo nests them all
under one "Expense" root). The screenshots/ folder is gitignored — these are
website handoff assets, not app source.
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "screenshots"
OUT.mkdir(exist_ok=True)
SRC = REPO / "mfl_public.mfl"
TMP = "/tmp/mfl_shots_demo.mfl"


def _prep_demo() -> str:
    for ext in ("", "-wal", "-shm"):
        p = TMP + ext
        if os.path.exists(p):
            os.remove(p)
    shutil.copy(SRC, TMP)
    con = sqlite3.connect(TMP)
    row = con.execute(
        "SELECT id FROM category WHERE name='Expense' AND parent_id IS NULL"
    ).fetchone()
    if row:
        con.execute(
            "UPDATE category SET parent_id=NULL WHERE parent_id=? AND kind='expense'",
            (row[0],),
        )
        con.commit()
    con.close()
    return TMP


def main() -> int:
    db = _prep_demo()

    from PySide6.QtWidgets import QApplication, QTabWidget
    app = QApplication(sys.argv)
    app.setApplicationName("MFL")
    app.setOrganizationName("MFL")

    from mfl_desktop.ui.theme import apply_theme
    from mfl_desktop import resources
    from mfl_desktop.db.repository import Repository
    from mfl_desktop.ui.register_window import RegisterWindow
    from mfl_desktop.reports.filters import (
        TYPE_SPENDING_OVER_TIME, TYPE_INCOME_EXPENSE, TYPE_SANKEY,
        TYPE_INVESTMENT_RETURNS,
    )

    apply_theme(app, "light")
    app.setWindowIcon(resources.app_icon())
    repo = Repository(db)

    def settle(n: int = 8):
        for _ in range(n):
            app.processEvents()

    def shot(widget, name: str, w: int, h: int):
        widget.resize(w, h)
        widget.show()
        settle()
        widget.grab().save(str(OUT / f"{name}.png"))
        print("  saved", name)

    win = RegisterWindow(repo, None)
    win.resize(1320, 840)
    win.show()
    settle()

    # ── main views ─────────────────────────────────────────────────────────
    win._show_home(); settle()
    shot(win, "01_home", 1320, 840)

    win._show_account("mrl:CashAccount_1"); settle()
    shot(win, "02_register", 1320, 840)

    win._show_all_transactions(); settle()
    shot(win, "03_all_transactions", 1320, 840)

    # ── report windows ──────────────────────────────────────────────────────
    win._on_net_worth(); settle()
    shot(win._net_worth_win, "04_net_worth", 1140, 740)

    reports = [
        (TYPE_SANKEY, "05_sankey", 1240, 780),
        (TYPE_INCOME_EXPENSE, "06_income_expense", 1240, 760),
        (TYPE_SPENDING_OVER_TIME, "07_spending_over_time", 1240, 760),
        (TYPE_INVESTMENT_RETURNS, "08_investment_returns", 1240, 780),
    ]
    for type_key, name, w, h in reports:
        win._open_bare_report(type_key)
        settle(12)
        shot(win._bare_report_wins[type_key], name, w, h)

    # ── investment holdings (account summary, Holdings tab) ──────────────────
    win._open_account_summary(6)  # US Brokerage
    summ = win._account_summary_wins[6]
    summ.resize(1240, 820); summ.show(); settle(12)
    tabs = summ.findChild(QTabWidget)
    if tabs is not None:
        tabs.setCurrentIndex(1)  # Holdings
        settle(10)
    shot(summ, "09_investments_holdings", 1240, 820)

    # ── loan amortisation (account summary for a loan) ───────────────────────
    win._open_account_summary(10)  # Home Mortgage
    settle(12)
    shot(win._account_summary_wins[10], "10_loan_amortisation", 1240, 820)

    # ── budget (annual + monthly) ────────────────────────────────────────────
    win._on_open_budget()
    budget = win._budget_win
    budget.resize(1320, 820); budget.show(); settle(12)
    budget._view.setCurrentIndex(0); settle(8)
    shot(budget, "11_budget_annual", 1320, 820)
    budget._view.setCurrentIndex(1); settle(8)
    shot(budget, "12_budget_monthly", 1320, 820)

    # ── About + first-run dialogs ────────────────────────────────────────────
    from mfl_desktop.ui.about_dialog import AboutDialog
    about = AboutDialog(); about.show(); settle()
    about.grab().save(str(OUT / "13_about.png")); print("  saved 13_about")

    from mfl_desktop.ui.first_run_dialog import FirstRunDialog
    fr = FirstRunDialog(repo); fr.show(); settle()
    fr.grab().save(str(OUT / "14_first_run.png")); print("  saved 14_first_run")

    # ── one dark-mode shot ───────────────────────────────────────────────────
    apply_theme(app, "dark")
    win._show_home(); settle(10)
    shot(win, "15_home_dark", 1320, 840)
    apply_theme(app, "light")

    for ext in ("", "-wal", "-shm"):
        p = TMP + ext
        if os.path.exists(p):
            os.remove(p)
    print("DONE — screenshots in", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
