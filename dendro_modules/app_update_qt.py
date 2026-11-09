#!/usr/bin/env python3
"""
app_update_qt.py - "Updates & support" menu for Qt programs (PySide6, PyQt6 or PyQt5).

    import app_update_qt
    self.updater = app_update_qt.QtUpdater(self, button=self.btn_updates)   # or menu_bar=self.menuBar()

The menu: check for updates, daily automatic check, download the latest version, user guides,
contact the developer (e-mail / GitHub issue), restore the previous version, version and help.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

if "PyQt5" in sys.modules:
    from PyQt5.QtCore import Qt, QTimer, QUrl  # type: ignore
    from PyQt5.QtGui import QDesktopServices  # type: ignore
    from PyQt5.QtWidgets import (QAction, QApplication, QCheckBox, QComboBox, QDialog, QFormLayout,  # type: ignore
                                 QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                                 QProgressDialog, QPushButton, QTextBrowser, QVBoxLayout)
elif "PyQt6" in sys.modules:
    from PyQt6.QtCore import Qt, QTimer, QUrl  # type: ignore
    from PyQt6.QtGui import QAction, QDesktopServices  # type: ignore
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,  # type: ignore
                                 QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog,
                                 QPushButton, QTextBrowser, QVBoxLayout)
else:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QAction, QDesktopServices
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                                   QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton,
                                   QTextBrowser, QVBoxLayout)

import app_update as U


def _exec(w) -> Any:
    return w.exec() if hasattr(w, "exec") else w.exec_()


def _open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


class _Job:
    """Run fn in a thread; deliver (result, error) in the GUI thread through a polling timer."""

    def __init__(self, parent, fn: Callable[[], Any], done: Callable[[Any, Optional[BaseException]], None],
                 progress: Optional[Callable[[int, int], None]] = None) -> None:
        self.q: "queue.Queue" = queue.Queue()
        self.done = done
        self.progress = progress
        self.timer = QTimer(parent)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll)

        def run() -> None:
            try:
                self.q.put(("done", fn(), None))
            except BaseException as exc:  # noqa: BLE001 - reported to the user
                self.q.put(("done", None, exc))

        threading.Thread(target=run, daemon=True).start()
        self.timer.start()

    def report(self, a: int, b: int) -> None:
        self.q.put(("progress", a, b))

    def _poll(self) -> None:
        while True:
            try:
                kind, a, b = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "progress":
                if self.progress:
                    self.progress(a, b)
            else:
                self.timer.stop()
                self.done(a, b)
                return


class ContactDialog(QDialog):
    KINDS = ["Suggestion", "Bug report", "Question", "Comment"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Contact the developer")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Suggestions, comments and problem reports for <b>{U.display_name()}</b> go to "
                             f"<b>{U.developer()}</b> &lt;{U.contact_email()}&gt;."))
        f = QFormLayout()
        self.cmb = QComboBox()
        self.cmb.addItems(self.KINDS)
        f.addRow("Type:", self.cmb)
        self.ed_name = QLineEdit()
        f.addRow("Your name (optional):", self.ed_name)
        self.ed_mail = QLineEdit()
        f.addRow("Your e-mail (optional):", self.ed_mail)
        lay.addLayout(f)
        self.txt = QPlainTextEdit()
        self.txt.setPlaceholderText("Your message. For a problem: what you did, what happened, what you expected.")
        lay.addWidget(self.txt, 1)
        self.chk_sys = QCheckBox("Add the program version and system information")
        self.chk_sys.setChecked(True)
        lay.addWidget(self.chk_sys)
        row = QHBoxLayout()
        b_mail = QPushButton("Open in my e-mail program")
        b_mail.clicked.connect(self._mail)
        b_copy = QPushButton("Copy text and address")
        b_copy.clicked.connect(self._copy)
        b_gh = QPushButton("Post on GitHub")
        b_gh.setToolTip("Open a new GitHub issue with this text (needs a GitHub account)")
        b_gh.clicked.connect(self._github)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.reject)
        for b in (b_mail, b_copy, b_gh):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        lay.addLayout(row)

    def _text(self) -> str:
        return self.txt.toPlainText().strip()

    def _mail(self) -> None:
        if not self._text():
            QMessageBox.information(self, "Contact", "Please write a message first.")
            return
        _open_url(U.mailto_url(self.cmb.currentText(), self._text(), self.ed_name.text(), self.ed_mail.text(),
                               self.chk_sys.isChecked()))

    def _copy(self) -> None:
        body = self._text() + (("\n\n---\n" + U.system_summary()) if self.chk_sys.isChecked() else "")
        QApplication.clipboard().setText(f"To: {U.contact_email()}\nSubject: [{U.product()}] "
                                         f"{self.cmb.currentText()}\n\n{body}")
        QMessageBox.information(self, "Contact", f"Copied. Paste it into an e-mail to {U.contact_email()}.")

    def _github(self) -> None:
        if not self._text():
            QMessageBox.information(self, "Contact", "Please write a message first.")
            return
        _open_url(U.issue_url(self.cmb.currentText(), self._text(), self.chk_sys.isChecked()))


class QtUpdater:
    """Owns the 'Updates & support' menu and the update workflow of one window."""

    def __init__(self, window, button=None, menu_bar=None, title: str = "Updates && support") -> None:
        self.window = window
        self.button = button
        self.info: Optional[dict] = None
        self._busy = False
        self._job = None
        self.menu = QMenu(title.replace("&&", "&"), window)
        self._build_menu()
        if button is not None:
            button.setMenu(self.menu)
        if menu_bar is not None:
            menu_bar.addMenu(self.menu)
        just = None
        try:
            just = U.acknowledge_start()
        except Exception:
            pass
        if just:
            QTimer.singleShot(1500, lambda j=just: self._report_finished(j))
        if U.due_for_auto_check():
            QTimer.singleShot(20000, lambda: self.check(manual=False))

    # ---------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        m = self.menu
        m.addAction("Check for updates...", lambda: self.check(manual=True))
        self.act_auto = QAction("Check automatically (daily)", m)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(bool(U.settings().get("auto_check", True)))
        self.act_auto.toggled.connect(lambda v: U.save_settings(auto_check=bool(v)))
        m.addAction(self.act_auto)
        m.addAction("Download the latest version (release page)", lambda: _open_url(U.latest_release_url()))
        m.addSeparator()
        g = m.addMenu("User guides")
        for label, asset in U.guides():
            g.addAction(f"Download: {label}", lambda a=asset: _open_url(U.guide_url(a)))
        loc = U.local_guides()
        if loc:
            g.addSeparator()
            for p in loc:
                g.addAction(f"Open on this computer: {p.name}", lambda p=p: U.open_path(str(p)))
        g.addSeparator()
        g.addAction("All files of the latest release", lambda: _open_url(U.latest_release_url()))
        m.addAction("Contact the developer...", lambda: _exec(ContactDialog(self.window)))
        m.addAction("Report a problem on GitHub", lambda: _open_url(f"https://github.com/{U.repository()}/issues"))
        m.addSeparator()
        m.addAction("Restore previous version...", self.rollback)
        m.addAction("Update status / log...", self.show_status)
        m.addAction("How updates work...", lambda: QMessageBox.information(self.window, "Updates", U.help_text()))
        m.addAction(f"Version {U.version_text()}", self.show_version)

    def _set_button_hint(self, text: Optional[str]) -> None:
        if self.button is None:
            return
        if not hasattr(self, "_btn_text"):
            self._btn_text = self.button.text()
        self.button.setText(text or self._btn_text)
        self.button.setStyleSheet("font-weight: bold; color: #b45309;" if text else "")

    # ---------------------------------------------------------------- check
    def check(self, manual: bool = True) -> None:
        if self._busy:
            return
        self._busy = True
        if manual:
            QApplication.setOverrideCursor(_wait_cursor())

        def done(info, err) -> None:
            self._busy = False
            if manual:
                QApplication.restoreOverrideCursor()
            if err is not None:
                U.log(f"check failed: {err}")
                if manual:
                    QMessageBox.warning(self.window, "Updates", f"Could not check for updates:\n{err}\n\n"
                                        f"You can always open the release page:\n{U.releases_url()}")
                return
            self.info = info
            if info.get("none"):
                if manual:
                    QMessageBox.information(self.window, "Updates", "No release has been published yet.")
                return
            if not info.get("newer"):
                self._set_button_hint(None)
                if manual:
                    QMessageBox.information(self.window, "Updates", f"You have the latest version: {U.version_text()}.")
                return
            ident = f"{info['version']}/{info['build']}"
            self._set_button_hint(f"Update {info['version']} available")
            if not manual and U.settings().get("skip") == ident:
                return
            self._offer(info)

        self._job = _Job(self.window, lambda: U.check_release(), done)

    def _offer(self, info: dict) -> None:
        dlg = QDialog(self.window)
        dlg.setWindowTitle("Update available")
        dlg.resize(620, 480)
        lay = QVBoxLayout(dlg)
        man = info.get("manifest")
        size = f", {man['size'] / 1024 ** 2:.0f} MB" if man else ""
        lay.addWidget(QLabel(f"<b>{U.display_name()} {info['version']}</b> (build {info['build']}{size}) is available."
                             f"<br>Installed: {U.version_text()}."))
        notes = QTextBrowser()
        try:
            notes.setMarkdown(info.get("notes") or "(no release notes)")
        except Exception:
            notes.setPlainText(info.get("notes") or "(no release notes)")
        notes.setOpenExternalLinks(True)
        lay.addWidget(notes, 1)
        row = QHBoxLayout()
        can_install = bool(man) and U.is_frozen()
        choice = {"v": None}
        if can_install:
            b = QPushButton("Download and install")
            b.setDefault(True)
            b.clicked.connect(lambda: (choice.update(v="install"), dlg.accept()))
            row.addWidget(b)
        b2 = QPushButton("Open the release page")
        b2.clicked.connect(lambda: (choice.update(v="page"), dlg.accept()))
        row.addWidget(b2)
        b3 = QPushButton("Skip this version")
        b3.clicked.connect(lambda: (choice.update(v="skip"), dlg.accept()))
        row.addWidget(b3)
        row.addStretch(1)
        b4 = QPushButton("Later")
        b4.clicked.connect(dlg.reject)
        row.addWidget(b4)
        lay.addLayout(row)
        if not can_install:
            lay.addWidget(QLabel("<i>" + ("Running from the Python source: download the package from the release page."
                                          if not U.is_frozen() else "No package for this system in the release yet.")
                                 + "</i>"))
        _exec(dlg)
        if choice["v"] == "install":
            self.download(info)
        elif choice["v"] == "page":
            _open_url(info.get("html_url") or U.latest_release_url())
        elif choice["v"] == "skip":
            U.save_settings(skip=f"{info['version']}/{info['build']}")

    # ---------------------------------------------------------------- download / install
    def download(self, info: dict) -> None:
        try:
            U.installed_target()
        except Exception as exc:
            QMessageBox.warning(self.window, "Updates", str(exc))
            return
        cancel = threading.Event()
        dlg = QProgressDialog("Downloading and verifying the update...", "Cancel", 0, 100, self.window)
        dlg.setWindowTitle("Updates")
        dlg.setAutoClose(False)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(cancel.set)
        dlg.show()
        holder = {}

        def prog(a: int, b: int) -> None:
            dlg.setValue(int(100 * a / max(1, b)))
            dlg.setLabelText(f"Downloading and verifying the update... {a / 1024 ** 2:.0f} / {b / 1024 ** 2:.0f} MB")

        def done(work, err) -> None:
            dlg.close()
            if err is not None:
                if not isinstance(err, InterruptedError):
                    QMessageBox.warning(self.window, "Updates", f"The download failed:\n{err}")
                return
            self._prepare(Path(work))

        job = _Job(self.window, lambda: U.download_release(info, holder["job"].report, cancel), done, prog)
        holder["job"] = job
        self._job = job

    def _prepare(self, work: Path) -> None:
        QApplication.setOverrideCursor(_wait_cursor())

        def done(job, err) -> None:
            QApplication.restoreOverrideCursor()
            if err is not None:
                QMessageBox.warning(self.window, "Updates", f"The update could not be prepared:\n{err}")
                return
            self._ask_apply(work)

        self._job = _Job(self.window, lambda: U.prepare_update(work), done)

    def _ask_apply(self, work: Path) -> None:
        target = U.installed_target()
        others = [n for p, n in U.running_copies(target) if p != os.getpid()]
        text = ("The update is downloaded, verified and ready.\n\nIt is installed when all windows of "
                f"{U.display_name()} are closed; the program then starts again by itself.")
        if others:
            text += "\n\nAlso close: " + ", ".join(sorted(set(others)))
        box = QMessageBox(self.window)
        box.setWindowTitle("Install the update")
        box.setText(text)
        b_now = box.addButton("Close and update now", QMessageBox.AcceptRole if hasattr(QMessageBox, "AcceptRole")
                              else QMessageBox.ButtonRole.AcceptRole)
        box.addButton("When I close the program", QMessageBox.RejectRole if hasattr(QMessageBox, "RejectRole")
                      else QMessageBox.ButtonRole.RejectRole)
        _exec(box)
        try:
            U.start_apply(work, [p for p, _n in U.running_copies(target)])
        except Exception as exc:
            QMessageBox.warning(self.window, "Updates", f"The update helper could not be started:\n{exc}")
            return
        self._set_button_hint("Update on close")
        if box.clickedButton() == b_now:
            QTimer.singleShot(100, self.window.close)

    # ---------------------------------------------------------------- other
    def rollback(self) -> None:
        try:
            target = U.installed_target()
            prev = U.previous_versions(target)
        except Exception as exc:
            QMessageBox.information(self.window, "Restore previous version", str(exc))
            return
        if not prev:
            QMessageBox.information(self.window, "Restore previous version", "No previous version is kept.")
            return
        idx = U.read_json(prev[0] / U.index_name(), {}) or {}
        if QMessageBox.question(self.window, "Restore previous version",
                                f"Go back to version {idx.get('version', '?')} (build {idx.get('build', '?')})?\n\n"
                                f"It is restored when all windows of the program are closed.") != QMessageBox.Yes:
            return
        try:
            U.start_rollback([p for p, _n in U.running_copies(target)])
        except Exception as exc:
            QMessageBox.warning(self.window, "Restore previous version", str(exc))
            return
        QTimer.singleShot(100, self.window.close)

    def show_status(self) -> None:
        home = U.update_home()
        runs = sorted(home.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        lines = [f"Installed: {U.version_text()}", f"Update folder: {home}"]
        if runs:
            st = U.job_status(runs[0])
            job = U.read_json(runs[0] / "job.json", {}) or {}
            lines.append(f"Last update: {job.get('version', '?')}/{job.get('build', '?')} - "
                         f"{U.STATUS_NAMES.get(st.get('state'), st.get('state', 'downloaded'))} {st.get('message', '')}")
        try:
            prev = U.previous_versions()
            if prev:
                lines.append("Previous version kept: " + str(prev[0]))
        except Exception:
            pass
        box = QMessageBox(self.window)
        box.setWindowTitle("Update status")
        box.setText("\n".join(lines))
        b = box.addButton("Open the update folder", QMessageBox.ActionRole if hasattr(QMessageBox, "ActionRole")
                          else QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.Close if hasattr(QMessageBox, "Close") else QMessageBox.StandardButton.Close)
        _exec(box)
        if box.clickedButton() == b:
            U.open_path(str(home))

    def show_version(self) -> None:
        QMessageBox.information(self.window, "Version", f"{U.display_name()}\nVersion {U.version_text()}\n\n"
                                f"Releases: {U.releases_url()}\nContact: {U.contact_email()}")

    def _report_finished(self, job: dict) -> None:
        if job.get("error"):
            QMessageBox.warning(self.window, "Updates", "The last update did not complete:\n" + job["error"]
                                + "\n\nThe previous version was kept or restored.")
            return
        if job.get("rollback"):
            msg = f"The previous version {job.get('version')} was restored."
        else:
            msg = (f"{U.display_name()} was updated to {job.get('version')} (build {job.get('build')}).\n"
                   f"The previous version is kept next to the program folder for a while.")
        try:
            self.window.statusBar().showMessage(msg.split("\n")[0], 15000)
        except Exception:
            pass
        QMessageBox.information(self.window, "Updates", msg)


def _wait_cursor():
    try:
        return Qt.WaitCursor
    except AttributeError:
        return Qt.CursorShape.WaitCursor
