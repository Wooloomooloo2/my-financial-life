"""Remembered group expansion in the sidebar (ADR-168).

Collapsing a group in the left panel used to reset to the default (expanded)
on the next reload, navigation, or app restart — the expand/collapse state
lived only on the transient QTreeWidgetItem objects, which _populate rebuilds
from scratch. The state is now persisted per-file in the .mfl's `setting`
table (folder ids are per-file, so it must not be an app-level preference), so
a collapsed group stays collapsed until the user re-opens it.

Needs offscreen Qt. The persistence store is the file's own `setting` table,
so — unlike the balance-mode test — there is no shared QSettings key to guard.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository


def _app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MFL")
    app.setApplicationName("MFL")
    return app


def _repo_with_folder():
    """A file with one account folder holding one account, so the sidebar
    renders a collapsible 'folder' group with a stable DB id."""
    db = Path(tempfile.mkdtemp(prefix="mfl_grp_")) / "m.mfl"
    repo = Repository(db)
    folder = repo.create_folder("Everyday")
    acct = repo.create_account(
        name="Cur", type_key="cash", currency="GBP",
        opening_balance=Decimal("100.00"),
    )
    repo.set_account_folder(acct.id, folder.id)
    return repo, folder.id


def _sidebar(repo):
    from mfl_desktop.ui.sidebar import Sidebar
    accounts = repo.list_accounts(include_closed=True)
    folders = repo.list_folders()
    balances = repo.compute_account_balances(include_closed=True)
    return Sidebar(accounts, folders, balances, repo=repo)


def _folder_item(sb, folder_id: int):
    """The QTreeWidgetItem for the given account-folder id."""
    from mfl_desktop.ui.sidebar import KIND_ROLE
    from PySide6.QtCore import Qt
    for i in range(sb.topLevelItemCount()):
        top = sb.topLevelItem(i)
        for j in range(top.childCount()):
            child = top.child(j)
            if (child.data(0, KIND_ROLE) == "folder"
                    and child.data(0, Qt.UserRole) == folder_id):
                return child
    raise AssertionError(f"folder {folder_id} not found in sidebar")


# ── tests ────────────────────────────────────────────────────────────────────


def test_collapse_persists_across_reload():
    _app()
    repo, fid = _repo_with_folder()
    sb = _sidebar(repo)

    folder = _folder_item(sb, fid)
    assert folder.isExpanded()                     # default is expanded

    # Collapse it the way a click does — this drives the itemCollapsed signal.
    folder.setExpanded(False)
    assert not folder.isExpanded()

    # A reload (balance refresh, navigation, report change) rebuilds the tree.
    sb.reload(
        repo.list_accounts(include_closed=True),
        repo.list_folders(),
        repo.compute_account_balances(include_closed=True),
    )
    assert not _folder_item(sb, fid).isExpanded()  # stayed collapsed


def test_collapse_persists_across_restart():
    _app()
    repo, fid = _repo_with_folder()
    db_path = repo.db_path

    sb = _sidebar(repo)
    _folder_item(sb, fid).setExpanded(False)

    # Simulate an app restart: close the file and open it fresh.
    repo.checkpoint()
    repo.close()
    repo2 = Repository(db_path)
    sb2 = _sidebar(repo2)
    assert not _folder_item(sb2, fid).isExpanded()  # remembered from the file


def test_reopening_clears_the_memory():
    _app()
    repo, fid = _repo_with_folder()
    sb = _sidebar(repo)

    _folder_item(sb, fid).setExpanded(False)
    _folder_item(sb, fid).setExpanded(True)        # user opens it again

    sb.reload(
        repo.list_accounts(include_closed=True),
        repo.list_folders(),
        repo.compute_account_balances(include_closed=True),
    )
    assert _folder_item(sb, fid).isExpanded()      # back to expanded, and stays


def test_state_is_per_file_not_shared():
    _app()
    repo_a, fid_a = _repo_with_folder()
    repo_b, fid_b = _repo_with_folder()

    sb_a = _sidebar(repo_a)
    _folder_item(sb_a, fid_a).setExpanded(False)   # collapse in file A only

    # File B, opened independently, must be unaffected.
    sb_b = _sidebar(repo_b)
    assert _folder_item(sb_b, fid_b).isExpanded()


def test_set_repo_switches_remembered_state():
    """A file switch reuses the same Sidebar via set_repo — the newly-adopted
    file's remembered expansion must take over from the previous file's."""
    _app()
    repo_a, fid_a = _repo_with_folder()
    repo_b, fid_b = _repo_with_folder()

    sb = _sidebar(repo_a)
    _folder_item(sb, fid_a).setExpanded(False)     # collapse a group in file A

    # Adopt file B (its group has never been collapsed) the way the register
    # window does: set_repo, then reload with B's data.
    sb.set_repo(repo_b)
    sb.reload(
        repo_b.list_accounts(include_closed=True),
        repo_b.list_folders(),
        repo_b.compute_account_balances(include_closed=True),
    )
    assert _folder_item(sb, fid_b).isExpanded()     # B's own (default) state

    # And writes now land in file B, not the stale file A.
    _folder_item(sb, fid_b).setExpanded(False)
    assert repo_b.get_setting(sb._GROUP_EXPANSION_KEY)


def test_repoless_sidebar_is_harmless():
    """Tests construct repo-less sidebars; persistence must no-op, not raise."""
    _app()
    from mfl_desktop.ui.sidebar import Sidebar
    sb = Sidebar([], [], {})                        # no repo
    assert sb._load_group_expansion() == {}
    # Toggling a (would-be) group with no repo must not raise.
    sb.set_repo(None)


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
