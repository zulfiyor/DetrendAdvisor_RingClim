#!/usr/bin/env python3
"""
app_update_tk.py - "Updates & support" menu for Tkinter programs (same features as app_update_qt.py).

    import app_update_tk
    self.updater = app_update_tk.TkUpdater(root, button=ttk_button)    # menu pops up under the button
    # or: menubar.add_cascade(label="Updates & support", menu=self.updater.menu)
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

import app_update as U


def _open_url(url: str) -> None:
    webbrowser.open(url)


class _Job:
    def __init__(self, root, fn: Callable[[], Any], done: Callable[[Any, Optional[BaseException]], None],
                 progress: Optional[Callable[[int, int], None]] = None) -> None:
        self.root, self.done, self.progress = root, done, progress
        self.q: "queue.Queue" = queue.Queue()

        def run() -> None:
            try:
                self.q.put(("done", fn(), None))
            except BaseException as exc:  # noqa: BLE001
                self.q.put(("done", None, exc))

        threading.Thread(target=run, daemon=True).start()
        self.root.after(150, self._poll)

    def report(self, a: int, b: int) -> None:
        self.q.put(("progress", a, b))

    def _poll(self) -> None:
        while True:
            try:
                kind, a, b = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                if self.progress:
                    self.progress(a, b)
            else:
                self.done(a, b)
                return
        try:
            self.root.after(150, self._poll)
        except tk.TclError:
            pass


class ContactDialog(tk.Toplevel):
    KINDS = ["Suggestion", "Bug report", "Question", "Comment"]

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Contact the developer")
        self.geometry("580x460")
        self.transient(parent)
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Suggestions, comments and problem reports for {U.display_name()} go to "
                            f"{U.developer()} <{U.contact_email()}>.", wraplength=540).pack(anchor="w")
        grid = ttk.Frame(frm)
        grid.pack(fill="x", pady=6)
        ttk.Label(grid, text="Type:").grid(row=0, column=0, sticky="w")
        self.kind = tk.StringVar(value=self.KINDS[0])
        ttk.Combobox(grid, textvariable=self.kind, values=self.KINDS, state="readonly", width=20).grid(
            row=0, column=1, sticky="w")
        ttk.Label(grid, text="Your name (optional):").grid(row=1, column=0, sticky="w")
        self.name = tk.StringVar()
        ttk.Entry(grid, textvariable=self.name, width=40).grid(row=1, column=1, sticky="we")
        ttk.Label(grid, text="Your e-mail (optional):").grid(row=2, column=0, sticky="w")
        self.mail = tk.StringVar()
        ttk.Entry(grid, textvariable=self.mail, width=40).grid(row=2, column=1, sticky="we")
        self.txt = tk.Text(frm, height=12, wrap="word")
        self.txt.pack(fill="both", expand=True)
        self.sysinfo = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Add the program version and system information",
                        variable=self.sysinfo).pack(anchor="w", pady=4)
        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Button(row, text="Open in my e-mail program", command=self._mail).pack(side="left")
        ttk.Button(row, text="Copy text and address", command=self._copy).pack(side="left", padx=4)
        ttk.Button(row, text="Post on GitHub", command=self._github).pack(side="left")
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")

    def _text(self) -> str:
        return self.txt.get("1.0", "end").strip()

    def _mail(self) -> None:
        if not self._text():
            messagebox.showinfo("Contact", "Please write a message first.", parent=self)
            return
        _open_url(U.mailto_url(self.kind.get(), self._text(), self.name.get(), self.mail.get(), self.sysinfo.get()))

    def _copy(self) -> None:
        body = self._text() + (("\n\n---\n" + U.system_summary()) if self.sysinfo.get() else "")
        self.clipboard_clear()
        self.clipboard_append(f"To: {U.contact_email()}\nSubject: [{U.product()}] {self.kind.get()}\n\n{body}")
        messagebox.showinfo("Contact", f"Copied. Paste it into an e-mail to {U.contact_email()}.", parent=self)

    def _github(self) -> None:
        if not self._text():
            messagebox.showinfo("Contact", "Please write a message first.", parent=self)
            return
        _open_url(U.issue_url(self.kind.get(), self._text(), self.sysinfo.get()))


class TkUpdater:
    def __init__(self, root, button=None, close: Optional[Callable[[], None]] = None) -> None:
        self.root = root
        self.button = button
        self.close = close or getattr(root, "on_close", None) or root.destroy
        self._busy = False
        self.menu = tk.Menu(root, tearoff=False)
        self._build_menu()
        if button is not None:
            button.configure(command=self.popup)
            self._btn_text = button.cget("text")
        just = None
        try:
            just = U.acknowledge_start()
        except Exception:
            pass
        if just:
            root.after(1500, lambda j=just: self._report_finished(j))
        if U.due_for_auto_check():
            root.after(20000, lambda: self.check(manual=False))

    def popup(self) -> None:
        b = self.button
        try:
            self.menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() - 10 - 22 * self.menu.index("end"))
        finally:
            self.menu.grab_release()

    def _build_menu(self) -> None:
        m = self.menu
        m.add_command(label="Check for updates...", command=lambda: self.check(manual=True))
        self.auto = tk.BooleanVar(value=bool(U.settings().get("auto_check", True)))
        m.add_checkbutton(label="Check automatically (daily)", variable=self.auto,
                          command=lambda: U.save_settings(auto_check=bool(self.auto.get())))
        m.add_command(label="Download the latest version (release page)",
                      command=lambda: _open_url(U.latest_release_url()))
        m.add_separator()
        g = tk.Menu(m, tearoff=False)
        for label, asset in U.guides():
            g.add_command(label=f"Download: {label}", command=lambda a=asset: _open_url(U.guide_url(a)))
        loc = U.local_guides()
        if loc:
            g.add_separator()
            for p in loc:
                g.add_command(label=f"Open on this computer: {p.name}", command=lambda p=p: U.open_path(str(p)))
        g.add_separator()
        g.add_command(label="All files of the latest release", command=lambda: _open_url(U.latest_release_url()))
        m.add_cascade(label="User guides", menu=g)
        m.add_command(label="Contact the developer...", command=lambda: ContactDialog(self.root))
        m.add_command(label="Report a problem on GitHub",
                      command=lambda: _open_url(f"https://github.com/{U.repository()}/issues"))
        m.add_separator()
        m.add_command(label="Restore previous version...", command=self.rollback)
        m.add_command(label="Update status / log...", command=self.show_status)
        m.add_command(label="How updates work...", command=lambda: messagebox.showinfo("Updates", U.help_text()))
        m.add_command(label=f"Version {U.version_text()}", command=self.show_version)

    def _hint(self, text: Optional[str]) -> None:
        if self.button is not None:
            self.button.configure(text=text or self._btn_text)

    # ---------------------------------------------------------------- check
    def check(self, manual: bool = True) -> None:
        if self._busy:
            return
        self._busy = True

        def done(info, err) -> None:
            self._busy = False
            if err is not None:
                U.log(f"check failed: {err}")
                if manual:
                    messagebox.showwarning("Updates", f"Could not check for updates:\n{err}\n\nRelease page:\n"
                                                      f"{U.releases_url()}")
                return
            if info.get("none"):
                if manual:
                    messagebox.showinfo("Updates", "No release has been published yet.")
                return
            if not info.get("newer"):
                self._hint(None)
                if manual:
                    messagebox.showinfo("Updates", f"You have the latest version: {U.version_text()}.")
                return
            ident = f"{info['version']}/{info['build']}"
            self._hint(f"Update {info['version']}!")
            if not manual and U.settings().get("skip") == ident:
                return
            self._offer(info)

        _Job(self.root, U.check_release, done)

    def _offer(self, info: dict) -> None:
        top = tk.Toplevel(self.root)
        top.title("Update available")
        top.geometry("640x460")
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        man = info.get("manifest")
        size = f", {man['size'] / 1024 ** 2:.0f} MB" if man else ""
        ttk.Label(frm, text=f"{U.display_name()} {info['version']} (build {info['build']}{size}) is available.\n"
                            f"Installed: {U.version_text()}.", font=("Arial", 10, "bold")).pack(anchor="w")
        txt = tk.Text(frm, wrap="word", height=14)
        txt.insert("1.0", info.get("notes") or "(no release notes)")
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(frm)
        row.pack(fill="x")
        can = bool(man) and U.is_frozen()

        def act(what: str) -> None:
            top.destroy()
            if what == "install":
                self.download(info)
            elif what == "page":
                _open_url(info.get("html_url") or U.latest_release_url())
            elif what == "skip":
                U.save_settings(skip=f"{info['version']}/{info['build']}")

        if can:
            ttk.Button(row, text="Download and install", command=lambda: act("install")).pack(side="left")
        ttk.Button(row, text="Open the release page", command=lambda: act("page")).pack(side="left", padx=4)
        ttk.Button(row, text="Skip this version", command=lambda: act("skip")).pack(side="left")
        ttk.Button(row, text="Later", command=top.destroy).pack(side="right")
        if not can:
            ttk.Label(frm, text="Running from the Python source: download the package from the release page."
                      if not U.is_frozen() else "No package for this system in the release yet.").pack(anchor="w")

    # ---------------------------------------------------------------- download / install
    def download(self, info: dict) -> None:
        try:
            U.installed_target()
        except Exception as exc:
            messagebox.showwarning("Updates", str(exc))
            return
        cancel = threading.Event()
        top = tk.Toplevel(self.root)
        top.title("Updates")
        top.geometry("460x120")
        lbl = ttk.Label(top, text="Downloading and verifying the update...")
        lbl.pack(padx=10, pady=8, anchor="w")
        bar = ttk.Progressbar(top, maximum=100, length=420)
        bar.pack(padx=10)
        ttk.Button(top, text="Cancel", command=cancel.set).pack(pady=6)
        holder = {}

        def prog(a: int, b: int) -> None:
            bar["value"] = 100 * a / max(1, b)
            lbl.configure(text=f"Downloading and verifying the update... {a / 1024 ** 2:.0f} / {b / 1024 ** 2:.0f} MB")

        def done(work, err) -> None:
            top.destroy()
            if err is not None:
                if not isinstance(err, InterruptedError):
                    messagebox.showwarning("Updates", f"The download failed:\n{err}")
                return
            self._prepare(Path(work))

        holder["job"] = _Job(self.root, lambda: U.download_release(info, holder["job"].report, cancel), done, prog)

    def _prepare(self, work: Path) -> None:
        def done(job, err) -> None:
            if err is not None:
                messagebox.showwarning("Updates", f"The update could not be prepared:\n{err}")
                return
            self._ask_apply(work)

        _Job(self.root, lambda: U.prepare_update(work), done)

    def _ask_apply(self, work: Path) -> None:
        target = U.installed_target()
        others = sorted({n for p, n in U.running_copies(target) if p != os.getpid()})
        text = ("The update is downloaded, verified and ready.\n\nIt is installed when all windows of "
                f"{U.display_name()} are closed; the program then starts again by itself."
                + (("\n\nAlso close: " + ", ".join(others)) if others else "")
                + "\n\nClose the program and update now? (No = install when you close it later)")
        now = messagebox.askyesno("Install the update", text)
        try:
            U.start_apply(work, [p for p, _n in U.running_copies(target)])
        except Exception as exc:
            messagebox.showwarning("Updates", f"The update helper could not be started:\n{exc}")
            return
        self._hint("Update on close")
        if now:
            self.root.after(100, self.close)

    # ---------------------------------------------------------------- other
    def rollback(self) -> None:
        try:
            target = U.installed_target()
            prev = U.previous_versions(target)
        except Exception as exc:
            messagebox.showinfo("Restore previous version", str(exc))
            return
        if not prev:
            messagebox.showinfo("Restore previous version", "No previous version is kept.")
            return
        idx = U.read_json(prev[0] / U.index_name(), {}) or {}
        if not messagebox.askyesno("Restore previous version",
                                   f"Go back to version {idx.get('version', '?')} (build {idx.get('build', '?')})?\n\n"
                                   f"It is restored when all windows of the program are closed."):
            return
        try:
            U.start_rollback([p for p, _n in U.running_copies(target)])
        except Exception as exc:
            messagebox.showwarning("Restore previous version", str(exc))
            return
        self.root.after(100, self.close)

    def show_status(self) -> None:
        home = U.update_home()
        runs = sorted(home.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        lines = [f"Installed: {U.version_text()}", f"Update folder: {home}"]
        if runs:
            st = U.job_status(runs[0])
            job = U.read_json(runs[0] / "job.json", {}) or {}
            lines.append(f"Last update: {job.get('version', '?')}/{job.get('build', '?')} - "
                         f"{U.STATUS_NAMES.get(st.get('state'), st.get('state', 'downloaded'))} {st.get('message', '')}")
        if messagebox.askyesno("Update status", "\n".join(lines) + "\n\nOpen the update folder?"):
            U.open_path(str(home))

    def show_version(self) -> None:
        messagebox.showinfo("Version", f"{U.display_name()}\nVersion {U.version_text()}\n\n"
                                       f"Releases: {U.releases_url()}\nContact: {U.contact_email()}")

    def _report_finished(self, job: dict) -> None:
        if job.get("error"):
            messagebox.showwarning("Updates", "The last update did not complete:\n" + job["error"]
                                   + "\n\nThe previous version was kept or restored.")
        elif job.get("rollback"):
            messagebox.showinfo("Updates", f"The previous version {job.get('version')} was restored.")
        else:
            messagebox.showinfo("Updates", f"{U.display_name()} was updated to {job.get('version')} "
                                           f"(build {job.get('build')}).")
